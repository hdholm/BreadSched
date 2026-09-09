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
from datetime import date, timedelta
from enum import Enum
from typing import Any

__all__ = ["PeriodType", "WeekendAdjust", "Recurrence", "add_months"]


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

    def _raw_occurrences(self) -> Iterator[date]:
        """Unadjusted firing dates, ascending and unbounded (caller must stop)."""
        if self.period is PeriodType.ONCE:
            yield self.start
            return

        if self.period is PeriodType.SEMI_MONTH:
            first = self.day_of_month or self.start.day
            second = self.second_day_of_month or 15
            step = 0
            while True:
                base = add_months(self.start, step * self.interval, day=1)
                for day in sorted((first, second), key=lambda d: 32 if d == -1 else d):
                    when = add_months(base, 0, day=day)
                    if when >= self.start:
                        yield when
                step += 1
            # unreachable

        index = 0
        while True:
            if self.period is PeriodType.DAY:
                yield self.start + timedelta(days=index * self.interval)
            elif self.period is PeriodType.WEEK:
                yield self.start + timedelta(weeks=index * self.interval)
            elif self.period is PeriodType.MONTH:
                yield add_months(self.start, index * self.interval, day=self.day_of_month)
            elif self.period is PeriodType.YEAR:
                shifted = add_months(self.start, index * 12 * self.interval, day=self.day_of_month)
                yield shifted
            else:  # pragma: no cover - exhaustive
                raise ValueError(f"unhandled period {self.period}")
            index += 1

    def occurrences(self, until: date, since: date | None = None) -> list[date]:
        """Every firing date in ``[since, until]``, honouring end date and count."""
        results: list[date] = []
        fired = 0
        for raw in self._raw_occurrences():
            if self.count is not None and fired >= self.count:
                break
            if self.end and raw > self.end:
                break
            if raw > until and (self.weekend_adjust is WeekendAdjust.NONE):
                break
            if raw > until + timedelta(days=7):
                break
            fired += 1
            when = self._adjust(raw)
            if when > until:
                continue
            if since and when < since:
                continue
            results.append(when)
        return sorted(results)

    def next_after(self, moment: date) -> date | None:
        """First firing strictly after ``moment``, or ``None`` if the rule is spent."""
        fired = 0
        for raw in self._raw_occurrences():
            if self.count is not None and fired >= self.count:
                return None
            if self.end and raw > self.end:
                return None
            fired += 1
            when = self._adjust(raw)
            if when > moment:
                return when
            if fired > 10_000:  # pragma: no cover - runaway guard
                return None
        return None

    def index_of(self, when: date) -> int:
        """Which occurrence ``when`` is, counting the first as 1.

        Loan formulas need the period number to work out how much of that payment
        is interest. Computed arithmetically rather than by counting occurrences,
        because a projection asks this once per payment per month and counting
        would make a thirty-year mortgage quadratic.
        """
        if self.period is PeriodType.ONCE:
            return 1
        elapsed_days = (when - self.start).days
        if self.period is PeriodType.DAY:
            steps = elapsed_days // self.interval
        elif self.period is PeriodType.WEEK:
            steps = elapsed_days // (7 * self.interval)
        elif self.period is PeriodType.SEMI_MONTH:
            months = (when.year - self.start.year) * 12 + (when.month - self.start.month)
            steps = months * 2 + (1 if when.day >= 15 else 0)
        elif self.period is PeriodType.MONTH:
            months = (when.year - self.start.year) * 12 + (when.month - self.start.month)
            steps = months // self.interval
        else:  # YEAR
            steps = (when.year - self.start.year) // self.interval
        return max(1, steps + 1)

    def describe(self) -> str:
        if self.period is PeriodType.ONCE:
            return f"once on {self.start:%d %b %Y}"
        unit = {"day": "day", "week": "week", "month": "month", "year": "year",
                "semi_month": "half-month"}[self.period.value]
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
