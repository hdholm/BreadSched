"""Grouped columns for an engine ``ChartModel``, drawn with Cairo.

``paint_bars`` draws on any Cairo context, so the on-screen ``BarChart`` and native
printing (``report_printer``) draw the same marks from the same layout
(``presentation.chart_bar_layout``). Colours come from the shared palette, stepped
for a dark theme on screen; the legend and axis text use ink colours, never the
series colour. Hovering a column shows its exact amount.
"""

from __future__ import annotations

import math

from ...gen.engine.chart_model import ChartModel
from ...presentation import ChartBarLayout, chart_bar_layout, chart_chrome, chart_series_colour
from ..gi_setup import Gtk

__all__ = ["BarChart", "paint_bars"]

_LEFT, _TOP, _RIGHT, _BOTTOM = 78.0, 34.0, 12.0, 30.0


def _rgb(colour: str) -> tuple[float, float, float]:
    colour = colour.lstrip("#")
    return tuple(int(colour[index : index + 2], 16) / 255 for index in (0, 2, 4))  # type: ignore[return-value]


def _column(cr, x: float, y: float, width: float, height: float, negative: bool) -> None:
    """A column with a 4-unit rounded data end and a square foot on the baseline."""
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


def paint_bars(
    cr, model: ChartModel, width: float, height: float, *, dark: bool = False
) -> ChartBarLayout:
    """Draw ``model`` into a ``width`` by ``height`` area; returns where the bars went."""
    layout = chart_bar_layout(
        model, _LEFT, _TOP, max(width - _LEFT - _RIGHT, 1.0), max(height - _TOP - _BOTTOM, 1.0)
    )
    cr.select_font_face("Sans")
    cr.set_font_size(11)
    for value, y in layout.ticks:
        cr.set_source_rgb(*_rgb(chart_chrome("axis" if value == 0 else "grid", dark=dark)))
        cr.set_line_width(1)
        cr.move_to(_LEFT, round(y) + 0.5)
        cr.line_to(width - _RIGHT, round(y) + 0.5)
        cr.stroke()
        text = f"{value:,.0f}"
        extents = cr.text_extents(text)
        cr.set_source_rgb(*_rgb(chart_chrome("muted", dark=dark)))
        cr.move_to(_LEFT - 8 - extents.width, y + 4)
        cr.show_text(text)
    for bar in layout.bars:
        cr.set_source_rgb(*_rgb(chart_series_colour(model.series[bar.series].slot, dark=dark)))
        _column(cr, bar.x, bar.y, bar.width, bar.height, bar.negative)
        cr.fill()
    cr.set_source_rgb(*_rgb(chart_chrome("muted", dark=dark)))
    for centre, label in zip(layout.centres, model.categories, strict=True):
        extents = cr.text_extents(label)
        cr.move_to(centre - extents.width / 2, height - _BOTTOM + 16)
        cr.show_text(label)
    x = _LEFT
    for series in model.series:
        cr.set_source_rgb(*_rgb(chart_series_colour(series.slot, dark=dark)))
        cr.rectangle(x, 10, 12, 12)
        cr.fill()
        cr.set_source_rgb(*_rgb(chart_chrome("ink", dark=dark)))
        cr.move_to(x + 18, 20)
        cr.show_text(series.name)
        x += 18 + cr.text_extents(series.name).x_advance + 18
    return layout


class BarChart(Gtk.DrawingArea):
    """An engine chart on screen, with each column's exact amount on hover."""

    def __init__(self, model: ChartModel) -> None:
        super().__init__()
        self.model = model
        self.layout: ChartBarLayout | None = None
        self.set_content_height(240)
        self.set_hexpand(True)
        self.set_has_tooltip(True)
        self.connect("query-tooltip", self._tooltip)
        self.set_draw_func(self._draw)
        label = f"{model.title} ({model.currency})" if model.currency else model.title
        self.update_property([Gtk.AccessibleProperty.LABEL], [label])

    def _dark(self) -> bool:
        colour = self.get_color()
        return (colour.red + colour.green + colour.blue) / 3 > 0.5

    def _draw(self, _area, cr, width: int, height: int) -> None:
        self.layout = paint_bars(cr, self.model, width, height, dark=self._dark())

    def tooltip_at(self, x: float, y: float) -> str | None:
        """The exact amount of the column under ``(x, y)``, if any."""
        if self.layout is None:
            return None
        for bar in self.layout.bars:
            if bar.x - 2 <= x <= bar.x + bar.width + 2 and bar.y - 4 <= y <= bar.y + bar.height + 4:
                series = self.model.series[bar.series]
                value = series.values[bar.category]
                amount = value.format(parens_negative=True) if value is not None else "—"
                category = self.model.categories[bar.category]
                return f"{category}, {series.name}: {amount} {self.model.currency}".strip()
        return None

    def _tooltip(self, _widget, x, y, _keyboard, tooltip) -> bool:
        text = self.tooltip_at(x, y)
        if text is None:
            return False
        tooltip.set_text(text)
        return True
