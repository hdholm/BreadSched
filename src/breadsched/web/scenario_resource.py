"""HTTP input and output for scenarios: assumptions, dated periods, and events.

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
from decimal import Decimal
from typing import TYPE_CHECKING

from ..gen.db.sqlite import DbSQLite
from ..gen.lib import (
    AccountClass,
    AssumptionPeriod,
    Assumptions,
    InvestmentActivityKind,
    PlanningFlowKind,
    Rate,
    Scenario,
    ScenarioSchedule,
    ScheduleGrowthPolicy,
)
from ..gen.services import (
    DeleteAssumptionPeriod,
    DeleteScenario,
    DuplicateScenario,
    FixedScheduleInput,
    SaveAssumptionPeriod,
    SaveBaseAssumptions,
    SaveFixedScenarioSchedule,
    SaveScenarioAssumptions,
    SuppressScenarioSchedule,
    delete_assumption_period,
    delete_scenario,
    duplicate_scenario,
    save_assumption_period,
    save_base_assumptions,
    save_fixed_scenario_schedule,
    save_scenario_assumptions,
    suppress_scenario_schedule,
)
from .controls import input_money, service_error
from .schedule_controls import (
    frequency_key,
    parse_additional_splits,
    parse_amount_changes,
    parse_occurrence_adjustments,
    parse_seasonal_amounts,
    parse_skipped,
    schedule_recurrence,
    simple_schedule_parts,
    weekend_key,
)

if TYPE_CHECKING:
    from .context import Api


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
    }


def scenarios_report(db: DbSQLite, base: Scenario) -> dict:
    """Project Base, saved scenarios, and eligible rate accounts."""
    projection_accounts = []
    for account in db.iter_accounts():
        if not (
            account.account_class is AccountClass.LIABILITY
            or (account.account_class is AccountClass.ASSET and account.atype.is_investment)
        ):
            continue
        projection_accounts.append(
            {
                "handle": account.handle,
                "name": db.full_name(account),
                "class": account.account_class.value,
                "account_rate": (
                    account.annual_interest
                    if account.account_class is AccountClass.LIABILITY
                    else account.annual_return
                ),
            }
        )
    projection_accounts.sort(key=lambda item: item["name"].casefold())
    return {
        "scenarios": [
            scenario_payload(base, base=True),
            *(scenario_payload(item) for item in db.iter_scenarios()),
        ],
        "projection_accounts": projection_accounts,
    }


def base_scenario(db: DbSQLite, start: date, end: date) -> Scenario:
    scenario = Scenario(
        name="Base scenario",
        start=start,
        years=max(1, end.year - start.year + 1),
    )
    stored = db.get_metadata("planning.base_assumptions", None)
    if isinstance(stored, dict):
        scenario.assumptions = Assumptions.from_dict(stored)
    return scenario


def management_base_scenario(db: DbSQLite) -> Scenario:
    today = date.today()
    return base_scenario(
        db,
        date(today.year, 1, 1),
        date(today.year + 9, 12, 31),
    )


def per_account_rates(
    db: DbSQLite,
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
    for handle, raw_rate in payload.items():
        account = db.get_account(str(handle))
        if account is None:
            raise ValueError(f"unknown account assumption: {handle}")
        if not (
            account.account_class is AccountClass.LIABILITY
            or (account.account_class is AccountClass.ASSET and account.atype.is_investment)
        ):
            raise ValueError(
                f"account-specific projection rate is not valid for {db.full_name(account)}"
            )
        if raw_rate is None or str(raw_rate).strip() == "":
            continue
        rate = Decimal(str(raw_rate))
        if rate < Decimal("-1") or rate > Decimal("1"):
            raise ValueError("account-specific rates must be between -1 and 1")
        per_account[account.handle] = rate
    return per_account


def assumptions_from_payload(
    db: DbSQLite, payload: object, existing: Assumptions | None = None
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
        db,
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


def scenario_for_events(db: DbSQLite, handle: object) -> Scenario:
    value = str(handle or "").strip()
    if not value:
        raise ValueError("scenario event changes require a saved scenario")
    scenario = db.get_scenario(value)
    if scenario is None:
        raise KeyError(value)
    return scenario


def scenario_event_payload(db: DbSQLite, item: ScenarioSchedule) -> dict:
    source = db.get_scheduled(item.source_schedule) if item.source_schedule else None
    simple = simple_schedule_parts(db, item)
    return {
        "handle": item.handle,
        "name": item.name,
        "source_schedule": item.source_schedule,
        "source_name": source.name if source is not None else None,
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
    return scenarios_report(api.db, management_base_scenario(api.db))


def scenario_save(api: Api, payload: dict) -> dict:
    handle = payload.get("handle")
    if not handle:
        base = management_base_scenario(api.db)
        assumptions = assumptions_from_payload(api.db, payload.get("assumptions"), base.assumptions)
        base_result = save_base_assumptions(api.db, SaveBaseAssumptions(assumptions))
        if not base_result.ok:
            raise service_error(base_result.errors[0])
        return scenario_payload(management_base_scenario(api.db), base=True)

    scenario = api.db.get_scenario(str(handle))
    if scenario is None:
        raise KeyError(str(handle))
    name = str(payload.get("name", "")).strip()
    if not name:
        raise ValueError("give the scenario a name first")
    duplicate = api.db.get_scenario_by_name(name)
    if duplicate is not None and duplicate.handle != scenario.handle:
        raise ValueError(f'a scenario named "{name}" already exists')
    scenario.name = name
    scenario.description = str(payload.get("description", "")).strip()
    previous = scenario.effective_assumptions()
    updated = assumptions_from_payload(api.db, payload.get("assumptions"), previous)
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
    result = save_scenario_assumptions(
        api.db,
        SaveScenarioAssumptions(scenario, existing_handle=scenario.handle),
    )
    if not result.ok:
        raise service_error(result.errors[0])
    reloaded = api.db.get_scenario(scenario.handle)
    if reloaded is None:  # pragma: no cover - guarded by the successful commit
        raise KeyError(scenario.handle)
    return scenario_payload(reloaded)


def scenario_duplicate(api: Api, payload: dict) -> dict:
    handle = payload.get("handle")
    if handle:
        source = api.db.get_scenario(str(handle))
        if source is None:
            raise KeyError(str(handle))
    else:
        source = management_base_scenario(api.db)
    result = duplicate_scenario(
        api.db,
        DuplicateScenario(source, from_base=not bool(handle)),
    )
    if not result.ok:
        raise service_error(result.errors[0])
    saved = result.value
    assert saved is not None
    clone = api.db.get_scenario(saved.handle)
    assert clone is not None
    return scenario_payload(clone)


def scenario_delete(api: Api, payload: dict) -> dict:
    handle = str(payload.get("handle") or "").strip()
    if not handle:
        raise ValueError("Base scenario cannot be deleted")
    result = delete_scenario(api.db, DeleteScenario(handle))
    if not result.ok:
        raise service_error(result.errors[0])
    return {"deleted": handle}


def scenario_period_save(api: Api, payload: dict) -> dict:
    handle = str(payload.get("handle", "")).strip()
    scenario = api.db.get_scenario(handle) if handle else None
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
            api.db,
            payload.get("per_account") if "per_account" in payload else None,
            existing_period.per_account if existing_period is not None else None,
        ),
    )
    result = save_assumption_period(
        api.db,
        SaveAssumptionPeriod(
            scenario.handle,
            period,
            index=None if existing_period is None else index,
        ),
    )
    if not result.ok:
        raise service_error(result.errors[0])
    saved = api.db.get_scenario(scenario.handle)
    assert saved is not None
    return scenario_payload(saved)


def scenario_period_delete(api: Api, payload: dict) -> dict:
    handle = str(payload.get("handle", "")).strip()
    scenario = api.db.get_scenario(handle) if handle else None
    if scenario is None:
        raise ValueError("dated assumptions belong to a saved scenario")
    index = int(payload.get("index", -1))
    if index < 0 or index >= len(scenario.assumption_periods):
        raise ValueError("dated assumption period no longer exists")
    result = delete_assumption_period(api.db, DeleteAssumptionPeriod(scenario.handle, index))
    if not result.ok:
        raise service_error(result.errors[0])
    saved = api.db.get_scenario(scenario.handle)
    assert saved is not None
    return scenario_payload(saved)


def scenario_events(api: Api, handle: str | None) -> dict:
    scenario = scenario_for_events(api.db, handle)
    accounts = sorted(
        (
            account
            for account in api.db.iter_accounts()
            if not account.is_root and not account.placeholder
        ),
        key=api.db.full_name,
    )
    schedules = list(api.db.iter_scheduled())
    return {
        "scenario": {"handle": scenario.handle, "name": scenario.name},
        "accounts": [
            {
                "handle": account.handle,
                "name": api.db.full_name(account),
                "class": account.account_class.value,
            }
            for account in accounts
        ],
        "baseline": [
            {
                "handle": item.handle,
                "name": item.name,
                "growth_policy": item.growth_policy.value,
                "simple": (parts := simple_schedule_parts(api.db, item)) is not None
                and (frequency := frequency_key(item.recurrence)) is not None,
                "category": parts["category"] if parts else None,
                "funding": parts["funding"] if parts else None,
                "amount": parts["amount"] if parts else None,
                "category_planning_flow": (parts["category_planning_flow"] if parts else None),
                "frequency": frequency if parts else None,
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
            for item in schedules
        ],
        "changes": [scenario_event_payload(api.db, item) for item in scenario.schedule_overrides],
    }


def scenario_event_suppress(api: Api, payload: dict) -> dict:
    scenario = scenario_for_events(api.db, payload.get("handle"))
    result = suppress_scenario_schedule(
        api.db,
        SuppressScenarioSchedule(scenario.handle, str(payload.get("source_schedule", "")).strip()),
    )
    if not result.ok:
        raise service_error(result.errors[0])
    return scenario_events(api, scenario.handle)


def scenario_event_save(api: Api, payload: dict) -> dict:
    """Save a scenario schedule through the shared service; return the scenario's events."""
    scenario = scenario_for_events(api.db, payload.get("handle"))
    source_handle = str(payload.get("source_schedule") or "").strip() or None
    source = api.db.get_scheduled(source_handle) if source_handle else None
    if source_handle and source is None:
        raise KeyError(source_handle)
    if source is not None and simple_schedule_parts(api.db, source) is None:
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
        amount = abs(input_money(payload, payload.get("amount", "")))
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
    result = save_fixed_scenario_schedule(
        api.db,
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
    if result.value is None:
        raise service_error(result.errors[0])
    return scenario_events(api, scenario.handle)
