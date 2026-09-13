"""Recurrence rules.

Deliberately modelled on GnuCash's ``Recurrence`` rather than on iCalendar RRULE:
a period type plus a multiplier covers every schedule a household budget actually
has (weekly pay, fortnightly pay, the 1st and 15th, quarterly tax, annual premium)
without dragging in RRULE's exception machinery.

Two behaviours matter for cash-flow accuracy and are easy to get wrong:

* **Month-end clamping.** A rule anchored on the 31st must fire on 28 or 30 in the
  short months and then return to the 31st, not drift earlier every month.
* **Business-day adjustment.** Direct debits and payroll land on a working day.
  A rule can push its occurrence to the previous or next weekday, which changes
  which *month* a cash movement falls in and therefore changes a monthly forecast.
"""

from __future__ import annotations

import calendar
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum
from typing import Any

__all__ = ["PeriodType", "WeekendAdjust", "RecurrenceOccurrence", "Recurrence", "add_months"]


class PeriodType(str, Enum):
    ONCE = "once"
    DAY = "day"
    WEEK = "week"
    SEMI_MONTH = "semi_month"
    MONTH = "month"
    YEAR = "year"


class WeekendAdjust(str, Enum):
    NONE = "none"
    PREVIOUS = "previous"
    NEXT = "next"


def add_months(anchor: date, months: int, day: int | None = None) -> date:
    """Shift by whole months, clamping the day to the target month's length.

    ``day`` may be ``-1`` to mean "last day of the month".
    """
    total = anchor.month - 1 + months
    year = anchor.year + total // 12
    month = total % 12 + 1
    last = calendar.monthrange(year, month)[1]
    wanted = anchor.day if day is None else day
    if wanted == -1:
        wanted = last
    return date(year, month, min(wanted, last))


@dataclass(frozen=True, slots=True)
class RecurrenceOccurrence:
    """One numbered recurrence firing before and after weekend adjustment."""

    number: int
    nominal: date
    adjusted: date


class Recurrence:
    """How often something happens, and when it stops."""

    def __init__(
        self,
        period: PeriodType | str = PeriodType.MONTH,
        interval: int = 1,
        start: date | None = None,
        end: date | None = None,
        count: int | None = None,
        day_of_month: int | None = None,
        second_day_of_month: int | None = None,
        weekend_adjust: WeekendAdjust | str = WeekendAdjust.NONE,
    ) -> None:
        self.period = PeriodType(period) if not isinstance(period, PeriodType) else period
        if interval < 1:
            raise ValueError("interval must be >= 1")
        self.interval = interval
        self.start = start or date.today()
        self.end = end
        self.count = count
        #: For MONTH/YEAR rules; ``-1`` means the last day of the month.
        self.day_of_month = day_of_month
        #: Second firing day for SEMI_MONTH rules (e.g. the 15th alongside the 1st).
        self.second_day_of_month = second_day_of_month
        self.weekend_adjust = (
            WeekendAdjust(weekend_adjust)
            if not isinstance(weekend_adjust, WeekendAdjust)
            else weekend_adjust
        )

    # ------------------------------------------------------------------- firing

    def _adjust(self, when: date) -> date:
        if self.weekend_adjust is WeekendAdjust.NONE or when.weekday() < 5:
            return when
        if self.weekend_adjust is WeekendAdjust.PREVIOUS:
            return when - timedelta(days=when.weekday() - 4)
        return when + timedelta(days=7 - when.weekday())

    def _raw_occurrences(self, start_index: int = 0) -> Iterator[date]:
        """Unadjusted firing dates for simple recurrence types."""
        if self.period is PeriodType.ONCE:
            if start_index == 0:
                yield self.start
            return

        if self.period is PeriodType.SEMI_MONTH:
            raise ValueError("semi-monthly occurrences need their own generator")

        index = max(0, start_index)
        while True:
            if self.period is PeriodType.DAY:
                yield self.start + timedelta(days=index * self.interval)
            elif self.period is PeriodType.WEEK:
                yield self.start + timedelta(weeks=index * self.interval)
            elif self.period is PeriodType.MONTH:
                yield add_months(self.start, index * self.interval, day=self.day_of_month)
            elif self.period is PeriodType.YEAR:
                yield add_months(self.start, index * 12 * self.interval, day=self.day_of_month)
            else:  # pragma: no cover - exhaustive
                raise ValueError(f"unhandled period {self.period}")
            index += 1

    def _start_index_near(self, since: date | None) -> int:
        """Return a conservative raw index near ``since`` for simple rules."""
        if since is None or since <= self.start:
            return 0
        target = since - timedelta(days=7)
        if target <= self.start:
            return 0
        if self.period is PeriodType.DAY:
            return max(0, (target - self.start).days // self.interval)
        if self.period is PeriodType.WEEK:
            return max(0, (target - self.start).days // (7 * self.interval))
        if self.period is PeriodType.MONTH:
            months = (target.year - self.start.year) * 12 + target.month - self.start.month
            return max(0, months // self.interval - 1)
        if self.period is PeriodType.YEAR:
            years = target.year - self.start.year
            return max(0, years // self.interval - 1)
        return 0

    def _semi_month_start_step_near(self, since: date | None) -> int:
        if since is None or since <= self.start:
            return 0
        target = since - timedelta(days=7)
        months = (target.year - self.start.year) * 12 + target.month - self.start.month
        return max(0, months // self.interval - 1)

    def _semi_month_days(self, base: date) -> list[date]:
        first = self.day_of_month or self.start.day
        second = self.second_day_of_month or 15
        return [
            add_months(base, 0, day=day)
            for day in sorted((first, second), key=lambda value: 32 if value == -1 else value)
        ]

    def _semi_month_first_count(self) -> int:
        base = add_months(self.start, 0, day=1)
        return sum(1 for when in self._semi_month_days(base) if when >= self.start)

    def _numbered_raw_occurrences(self, since: date | None = None) -> Iterator[tuple[int, date]]:
        """Yield ``(occurrence number, nominal date)`` without losing ordinals."""
        if self.period is PeriodType.SEMI_MONTH:
            first_count = self._semi_month_first_count()
            step = self._semi_month_start_step_near(since)
            while True:
                base = add_months(self.start, step * self.interval, day=1)
                valid = [
                    when for when in self._semi_month_days(base) if step > 0 or when >= self.start
                ]
                before = 0 if step == 0 else first_count + (step - 1) * 2
                for offset, when in enumerate(valid, start=1):
                    yield before + offset, when
                step += 1
            # unreachable

        start_index = self._start_index_near(since)
        yield from enumerate(self._raw_occurrences(start_index), start=start_index + 1)

    def occurrence_details(
        self, until: date, since: date | None = None
    ) -> list[RecurrenceOccurrence]:
        """Numbered nominal/adjusted firings in ``[since, until]``.

        The occurrence number belongs to the nominal recurrence sequence. Weekend
        adjustment may move the cash date across a month boundary, but must never
        change the period number used by loan formulas.
        """
        results: list[RecurrenceOccurrence] = []
        for number, raw in self._numbered_raw_occurrences(since):
            if self.count is not None and number > self.count:
                break
            if self.end and raw > self.end:
                break
            if raw > until and self.weekend_adjust is WeekendAdjust.NONE:
                break
            if raw > until + timedelta(days=7):
                break
            adjusted = self._adjust(raw)
            if adjusted > until:
                continue
            if since and adjusted < since:
                continue
            results.append(RecurrenceOccurrence(number, raw, adjusted))
        return sorted(results, key=lambda item: (item.adjusted, item.number))

    def occurrences(self, until: date, since: date | None = None) -> list[date]:
        """Every adjusted firing date in ``[since, until]``."""
        return [item.adjusted for item in self.occurrence_details(until, since)]

    def next_after(self, moment: date) -> date | None:
        """First firing strictly after ``moment``, or ``None`` if the rule is spent."""
        since = moment + timedelta(days=1)
        for number, raw in self._numbered_raw_occurrences(since):
            if self.count is not None and number > self.count:
                return None
            if self.end and raw > self.end:
                return None
            adjusted = self._adjust(raw)
            if adjusted > moment:
                return adjusted
        return None

    def last_occurrence(self) -> date | None:
        """The final adjusted firing, or ``None`` for an unbounded rule."""
        if self.period is not PeriodType.ONCE and self.end is None and self.count is None:
            return None
        last: date | None = None
        for number, raw in self._numbered_raw_occurrences():
            if self.count is not None and number > self.count:
                break
            if self.end is not None and raw > self.end:
                break
            last = self._adjust(raw)
            if self.period is PeriodType.ONCE:
                break
        return last

    def index_of(self, when: date) -> int:
        """Which adjusted occurrence ``when`` is, counting the first as 1."""
        window_start = when - timedelta(days=7)
        window_end = when + timedelta(days=7)
        for occurrence in self.occurrence_details(window_end, since=window_start):
            if occurrence.adjusted == when:
                return occurrence.number
        raise ValueError(f"{when.isoformat()} is not an occurrence of this recurrence")

    def describe(self) -> str:
        if self.period is PeriodType.ONCE:
            return f"once on {self.start:%d %b %Y}"
        unit = {
            "day": "day",
            "week": "week",
            "month": "month",
            "year": "year",
            "semi_month": "half-month",
        }[self.period.value]
        every = unit if self.interval == 1 else f"{self.interval} {unit}s"
        text = f"every {every}"
        if self.end:
            text += f" until {self.end:%d %b %Y}"
        elif self.count:
            text += f", {self.count} times"
        return text

    # ------------------------------------------------------------ serialisation

    def serialize(self) -> dict[str, Any]:
        return {
            "period": self.period.value,
            "interval": self.interval,
            "start": self.start.isoformat(),
            "end": self.end.isoformat() if self.end else None,
            "count": self.count,
            "day_of_month": self.day_of_month,
            "second_day_of_month": self.second_day_of_month,
            "weekend_adjust": self.weekend_adjust.value,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Recurrence:
        return cls(
            period=data.get("period", "month"),
            interval=data.get("interval", 1),
            start=date.fromisoformat(data["start"]),
            end=date.fromisoformat(data["end"]) if data.get("end") else None,
            count=data.get("count"),
            day_of_month=data.get("day_of_month"),
            second_day_of_month=data.get("second_day_of_month"),
            weekend_adjust=data.get("weekend_adjust", "none"),
        )

    def __repr__(self) -> str:
        return f"<Recurrence {self.describe()}>"
