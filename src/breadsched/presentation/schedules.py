"""How a schedule's repetition is offered for editing: every N of a unit (#370).

Every interface offers the same choice: a number and a unit, such as every 4
weeks, every 2 months, or every 3 years, plus the shapes that take no number
(twice a month, once). The day comes from the first due date: a weekly schedule
repeats on that weekday, a monthly one on that day of the month, and the
same-weekday rule on that weekday of its week of the month. Before #370 the
editors offered a fixed list of presets, so a rule such as every 4 weeks, which
GnuCash imports, showed only as an "imported rule" and the browser refused to
edit it.
"""

from __future__ import annotations

from ..gen.lib.recurrence import PeriodType

__all__ = [
    "FIXED_INTERVAL_UNITS",
    "FREQUENCY_UNITS",
    "LEGACY_FREQUENCIES",
    "MAX_INTERVAL",
    "frequency_period",
    "frequency_unit",
]

#: (key, label, period) for each unit, in the order the editors offer them.
FREQUENCY_UNITS: tuple[tuple[str, str, PeriodType], ...] = (
    ("days", "days", PeriodType.DAY),
    ("weeks", "weeks", PeriodType.WEEK),
    ("months", "months", PeriodType.MONTH),
    ("nth_weekday", "months, on the same weekday", PeriodType.NTH_WEEKDAY),
    ("last_weekday", "months, on the last weekday", PeriodType.LAST_WEEKDAY),
    ("years", "years", PeriodType.YEAR),
    ("semimonthly", "twice a month", PeriodType.SEMI_MONTH),
    ("once", "once", PeriodType.ONCE),
)

#: Units that take no number: their interval stays what it was (normally 1).
FIXED_INTERVAL_UNITS = frozenset({"semimonthly", "once"})

#: The largest "every N" an editor accepts.
MAX_INTERVAL = 999

#: The browser's earlier preset keys, still accepted as (unit, interval).
LEGACY_FREQUENCIES: dict[str, tuple[str, int]] = {
    "weekly": ("weeks", 1),
    "biweekly": ("weeks", 2),
    "monthly": ("months", 1),
    "quarterly": ("months", 3),
    "semiannual": ("months", 6),
    "annual": ("years", 1),
}


def frequency_unit(period: PeriodType) -> str | None:
    """The editor unit for a period, or None for one no editor offers."""
    return next((key for key, _label, item in FREQUENCY_UNITS if item is period), None)


def frequency_period(unit: str) -> PeriodType:
    """The period for an editor unit key; ValueError for an unknown key."""
    for key, _label, period in FREQUENCY_UNITS:
        if key == unit:
            return period
    raise ValueError(f"unsupported schedule frequency: {unit}")
