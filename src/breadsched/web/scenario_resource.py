"""HTTP input and output for scenarios: assumptions, dated periods, events, drawdowns.

This adapter parses browser assumption and schedule controls and translates the
shared scenario services' results. Validation and every write stay in
``services.scenarios``, ``services.assumptions``, and ``services.schedules``, so a
rejected request never changes the book. ``management_base_scenario`` and
``assumptions_from_payload`` are also what the projection endpoints build their
detached drafts from.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

from ..api.contract import ApiError
from ..api.protocol import BookApi
from ..gen.lib import (
    AccountClass,
    AssumptionPeriod,
    Assumptions,
    Drawdown,
    InvestmentActivityKind,
    PlanningFlowKind,
    Rate,
    Scenario,
    ScheduleGrowthPolicy,
)
from ..gen.services import (
    DuplicateScenario,
    FixedScheduleInput,
    SaveBaseAssumptions,
    SaveDrawdown,
    SaveFixedScenarioSchedule,
    SaveScenarioAssumptions,
    SuppressScenarioSchedule,
)
from ..gen.services.schedule_editing import ScenarioChangeView
from .controls import input_amount, input_money
from .schedule_controls import (
    frequency_key,
    parse_additional_splits,
    parse_amount_changes,
    parse_occurrence_adjustments,
    parse_seasonal_amounts,
    parse_skipped,
    schedule_recurrence,
    simple_parts,
    weekend_key,
)

if TYPE_CHECKING:
    from .context import Api


_MISSING = object()


def _stored(api: Api, handle: str, missing: object = _MISSING) -> Scenario:
    """A saved scenario by handle, through the book server.

    A missing one is a ``KeyError`` (a 404), or ``missing`` when one is given.
    """
    try:
        found = api.book.find_scenario(handle)
    except ApiError as exc:
        if exc.status != 404:
            raise
        if missing is _MISSING:
            raise KeyError(handle) from None
        return missing  # type: ignore[return-value]
    if found.handle != handle:
        # Found by name: the caller asked for a handle.
        if missing is _MISSING:
            raise KeyError(handle)
        return missing  # type: ignore[return-value]
    return found


def scenario_payload(scenario: Scenario, *, base: bool = False) -> dict:
    """Serialize effective assumptions and their inheritance evidence."""
    assumptions = scenario.effective_assumptions()
    return {
        "handle": None if base else scenario.handle,
        "base": base,
        "name": "Base scenario" if base else scenario.name,
        "description": "" if base else scenario.description,
        "parent_handle": None if base else scenario.parent_handle,
        "assumptions": assumptions.serialize(),
        "assumption_sources": scenario.assumption_sources(),
        "account_assumption_sources": scenario.account_assumption_sources(),
        "assumption_overrides": sorted(scenario.assumption_overrides),
        "periods": [
            {"index": index, **period.serialize()}
            for index, period in enumerate(scenario.assumption_periods)
        ],
        "schedule_changes": 0 if base else len(scenario.schedule_overrides),
        "drawdowns": [] if base else [drawdown_payload(item) for item in scenario.drawdowns],
    }


def drawdown_payload(drawdown: Drawdown) -> dict:
    """One drawdown rule; the JSON encoder writes its amount and rate as exact text."""
    return {
        "handle": drawdown.handle,
        "account": drawdown.account,
        "into": drawdown.into,
        "start": drawdown.start.isoformat(),
        "end": drawdown.end.isoformat() if drawdown.end else None,
        "annual_amount": drawdown.annual_amount,
        "annual_rate": str(drawdown.annual_rate) if drawdown.annual_rate is not None else None,
        "escalate": drawdown.escalate,
    }


def scenarios_report(api: Api, base: Scenario) -> dict:
    """Project Base, saved scenarios, and eligible rate accounts."""
    projection_accounts = []
    names = {}
    for record in api.book.accounts():
        account = record.account
        names[account.handle] = record.full_name
        if not (
            account.account_class is AccountClass.LIABILITY
            or (account.account_class is AccountClass.ASSET and account.atype.is_investment)
        ):
            continue
        projection_accounts.append(
            {
                "handle": account.handle,
                "name": record.full_name,
                "class": account.account_class.value,
                "account_rate": (
                    account.liability.annual_interest
                    if account.account_class is AccountClass.LIABILITY
                    else account.investment.annual_return
                ),
            }
        )
    projection_accounts.sort(key=lambda item: item["name"].casefold())
    sources, targets = api.book.drawdown_accounts()

    def choices(handles: list[str]) -> list[dict]:
        named = [{"handle": handle, "name": names.get(handle, handle)} for handle in handles]
        return sorted(named, key=lambda item: item["name"].casefold())

    return {
        "scenarios": [
            scenario_payload(base, base=True),
            *(scenario_payload(item) for item in api.book.scenarios()),
        ],
        "projection_accounts": projection_accounts,
        "drawdown_sources": choices(sources),
        "drawdown_targets": choices(targets),
    }


def base_scenario(assumptions: Assumptions, start: date, end: date) -> Scenario:
    scenario = Scenario(
        name="Base scenario",
        start=start,
        years=max(1, end.year - start.year + 1),
    )
    scenario.assumptions = assumptions
    return scenario


def management_base_scenario(assumptions: Assumptions) -> Scenario:
    today = date.today()
    return base_scenario(
        assumptions,
        date(today.year, 1, 1),
        date(today.year + 9, 12, 31),
    )


def per_account_rates(
    book: BookApi,
    payload: object,
    existing: Mapping[str, Decimal | Rate] | None = None,
) -> dict[str, Decimal]:
    if payload is None:
        return {
            handle: value.decimal if isinstance(value, Rate) else value
            for handle, value in (existing or {}).items()
        }
    if not isinstance(payload, dict):
        raise ValueError("per_account assumptions must be an object")
    per_account: dict[str, Decimal] = {}
    records = {record.account.handle: record for record in book.accounts()} if payload else {}
    for handle, raw_rate in payload.items():
        record = records.get(str(handle))
        if record is None:
            raise ValueError(f"unknown account assumption: {handle}")
        account = record.account
        if not (
            account.account_class is AccountClass.LIABILITY
            or (account.account_class is AccountClass.ASSET and account.atype.is_investment)
        ):
            raise ValueError(
                f"account-specific projection rate is not valid for {record.full_name}"
            )
        if raw_rate is None or str(raw_rate).strip() == "":
            continue
        rate = Decimal(str(raw_rate))
        if rate < Decimal("-1") or rate > Decimal("1"):
            raise ValueError("account-specific rates must be between -1 and 1")
        per_account[account.handle] = rate
    return per_account


def assumptions_from_payload(
    book: BookApi, payload: object, existing: Assumptions | None = None
) -> Assumptions:
    if not isinstance(payload, dict):
        raise ValueError("assumptions must be an object")
    fields = (
        "income_growth",
        "expense_inflation",
        "investment_return",
        "cash_interest",
        "liability_interest",
    )
    values: dict[str, Decimal] = {}
    for field in fields:
        if field not in payload:
            raise ValueError(f"missing assumption: {field}")
        value = Decimal(str(payload[field]))
        if value < Decimal("-1") or value > Decimal("1"):
            raise ValueError(f"{field} must be between -1 and 1")
        values[field] = value
    per_account = per_account_rates(
        book,
        payload.get("per_account") if "per_account" in payload else None,
        existing.per_account if existing is not None else None,
    )
    return Assumptions(
        income_growth=values["income_growth"],
        expense_inflation=values["expense_inflation"],
        investment_return=values["investment_return"],
        cash_interest=values["cash_interest"],
        liability_interest=values["liability_interest"],
        per_account=per_account,
    )


def optional_rate(value: object) -> Decimal | None:
    if value is None or str(value).strip() == "":
        return None
    rate = Decimal(str(value))
    if rate < Decimal("-1") or rate > Decimal("1"):
        raise ValueError("dated assumption rates must be between -1 and 1")
    return rate


def _events_handle(handle: object) -> str:
    value = str(handle or "").strip()
    if not value:
        raise ValueError("scenario event changes require a saved scenario")
    return value


def scenario_event_payload(change: ScenarioChangeView) -> dict:
    item = change.schedule
    simple = simple_parts(change)
    return {
        "handle": item.handle,
        "name": item.name,
        "source_schedule": item.source_schedule,
        "source_name": change.source_name,
        "enabled": item.enabled,
        "growth_policy": item.growth_policy.value,
        "simple": (simple is not None and frequency_key(item.recurrence) is not None),
        "category": simple["category"] if simple else None,
        "funding": simple["funding"] if simple else None,
        "amount": simple["amount"] if simple else None,
        "planning_flow": simple["planning_flow"] if simple else None,
        "category_planning_flow": (simple["category_planning_flow"] if simple else None),
        "investment_activity": simple["investment_activity"] if simple else None,
        "additional_splits": simple["additional_splits"] if simple else [],
        "frequency": frequency_key(item.recurrence),
        "interval": item.recurrence.interval,
        "start": item.recurrence.start.isoformat(),
        "end": item.recurrence.end.isoformat() if item.recurrence.end else None,
        "count": item.recurrence.count,
        "weekend": weekend_key(item.recurrence.weekend_adjust),
        "amount_changes": [
            {"start": change.start.isoformat(), "amount": change.amount}
            for change in item.amount_changes
        ],
        "seasonal_amounts": [
            {"month": value.month, "amount": value.amount} for value in item.seasonal_amounts
        ],
        "skipped": [when.isoformat() for when in item.skipped],
        "occurrence_adjustments": [
            {"when": change.when.isoformat(), "amount": change.amount}
            for change in item.occurrence_adjustments
        ],
        "estimate_evidence": item.estimate_evidence,
    }


def scenarios(api: Api) -> dict:
    """Base and saved planning scenarios for the management surface."""
    return scenarios_report(api, management_base_scenario(api.book.base_assumptions()))


def scenario_save(api: Api, payload: dict) -> dict:
    handle = payload.get("handle")
    if not handle:
        base = management_base_scenario(api.book.base_assumptions())
        assumptions = assumptions_from_payload(
            api.book, payload.get("assumptions"), base.assumptions
        )
        api.book.save_base_assumptions(SaveBaseAssumptions(assumptions))
        return scenario_payload(management_base_scenario(api.book.base_assumptions()), base=True)

    scenario = _stored(api, str(handle))
    name = str(payload.get("name", "")).strip()
    if not name:
        raise ValueError("give the scenario a name first")
    duplicate = next((item for item in api.book.scenarios() if item.name == name), None)
    if duplicate is not None and duplicate.handle != scenario.handle:
        raise ValueError(f'a scenario named "{name}" already exists')
    scenario.name = name
    scenario.description = str(payload.get("description", "")).strip()
    previous = scenario.effective_assumptions()
    updated = assumptions_from_payload(api.book, payload.get("assumptions"), previous)
    if "parent_handle" in payload:
        requested_parent = payload.get("parent_handle")
        scenario.parent_handle = str(requested_parent).strip() if requested_parent else None
        scenario.inherits_base_assumptions = True
    if scenario.inherits_base_assumptions:
        requested = payload.get("assumption_overrides")
        if requested is not None:
            if not isinstance(requested, list):
                raise ValueError("assumption_overrides must be a list")
            fields = {
                "income_growth",
                "expense_inflation",
                "investment_return",
                "cash_interest",
                "liability_interest",
            }
            overrides = {str(item) for item in requested}
            if not overrides <= fields:
                raise ValueError("unknown assumption override")
            scenario.assumption_overrides = overrides
        else:
            for field in scenario.assumption_sources():
                if getattr(updated, field) != getattr(previous, field):
                    scenario.assumption_overrides.add(field)
        changed_accounts = set(updated.per_account) | set(previous.per_account)
        for account_handle in changed_accounts:
            if updated.per_account.get(account_handle) == previous.per_account.get(account_handle):
                continue
            if account_handle in updated.per_account:
                scenario.set_account_assumption_override(
                    account_handle, updated.per_account[account_handle]
                )
            else:
                scenario.inherit_account_assumption(account_handle)
    scenario.assumptions = updated
    api.book.save_scenario_assumptions(
        SaveScenarioAssumptions(scenario, existing_handle=scenario.handle)
    )
    return scenario_payload(_stored(api, scenario.handle))


def scenario_duplicate(api: Api, payload: dict) -> dict:
    handle = payload.get("handle")
    if handle:
        source = _stored(api, str(handle))
    else:
        source = management_base_scenario(api.book.base_assumptions())
    saved = api.book.duplicate_scenario(DuplicateScenario(source, from_base=not bool(handle)))
    return scenario_payload(_stored(api, saved.handle))


def scenario_delete(api: Api, payload: dict) -> dict:
    handle = str(payload.get("handle") or "").strip()
    if not handle:
        raise ValueError("Base scenario cannot be deleted")
    api.book.delete_scenario(handle)
    return {"deleted": handle}


def scenario_period_save(api: Api, payload: dict) -> dict:
    handle = str(payload.get("handle", "")).strip()
    scenario = _stored(api, handle, missing=None) if handle else None
    if scenario is None:
        raise ValueError("dated assumptions belong to a saved scenario")
    start = date.fromisoformat(str(payload.get("start", "")))
    end_value = str(payload.get("end", "")).strip()
    end = date.fromisoformat(end_value) if end_value else None
    index_value = payload.get("index")
    existing_period = None
    if index_value is not None and index_value != "":
        index = int(index_value)
        if index < 0 or index >= len(scenario.assumption_periods):
            raise ValueError("dated assumption period no longer exists")
        existing_period = scenario.assumption_periods[index]
    period = AssumptionPeriod(
        start=start,
        end=end,
        description=str(payload.get("description", "")).strip(),
        income_growth=optional_rate(payload.get("income_growth")),
        expense_inflation=optional_rate(payload.get("expense_inflation")),
        investment_return=optional_rate(payload.get("investment_return")),
        cash_interest=optional_rate(payload.get("cash_interest")),
        liability_interest=optional_rate(payload.get("liability_interest")),
        per_account=per_account_rates(
            api.book,
            payload.get("per_account") if "per_account" in payload else None,
            existing_period.per_account if existing_period is not None else None,
        ),
    )
    api.book.save_assumption_period(
        scenario.handle, period, None if existing_period is None else index
    )
    return scenario_payload(_stored(api, scenario.handle))


def scenario_period_delete(api: Api, payload: dict) -> dict:
    handle = str(payload.get("handle", "")).strip()
    scenario = _stored(api, handle, missing=None) if handle else None
    if scenario is None:
        raise ValueError("dated assumptions belong to a saved scenario")
    index = int(payload.get("index", -1))
    if index < 0 or index >= len(scenario.assumption_periods):
        raise ValueError("dated assumption period no longer exists")
    api.book.delete_assumption_period(scenario.handle, index)
    return scenario_payload(_stored(api, scenario.handle))


def _drawdown_rate(fraction: str, percent: str) -> Decimal | None:
    """A yearly share of the balance, as a fraction or an exact percent (4 is 0.04).

    The service checks its range.
    """
    text = percent or fraction
    if not text:
        return None
    try:
        value = Decimal(text.rstrip("%").strip())
    except InvalidOperation as exc:
        raise ValueError("the yearly share must be a number such as 4") from exc
    return (value / 100).normalize() if percent else value


def scenario_drawdown_save(api: Api, payload: dict) -> dict:
    """Add or replace one drawdown; the service validates and writes it."""
    method = str(payload.get("method") or "amount")
    if method not in {"amount", "rate"}:
        raise ValueError("choose a fixed amount or a share of the balance")
    amount_text = str(payload.get("annual_amount") or "").strip()
    rate_text = str(payload.get("annual_rate") or "").strip()
    percent_text = str(payload.get("annual_percent") or "").strip()
    end_text = str(payload.get("end") or "").strip()
    handle = str(payload.get("drawdown") or "").strip() or None
    api.book.save_drawdown(
        SaveDrawdown(
            str(payload.get("handle") or "").strip(),
            str(payload.get("account") or "").strip(),
            str(payload.get("into") or "").strip(),
            date.fromisoformat(str(payload.get("start") or "")),
            annual_amount=(
                input_money(payload, amount_text) if method == "amount" and amount_text else None
            ),
            annual_rate=_drawdown_rate(rate_text, percent_text) if method == "rate" else None,
            end=date.fromisoformat(end_text) if end_text else None,
            escalate=bool(payload.get("escalate", True)),
            handle=handle,
        ),
    )
    return scenario_payload(_stored(api, str(payload.get("handle"))))


def scenario_drawdown_delete(api: Api, payload: dict) -> dict:
    scenario_handle = str(payload.get("handle") or "").strip()
    api.book.remove_drawdown(scenario_handle, str(payload.get("drawdown") or "").strip())
    return scenario_payload(_stored(api, scenario_handle))


def scenario_events(api: Api, handle: str | None) -> dict:
    """A scenario's estimates beside the baseline, from one book-server read (#358)."""
    view = api.book.scenario_events(_events_handle(handle))
    scenario = view.scenario
    return {
        "scenario": {"handle": scenario.handle, "name": scenario.name},
        "accounts": [
            {
                "handle": account.handle,
                "name": account.name,
                "class": account.account_class.value,
            }
            for account in view.accounts
        ],
        "baseline": [
            {
                "handle": item.handle,
                "name": item.name,
                "growth_policy": item.growth_policy.value,
                "simple": (parts := simple_parts(editor)) is not None
                and (frequency := frequency_key(item.recurrence)) is not None,
                "category": parts["category"] if parts else None,
                "funding": parts["funding"] if parts else None,
                "amount": parts["amount"] if parts else None,
                "category_planning_flow": (parts["category_planning_flow"] if parts else None),
                "frequency": frequency if parts else None,
                "interval": item.recurrence.interval,
                "start": item.recurrence.start.isoformat(),
                "end": item.recurrence.end.isoformat() if item.recurrence.end else None,
                "count": item.recurrence.count,
                "weekend": weekend_key(item.recurrence.weekend_adjust),
                "amount_changes": [
                    {"start": change.start.isoformat(), "amount": change.amount}
                    for change in item.amount_changes
                ],
                "seasonal_amounts": [
                    {"month": value.month, "amount": value.amount}
                    for value in item.seasonal_amounts
                ],
                "skipped": [when.isoformat() for when in item.skipped],
                "occurrence_adjustments": [
                    {"when": change.when.isoformat(), "amount": change.amount}
                    for change in item.occurrence_adjustments
                ],
                "estimate_evidence": item.estimate_evidence,
            }
            for editor in view.baseline
            for item in (editor.schedule,)
        ],
        "changes": [scenario_event_payload(change) for change in view.changes],
    }


def scenario_event_suppress(api: Api, payload: dict) -> dict:
    handle = _events_handle(payload.get("handle"))
    scenario = api.book.find_scenario(handle)
    api.book.suppress_scenario_schedule(
        SuppressScenarioSchedule(scenario.handle, str(payload.get("source_schedule", "")).strip())
    )
    return scenario_events(api, scenario.handle)


def scenario_event_save(api: Api, payload: dict) -> dict:
    """Save a scenario schedule through the shared service; return the scenario's events."""
    view = api.book.scenario_events(_events_handle(payload.get("handle")))
    scenario = view.scenario
    source_handle = str(payload.get("source_schedule") or "").strip() or None
    source_editor = (
        next((item for item in view.baseline if item.schedule.handle == source_handle), None)
        if source_handle
        else None
    )
    if source_handle and source_editor is None:
        raise KeyError(source_handle)
    source = source_editor.schedule if source_editor is not None else None
    if source_editor is not None and simple_parts(source_editor) is None:
        raise ValueError("complex schedules can only be suppressed for now")

    name = str(payload.get("name", "")).strip()
    if not name:
        raise ValueError("give the scenario estimate a name")
    category_handle = str(payload.get("category", "")).strip()
    funding_handle = str(payload.get("funding", "")).strip()
    investment_activity_raw = str(payload.get("investment_activity") or "").strip()
    try:
        investment_activity = (
            InvestmentActivityKind(investment_activity_raw) if investment_activity_raw else None
        )
    except ValueError:
        raise ValueError("choose a valid investment activity") from None
    category_planning_flow_raw = str(payload.get("category_planning_flow") or "").strip()
    try:
        category_planning_flow = (
            PlanningFlowKind(category_planning_flow_raw) if category_planning_flow_raw else None
        )
    except ValueError:
        raise ValueError("choose a valid category planning purpose") from None
    try:
        amount = abs(input_amount(payload, payload.get("amount", "")))
    except (ValueError, ArithmeticError):
        raise ValueError("enter a valid amount") from None
    recurrence = schedule_recurrence(payload)
    start = recurrence.start
    skipped = parse_skipped(payload, recurrence)
    adjustments = parse_occurrence_adjustments(payload, recurrence)
    if set(skipped) & {item.when for item in adjustments}:
        raise ValueError("an occurrence cannot be both skipped and overridden")
    planning_flow_raw = str(payload.get("planning_flow") or "").strip()
    try:
        planning_flow = PlanningFlowKind(planning_flow_raw) if planning_flow_raw else None
    except ValueError:
        raise ValueError("choose a valid planning purpose") from None
    additional_splits = parse_additional_splits(payload)
    existing_change = (
        next(
            (
                item
                for item in scenario.schedule_overrides
                if item.source_schedule == source_handle and item.enabled
            ),
            None,
        )
        if source_handle is not None
        else None
    )
    default_growth_policy = (
        existing_change.growth_policy
        if existing_change is not None
        else source.growth_policy
        if source is not None
        else ScheduleGrowthPolicy.AUTO
    )
    try:
        growth_policy = ScheduleGrowthPolicy(
            str(payload.get("growth_policy") or default_growth_policy.value)
        )
    except ValueError:
        raise ValueError("choose a valid projection growth policy") from None
    api.book.save_fixed_scenario_schedule(
        SaveFixedScenarioSchedule(
            scenario_handle=scenario.handle,
            source_schedule=source_handle,
            definition=FixedScheduleInput(
                name=name,
                recurrence=recurrence,
                category=category_handle,
                funding=funding_handle,
                amount=amount,
                category_planning_flow=category_planning_flow,
                funding_planning_flow=planning_flow,
                investment_activity=investment_activity,
                additional_splits=additional_splits,
                enabled=True,
                placeholder=source.placeholder if source is not None else True,
                growth_policy=growth_policy,
                amount_changes=tuple(parse_amount_changes(payload, start)),
                seasonal_amounts=tuple(
                    parse_seasonal_amounts(payload)
                    if "seasonal_amounts" in payload
                    else source.seasonal_amounts
                    if source is not None
                    else ()
                ),
                skipped=tuple(skipped),
                occurrence_adjustments=tuple(adjustments),
                estimate_evidence=(
                    payload.get("estimate_evidence")
                    if isinstance(payload.get("estimate_evidence"), dict)
                    else existing_change.estimate_evidence
                    if existing_change is not None
                    else source.estimate_evidence
                    if source is not None
                    else None
                ),
            ),
        ),
    )
    return scenario_events(api, scenario.handle)
