"""Web fixed-schedule request construction over the shared mutation service.

The parser protocol records the existing web control parsers used by this adapter.
Financial validation and transaction ownership remain in the schedule service.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol

from ..gen.db.sqlite import DbSQLite
from ..gen.engine import schedule
from ..gen.lib import (
    InvestmentActivityKind,
    Money,
    PlanningFlowKind,
    Recurrence,
    ScheduledAmountChange,
    ScheduledMonthAmount,
    ScheduledOccurrenceAdjustment,
    ScheduledSplitAmountChange,
    ScheduledTransaction,
    ScheduleGrowthPolicy,
)
from ..gen.services import (
    FixedScheduleInput,
    FixedSplitInput,
    SaveFixedSchedule,
    ServiceError,
    save_fixed_schedule,
)
from .resources import ResourceError


class FixedScheduleRequestParser(Protocol):
    db: DbSQLite

    def _simple_schedule_parts(self, scheduled: ScheduledTransaction) -> dict | None: ...

    def _frequency_key(self, recurrence: Recurrence) -> str | None: ...

    def _input_money(self, payload: dict, raw: object) -> Money: ...

    def _schedule_recurrence_from_payload(
        self, payload: dict, existing: ScheduledTransaction | None = None
    ) -> Recurrence: ...

    def _parse_amount_changes(
        self, payload: dict, schedule_start: date
    ) -> list[ScheduledAmountChange]: ...

    def _parse_seasonal_amounts(self, payload: dict) -> list[ScheduledMonthAmount]: ...

    def _parse_skipped(self, payload: dict, recurrence: Recurrence) -> list[date]: ...

    def _parse_occurrence_adjustments(
        self, payload: dict, recurrence: Recurrence
    ) -> list[ScheduledOccurrenceAdjustment]: ...

    def _parse_additional_splits(
        self, payload: dict, excluded: set[str]
    ) -> tuple[FixedSplitInput, ...]: ...

    def _parse_split_amount_changes(
        self, payload: dict, schedule_start: date, allowed_accounts: set[str]
    ) -> dict[str, list[ScheduledSplitAmountChange]]: ...

    def _service_resource_error(self, error: ServiceError) -> ResourceError: ...


def save_fixed_schedule_request(adapter: FixedScheduleRequestParser, payload: dict) -> dict:
    """Create or update a fixed-split baseline schedule."""
    handle = str(payload.get("handle") or "").strip()
    existing = adapter.db.get_scheduled(handle) if handle else None
    if handle and existing is None:
        raise KeyError(handle)
    existing_parts = adapter._simple_schedule_parts(existing) if existing is not None else None
    if existing is not None:
        editability = schedule.schedule_editability(adapter.db, existing)
        if not editability.editable:
            raise ValueError(editability.reason)
        if (
            editability.mode is not schedule.ScheduleEditorMode.FIXED
            or existing_parts is None
            or adapter._frequency_key(existing.recurrence) is None
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
        amount = adapter._input_money(payload, payload.get("amount") or "0")
    except (ValueError, ArithmeticError) as exc:
        raise ValueError("amount must be a valid number") from exc
    recurrence = adapter._schedule_recurrence_from_payload(payload, existing)
    amount_changes = adapter._parse_amount_changes(payload, recurrence.start)
    skipped = adapter._parse_skipped(payload, recurrence)
    adjustments = adapter._parse_occurrence_adjustments(payload, recurrence)
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
    additional_splits = adapter._parse_additional_splits(payload, {category_handle, funding_handle})
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
        adapter._parse_split_amount_changes(
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
        adapter._parse_seasonal_amounts(payload)
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
        adapter.db,
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
        raise adapter._service_resource_error(result.errors[0])
    return {"handle": result.value.handle, "name": result.value.name}
