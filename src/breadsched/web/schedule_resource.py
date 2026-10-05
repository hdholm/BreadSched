"""HTTP input and output for baseline schedules, estimates, and due occurrences.

This adapter parses browser controls (``schedule_controls``) into the shared
schedule, estimate, and due-review services' requests and translates their
results. Validation and every write stay in those services, so a rejected request
never changes the book.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from ..gen.db.sqlite import DbSQLite
from ..gen.engine import estimates, schedule
from ..gen.lib import (
    InvestmentActivityKind,
    PlanningFlowKind,
    ScheduleGrowthPolicy,
    scheduled_occurrence_preview,
)
from ..gen.services import (
    DeleteSchedule,
    DueDecision,
    DuplicateSchedule,
    FixedScheduleInput,
    FormulaScheduleInput,
    ResolveDue,
    SaveFixedSchedule,
    delete_schedule,
    duplicate_schedule,
    pending_due_review,
    resolve_due,
    save_fixed_schedule,
    save_formula_schedule,
)
from .controls import input_money, service_error
from .schedule_controls import (
    frequency_key,
    parse_additional_splits,
    parse_amount_changes,
    parse_occurrence_adjustments,
    parse_seasonal_amounts,
    parse_skipped,
    parse_split_amount_changes,
    schedule_recurrence,
    simple_schedule_parts,
    weekend_key,
)

if TYPE_CHECKING:
    from .context import Api


def scheduled(api: Api, days: int = 60) -> dict:
    occurrences = schedule.upcoming_occurrences(api.db, horizon_days=days)
    accounts = sorted(
        (
            account
            for account in api.db.iter_accounts()
            if not account.is_root and not account.placeholder
        ),
        key=api.db.full_name,
    )
    definitions = []
    saved_schedules = list(api.db.iter_scheduled())
    for item in saved_schedules:
        projection = schedule.schedule_edit_projection(api.db, item)
        simple = simple_schedule_parts(api.db, item)
        frequency = frequency_key(item.recurrence)
        editability = projection.editability
        definitions.append(
            {
                "handle": item.handle,
                "account_linked": False,
                "linked_account": None,
                "name": item.name,
                "frequency": item.recurrence.describe(),
                "frequency_key": frequency,
                "amount": item.amount(),
                "enabled": item.enabled,
                "placeholder": item.placeholder,
                "auto": item.auto_create,
                "growth_policy": item.growth_policy.value,
                "simple": simple is not None and frequency is not None,
                "editable": editability.editable,
                "editor_mode": editability.mode.value,
                "editability_reason": editability.reason,
                "unsupported_reason": (editability.reason if not editability.editable else ""),
                "source_recurrence": item.source_recurrence,
                "category": simple["category"] if simple else None,
                "funding": simple["funding"] if simple else None,
                "planning_flow": simple["planning_flow"] if simple else None,
                "category_planning_flow": (simple["category_planning_flow"] if simple else None),
                "investment_activity": (simple["investment_activity"] if simple else None),
                "category_memo": simple["category_memo"] if simple else "",
                "funding_memo": simple["funding_memo"] if simple else "",
                "additional_splits": (simple["additional_splits"] if simple else []),
                "formula_splits": [
                    {
                        "index": index,
                        "account": item.splits[index].account,
                        "account_name": (
                            api.db.full_name(account)
                            if (account := api.db.get_account(item.splits[index].account))
                            is not None
                            else item.splits[index].account
                        ),
                        "formula": item.splits[index].formula,
                    }
                    for index in projection.formula_split_indices
                ],
                "variables": dict(item.variables),
                "account_handles": [split.account for split in item.splits],
                "start": item.recurrence.start.isoformat(),
                "end": (
                    item.recurrence.end.isoformat() if item.recurrence.end is not None else None
                ),
                "count": item.recurrence.count,
                "weekend": weekend_key(item.recurrence.weekend_adjust),
                "amount_changes": [
                    {"start": change.start.isoformat(), "amount": change.amount}
                    for change in item.amount_changes
                ],
                "split_amount_changes": [
                    {
                        "account": split.account,
                        "start": change.start.isoformat(),
                        "amount": change.amount,
                    }
                    for split in item.splits
                    for change in split.amount_changes
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
        )
    for payment_definition in schedule.account_payment_definitions(
        api.db, schedules=saved_schedules
    ):
        definitions.append(
            {
                "handle": payment_definition.handle,
                "account_linked": True,
                "linked_account": payment_definition.account,
                "name": payment_definition.name,
                "frequency": payment_definition.recurrence.describe(),
                "frequency_key": "monthly",
                "amount": payment_definition.amount_due,
                "enabled": True,
                "placeholder": False,
                "auto": False,
                "growth_policy": "none",
                "simple": False,
                "editable": True,
                "editor_mode": "account",
                "editability_reason": "",
                "unsupported_reason": "",
                "source_recurrence": None,
                "category": None,
                "funding": payment_definition.payment_account,
                "planning_flow": None,
                "investment_activity": None,
                "category_memo": "",
                "funding_memo": "",
                "additional_splits": [],
                "account_handles": [split.account for split in payment_definition.splits],
                "start": payment_definition.next_due.isoformat(),
                "end": None,
                "count": None,
                "weekend": "none",
                "amount_changes": [],
                "seasonal_amounts": [],
                "skipped": [],
                "occurrence_adjustments": [],
            }
        )
    return {
        "definitions": definitions,
        "accounts": [
            {
                "handle": account.handle,
                "name": api.db.full_name(account),
                "class": account.account_class.value,
                "hidden": account.hidden,
            }
            for account in accounts
        ],
        "upcoming": [
            {
                "date": occurrence.when,
                "name": occurrence.name,
                "amount": occurrence.amount,
                "schedule": occurrence.schedule.handle,
                "account_linked": isinstance(
                    occurrence.schedule, schedule.AccountPaymentDefinition
                ),
                "linked_account": getattr(occurrence.schedule, "account", None),
            }
            for occurrence in occurrences
        ],
    }


def scheduled_draft(api: Api, payload: dict) -> dict:
    """Represent an actual as a reviewable, unsaved fixed-schedule draft."""
    transaction = api.db.get_transaction(str(payload.get("transaction") or ""))
    if transaction is None:
        raise KeyError(str(payload.get("transaction") or ""))
    draft = schedule.from_transaction(transaction)
    simple = simple_schedule_parts(api.db, draft)
    if simple is None:
        raise ValueError("this transaction's split structure needs the desktop schedule editor")
    return {
        "handle": None,
        "name": draft.name,
        "frequency": draft.recurrence.describe(),
        "frequency_key": "once",
        "amount": draft.amount(),
        "enabled": True,
        "placeholder": False,
        "auto": False,
        "growth_policy": draft.growth_policy.value,
        "simple": True,
        "category": simple["category"],
        "funding": simple["funding"],
        "planning_flow": simple["planning_flow"],
        "investment_activity": simple["investment_activity"],
        "category_memo": simple["category_memo"],
        "funding_memo": simple["funding_memo"],
        "additional_splits": simple["additional_splits"],
        "account_handles": [split.account for split in draft.splits],
        "start": draft.recurrence.start.isoformat(),
        "end": None,
        "count": None,
        "weekend": "none",
        "amount_changes": [],
        "split_amount_changes": [],
        "skipped": [],
        "occurrence_adjustments": [],
    }


def scheduled_occurrence_options(api: Api, payload: dict) -> dict:
    """Return selectable occurrence dates for schedule exception editors."""
    recurrence = schedule_recurrence(payload)
    start = recurrence.start
    horizon = date(min(start.year + 10, 9999), 12, 31)
    result: dict[str, object] = {
        "occurrences": [item.isoformat() for item in recurrence.occurrences(horizon)[:500]]
    }
    raw_amount = str(payload.get("amount") or "").strip()
    if raw_amount:
        try:
            amount = abs(input_money(payload, raw_amount))
        except (ValueError, ArithmeticError) as exc:
            raise ValueError("amount must be a valid number") from exc
        if not amount:
            raise ValueError("amount must be greater than zero")
        amount_changes = parse_amount_changes(payload, start)
        skipped = parse_skipped(payload, recurrence)
        adjustments = parse_occurrence_adjustments(payload, recurrence)
        if set(skipped) & {item.when for item in adjustments}:
            raise ValueError("an occurrence cannot be both skipped and overridden")
        result["preview"] = [
            {"when": when, "amount": value, "status": status}
            for when, value, status in scheduled_occurrence_preview(
                recurrence, amount, amount_changes, skipped, adjustments
            )
        ]
    return result


def scheduled_save(api: Api, payload: dict) -> dict:
    """Create or update a fixed-split baseline schedule."""
    handle = str(payload.get("handle") or "").strip()
    existing = api.db.get_scheduled(handle) if handle else None
    if handle and existing is None:
        raise KeyError(handle)
    existing_parts = simple_schedule_parts(api.db, existing) if existing is not None else None
    if existing is not None:
        editability = schedule.schedule_editability(api.db, existing)
        if not editability.editable:
            raise ValueError(editability.reason)
        if (
            editability.mode is not schedule.ScheduleEditorMode.FIXED
            or existing_parts is None
            or frequency_key(existing.recurrence) is None
        ):
            raise ValueError("this schedule needs an editor that preserves its complete structure")

    name = str(payload.get("name") or "").strip()
    if not name:
        raise ValueError("schedule name is required")
    category_handle = str(payload.get("category") or "").strip()
    funding_handle = str(payload.get("funding") or "").strip()
    investment_activity_raw = str(payload.get("investment_activity") or "").strip()
    try:
        investment_activity = (
            InvestmentActivityKind(investment_activity_raw) if investment_activity_raw else None
        )
    except ValueError:
        raise ValueError("choose a valid investment activity") from None
    category_planning_flow = None
    category_ledger_direction = None
    if existing is not None and existing_parts is not None:
        raw_category_flow = existing_parts.get("category_planning_flow")
        category_planning_flow = (
            PlanningFlowKind(str(raw_category_flow)) if raw_category_flow else None
        )
        raw_direction = existing_parts.get("category_ledger_direction")
        category_ledger_direction = int(raw_direction) if raw_direction is not None else None
    if "category_planning_flow" in payload:
        raw_category_flow = str(payload.get("category_planning_flow") or "").strip()
        try:
            category_planning_flow = (
                PlanningFlowKind(raw_category_flow) if raw_category_flow else None
            )
        except ValueError:
            raise ValueError("choose a valid category planning purpose") from None
        category_ledger_direction = None
    try:
        amount = input_money(payload, payload.get("amount") or "0")
    except (ValueError, ArithmeticError) as exc:
        raise ValueError("amount must be a valid number") from exc
    recurrence = schedule_recurrence(payload, existing)
    amount_changes = parse_amount_changes(payload, recurrence.start)
    skipped = parse_skipped(payload, recurrence)
    adjustments = parse_occurrence_adjustments(payload, recurrence)
    if set(skipped) & {change.when for change in adjustments}:
        raise ValueError("an occurrence cannot be both skipped and overridden")

    default_growth_policy = (
        existing.growth_policy if existing is not None else ScheduleGrowthPolicy.AUTO
    )
    try:
        growth_policy = ScheduleGrowthPolicy(
            str(payload.get("growth_policy") or default_growth_policy.value)
        )
    except ValueError:
        raise ValueError("choose a valid projection growth policy") from None

    planning_flow_raw = str(payload.get("planning_flow") or "").strip()
    try:
        planning_flow = PlanningFlowKind(planning_flow_raw) if planning_flow_raw else None
    except ValueError:
        raise ValueError("choose a valid planning purpose") from None
    additional_splits = parse_additional_splits(payload)
    category_memo = (
        str(payload.get("category_memo") or "").strip()
        if "category_memo" in payload
        else str(existing_parts["category_memo"])
        if existing_parts is not None
        else ""
    )
    funding_memo = (
        str(payload.get("funding_memo") or "").strip()
        if "funding_memo" in payload
        else str(existing_parts["funding_memo"])
        if existing_parts is not None
        else ""
    )
    selected_handles = {
        category_handle,
        funding_handle,
        *(split.account for split in additional_splits),
    }
    existing_split_changes = (
        {split.account: tuple(split.amount_changes) for split in existing.splits}
        if existing is not None
        else {}
    )
    split_changes = (
        parse_split_amount_changes(
            payload,
            recurrence.start,
            selected_handles,
        )
        if "split_amount_changes" in payload
        else existing_split_changes
    )
    placeholder = bool(payload.get("placeholder", False))
    evidence = existing.estimate_evidence if existing is not None else None
    if "estimate_evidence" in payload:
        raw_evidence = payload.get("estimate_evidence")
        if raw_evidence is not None and not isinstance(raw_evidence, dict):
            raise ValueError("estimate evidence must be an object")
        evidence = raw_evidence
    seasonal_amounts = (
        parse_seasonal_amounts(payload)
        if "seasonal_amounts" in payload
        else list(existing.seasonal_amounts)
        if existing is not None
        else []
    )
    enabled = existing.enabled if existing is not None else True
    if "enabled" in payload:
        if not isinstance(payload["enabled"], bool):
            raise ValueError("active status must be true or false")
        enabled = payload["enabled"]

    result = save_fixed_schedule(
        api.db,
        SaveFixedSchedule(
            definition=FixedScheduleInput(
                name=name,
                recurrence=recurrence,
                category=category_handle,
                funding=funding_handle,
                amount=amount,
                category_planning_flow=category_planning_flow,
                funding_planning_flow=planning_flow,
                investment_activity=investment_activity,
                category_ledger_direction=category_ledger_direction,
                additional_splits=additional_splits,
                category_memo=category_memo,
                funding_memo=funding_memo,
                enabled=enabled,
                auto_create=bool(payload.get("auto", False)),
                placeholder=placeholder,
                growth_policy=growth_policy,
                amount_changes=tuple(amount_changes),
                split_amount_changes={
                    account: tuple(changes) for account, changes in split_changes.items()
                },
                seasonal_amounts=tuple(seasonal_amounts),
                skipped=tuple(skipped),
                occurrence_adjustments=tuple(adjustments),
                estimate_evidence=evidence,
            ),
            existing_handle=existing.handle if existing is not None else None,
        ),
    )
    if result.value is None:
        raise service_error(result.errors[0])
    return {"handle": result.value.handle, "name": result.value.name}


def scheduled_formula_save(api: Api, payload: dict) -> dict:
    """Update formula inputs and ordinary metadata without exposing owned mechanics."""
    handle = str(payload.get("handle") or "").strip()
    existing = api.db.get_scheduled(handle)
    if existing is None:
        raise KeyError(handle)
    raw_formulas = payload.get("formulas")
    if not isinstance(raw_formulas, list):
        raise ValueError("formula expressions must be a list")
    formulas: dict[int, str] = {}
    for row in raw_formulas:
        if not isinstance(row, dict):
            raise ValueError("formula expressions must identify their split")
        raw_index = row.get("index")
        if not isinstance(raw_index, (int, str)):
            raise ValueError("formula split index is invalid")
        try:
            index = int(raw_index)
        except (TypeError, ValueError) as exc:
            raise ValueError("formula split index is invalid") from exc
        if index in formulas:
            raise ValueError("formula split indices cannot be repeated")
        formulas[index] = str(row.get("formula") or "")
    raw_variables = payload.get("variables")
    if not isinstance(raw_variables, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in raw_variables.items()
    ):
        raise ValueError("formula variables must be a name-to-value object")
    name = str(payload.get("name") or "").strip()
    if not name:
        raise ValueError("schedule name is required")
    recurrence = schedule_recurrence(payload, existing)
    try:
        growth_policy = ScheduleGrowthPolicy(
            str(payload.get("growth_policy") or existing.growth_policy.value)
        )
    except ValueError:
        raise ValueError("choose a valid projection growth policy") from None
    enabled = existing.enabled
    if "enabled" in payload:
        if not isinstance(payload["enabled"], bool):
            raise ValueError("active status must be true or false")
        enabled = payload["enabled"]
    placeholder = existing.placeholder
    if "placeholder" in payload:
        if not isinstance(payload["placeholder"], bool):
            raise ValueError("schedule kind must be a boolean estimate flag")
        placeholder = payload["placeholder"]
    auto_create = existing.auto_create
    if "auto" in payload:
        if not isinstance(payload["auto"], bool):
            raise ValueError("automatic posting status must be true or false")
        auto_create = payload["auto"]

    result = save_formula_schedule(
        api.db,
        FormulaScheduleInput(
            existing_handle=existing.handle,
            name=name,
            recurrence=recurrence,
            formulas=formulas,
            variables=raw_variables,
            enabled=enabled,
            auto_create=auto_create,
            placeholder=placeholder,
            growth_policy=growth_policy,
            skipped=tuple(parse_skipped(payload, recurrence)),
        ),
    )
    if result.value is None:
        raise service_error(result.errors[0])
    return {"handle": result.value.handle, "name": result.value.name}


def scheduled_delete(api: Api, payload: dict) -> dict:
    """Remove one definition while retaining its posted ledger history."""
    handle = str(payload.get("handle") or "").strip()
    result = delete_schedule(api.db, DeleteSchedule(handle))
    if not result.ok:
        raise service_error(result.errors[0])
    deleted = result.value
    assert deleted is not None
    return {"handle": deleted.handle, "name": deleted.name}


def scheduled_duplicate(api: Api, payload: dict) -> dict:
    """Save an independent exact copy, including editor-protected fields."""
    result = duplicate_schedule(
        api.db,
        DuplicateSchedule(
            str(payload.get("handle") or ""),
            name=str(payload.get("name") or ""),
        ),
    )
    if not result.ok:
        raise service_error(result.errors[0])
    copied = result.value
    assert copied is not None
    return {"handle": copied.handle, "name": copied.name}


def historical_estimates(
    api: Api,
    months: int = 12,
    min_active_months: int = 3,
    scenario_handle: str | None = None,
) -> dict:
    """Return reviewable category estimates inferred from closed history."""
    proposals = estimates.propose_historical_estimates(
        api.db,
        months=months,
        min_active_months=min_active_months,
        scenario_handle=scenario_handle,
    )
    return {
        "months": months,
        "proposals": [
            {
                "key": item.key,
                "category": item.category,
                "category_name": item.category_name,
                "purpose_name": item.purpose_name,
                "funding": item.funding,
                "funding_name": item.funding_name,
                "amount": item.display_amount,
                "source_name": item.source_name,
                "destination_name": item.destination_name,
                "start": item.recurrence.start,
                "frequency": item.recurrence.describe(),
                "frequency_key": frequency_key(item.recurrence),
                "seasonal_amounts": [
                    {"month": value.month, "amount": value.amount}
                    for value in item.seasonal_amounts
                ],
                "scheduled_amount": item.scheduled_amount,
                "active_months": item.active_months,
                "transaction_count": item.transaction_count,
                "confidence": item.confidence,
                "outlier_months": item.outlier_months,
                "variability": item.variability,
                "reason": item.reason,
                "evidence": item.evidence.serialize(),
                "draft": historical_estimate_draft(api.db, item),
                "planning_flow": (
                    item.planning_flow.value if item.planning_flow is not None else None
                ),
                "investment_activity": (
                    item.investment_activity.value if item.investment_activity is not None else None
                ),
            }
            for item in proposals
        ],
        "targets": [
            {"handle": None, "name": "Base"},
            *[
                {"handle": scenario.handle, "name": scenario.name}
                for scenario in api.db.iter_scenarios()
            ],
        ],
    }


def historical_estimate_draft(db: DbSQLite, proposal: estimates.HistoricalEstimateProposal) -> dict:
    """Translate the engine-owned draft once for every interactive client."""
    draft = estimates.draft_historical_estimate(db, proposal)
    parts = simple_schedule_parts(db, draft)
    if parts is None:
        raise ValueError("historical estimate draft is not safely editable")
    frequency = frequency_key(draft.recurrence)
    if frequency is None:
        raise ValueError("historical estimate cadence is not safely editable")
    return {
        "handle": None,
        "name": draft.name,
        "category": parts["category"],
        "funding": parts["funding"],
        "amount": parts["amount"],
        "category_planning_flow": parts["category_planning_flow"],
        "planning_flow": parts["planning_flow"],
        "investment_activity": parts["investment_activity"],
        "frequency": frequency,
        "frequency_key": frequency,
        "start": draft.recurrence.start.isoformat(),
        "end": draft.recurrence.end.isoformat() if draft.recurrence.end else None,
        "count": draft.recurrence.count,
        "weekend": weekend_key(draft.recurrence.weekend_adjust),
        "enabled": True,
        "placeholder": True,
        "auto": False,
        "growth_policy": draft.growth_policy.value,
        "category_memo": parts["category_memo"],
        "funding_memo": parts["funding_memo"],
        "additional_splits": parts["additional_splits"],
        "amount_changes": [],
        "split_amount_changes": [],
        "seasonal_amounts": [
            {"month": item.month, "amount": item.amount} for item in draft.seasonal_amounts
        ],
        "skipped": [],
        "occurrence_adjustments": [],
        "account_handles": [parts["category"], parts["funding"]],
        "estimate_evidence": draft.estimate_evidence,
    }


def historical_estimate_accept(api: Api, payload: dict) -> dict:
    """Accept one historical proposal as a normal planning estimate."""
    months = int(payload.get("months") or 12)
    minimum = int(payload.get("min_active_months") or 3)
    proposal_key = str(payload.get("key") or "")
    category = str(payload.get("category") or "")
    scenario = str(payload.get("scenario") or "").strip() or None
    proposals = estimates.propose_historical_estimates(
        api.db,
        months=months,
        min_active_months=minimum,
        scenario_handle=scenario,
    )
    proposal = next(
        (
            item
            for item in proposals
            if item.key == proposal_key or (not proposal_key and item.category == category)
        ),
        None,
    )
    if proposal is None:
        raise ValueError("historical estimate proposal is no longer available")
    handle = estimates.accept_historical_estimate(api.db, proposal, scenario_handle=scenario)
    return {"handle": handle, "category": proposal.category_name}


def due_review(api: Api) -> dict:
    """Due and missed scheduled occurrences, grouped by schedule."""
    return {
        "schedules": [
            {
                "schedule": review.schedule,
                "name": review.name,
                "frequency": review.frequency,
                "total": str(review.total.to_decimal()),
                "items": [
                    {
                        "date": item.when.isoformat(),
                        "amount": str(item.amount.to_decimal()),
                        "overdue": item.overdue,
                    }
                    for item in review.items
                ],
            }
            for review in pending_due_review(api.db)
        ]
    }


def due_review_resolve(api: Api, payload: dict) -> dict:
    raw = payload.get("decisions")
    if not isinstance(raw, list):
        raise ValueError("decisions must be a list")
    decisions: list[tuple[str, date, DueDecision]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("each decision must be an object")
        decisions.append(
            (
                str(item.get("schedule", "")),
                date.fromisoformat(str(item.get("date", ""))),
                DueDecision(str(item.get("decision"))),
            )
        )
    result = resolve_due(api.db, ResolveDue(tuple(decisions)))
    if not result.ok:
        raise service_error(result.errors[0])
    outcome = result.value
    assert outcome is not None
    return {
        "posted": outcome.posted,
        "skipped": outcome.skipped,
        "deferred": outcome.deferred,
    }


def post_scheduled(api: Api) -> dict:
    posted = schedule.post_due(api.db, only_auto=False)
    return {
        "posted": len(posted),
        "transactions": [{"date": t.post_date, "description": t.description} for t in posted],
    }
