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
    ScheduledSplit,
    ScheduledTransaction,
    ScheduledVersion,
    VersionKind,
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


def parse_versions(payload: dict) -> list[ScheduledVersion]:
    """The editor's versions: each ``{kind, start | month, legs}``, every leg with its
    account, memo, an ``amount`` or a ``formula``, and its ``direction`` (-1 for
    the Decrease column, which negates an amount; an amount may also be signed).
    Validation of the whole schedule (balance, dates on the schedule) is the
    service's."""
    raw_versions = payload.get("versions") or []
    if not isinstance(raw_versions, list):
        raise ValueError("versions must be a list")
    versions = []
    for raw in raw_versions:
        if not isinstance(raw, dict):
            raise ValueError("a version is invalid")
        kind = str(raw.get("kind") or "")
        if kind not in {item.value for item in VersionKind}:
            raise ValueError("a version is from a date, on one date, or for a month")
        try:
            start = date.fromisoformat(str(raw["start"])) if raw.get("start") else None
            month = int(raw["month"]) if raw.get("month") not in (None, "") else None
        except (TypeError, ValueError):
            raise ValueError("a version needs a YYYY-MM-DD date or a month from 1 to 12") from None
        legs = raw.get("legs") or []
        if not isinstance(legs, list):
            raise ValueError("a version's legs must be a list")
        splits = []
        for leg in legs:
            if not isinstance(leg, dict):
                raise ValueError("a version's leg is invalid")
            formula = str(leg.get("formula") or "").strip()
            amount = None
            if not formula:
                try:
                    amount = input_amount(payload, leg.get("amount") or "0")
                except (ValueError, ArithmeticError):
                    raise ValueError("each leg needs an amount or a formula") from None
                if str(leg.get("direction")) == "-1":
                    amount = -amount  # typed in the Decrease column
            raw_purpose = str(leg.get("planning_flow") or "").strip()
            raw_activity = str(leg.get("investment_activity") or "").strip()
            try:
                splits.append(
                    ScheduledSplit(
                        str(leg.get("account") or "").strip(),
                        amount,
                        formula=formula,
                        memo=str(leg.get("memo") or "").strip(),
                        planning_flow=PlanningFlowKind(raw_purpose) if raw_purpose else None,
                        investment_activity=(
                            InvestmentActivityKind(raw_activity) if raw_activity else None
                        ),
                        direction=(-1 if formula and str(leg.get("direction")) == "-1" else 1),
                    )
                )
            except ValueError:
                raise ValueError("choose a valid planning purpose or activity") from None
        try:
            versions.append(ScheduledVersion(kind, splits, start=start, month=month))
        except ValueError as exc:
            raise ValueError(str(exc)) from None
    return versions


def parse_new_version(payload: dict) -> tuple[VersionKind, date | None, int | None]:
    """The kind, date, and month of the version an editor is adding
    (``new_version``: ``{kind, start | month}``)."""
    raw = payload.get("new_version")
    if not isinstance(raw, dict):
        raise ValueError("new_version must name the version's kind and date or month")
    try:
        kind = VersionKind(str(raw.get("kind") or ""))
    except ValueError:
        raise ValueError("a version is from a date, on one date, or for a month") from None
    try:
        if kind is VersionKind.MONTH:
            month = int(raw["month"])
            if not 1 <= month <= 12:
                raise ValueError
            return kind, None, month
        return kind, date.fromisoformat(str(raw["start"])), None
    except (KeyError, TypeError, ValueError):
        raise ValueError("a version needs a YYYY-MM-DD date or a month from 1 to 12") from None


def versions_json(versions, names: dict[str, str]) -> list[dict]:
    """Versions as the browser editor reads them (see :func:`parse_versions`)."""
    return [
        {
            "kind": version.kind.value,
            "start": version.start.isoformat() if version.start else None,
            "month": version.month,
            "label": version.label(),
            "legs": [
                {
                    "account": split.account,
                    "account_name": names.get(split.account, split.account),
                    "memo": split.memo,
                    "amount": (
                        str(split.amount.to_decimal())
                        if split.amount is not None and not split.formula
                        else None
                    ),
                    "formula": split.formula,
                    "direction": split.direction,
                    "planning_flow": split.planning_flow.value if split.planning_flow else None,
                    "investment_activity": (
                        split.investment_activity.value if split.investment_activity else None
                    ),
                }
                for split in version.splits
            ],
        }
        for version in versions
    ]
