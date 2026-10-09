"""Charts as data: categories, series of exact values, and labels.

Engines describe a chart here and every interface draws it: GTK with Cairo, the
browser as inline SVG, and printing through the shared report layout. A chart never
computes its own totals: each value is one the engine already reports, so the table
printed beside a chart and the chart itself always agree.

A series names its categorical colour **slot** (1 to 8) by what it is, not by its
position, so a series keeps its colour when others are hidden or added; the colours
themselves belong to ``presentation.chart_palette``.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..lib.money import Money

__all__ = ["BARS", "LINE", "ChartModel", "ChartSeries"]

#: Grouped columns, one group per category, one column per series.
BARS = "bars"
#: One line per series across the categories.
LINE = "line"


@dataclass(frozen=True, slots=True)
class ChartSeries:
    key: str
    name: str
    #: One value per category; None where the value is unavailable.
    values: tuple[Money | None, ...]
    #: The categorical colour slot, 1 to 8, chosen by what the series is.
    slot: int


@dataclass(frozen=True, slots=True)
class ChartModel:
    key: str
    title: str
    kind: str
    categories: tuple[str, ...]
    series: tuple[ChartSeries, ...]
    #: The currency every value is in ("" when the book's commodity has no code).
    currency: str = ""

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
        }

    def rows(self) -> tuple[tuple[str, tuple[Money | None, ...]], ...]:
        """The chart's exact values by category, for the table beside it."""
        return tuple(
            (category, tuple(series.values[index] for series in self.series))
            for index, category in enumerate(self.categories)
        )
