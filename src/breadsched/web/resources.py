"""HTTP resource adapters and strict query-field translation."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs

if TYPE_CHECKING:
    from .server import Api


@dataclass(frozen=True, slots=True)
class QueryError(ValueError):
    """A stable query-contract failure suitable for an HTTP error response."""

    code: str
    fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResourceError(Exception):
    """A resource-adapter failure with a stable HTTP representation."""

    status: int
    code: str
    fields: tuple[str, ...] = ()
    message: str | None = None


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

    def finish(self) -> None:
        unknown = tuple(sorted(set(self._values) - self._used))
        if unknown:
            raise QueryError("query.unknown", unknown)


GetRoute = Callable[["Api", QueryParams], object]
PostRoute = Callable[["Api", Mapping[str, Any]], object]


def _dashboard(api: Api, query: QueryParams) -> object:
    liquidity = query.integer("liquidity_days", default=0, minimum=0, maximum=36500)
    emergency = query.integer("emergency_months", default=0, minimum=0, maximum=1200)
    query.finish()
    assert liquidity is not None and emergency is not None
    return api.dashboard(liquidity, emergency)


def _register(api: Api, query: QueryParams) -> object:
    account = query.text("account", required=True)
    limit = query.integer("limit", default=250, minimum=1, maximum=10000)
    query.finish()
    assert account is not None and limit is not None
    return api.register(account, limit)


def _reconciliation(api: Api, query: QueryParams) -> object:
    account = query.text("account", required=True)
    query.finish()
    assert account is not None
    return api.reconciliation(account)


def _scheduled(api: Api, query: QueryParams) -> object:
    days = query.integer("days", default=60, minimum=0, maximum=36500)
    query.finish()
    assert days is not None
    return api.scheduled(days)


def _historical_estimates(api: Api, query: QueryParams) -> object:
    months = query.integer("months", default=12, minimum=1, maximum=1200)
    active = query.integer("min_active_months", default=3, minimum=1, maximum=1200)
    scenario = query.text("scenario")
    query.finish()
    assert months is not None and active is not None
    return api.historical_estimates(months, active, scenario)


def _plan(api: Api, query: QueryParams) -> object:
    values = tuple(
        query.text(name) for name in ("from", "through", "period", "scenario", "compare", "measure")
    )
    query.finish()
    return api.plan(*values)


def _plan_detail(api: Api, query: QueryParams) -> object:
    account = query.text("account", required=True)
    start = query.text("start", required=True)
    end = query.text("end", required=True)
    scenario = query.text("scenario")
    flow_kind = query.text("flow_kind")
    requirement_kind = query.text("requirement_kind")
    query.finish()
    assert account is not None and start is not None and end is not None
    return api.plan_detail(account, start, end, scenario, flow_kind, requirement_kind)


def _expense_explorer(api: Api, query: QueryParams) -> object:
    start = query.text("from")
    through = query.text("through")
    period = query.text("period")
    scenario = query.text("scenario")
    account = query.text("account")
    index = query.integer("index", minimum=0)
    query.finish()
    return api.expense_explorer(start, through, period, scenario, account, index)


def _projection(api: Api, query: QueryParams) -> object:
    scenario = query.text("scenario")
    years = query.integer("years", minimum=1, maximum=100)
    query.finish()
    return api.projection(scenario, years)


def _no_query(method: str) -> GetRoute:
    def call(api: Api, query: QueryParams) -> object:
        query.finish()
        return getattr(api, method)()

    return call


def _optional_text(method: str, field: str) -> GetRoute:
    def call(api: Api, query: QueryParams) -> object:
        value = query.text(field)
        query.finish()
        return getattr(api, method)(value)

    return call


GET_ROUTES: dict[str, GetRoute] = {
    "/api/dashboard": _dashboard,
    "/api/fsa/dashboard": _no_query("fsa_dashboard"),
    "/api/summary": _no_query("summary"),
    "/api/accounts": _no_query("accounts"),
    "/api/loan/options": _no_query("loan_options"),
    "/api/commodities": _no_query("commodities"),
    "/api/fsa/claims": _no_query("fsa_claims"),
    "/api/register": _register,
    "/api/reconciliation": _reconciliation,
    "/api/scheduled": _scheduled,
    "/api/historical-estimates": _historical_estimates,
    "/api/plan": _plan,
    "/api/plan/detail": _plan_detail,
    "/api/expense-explorer": _expense_explorer,
    "/api/review": _optional_text("review", "transaction"),
    "/api/scenarios": _no_query("scenarios"),
    "/api/scenario/events": _optional_text("scenario_events", "handle"),
    "/api/projection": _projection,
    "/api/import": _no_query("import_defaults"),
    "/api/verify": _no_query("verify"),
}


def _post(method: str) -> PostRoute:
    def call(api: Api, body: Mapping[str, Any]) -> object:
        return getattr(api, method)(body)

    return call


def _post_without_body(method: str) -> PostRoute:
    def call(api: Api, _body: Mapping[str, Any]) -> object:
        return getattr(api, method)()

    return call


POST_ROUTES: dict[str, PostRoute] = {
    "/api/dashboard/config": _post("dashboard_config_save"),
    "/api/account/type": _post("account_type_save"),
    "/api/account/card": _post("account_card_save"),
    "/api/account/emergency-fund": _post("account_emergency_fund_save"),
    "/api/commodity/price": _post("commodity_price_save"),
    "/api/plan/settings": _post("plan_settings_save"),
    "/api/account/fsa-years": _post("account_fsa_years_save"),
    "/api/fsa/claim/save": _post("fsa_claim_save"),
    "/api/fsa/claim/delete": _post("fsa_claim_delete"),
    "/api/transaction": _post("add_transaction"),
    "/api/reconciliation/start": _post("reconciliation_start"),
    "/api/reconciliation/update": _post("reconciliation_update"),
    "/api/reconciliation/complete": _post("reconciliation_complete"),
    "/api/reconciliation/cancel": _post("reconciliation_cancel"),
    "/api/reconciliation/reopen": _post("reconciliation_reopen"),
    "/api/import": _post("import_local"),
    "/api/post-scheduled": _post_without_body("post_scheduled"),
    "/api/scheduled/occurrences": _post("scheduled_occurrence_options"),
    "/api/scheduled/save": _post("scheduled_save"),
    "/api/scheduled/formula-save": _post("scheduled_formula_save"),
    "/api/scheduled/delete": _post("scheduled_delete"),
    "/api/scheduled/duplicate": _post("scheduled_duplicate"),
    "/api/scheduled/draft": _post("scheduled_draft"),
    "/api/loan/preview": _post("loan_preview"),
    "/api/loan/save": _post("loan_save"),
    "/api/historical-estimate/accept": _post("historical_estimate_accept"),
    "/api/review/match": _post("review_match"),
    "/api/review/reject": _post("review_reject"),
    "/api/review/skip": _post("review_skip"),
    "/api/review/fsa-attach": _post("review_fsa_attach"),
    "/api/review/unexpected": _post("review_unexpected"),
    "/api/scenario/save": _post("scenario_save"),
    "/api/scenario/duplicate": _post("scenario_duplicate"),
    "/api/scenario/delete": _post("scenario_delete"),
    "/api/scenario/period/save": _post("scenario_period_save"),
    "/api/scenario/period/delete": _post("scenario_period_delete"),
    "/api/scenario/event/save": _post("scenario_event_save"),
    "/api/scenario/event/suppress": _post("scenario_event_suppress"),
    "/api/projection/calculate": _post("projection_calculate"),
    "/api/projection/compare": _post("projection_compare"),
    "/api/projection/explain": _post("projection_explain"),
    "/api/projection/save": _post("projection_save"),
}
