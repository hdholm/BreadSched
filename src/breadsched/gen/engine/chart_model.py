"""Charts as data: categories, series of exact values, and labels.

Engines describe a chart here and every interface draws it: GTK with Cairo, the
browser as inline SVG, and printing through the shared report layout. A chart never
computes its own totals: each value is one the engine already reports, so the table
printed beside a chart and the chart itself always agree. A share chart divides each
value by the category total the engine reported with it (``totals``), never by a sum
of its own.

A series names its categorical colour **slot** (1 to 8) by what it is, not by its
position, so a series keeps its colour when others are hidden or added; the colours
themselves belong to ``presentation.charts``.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..lib.money import Money

__all__ = [
    "BARS",
    "COLUMN_KINDS",
    "LINE",
    "SHARE",
    "STACKED",
    "ChartMarker",
    "ChartModel",
    "ChartSeries",
]

#: Grouped columns, one group per category, one column per series.
BARS = "bars"
#: One line per series across the categories.
LINE = "line"
#: One column per category, its series stacked (positive up, negative down from zero).
STACKED = "stacked"
#: STACKED, each value drawn as its share of the category's reported total.
SHARE = "share"
#: The forms drawn as columns rather than lines.
COLUMN_KINDS = frozenset({BARS, STACKED, SHARE})


@dataclass(frozen=True, slots=True)
class ChartSeries:
    key: str
    name: str
    #: One value per category; None where the value is unavailable.
    values: tuple[Money | None, ...]
    #: The categorical colour slot, 1 to 8, chosen by what the series is.
    slot: int


@dataclass(frozen=True, slots=True)
class ChartMarker:
    """A labelled vertical rule at one category, such as the first cash shortfall."""

    index: int
    label: str


@dataclass(frozen=True, slots=True)
class ChartModel:
    key: str
    title: str
    kind: str
    categories: tuple[str, ...]
    series: tuple[ChartSeries, ...]
    #: The currency every value is in ("" when the book's commodity has no code).
    currency: str = ""
    markers: tuple[ChartMarker, ...] = ()
    #: From this category on, values leave something out (shaded, with ``partial_note``).
    partial_from: int | None = None
    partial_note: str = ""
    #: Each category's total as the engine reports it; a SHARE chart divides by it.
    #: The series of a share chart sum to these exactly.
    totals: tuple[Money | None, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.categories or all(
            value is None or not value for series in self.series for value in series.values
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "title": self.title,
            "kind": self.kind,
            "categories": list(self.categories),
            "currency": self.currency,
            "series": [
                {"key": item.key, "name": item.name, "slot": item.slot, "values": list(item.values)}
                for item in self.series
            ],
            "markers": [{"index": item.index, "label": item.label} for item in self.markers],
            "partial_from": self.partial_from,
            "partial_note": self.partial_note,
            "totals": list(self.totals),
        }

    def rows(self) -> tuple[tuple[str, tuple[Money | None, ...]], ...]:
        """The chart's exact values by category, for the table beside it.

        A share chart's table also needs its totals; see ``share``.
        """
        return tuple(
            (category, tuple(series.values[index] for series in self.series))
            for index, category in enumerate(self.categories)
        )

    def share(self, series: int, category: int) -> float | None:
        """A value's percentage of its category's total, or None without a positive total."""
        value = self.series[series].values[category]
        total = self.totals[category] if category < len(self.totals) else None
        if value is None or total is None or not total or total < Money(0):
            return None
        return float(value / total) * 100
