"""Browser schedule controls: recurrence, exceptions, and split rows.

The fixed-schedule and scenario-event editors in the browser send the same
controls. These functions parse them into the typed values the schedule services
take, rejecting anything malformed with a message for the person, and describe a
saved schedule back in the editor's terms (``simple_schedule_parts``) only when the
shared editability projection says the simple editor can reproduce it.
"""

from __future__ import annotations

from datetime import date

from ..gen.db.sqlite import DbSQLite
from ..gen.engine import schedule
from ..gen.lib import (
    AccountClass,
    InvestmentActivityKind,
    PeriodType,
    PlanningFlowKind,
    Recurrence,
    ScheduledAmountChange,
    ScheduledMonthAmount,
    ScheduledOccurrenceAdjustment,
    ScheduledSplitAmountChange,
    ScheduledTransaction,
    WeekendAdjust,
)
from ..gen.services import FixedSplitInput
from ..gen.services.schedule_editing import ScenarioChangeView, ScheduleEditorView, editor_view
from ..presentation import (
    FIXED_INTERVAL_UNITS,
    LEGACY_FREQUENCIES,
    MAX_INTERVAL,
    frequency_period,
    frequency_unit,
)
from .controls import input_amount

#: Editor weekend keys.
WEEKENDS = {
    "none": WeekendAdjust.NONE,
    "previous": WeekendAdjust.PREVIOUS,
    "next": WeekendAdjust.NEXT,
}


def simple_schedule_parts(db: DbSQLite, scheduled) -> dict | None:
    """The simple editor's fields for ``scheduled``, or ``None`` when it cannot show it."""
    return simple_parts(editor_view(db, scheduled))


def simple_parts(view: ScheduleEditorView | ScenarioChangeView) -> dict | None:
    """:func:`simple_schedule_parts` from a book server's editor view."""
    projection, scheduled = view.projection, view.schedule
    if (
        projection.editability.mode is not schedule.ScheduleEditorMode.FIXED
        or projection.primary is None
        or projection.funding is None
    ):
        return None
    primary_projection = projection.primary
    funding_projection = projection.funding
    flow = scheduled.splits[primary_projection.index]
    funding = scheduled.splits[funding_projection.index]
    flow_class = view.classes.get(flow.account)
    assert flow_class is not None
    additional = []
    for item in projection.additional:
        split = scheduled.splits[item.index]
        row = {
            "account": split.account,
            "amount": str(item.amount.to_decimal()),
            "planning_flow": (
                split.planning_flow.value if split.planning_flow is not None else None
            ),
        }
        if split.investment_activity is not None:
            row["investment_activity"] = split.investment_activity.value
        if item.opposite_direction:
            row["direction"] = "opposite"
        if split.memo:
            row["memo"] = split.memo
        additional.append(row)
    return {
        "category": flow.account,
        "funding": funding.account,
        "amount": str(primary_projection.amount.to_decimal()),
        "category_memo": flow.memo,
        "funding_memo": funding.memo,
        "category_planning_flow": (
            flow.planning_flow.value if flow.planning_flow is not None else None
        ),
        "category_ledger_direction": (
            primary_projection.ledger_direction
            if flow_class not in {AccountClass.INCOME, AccountClass.EXPENSE}
            else None
        ),
        "planning_flow": (
            funding.planning_flow.value if funding.planning_flow is not None else None
        ),
        "investment_activity": (
            flow.investment_activity.value if flow.investment_activity is not None else None
        ),
        "additional_splits": additional,
    }


def frequency_key(recurrence: Recurrence) -> str | None:
    """The editor's unit for a rule (with ``recurrence.interval`` as its "every N"),
    or None for a period no editor offers (#370)."""
    return frequency_unit(recurrence.period)


def schedule_recurrence(
    payload: dict,
    existing: ScheduledTransaction | None = None,
) -> Recurrence:
    """Parse the editors' recurrence controls.

    With ``existing`` of the same period and interval, its day-of-month details
    (which the browser editors do not show) are kept.
    """
    frequency = str(payload.get("frequency") or "months")
    # The earlier preset keys (weekly, biweekly, quarterly, …) still arrive from
    # older pages and saved drafts; each names a unit and an interval.
    legacy = LEGACY_FREQUENCIES.get(frequency)
    unit, default_interval = legacy if legacy is not None else (frequency, 1)
    try:
        period = frequency_period(unit)
    except ValueError:
        raise ValueError("unsupported schedule frequency") from None
    if unit in FIXED_INTERVAL_UNITS:
        # Twice a month keeps an imported rule's interval, which the editors do
        # not show; once has none.
        interval = (
            existing.recurrence.interval
            if unit == "semimonthly"
            and existing is not None
            and existing.recurrence.period is period
            else 1
        )
    else:
        raw_interval = payload.get("interval")
        if legacy is not None or raw_interval is None or str(raw_interval).strip() == "":
            interval = default_interval
        else:
            try:
                interval = int(str(raw_interval).strip())
            except ValueError:
                raise ValueError("repeat every must be a whole number") from None
        if not 1 <= interval <= MAX_INTERVAL:
            raise ValueError(f"repeat every must be from 1 to {MAX_INTERVAL}")
    try:
        start = date.fromisoformat(str(payload.get("start") or ""))
    except ValueError as exc:
        raise ValueError("first due date is invalid") from exc
    end = None
    count = None
    # A one-time schedule has no end or count; its editors may leave those fields set.
    if period is not PeriodType.ONCE:
        raw_end = str(payload.get("end") or "").strip()
        if raw_end:
            try:
                end = date.fromisoformat(raw_end)
            except ValueError as exc:
                raise ValueError("end date is invalid") from exc
            if end < start:
                raise ValueError("end date cannot precede first due date")
        raw_count = payload.get("count")
        if raw_count is not None and str(raw_count).strip() != "":
            try:
                count = int(raw_count)
            except (TypeError, ValueError) as exc:
                raise ValueError("occurrence count must be a whole number") from exc
            if count < 1:
                raise ValueError("occurrence count must be positive")
        if end is not None and count is not None:
            raise ValueError("choose an end date or occurrence count, not both")
    weekend_key = str(payload.get("weekend") or "none")
    if weekend_key not in WEEKENDS:
        raise ValueError("unsupported weekend adjustment")
    preserve_shape = (
        existing is not None
        and existing.recurrence.period is period
        and existing.recurrence.interval == interval
    )
    day_of_month = existing.recurrence.day_of_month if existing and preserve_shape else None
    second_day_of_month = (
        existing.recurrence.second_day_of_month if existing and preserve_shape else None
    )
    return Recurrence(
        period=period,
        interval=interval,
        start=start,
        end=end,
        count=count,
        day_of_month=day_of_month,
        second_day_of_month=second_day_of_month,
        weekend_adjust=WEEKENDS[weekend_key],
    )


def weekend_key(adjust: WeekendAdjust) -> str:
    for key, value in WEEKENDS.items():
        if adjust is value:
            return key
    return "none"


def parse_amount_changes(payload: dict, schedule_start: date) -> list[ScheduledAmountChange]:
    raw_changes = payload.get("amount_changes") or []
    if not isinstance(raw_changes, list):
        raise ValueError("future amounts must be a list")
    changes = []
    seen = set()
    for raw in raw_changes:
        if not isinstance(raw, dict):
            raise ValueError("future amount entry is invalid")
        try:
            when = date.fromisoformat(str(raw.get("start") or ""))
            amount = abs(input_amount(payload, raw.get("amount") or ""))
        except (ValueError, ArithmeticError):
            raise ValueError("future amounts require YYYY-MM-DD dates and valid amounts") from None
        if when < schedule_start:
            raise ValueError("future amount date cannot precede first occurrence")
        if not amount:
            raise ValueError("future amount must be greater than zero")
        if when in seen:
            raise ValueError("future amount dates must be unique")
        seen.add(when)
        changes.append(ScheduledAmountChange(when, amount))
    return sorted(changes, key=lambda item: item.start)


def parse_seasonal_amounts(payload: dict) -> list[ScheduledMonthAmount]:
    raw_amounts = payload.get("seasonal_amounts") or []
    if not isinstance(raw_amounts, list):
        raise ValueError("seasonal amounts must be a list")
    amounts = []
    seen = set()
    for raw in raw_amounts:
        if not isinstance(raw, dict):
            raise ValueError("seasonal amount entry is invalid")
        try:
            raw_month = raw.get("month")
            if raw_month is None:
                raise ValueError
            month = int(raw_month)
            amount = abs(input_amount(payload, raw.get("amount") or ""))
            item = ScheduledMonthAmount(month, amount)
        except (TypeError, ValueError, ArithmeticError):
            raise ValueError(
                "seasonal amounts require a month from 1 through 12 and a positive amount"
            ) from None
        if month in seen:
            raise ValueError("seasonal amount months must be unique")
        seen.add(month)
        amounts.append(item)
    return sorted(amounts, key=lambda item: item.month)


def parse_skipped(payload: dict, recurrence: Recurrence) -> list[date]:
    raw_skipped = payload.get("skipped") or []
    if not isinstance(raw_skipped, list):
        raise ValueError("skipped occurrences must be a list")
    skipped = []
    seen = set()
    for raw in raw_skipped:
        try:
            when = date.fromisoformat(str(raw))
        except ValueError:
            raise ValueError("skipped occurrences require YYYY-MM-DD dates") from None
        if when in seen:
            raise ValueError("skipped occurrence dates must be unique")
        if when not in recurrence.occurrences(when, since=when):
            raise ValueError(f"{when.isoformat()} is not an occurrence of this schedule")
        seen.add(when)
        skipped.append(when)
    return sorted(skipped)


def parse_occurrence_adjustments(
    payload: dict, recurrence: Recurrence
) -> list[ScheduledOccurrenceAdjustment]:
    raw_changes = payload.get("occurrence_adjustments") or []
    if not isinstance(raw_changes, list):
        raise ValueError("one-time amounts must be a list")
    changes = []
    seen = set()
    for raw in raw_changes:
        if not isinstance(raw, dict):
            raise ValueError("one-time amount entry is invalid")
        try:
            when = date.fromisoformat(str(raw.get("when") or ""))
            amount = abs(input_amount(payload, raw.get("amount") or ""))
        except (ValueError, ArithmeticError):
            raise ValueError(
                "one-time amounts require YYYY-MM-DD dates and valid amounts"
            ) from None
        if not amount:
            raise ValueError("one-time amount must be greater than zero")
        if when in seen:
            raise ValueError("one-time amount dates must be unique")
        if when not in recurrence.occurrences(when, since=when):
            raise ValueError(f"{when.isoformat()} is not an occurrence of this schedule")
        seen.add(when)
        changes.append(ScheduledOccurrenceAdjustment(when, amount))
    return sorted(changes, key=lambda item: item.when)


def parse_additional_splits(payload: dict) -> tuple[FixedSplitInput, ...]:
    raw_splits = payload.get("additional_splits") or []
    if not isinstance(raw_splits, list):
        raise ValueError("additional splits must be a list")
    splits: list[FixedSplitInput] = []
    for raw in raw_splits:
        if not isinstance(raw, dict):
            raise ValueError("additional split entry is invalid")
        handle = str(raw.get("account") or "").strip()
        try:
            amount = input_amount(payload, raw.get("amount") or "0")
        except (ValueError, ArithmeticError) as exc:
            raise ValueError("additional split amount must be a valid number") from exc
        raw_purpose = str(raw.get("planning_flow") or "").strip()
        try:
            purpose = PlanningFlowKind(raw_purpose) if raw_purpose else None
        except ValueError:
            raise ValueError("choose a valid planning purpose") from None
        raw_activity = str(raw.get("investment_activity") or "").strip()
        try:
            investment_activity = InvestmentActivityKind(raw_activity) if raw_activity else None
        except ValueError:
            raise ValueError("choose a valid investment activity") from None
        direction = str(raw.get("direction") or "normal")
        if direction not in {"normal", "opposite"}:
            raise ValueError("choose a valid additional split direction")
        splits.append(
            FixedSplitInput(
                account=handle,
                amount=amount,
                memo=str(raw.get("memo") or "").strip(),
                planning_flow=purpose,
                investment_activity=investment_activity,
                opposite_direction=direction == "opposite",
            )
        )
    return tuple(splits)


def parse_split_amount_changes(
    payload: dict,
    schedule_start: date,
    allowed_accounts: set[str],
) -> dict[str, list[ScheduledSplitAmountChange]]:
    raw_changes = payload.get("split_amount_changes") or []
    if not isinstance(raw_changes, list):
        raise ValueError("per-leg amount changes must be a list")
    grouped: dict[str, list[ScheduledSplitAmountChange]] = {}
    seen: set[tuple[str, date]] = set()
    for raw in raw_changes:
        if not isinstance(raw, dict):
            raise ValueError("per-leg amount change entry is invalid")
        account = str(raw.get("account") or "").strip()
        if account not in allowed_accounts:
            raise ValueError("per-leg amount change must reference a selected split account")
        try:
            when = date.fromisoformat(str(raw.get("start") or ""))
            amount = input_amount(payload, raw.get("amount") or "0")
        except (ValueError, ArithmeticError):
            raise ValueError(
                "per-leg amounts require YYYY-MM-DD dates and valid signed amounts"
            ) from None
        if when < schedule_start:
            raise ValueError("per-leg amount changes cannot precede the first occurrence")
        key = (account, when)
        if key in seen:
            raise ValueError("per-leg amount dates must be unique for each account")
        seen.add(key)
        grouped.setdefault(account, []).append(ScheduledSplitAmountChange(when, amount))
    for changes in grouped.values():
        changes.sort(key=lambda item: item.start)
    return grouped
