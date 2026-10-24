"""HTTP resource adapters and strict query-field translation."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, unquote

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
from .entry_input_resource import (
    entry_accounts,
    entry_amount,
    entry_date,
    entry_num,
    entry_trade,
)
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
from .holdings_resource import (
    holdings,
    realized_gains_report,
    sale_lots_save,
    sale_lots_view,
)
from .import_resource import import_defaults, import_local, import_review, import_review_resolve
from .jars_resource import jars_report
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
from .quote_resource import quote_key_save, quote_source_save, quotes, quotes_update
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
from .register_entry_resource import register_cleared_toggle, register_entry_save
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
    scheduled_next,
    scheduled_occurrence_options,
    scheduled_save,
)
from .tax_resource import tax_marks_save, tax_marks_view, tax_year_report
from .v1_resource import (
    account_balance_get,
    account_delete,
    account_get,
    account_lookup,
    account_save,
    accounts_get,
    advance_days_get,
    advance_days_patch,
    assumption_period_delete,
    assumption_period_save,
    balances_get,
    base_assumptions_get,
    base_assumptions_save,
    benefit_years_get,
    book_get,
    chart_get,
    claim_links_accept,
    claim_proposals_get,
    cleared_toggle,
    currency_get,
    drawdown_accounts_get,
    drawdown_remove,
    drawdown_save,
    due_review_get,
    due_review_post,
    fsa_claim_close_v1,
    fsa_claim_lookup,
    fsa_claim_remove,
    fsa_claim_reopen_v1,
    fsa_claim_report_get,
    fsa_claim_store,
    fsa_claims_get,
    goal_allocate,
    goal_close,
    goal_delete,
    goal_lookup,
    goal_override,
    goal_reopen,
    goal_save,
    goals_get,
    next_get,
    next_post,
    pay_change_apply,
    pay_change_preview,
    paycheck_create,
    paycheck_get,
    payroll_get,
    payroll_template_draft,
    payroll_template_remove,
    payroll_template_store,
    post_due_post,
    receivable_candidates_get,
    receivable_clear_dispute,
    receivable_detach,
    receivable_lookup,
    receivables_get,
    reconciliation_get,
    redo_post,
    register_get,
    reimbursement_proposals_accept,
    reimbursement_proposals_get,
    scenario_assumptions_save,
    scenario_delete_v1,
    scenario_duplicate_v1,
    scenario_lookup,
    scenario_save_v1,
    scenario_suppress,
    scenarios_get,
    schedule_delete,
    schedule_duplicate,
    schedule_lookup,
    schedule_save_fixed,
    schedule_save_formula,
    tagged_get,
    tags_get,
    tags_patch,
    transaction_delete,
    transaction_editable,
    transaction_lookup,
    transaction_save,
    undo_post,
)
from .v1_resource import receivable_delete as v1_receivable_delete
from .v1_resource import receivable_dispute as v1_receivable_dispute
from .v1_resource import receivable_link as v1_receivable_link
from .v1_resource import receivable_save as v1_receivable_save
from .v1_resource import receivable_write_off as v1_receivable_write_off
from .v1_resource import reconciliation_cancel as v1_reconciliation_cancel
from .v1_resource import reconciliation_complete as v1_reconciliation_complete
from .v1_resource import reconciliation_reopen as v1_reconciliation_reopen
from .v1_resource import reconciliation_start as v1_reconciliation_start
from .v1_resource import reconciliation_update as v1_reconciliation_update
from .v1_resource import reimbursement_override as v1_reimbursement_override
from .v1_resource import rule_add as v1_rule_add
from .v1_resource import rule_delete as v1_rule_delete
from .v1_resource import rule_move as v1_rule_move
from .v1_resource import rules_accept as v1_rules_accept
from .v1_resource import rules_get as v1_rules_get

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
    "/api/quotes": _without_query(quotes),
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
    "/api/holdings/sale-lots": sale_lots_view,
    "/api/realized-gains": realized_gains_report,
    "/api/tax-year": tax_year_report,
    "/api/budget-jars": jars_report,
    "/api/tax-marks": tax_marks_view,
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
    "/api/entry/trade": entry_trade,
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
    "/api/quotes/source": quote_source_save,
    "/api/quotes/key": quote_key_save,
    "/api/quotes/update": quotes_update,
    "/api/holdings/sale-lots": sale_lots_save,
    "/api/tax-marks": tax_marks_save,
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
    "/api/register/cleared": register_cleared_toggle,
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
    "/api/scheduled/next": scheduled_next,
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


# ------------------------------------------------------------------ /api/v1
#
# The versioned book-server resources (#357, experimental). A route is a method and
# a path pattern whose ``{name}`` segments are handed to the handler; GET handlers
# take the typed query, writes the JSON body. ``/api/v1/events`` is a stream the
# transport serves itself.

V1Get = Callable[["Api", dict[str, str], QueryParams], object]
V1Write = Callable[["Api", dict[str, str], dict[str, Any]], object]

V1_PREFIX = "/api/v1"
V1_EVENTS = "/api/v1/events"

V1_ROUTES: tuple[tuple[str, str, Callable[..., object]], ...] = (
    ("GET", "/book", book_get),
    ("GET", "/due-review", due_review_get),
    ("POST", "/due-review", due_review_post),
    ("GET", "/schedules/lookup", schedule_lookup),
    ("GET", "/schedules/{handle}/next", next_get),
    ("POST", "/schedules/{handle}/next", next_post),
    ("GET", "/schedules/{handle}/advance-days", advance_days_get),
    ("PATCH", "/schedules/{handle}/advance-days", advance_days_patch),
    ("DELETE", "/schedules/{handle}", schedule_delete),
    ("POST", "/schedules/{handle}/duplicate", schedule_duplicate),
    ("POST", "/post-due", post_due_post),
    ("POST", "/schedules/fixed", schedule_save_fixed),
    ("POST", "/schedules/formula", schedule_save_formula),
    ("POST", "/transactions", transaction_save),
    ("DELETE", "/transactions/{handle}", transaction_delete),
    ("GET", "/currency", currency_get),
    ("GET", "/accounts/lookup", account_lookup),
    ("GET", "/transactions/lookup", transaction_lookup),
    ("GET", "/transactions/{handle}/editable", transaction_editable),
    ("GET", "/accounts/{handle}/register", register_get),
    ("GET", "/accounts/{handle}/balance", account_balance_get),
    ("GET", "/balances", balances_get),
    ("GET", "/accounts", accounts_get),
    ("POST", "/accounts", account_save),
    ("GET", "/accounts/{handle}", account_get),
    ("DELETE", "/accounts/{handle}", account_delete),
    ("GET", "/chart", chart_get),
    ("GET", "/receivables", receivables_get),
    ("POST", "/receivables", v1_receivable_save),
    ("GET", "/receivables/lookup", receivable_lookup),
    ("GET", "/receivables/candidates", receivable_candidates_get),
    ("POST", "/receivables/{handle}/splits", v1_receivable_link),
    ("DELETE", "/receivables/{handle}/splits/{transaction}/{split}", receivable_detach),
    ("POST", "/receivables/{handle}/dispute", v1_receivable_dispute),
    ("DELETE", "/receivables/{handle}/dispute", receivable_clear_dispute),
    ("POST", "/receivables/{handle}/write-offs", v1_receivable_write_off),
    ("DELETE", "/receivables/{handle}", v1_receivable_delete),
    ("GET", "/reimbursement-proposals", reimbursement_proposals_get),
    ("POST", "/reimbursement-proposals/accept", reimbursement_proposals_accept),
    ("POST", "/scenarios/{handle}/reimbursements", v1_reimbursement_override),
    ("GET", "/scenarios", scenarios_get),
    ("POST", "/scenarios", scenario_save_v1),
    ("GET", "/scenarios/lookup", scenario_lookup),
    ("POST", "/scenario-assumptions", scenario_assumptions_save),
    ("POST", "/scenarios/duplicate", scenario_duplicate_v1),
    ("DELETE", "/scenarios/{handle}", scenario_delete_v1),
    ("POST", "/scenarios/{handle}/suppressed-schedules", scenario_suppress),
    ("GET", "/drawdown-accounts", drawdown_accounts_get),
    ("POST", "/drawdowns", drawdown_save),
    ("DELETE", "/scenarios/{handle}/drawdowns/{drawdown}", drawdown_remove),
    ("GET", "/assumptions", base_assumptions_get),
    ("POST", "/assumptions", base_assumptions_save),
    ("POST", "/scenarios/{handle}/periods", assumption_period_save),
    ("DELETE", "/scenarios/{handle}/periods/{index}", assumption_period_delete),
    ("GET", "/payroll", payroll_get),
    ("GET", "/schedules/{handle}/paycheck", paycheck_get),
    ("POST", "/payroll/templates/draft", payroll_template_draft),
    ("POST", "/payroll/templates", payroll_template_store),
    ("DELETE", "/payroll/templates/{name}", payroll_template_remove),
    ("POST", "/payroll/paychecks", paycheck_create),
    ("POST", "/payroll/pay-changes/preview", pay_change_preview),
    ("POST", "/payroll/pay-changes", pay_change_apply),
    ("GET", "/fsa-claims", fsa_claims_get),
    ("GET", "/fsa-claims/lookup", fsa_claim_lookup),
    ("GET", "/fsa-claims/report", fsa_claim_report_get),
    ("POST", "/fsa-claims", fsa_claim_store),
    ("DELETE", "/fsa-claims/{handle}", fsa_claim_remove),
    ("POST", "/fsa-claims/{handle}/close", fsa_claim_close_v1),
    ("POST", "/fsa-claims/{handle}/reopen", fsa_claim_reopen_v1),
    ("GET", "/fsa-years", benefit_years_get),
    ("GET", "/fsa-claim-proposals", claim_proposals_get),
    ("POST", "/fsa-claim-proposals/accept", claim_links_accept),
    ("GET", "/accounts/{handle}/reconciliation", reconciliation_get),
    ("POST", "/reconciliations", v1_reconciliation_start),
    ("PATCH", "/reconciliations/{handle}", v1_reconciliation_update),
    ("POST", "/reconciliations/{handle}/complete", v1_reconciliation_complete),
    ("POST", "/reconciliations/{handle}/cancel", v1_reconciliation_cancel),
    ("POST", "/reconciliations/{handle}/reopen", v1_reconciliation_reopen),
    ("POST", "/transactions/{handle}/splits/{split}/cleared", cleared_toggle),
    ("GET", "/goals", goals_get),
    ("POST", "/goals", goal_save),
    ("GET", "/goals/lookup", goal_lookup),
    ("POST", "/goals/{handle}/allocations", goal_allocate),
    ("POST", "/goals/{handle}/close", goal_close),
    ("POST", "/goals/{handle}/reopen", goal_reopen),
    ("POST", "/goals/{handle}/override", goal_override),
    ("DELETE", "/goals/{handle}", goal_delete),
    ("GET", "/tags", tags_get),
    ("GET", "/tags/transactions", tagged_get),
    ("PATCH", "/transactions/{handle}/tags", tags_patch),
    ("GET", "/rules", v1_rules_get),
    ("POST", "/rules", v1_rule_add),
    ("POST", "/rules/accept", v1_rules_accept),
    ("PATCH", "/rules/{handle}", v1_rule_move),
    ("DELETE", "/rules/{handle}", v1_rule_delete),
    ("POST", "/undo", undo_post),
    ("POST", "/redo", redo_post),
)


def match_v1(method: str, path: str) -> tuple[Callable[..., object], dict[str, str]] | None:
    """The handler and path values for ``method path``, or None.

    A path that names a resource with other methods raises ``LookupError`` so the
    transport can answer 405 rather than 404.
    """
    if not path.startswith(V1_PREFIX + "/"):
        return None
    parts = path[len(V1_PREFIX) :].strip("/").split("/")
    other_method = False
    for route_method, pattern, handler in V1_ROUTES:
        expected = pattern.strip("/").split("/")
        if len(expected) != len(parts):
            continue
        values: dict[str, str] = {}
        for want, got in zip(expected, parts, strict=True):
            if want.startswith("{") and want.endswith("}"):
                if not got:
                    break
                values[want[1:-1]] = unquote(got)
            elif want != got:
                break
        else:
            if route_method == method:
                return handler, values
            other_method = True
    if other_method:
        raise LookupError(path)
    return None
