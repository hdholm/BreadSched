"""How charts look and where their marks go, shared by every renderer.

**Colours.** One categorical palette, stepped for light and dark surfaces. A series
names its slot (1 to 8) by what it is, so its colour never depends on how many
other series are shown. The eight hues are used in this fixed order, which keeps
neighbouring series apart for colour-blind readers in both themes; the dark column
is the same hues stepped for a dark surface, not a separate palette. Slots 3 to 5
fall below 3:1 contrast on the light surface, so every chart is shown with a table
of its exact values beside it.

**Geometry.** GTK (Cairo on screen), printing (Cairo on paper), and the HTML
export lay a ``ChartModel`` out with the same scale, ticks, and bar rectangles, so
the three cannot disagree. Values become floats only here, for placing marks; the
amounts shown in labels, tooltips, and tables stay exact. Columns follow the mark
spec: at most 24 units thick, a 2-unit gap between the columns of one group,
growing from a zero baseline (negative values grow down). A stacked or share chart
draws one column per category with its series stacked in order, positive values up
from zero and negative values down, the outermost segment carrying the rounded end
and a 1-unit gap between segments; a share chart's scale is percent.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..gen.engine.chart_model import BARS, SHARE, ChartModel

__all__ = [
    "CHART_CHROME_DARK",
    "CHART_CHROME_LIGHT",
    "CHART_SERIES_DARK",
    "CHART_SERIES_LIGHT",
    "ChartBar",
    "ChartBarLayout",
    "ChartLineLayout",
    "chart_bar_layout",
    "chart_label_indices",
    "chart_line_layout",
    "chart_chrome",
    "chart_nice_ticks",
    "chart_series_colour",
    "chart_tick_label",
]

CHART_SERIES_LIGHT = (
    "#2a78d6",  # 1 blue
    "#eb6834",  # 2 orange
    "#1baf7a",  # 3 aqua
    "#eda100",  # 4 yellow
    "#e87ba4",  # 5 magenta
    "#008300",  # 6 green
    "#4a3aa7",  # 7 violet
    "#e34948",  # 8 red
)
CHART_SERIES_DARK = (
    "#3987e5",
    "#d95926",
    "#199e70",
    "#c98500",
    "#d55181",
    "#008300",
    "#9085e9",
    "#e66767",
)

#: Surface, ink, and rule colours; gridlines are hairlines one step off the surface.
CHART_CHROME_LIGHT = {
    "surface": "#fcfcfb",
    "ink": "#0b0b0b",
    "secondary": "#52514e",
    "muted": "#898781",
    "grid": "#e1e0d9",
    "axis": "#c3c2b7",
}
CHART_CHROME_DARK = {
    "surface": "#1a1a19",
    "ink": "#ffffff",
    "secondary": "#c3c2b7",
    "muted": "#898781",
    "grid": "#2c2c2a",
    "axis": "#383835",
}


def chart_series_colour(slot: int, *, dark: bool = False) -> str:
    """The colour of categorical slot ``slot`` (1 to 8)."""
    palette = CHART_SERIES_DARK if dark else CHART_SERIES_LIGHT
    return palette[(max(1, min(slot, len(palette))) - 1)]


def chart_chrome(role: str, *, dark: bool = False) -> str:
    return (CHART_CHROME_DARK if dark else CHART_CHROME_LIGHT)[role]


_MAX_BAR = 24.0
_GAP = 2.0


@dataclass(frozen=True, slots=True)
class ChartBar:
    series: int
    category: int
    x: float
    y: float
    width: float
    height: float
    #: True when the value is below zero (its rounded end is at the bottom).
    negative: bool
    #: False for a stacked segment that another segment continues beyond.
    rounded: bool = True
    #: In a share chart, the value's percentage of its category's total.
    share: float | None = None


@dataclass(frozen=True, slots=True)
class ChartBarLayout:
    bars: tuple[ChartBar, ...]
    #: (value, y) for each gridline, lowest first.
    ticks: tuple[tuple[float, float], ...]
    baseline: float
    #: The centre x of each category, for its label.
    centres: tuple[float, ...]
    left: float
    top: float
    width: float
    height: float
    #: The scale is percent (a share chart), not money.
    percent: bool = False
    #: Each category's band, (start x, width), for hit testing and selection.
    bands: tuple[tuple[float, float], ...] = ()
    #: The x where partial values begin (the start of that category's band), if any.
    partial_x: float | None = None


def chart_tick_label(value: float, *, percent: bool = False) -> str:
    """A gridline's label: whole currency units, or a whole percentage."""
    return f"{value:,.0f}%" if percent else f"{value:,.0f}"


def chart_nice_ticks(low: float, high: float, count: int = 4) -> list[float]:
    """Round gridline values covering ``low`` to ``high`` (always including zero)."""
    low, high = min(low, 0.0), max(high, 0.0)
    if math.isclose(low, high):
        high = low + 1.0
    raw = (high - low) / count
    magnitude = 10 ** math.floor(math.log10(raw))
    step = next(m * magnitude for m in (1, 2, 2.5, 5, 10) if m * magnitude >= raw)
    first = math.floor(low / step) * step
    ticks = []
    value = first
    while value < high + step * 0.999:
        ticks.append(round(value, 10))
        value += step
    return ticks


def _stack_values(model: ChartModel) -> list[list[float | None]]:
    """Per category, each series' plotted value: money, or percent in a share chart."""
    plotted: list[list[float | None]] = []
    for category in range(len(model.categories)):
        row: list[float | None] = []
        for index, series in enumerate(model.series):
            value = series.values[category]
            if model.kind == SHARE:
                row.append(model.share(index, category))
            else:
                row.append(float(value.to_decimal()) if value else None)
        plotted.append(row)
    return plotted


def chart_bar_layout(
    model: ChartModel, left: float, top: float, width: float, height: float
) -> ChartBarLayout:
    """Place grouped (or stacked) columns for ``model`` inside the plot rectangle."""
    stacked = model.kind != BARS
    if stacked:
        plotted = _stack_values(model)
        ends = [
            total
            for row in plotted
            for total in (
                sum(value for value in row if value and value > 0),
                sum(value for value in row if value and value < 0),
            )
        ]
    else:
        ends = [
            float(value.to_decimal()) for series in model.series for value in series.values if value
        ]
    ticks = chart_nice_ticks(min(ends, default=0.0), max(ends, default=1.0))
    low, high = ticks[0], ticks[-1]

    def y_for(value: float) -> float:
        return top + height * (high - value) / (high - low)

    baseline = y_for(0.0)
    count = max(1, len(model.categories))
    band = width / count
    series_count = 1 if stacked else max(1, len(model.series))
    bar = min(_MAX_BAR, max(1.0, (band * 0.7 - _GAP * (series_count - 1)) / series_count))
    group = bar * series_count + _GAP * (series_count - 1)
    bars: list[ChartBar] = []
    centres: list[float] = []
    for category in range(len(model.categories)):
        centre = left + band * (category + 0.5)
        centres.append(centre)
        start = centre - group / 2
        if stacked:
            bars.extend(_stack(model, plotted[category], category, start, bar, y_for))
            continue
        for index, series in enumerate(model.series):
            value = series.values[category]
            if value is None or not value:
                continue
            number = float(value.to_decimal())
            top_y, bottom_y = sorted((y_for(number), baseline))
            bars.append(
                ChartBar(
                    index,
                    category,
                    start + index * (bar + _GAP),
                    top_y,
                    bar,
                    max(bottom_y - top_y, 1.0),
                    number < 0,
                )
            )
    return ChartBarLayout(
        tuple(bars),
        tuple((value, y_for(value)) for value in ticks),
        baseline,
        tuple(centres),
        left,
        top,
        width,
        height,
        percent=model.kind == SHARE,
        bands=tuple((left + band * index, band) for index in range(len(model.categories))),
        partial_x=left + band * model.partial_from
        if model.partial_from is not None and model.partial_from < len(model.categories)
        else None,
    )


def _stack(model: ChartModel, row, category: int, x: float, bar: float, y_for) -> list[ChartBar]:
    """One category's segments, positive values up from zero and negative ones down."""
    segments: list[ChartBar] = []
    for negative in (False, True):
        chosen = [
            (index, value)
            for index, value in enumerate(row)
            if value and (value < 0 if negative else value > 0)
        ]
        level = 0.0
        for position, (index, value) in enumerate(chosen):
            outer = position == len(chosen) - 1
            top_y, bottom_y = sorted((y_for(level), y_for(level + value)))
            level += value
            extent = max(bottom_y - top_y, 1.0)
            if not outer:
                # A 1-unit gap where the next segment continues the column.
                extent = max(extent - 1.0, 0.5)
                if not negative:
                    top_y += 1.0
            segments.append(
                ChartBar(
                    index,
                    category,
                    x,
                    top_y,
                    bar,
                    extent,
                    negative,
                    rounded=outer,
                    share=value if model.kind == SHARE else None,
                )
            )
    return segments


@dataclass(frozen=True, slots=True)
class ChartLineLayout:
    #: Per series, a point per category, or None where the value is unavailable.
    points: tuple[tuple[tuple[float, float] | None, ...], ...]
    ticks: tuple[tuple[float, float], ...]
    #: The x of each category.
    xs: tuple[float, ...]
    #: (x, label) for each marker, in the model's order.
    markers: tuple[tuple[float, str], ...]
    #: The x where partial values begin, if any.
    partial_x: float | None
    left: float
    top: float
    width: float
    height: float
    #: Each category's band, (start x, width): halfway to its neighbours.
    bands: tuple[tuple[float, float], ...] = ()


def chart_label_indices(count: int, most: int = 8) -> tuple[int, ...]:
    """Which category labels to draw on a long axis: evenly spaced, the last included."""
    if count <= most:
        return tuple(range(count))
    stride = math.ceil(count / most)
    chosen = list(range(0, count, stride))
    if chosen[-1] != count - 1:
        if count - 1 - chosen[-1] < stride:
            chosen[-1] = count - 1
        else:
            chosen.append(count - 1)
    return tuple(chosen)


def chart_line_layout(
    model: ChartModel, left: float, top: float, width: float, height: float
) -> ChartLineLayout:
    """Place one line per series across the categories, first to last edge to edge."""
    values = [
        float(value.to_decimal())
        for series in model.series
        for value in series.values
        if value is not None
    ]
    ticks = chart_nice_ticks(min(values, default=0.0), max(values, default=1.0))
    low, high = ticks[0], ticks[-1]
    count = len(model.categories)

    def x_for(index: int) -> float:
        return left + (width * index / (count - 1) if count > 1 else width / 2)

    def y_for(value: float) -> float:
        return top + height * (high - value) / (high - low)

    xs = tuple(x_for(index) for index in range(count))
    points = tuple(
        tuple(
            (xs[index], y_for(float(value.to_decimal()))) if value is not None else None
            for index, value in enumerate(series.values)
        )
        for series in model.series
    )
    return ChartLineLayout(
        points,
        tuple((value, y_for(value)) for value in ticks),
        xs,
        tuple((xs[marker.index], marker.label) for marker in model.markers if marker.index < count),
        xs[model.partial_from]
        if model.partial_from is not None and model.partial_from < count
        else None,
        left,
        top,
        width,
        height,
        bands=_line_bands(xs, left, width),
    )


def _line_bands(
    xs: tuple[float, ...], left: float, width: float
) -> tuple[tuple[float, float], ...]:
    edges = [left, *((a + b) / 2 for a, b in zip(xs, xs[1:], strict=False)), left + width]
    return tuple((edges[index], edges[index + 1] - edges[index]) for index in range(len(xs)))
