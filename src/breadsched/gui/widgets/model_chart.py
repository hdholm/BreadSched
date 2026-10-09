"""Engine charts (``ChartModel``) drawn with Cairo: grouped or stacked columns, or lines.

``paint_chart`` draws on any Cairo context, so the on-screen ``ModelChartView`` and
native printing (``report_printer``) draw the same marks from the same layout
(``presentation.chart_bar_layout`` and ``chart_line_layout``). Colours come from the
shared palette, stepped for a dark theme on screen; the legend and axis text use
ink colours, never a series colour. Hovering shows exact amounts: a column's own
(with its share in a share chart), or every line's value at the nearest category.
A view made ``selectable`` shades its selected category and reports a click on
another one, so a chart can pick the period the rest of a screen shows.
"""

from __future__ import annotations

import math
from collections.abc import Callable

import cairo

from ...gen.engine.chart_model import COLUMN_KINDS, LINE, ChartModel
from ...presentation import (
    ChartBarLayout,
    ChartLineLayout,
    chart_bar_layout,
    chart_chrome,
    chart_label_indices,
    chart_line_layout,
    chart_series_colour,
    chart_tick_label,
)
from ..gi_setup import Gtk

__all__ = ["ModelChartView", "paint_chart"]

_LEFT, _TOP, _RIGHT, _BOTTOM = 78.0, 34.0, 12.0, 30.0


def _rgb(colour: str) -> tuple[float, float, float]:
    colour = colour.lstrip("#")
    return tuple(int(colour[index : index + 2], 16) / 255 for index in (0, 2, 4))  # type: ignore[return-value]


def _column(
    cr, x: float, y: float, width: float, height: float, negative: bool, rounded: bool = True
) -> None:
    """A column with a 4-unit rounded data end and a square foot on the baseline."""
    if not rounded:
        cr.rectangle(x, y, width, height)
        return
    r = min(4.0, width / 2, height)
    if negative:
        cr.move_to(x, y)
        cr.line_to(x + width, y)
        cr.line_to(x + width, y + height - r)
        cr.arc(x + width - r, y + height - r, r, 0, math.pi / 2)
        cr.line_to(x + r, y + height)
        cr.arc(x + r, y + height - r, r, math.pi / 2, math.pi)
    else:
        cr.move_to(x, y + height)
        cr.line_to(x, y + r)
        cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
        cr.line_to(x + width - r, y)
        cr.arc(x + width - r, y + r, r, 3 * math.pi / 2, 2 * math.pi)
        cr.line_to(x + width, y + height)
    cr.close_path()


def _shade(cr, x: float, extent: float, plot_height: float, colour: str, alpha: float) -> None:
    cr.set_source_rgba(*_rgb(colour), alpha)
    cr.rectangle(x, _TOP, extent, plot_height)
    cr.fill()


def _selection(cr, layout, selected: int | None, dark: bool) -> None:
    if selected is not None and 0 <= selected < len(layout.bands):
        x, extent = layout.bands[selected]
        _shade(cr, x, extent, layout.height, chart_series_colour(1, dark=dark), 0.12)


def paint_bars(
    cr,
    model: ChartModel,
    width: float,
    height: float,
    *,
    dark: bool = False,
    selected: int | None = None,
) -> ChartBarLayout:
    """Draw ``model`` into a ``width`` by ``height`` area; returns where the bars went."""
    layout = chart_bar_layout(
        model, _LEFT, _TOP, max(width - _LEFT - _RIGHT, 1.0), max(height - _TOP - _BOTTOM, 1.0)
    )
    cr.select_font_face("Sans")
    cr.set_font_size(11)
    if layout.partial_x is not None:
        _shade(
            cr,
            layout.partial_x,
            width - _RIGHT - layout.partial_x,
            layout.height,
            chart_chrome("muted", dark=dark),
            0.12,
        )
    _selection(cr, layout, selected, dark)
    _gridlines(cr, layout.ticks, width, dark, percent=layout.percent)
    for bar in layout.bars:
        cr.set_source_rgb(*_rgb(chart_series_colour(model.series[bar.series].slot, dark=dark)))
        _column(cr, bar.x, bar.y, bar.width, bar.height, bar.negative, bar.rounded)
        cr.fill()
    _category_labels(cr, model, layout.centres, height, dark)
    _legend(cr, model, dark)
    return layout


def _category_labels(cr, model: ChartModel, xs, height: float, dark: bool) -> None:
    cr.set_source_rgb(*_rgb(chart_chrome("muted", dark=dark)))
    right = xs[-1] if model.kind == LINE and xs else None
    for index in chart_label_indices(len(model.categories)):
        label = model.categories[index]
        extents = cr.text_extents(label)
        x = xs[index] - extents.width / 2
        if right is not None:
            # A line's end points sit on the plot's edges: keep their labels inside.
            x = min(max(x, _LEFT), right - extents.width)
        cr.move_to(x, height - _BOTTOM + 16)
        cr.show_text(label)


def _legend(cr, model: ChartModel, dark: bool) -> None:
    x = _LEFT
    for series in model.series:
        cr.set_source_rgb(*_rgb(chart_series_colour(series.slot, dark=dark)))
        if model.kind in COLUMN_KINDS:
            cr.rectangle(x, 10, 12, 12)
            cr.fill()
        else:
            cr.set_line_width(2)
            cr.move_to(x, 16)
            cr.line_to(x + 14, 16)
            cr.stroke()
        cr.set_source_rgb(*_rgb(chart_chrome("ink", dark=dark)))
        cr.move_to(x + 18, 20)
        cr.show_text(series.name)
        x += 18 + cr.text_extents(series.name).x_advance + 18


def _gridlines(cr, ticks, width: float, dark: bool, *, percent: bool = False) -> None:
    for value, y in ticks:
        cr.set_source_rgb(*_rgb(chart_chrome("axis" if value == 0 else "grid", dark=dark)))
        cr.set_line_width(1)
        cr.move_to(_LEFT, round(y) + 0.5)
        cr.line_to(width - _RIGHT, round(y) + 0.5)
        cr.stroke()
        text = chart_tick_label(value, percent=percent)
        extents = cr.text_extents(text)
        cr.set_source_rgb(*_rgb(chart_chrome("muted", dark=dark)))
        cr.move_to(_LEFT - 8 - extents.width, y + 4)
        cr.show_text(text)


def paint_lines(
    cr,
    model: ChartModel,
    width: float,
    height: float,
    *,
    dark: bool = False,
    selected: int | None = None,
) -> ChartLineLayout:
    """Draw ``model`` as lines, with its markers and partial shading."""
    layout = chart_line_layout(
        model, _LEFT, _TOP, max(width - _LEFT - _RIGHT, 1.0), max(height - _TOP - _BOTTOM, 1.0)
    )
    cr.select_font_face("Sans")
    cr.set_font_size(11)
    if layout.partial_x is not None:
        cr.set_source_rgba(*_rgb(chart_chrome("muted", dark=dark)), 0.12)
        cr.rectangle(layout.partial_x, _TOP, width - _RIGHT - layout.partial_x, layout.height)
        cr.fill()
    _selection(cr, layout, selected, dark)
    _gridlines(cr, layout.ticks, width, dark)
    for (x, label), _marker in zip(layout.markers, model.markers, strict=True):
        cr.set_source_rgb(*_rgb(chart_chrome("secondary", dark=dark)))
        cr.set_line_width(1)
        cr.move_to(round(x) + 0.5, _TOP)
        cr.line_to(round(x) + 0.5, _TOP + layout.height)
        cr.stroke()
        extents = cr.text_extents(label)
        cr.move_to(min(x + 4, width - _RIGHT - extents.width), _TOP + 12)
        cr.show_text(label)
    cr.set_line_join(cairo.LINE_JOIN_ROUND)
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    for series, points in zip(model.series, layout.points, strict=True):
        cr.set_source_rgb(*_rgb(chart_series_colour(series.slot, dark=dark)))
        cr.set_line_width(2)
        drawing = False
        for point in points:
            if point is None:
                drawing = False
                continue
            if drawing:
                cr.line_to(*point)
            else:
                cr.move_to(*point)
                drawing = True
        cr.stroke()
    _category_labels(cr, model, layout.xs, height, dark)
    _legend(cr, model, dark)
    return layout


def paint_chart(
    cr,
    model: ChartModel,
    width: float,
    height: float,
    *,
    dark: bool = False,
    selected: int | None = None,
) -> ChartBarLayout | ChartLineLayout:
    """Draw ``model`` in its own form, shading the ``selected`` category if any."""
    if model.kind in COLUMN_KINDS:
        return paint_bars(cr, model, width, height, dark=dark, selected=selected)
    return paint_lines(cr, model, width, height, dark=dark, selected=selected)


class ModelChartView(Gtk.DrawingArea):
    """An engine chart on screen, with exact amounts on hover."""

    def __init__(
        self,
        model: ChartModel | None = None,
        *,
        height: int = 240,
        on_select: Callable[[int], None] | None = None,
    ) -> None:
        super().__init__()
        self.model = model
        self.layout: ChartBarLayout | ChartLineLayout | None = None
        #: The shaded category, when the chart picks one for the rest of a screen.
        self.selected: int | None = None
        self.on_select = on_select
        if on_select is not None:
            click = Gtk.GestureClick()
            click.connect("pressed", lambda _gesture, _count, x, _y: self.choose_at(x))
            self.add_controller(click)
        self.set_content_height(height)
        self.set_hexpand(True)
        self.set_has_tooltip(True)
        self.connect("query-tooltip", self._tooltip)
        self.set_draw_func(self._draw)
        if model is not None:
            self.set_model(model)

    def set_model(self, model: ChartModel | None) -> None:
        self.model = model
        if model is not None:
            label = f"{model.title} ({model.currency})" if model.currency else model.title
            self.update_property([Gtk.AccessibleProperty.LABEL], [label])
        self.queue_draw()

    def set_selected(self, index: int | None) -> None:
        self.selected = index
        self.queue_draw()

    def category_at(self, x: float) -> int | None:
        """The category whose band contains ``x``, if any."""
        if self.layout is None:
            return None
        for index, (start, extent) in enumerate(self.layout.bands):
            if start <= x < start + extent:
                return index
        return None

    def choose_at(self, x: float) -> int | None:
        """Select the category under ``x`` and report it to ``on_select``."""
        index = self.category_at(x)
        if index is not None and self.on_select is not None:
            self.set_selected(index)
            self.on_select(index)
        return index

    @property
    def series(self):
        return self.model.series if self.model is not None else ()

    def _dark(self) -> bool:
        colour = self.get_color()
        return (colour.red + colour.green + colour.blue) / 3 > 0.5

    def _draw(self, _area, cr, width: int, height: int) -> None:
        if self.model is None or self.model.empty:
            self.layout = None
            return
        self.layout = paint_chart(
            cr, self.model, width, height, dark=self._dark(), selected=self.selected
        )

    def _amount(self, series_index: int, category: int) -> str:
        assert self.model is not None
        value = self.model.series[series_index].values[category]
        return value.format(parens_negative=True) if value is not None else "—"

    def tooltip_at(self, x: float, y: float) -> str | None:
        """Exact amounts under ``(x, y)``: one column's, or every line at that category."""
        if self.model is None or self.layout is None:
            return None
        currency = f" {self.model.currency}" if self.model.currency else ""
        if isinstance(self.layout, ChartBarLayout):
            for bar in self.layout.bars:
                if (
                    bar.x - 2 <= x <= bar.x + bar.width + 2
                    and bar.y - 4 <= y <= bar.y + bar.height + 4
                ):
                    series = self.model.series[bar.series]
                    name = self.model.categories[bar.category]
                    amount = self._amount(bar.series, bar.category)
                    if bar.share is not None:
                        return f"{name}, {series.name}: {bar.share:.0f}% ({amount}{currency})"
                    return f"{name}, {series.name}: {amount}{currency}"
            return None
        xs = self.layout.xs
        if not xs or not (self.layout.left - 6 <= x <= self.layout.left + self.layout.width + 6):
            return None
        nearest = min(range(len(xs)), key=lambda index: abs(xs[index] - x))
        lines = [self.model.categories[nearest]]
        lines.extend(
            f"{series.name}: {self._amount(index, nearest)}{currency}"
            for index, series in enumerate(self.model.series)
        )
        return "\n".join(lines)

    def _tooltip(self, _widget, x, y, _keyboard, tooltip) -> bool:
        text = self.tooltip_at(x, y)
        if text is None:
            return False
        tooltip.set_text(text)
        return True
