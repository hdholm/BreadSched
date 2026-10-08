"""HTTP resource adapters and strict query-field translation."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs

from .account_resource import (
    account_card_save,
    account_cost_basis_save,
    account_emergency_fund_save,
    account_fsa_years_save,
    account_type_save,
    accounts,
    commodities,
    commodity_price_save,
)
from .attachment_resource import (
    transaction_attachment_link,
    transaction_attachment_relink,
    transaction_attachment_remove,
    transaction_tags,
)
from .autocomplete_resource import entry_suggestion
from .book_resource import summary, verify
from .csv_import_resource import csv_import, csv_inspect, csv_preview
from .currency_quote_resource import save_currency_quote
from .dashboard_resource import dashboard_config_save, dashboard_report
from .entry_input_resource import entry_accounts, entry_amount, entry_date, entry_num
from .expense_resource import expense_report
from .fsa_claim_resource import (
    fsa_claim_close,
    fsa_claim_delete,
    fsa_claim_reopen,
    fsa_claim_save,
    fsa_claims,
)
from .fsa_resource import fsa_claim_links_accept, fsa_dashboard
from .gnucash_writeback_resource import (
    gnucash_writeback,
    gnucash_writeback_apply,
    gnucash_writeback_settings,
)
from .guide_resource import guide
from .holdings_resource import holdings
from .import_resource import import_defaults, import_local, import_review, import_review_resolve
from .loan_resource import loan_options, loan_preview, loan_save
from .net_worth_resource import net_worth_change, net_worth_history
from .payroll_resource import (
    payroll,
    payroll_change,
    payroll_change_preview,
    payroll_create,
    payroll_template_delete,
    payroll_template_from_schedule,
    payroll_template_save,
)
from .plan_detail_resource import plan_detail_report
from .plan_resource import plan_report, plan_settings_save
from .projection_resource import (
    projection_calculate,
    projection_compare,
    projection_explain,
    projection_save,
    scenario_projection,
)
from .receivable_resource import (
    receivable_accept,
    receivable_delete,
    receivable_dispute,
    receivable_link,
    receivable_save,
    receivable_scenario,
    receivable_unlink,
    receivable_write_off,
    receivables,
)
from .reconciliation_resource import (
    reconciliation,
    reconciliation_cancel,
    reconciliation_complete,
    reconciliation_reopen,
    reconciliation_start,
    reconciliation_update,
)
from .register_entry_resource import register_entry_save
from .register_resource import register, transaction_add
from .review_resource import (
    review,
    review_fsa_attach,
    review_match,
    review_reject,
    review_skip,
    review_unexpected,
)
from .rules_resource import rule_add, rule_delete, rule_move, rules, rules_accept
from .savings_goal_resource import (
    savings_goal_allocate,
    savings_goal_close,
    savings_goal_delete,
    savings_goal_override,
    savings_goal_reopen,
    savings_goal_save,
    savings_goals,
)
from .scenario_resource import (
    scenario_delete,
    scenario_drawdown_delete,
    scenario_drawdown_save,
    scenario_duplicate,
    scenario_event_save,
    scenario_event_suppress,
    scenario_events,
    scenario_period_delete,
    scenario_period_save,
    scenario_save,
    scenarios,
)
from .schedule_resource import (
    due_review,
    due_review_resolve,
    historical_estimate_accept,
    historical_estimates,
    post_scheduled,
    scheduled,
    scheduled_delete,
    scheduled_draft,
    scheduled_duplicate,
    scheduled_formula_save,
    scheduled_occurrence_options,
    scheduled_save,
)

if TYPE_CHECKING:
    from .context import Api


@dataclass(frozen=True, slots=True)
class QueryError(ValueError):
    """A stable query-contract failure suitable for an HTTP error response."""

    code: str
    fields: tuple[str, ...]


class QueryParams:
    """Consume exactly one value for every declared query field."""

    def __init__(self, raw: str) -> None:
        if not raw:
            self._values: dict[str, list[str]] = {}
            self._used: set[str] = set()
            return
        try:
            self._values = parse_qs(raw, keep_blank_values=True, strict_parsing=True)
        except ValueError:
            raise QueryError("query.malformed", ()) from None
        self._used = set()

    def _one(self, name: str, *, required: bool = False) -> str | None:
        self._used.add(name)
        values = self._values.get(name)
        if values is None:
            if required:
                raise QueryError("query.missing", (name,))
            return None
        if len(values) != 1:
            raise QueryError("query.repeated", (name,))
        value = values[0]
        if required and not value:
            raise QueryError("query.missing", (name,))
        return value or None

    def text(self, name: str, *, required: bool = False) -> str | None:
        return self._one(name, required=required)

    def integer(
        self,
        name: str,
        *,
        default: int | None = None,
        minimum: int | None = None,
        maximum: int | None = None,
    ) -> int | None:
        raw = self._one(name)
        if raw is None:
            return default
        try:
            value = int(raw)
        except ValueError:
            raise QueryError("query.integer.invalid", (name,)) from None
        if minimum is not None and value < minimum:
            raise QueryError("query.integer.out_of_range", (name,))
        if maximum is not None and value > maximum:
            raise QueryError("query.integer.out_of_range", (name,))
        return value

    def iso_date(self, name: str, *, required: bool = False) -> date | None:
        raw = self._one(name, required=required)
        if raw is None:
            return None
        try:
            return date.fromisoformat(raw)
        except ValueError:
            raise QueryError("query.date.invalid", (name,)) from None

    def choice(self, name: str, options: tuple[str, ...], default: str) -> str:
        raw = self._one(name)
        if raw is None:
            return default
        if raw not in options:
            raise QueryError("query.invalid", (name,))
        return raw

    def finish(self) -> None:
        unknown = tuple(sorted(set(self._values) - self._used))
        if unknown:
            raise QueryError("query.unknown", unknown)


GetRoute = Callable[["Api", QueryParams], object]
PostRoute = Callable[["Api", dict[str, Any]], object]


def _dashboard(api: Api, query: QueryParams) -> object:
    liquidity = query.integer("liquidity_days", default=0, minimum=0, maximum=36500)
    emergency = query.integer("emergency_months", default=0, minimum=0, maximum=1200)
    query.finish()
    assert liquidity is not None and emergency is not None
    return dashboard_report(api.db, liquidity, emergency)


def _scheduled(api: Api, query: QueryParams) -> object:
    days = query.integer("days", default=60, minimum=0, maximum=36500)
    query.finish()
    assert days is not None
    return scheduled(api, days)


def _historical_estimates(api: Api, query: QueryParams) -> object:
    months = query.integer("months", default=12, minimum=1, maximum=1200)
    active = query.integer("min_active_months", default=3, minimum=1, maximum=1200)
    scenario = query.text("scenario")
    query.finish()
    assert months is not None and active is not None
    return historical_estimates(api, months, active, scenario)


def _plan(api: Api, query: QueryParams) -> object:
    values = tuple(
        query.text(name) for name in ("from", "through", "period", "scenario", "compare", "measure")
    )
    query.finish()
    return plan_report(api.db, *values)


def _plan_detail(api: Api, query: QueryParams) -> object:
    account = query.text("account", required=True)
    start = query.text("start", required=True)
    end = query.text("end", required=True)
    scenario = query.text("scenario")
    flow_kind = query.text("flow_kind")
    requirement_kind = query.text("requirement_kind")
    query.finish()
    assert account is not None and start is not None and end is not None
    return plan_detail_report(api.db, account, start, end, scenario, flow_kind, requirement_kind)


def _expense_explorer(api: Api, query: QueryParams) -> object:
    start = query.text("from")
    through = query.text("through")
    period = query.text("period")
    scenario = query.text("scenario")
    account = query.text("account")
    index = query.integer("index", minimum=0)
    rollover = query.text("rollover")
    if rollover not in (None, "0", "1"):
        raise QueryError("query.invalid", ("rollover",))
    query.finish()
    return expense_report(api.db, start, through, period, scenario, account, index, rollover == "1")


def _projection(api: Api, query: QueryParams) -> object:
    scenario = query.text("scenario")
    years = query.integer("years", minimum=1, maximum=100)
    query.finish()
    return scenario_projection(api, scenario, years)


def _review(api: Api, query: QueryParams) -> object:
    transaction = query.text("transaction")
    query.finish()
    return review(api, transaction)


def _scenario_events(api: Api, query: QueryParams) -> object:
    handle = query.text("handle")
    query.finish()
    return scenario_events(api, handle)


def _without_query(route: Callable[[Api], object]) -> GetRoute:
    def call(api: Api, query: QueryParams) -> object:
        query.finish()
        return route(api)

    return call


GET_ROUTES: dict[str, GetRoute] = {
    "/api/dashboard": _dashboard,
    "/api/fsa/dashboard": fsa_dashboard,
    "/api/summary": _without_query(summary),
    "/api/accounts": _without_query(accounts),
    "/api/loan/options": _without_query(loan_options),
    "/api/commodities": _without_query(commodities),
    "/api/fsa/claims": fsa_claims,
    "/api/register": register,
    "/api/reconciliation": reconciliation,
    "/api/scheduled": _scheduled,
    "/api/historical-estimates": _historical_estimates,
    "/api/plan": _plan,
    "/api/plan/detail": _plan_detail,
    "/api/expense-explorer": _expense_explorer,
    "/api/net-worth-history": net_worth_history,
    "/api/holdings": holdings,
    "/api/net-worth-change": net_worth_change,
    "/api/review": _review,
    "/api/scenarios": _without_query(scenarios),
    "/api/scenario/events": _scenario_events,
    "/api/projection": _projection,
    "/api/import": _without_query(import_defaults),
    "/api/import/review": _without_query(import_review),
    "/api/due-review": _without_query(due_review),
    "/api/verify": _without_query(verify),
    "/api/gnucash/writeback": gnucash_writeback,
    "/api/receivables": receivables,
    "/api/savings-goals": savings_goals,
    "/api/guide": guide,
    "/api/rules": rules,
    "/api/payroll": payroll,
    "/api/entry/suggest": entry_suggestion,
    "/api/entry/date": entry_date,
    "/api/entry/amount": entry_amount,
    "/api/entry/accounts": entry_accounts,
    "/api/entry/num": entry_num,
}


def _without_body(route: Callable[[Api], object]) -> PostRoute:
    def call(api: Api, _body: Mapping[str, Any]) -> object:
        return route(api)

    return call


def _currency_quote(api: Api, body: Mapping[str, Any]) -> object:
    return save_currency_quote(api.db, body)


POST_ROUTES: dict[str, PostRoute] = {
    "/api/dashboard/config": dashboard_config_save,
    "/api/account/cost-basis": account_cost_basis_save,
    "/api/account/type": account_type_save,
    "/api/account/card": account_card_save,
    "/api/account/emergency-fund": account_emergency_fund_save,
    "/api/commodity/price": commodity_price_save,
    "/api/currency/quote": _currency_quote,
    "/api/plan/settings": plan_settings_save,
    "/api/account/fsa-years": account_fsa_years_save,
    "/api/fsa/claim/save": fsa_claim_save,
    "/api/fsa/claim/delete": fsa_claim_delete,
    "/api/fsa/claim/close": fsa_claim_close,
    "/api/fsa/claim/reopen": fsa_claim_reopen,
    "/api/transaction": transaction_add,
    "/api/reconciliation/start": reconciliation_start,
    "/api/reconciliation/update": reconciliation_update,
    "/api/reconciliation/complete": reconciliation_complete,
    "/api/reconciliation/cancel": reconciliation_cancel,
    "/api/reconciliation/reopen": reconciliation_reopen,
    "/api/import": import_local,
    "/api/import/review": import_review_resolve,
    "/api/import/csv/inspect": csv_inspect,
    "/api/import/csv/preview": csv_preview,
    "/api/import/csv": csv_import,
    "/api/gnucash/writeback": gnucash_writeback_apply,
    "/api/gnucash/writeback/settings": gnucash_writeback_settings,
    "/api/transaction/tags": transaction_tags,
    "/api/transaction/attachment/link": transaction_attachment_link,
    "/api/transaction/attachment/remove": transaction_attachment_remove,
    "/api/transaction/attachment/relink": transaction_attachment_relink,
    "/api/register/entry": register_entry_save,
    "/api/savings-goal/save": savings_goal_save,
    "/api/savings-goal/allocate": savings_goal_allocate,
    "/api/savings-goal/close": savings_goal_close,
    "/api/savings-goal/reopen": savings_goal_reopen,
    "/api/savings-goal/delete": savings_goal_delete,
    "/api/savings-goal/override": savings_goal_override,
    "/api/receivable/save": receivable_save,
    "/api/receivable/link": receivable_link,
    "/api/receivable/unlink": receivable_unlink,
    "/api/receivable/dispute": receivable_dispute,
    "/api/receivable/write-off": receivable_write_off,
    "/api/receivable/scenario": receivable_scenario,
    "/api/receivable/delete": receivable_delete,
    "/api/receivables/accept": receivable_accept,
    "/api/fsa/claim-links/accept": fsa_claim_links_accept,
    "/api/rule/add": rule_add,
    "/api/payroll/template/save": payroll_template_save,
    "/api/payroll/template/delete": payroll_template_delete,
    "/api/payroll/template/from-schedule": payroll_template_from_schedule,
    "/api/payroll/create": payroll_create,
    "/api/payroll/change/preview": payroll_change_preview,
    "/api/payroll/change": payroll_change,
    "/api/rule/delete": rule_delete,
    "/api/rule/move": rule_move,
    "/api/rules/accept": rules_accept,
    "/api/due-review": due_review_resolve,
    "/api/post-scheduled": _without_body(post_scheduled),
    "/api/scheduled/occurrences": scheduled_occurrence_options,
    "/api/scheduled/save": scheduled_save,
    "/api/scheduled/formula-save": scheduled_formula_save,
    "/api/scheduled/delete": scheduled_delete,
    "/api/scheduled/duplicate": scheduled_duplicate,
    "/api/scheduled/draft": scheduled_draft,
    "/api/loan/preview": loan_preview,
    "/api/loan/save": loan_save,
    "/api/historical-estimate/accept": historical_estimate_accept,
    "/api/review/match": review_match,
    "/api/review/reject": review_reject,
    "/api/review/skip": review_skip,
    "/api/review/fsa-attach": review_fsa_attach,
    "/api/review/unexpected": review_unexpected,
    "/api/scenario/save": scenario_save,
    "/api/scenario/duplicate": scenario_duplicate,
    "/api/scenario/delete": scenario_delete,
    "/api/scenario/period/save": scenario_period_save,
    "/api/scenario/period/delete": scenario_period_delete,
    "/api/scenario/drawdown/save": scenario_drawdown_save,
    "/api/scenario/drawdown/delete": scenario_drawdown_delete,
    "/api/scenario/event/save": scenario_event_save,
    "/api/scenario/event/suppress": scenario_event_suppress,
    "/api/projection/calculate": projection_calculate,
    "/api/projection/compare": projection_compare,
    "/api/projection/explain": projection_explain,
    "/api/projection/save": projection_save,
}
