"""A small line chart, drawn with Cairo.

Bringing in a plotting library for four series would add a heavy dependency for
something a ``Gtk.DrawingArea`` does in a couple of hundred lines, and would not
respect the GTK theme.  Drawing directly also means the zero line can be treated as
the significant feature it is in a cash forecast: it is drawn darker than the other
gridlines, and the area below it is tinted, so a projection that dips negative
reads as a problem at a glance rather than as a line that happens to be low.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import cairo

from ..gi_setup import Gtk

__all__ = ["Series", "LineChart"]

_PALETTE = [
    (0.20, 0.51, 0.89),   # blue
    (0.18, 0.65, 0.42),   # green
    (0.85, 0.45, 0.13),   # orange
    (0.60, 0.35, 0.71),   # purple
]


@dataclass
class Series:
    """One line: a label, values, and an optional explicit colour."""

    label: str
    values: list[float] = field(default_factory=list)
    colour: tuple[float, float, float] | None = None
    fill: bool = False


class LineChart(Gtk.DrawingArea):
    """Multi-series line chart with a legend and a highlighted zero line."""

    def __init__(self) -> None:
        super().__init__()
        self.series: list[Series] = []
        self.labels: list[str] = []
        #: Shown when there is nothing to draw. Callers should replace it with
        #: something that names the reason, since "no data" is rarely the reason.
        self.empty_message = "Nothing to plot yet"
        self.value_format = "{:,.0f}"
        self.set_draw_func(self._draw)
        self.set_content_height(300)
        self.set_hexpand(True)
        self.set_vexpand(True)

    def set_data(self, series: list[Series], labels: list[str] | None = None) -> None:
        self.series = series
        self.labels = labels or []
        self.queue_draw()

    # ------------------------------------------------------------------ layout

    def _bounds(self) -> tuple[float, float]:
        values = [v for s in self.series for v in s.values]
        if not values:
            return 0.0, 1.0
        low, high = min(values), max(values)
        low = min(low, 0.0)  # the zero line is always on the chart
        if math.isclose(low, high):
            high = low + 1.0
        padding = (high - low) * 0.08
        return low - padding, high + padding

    def _draw(self, _area, cr, width: int, height: int) -> None:
        margin_left, margin_right = 78.0, 16.0
        margin_top, margin_bottom = 16.0, 46.0
        plot_width = max(width - margin_left - margin_right, 1.0)
        plot_height = max(height - margin_top - margin_bottom, 1.0)

        low, high = self._bounds()
        span = high - low
        count = max((len(s.values) for s in self.series), default=0)
        if count < 2:
            self._draw_empty(cr, width, height)
            return

        def x_for(index: int) -> float:
            return margin_left + plot_width * index / (count - 1)

        def y_for(value: float) -> float:
            return margin_top + plot_height * (1 - (value - low) / span)

        cr.select_font_face("Sans")
        cr.set_font_size(11)

        # Gridlines and value axis.
        for step in range(5):
            value = low + span * step / 4
            y = y_for(value)
            cr.set_source_rgba(0.5, 0.5, 0.5, 0.18)
            cr.set_line_width(1)
            cr.move_to(margin_left, y)
            cr.line_to(margin_left + plot_width, y)
            cr.stroke()
            cr.set_source_rgba(0.5, 0.5, 0.5, 0.9)
            text = self.value_format.format(value)
            extents = cr.text_extents(text)
            cr.move_to(margin_left - 8 - extents.width, y + 4)
            cr.show_text(text)

        # The zero line, drawn heavier than the rest.
        if low < 0 < high:
            zero = y_for(0.0)
            cr.set_source_rgba(0.8, 0.2, 0.2, 0.55)
            cr.set_line_width(1.4)
            cr.move_to(margin_left, zero)
            cr.line_to(margin_left + plot_width, zero)
            cr.stroke()
            cr.set_source_rgba(0.8, 0.2, 0.2, 0.06)
            cr.rectangle(margin_left, zero, plot_width, margin_top + plot_height - zero)
            cr.fill()

        # Period labels, thinned so they never collide.
        if self.labels:
            stride = max(1, count // 8)
            cr.set_source_rgba(0.5, 0.5, 0.5, 0.9)
            for index in range(0, count, stride):
                if index >= len(self.labels):
                    break
                text = self.labels[index]
                extents = cr.text_extents(text)
                cr.move_to(x_for(index) - extents.width / 2, height - margin_bottom + 18)
                cr.show_text(text)

        # Series.
        for position, series in enumerate(self.series):
            colour = series.colour or _PALETTE[position % len(_PALETTE)]
            if series.fill:
                cr.set_source_rgba(*colour, 0.12)
                cr.move_to(x_for(0), y_for(series.values[0]))
                for index, value in enumerate(series.values):
                    cr.line_to(x_for(index), y_for(value))
                cr.line_to(x_for(len(series.values) - 1), y_for(max(low, 0.0)))
                cr.line_to(x_for(0), y_for(max(low, 0.0)))
                cr.close_path()
                cr.fill()

            cr.set_source_rgb(*colour)
            cr.set_line_width(2.0)
            cr.set_line_join(cairo.LINE_JOIN_ROUND)
            for index, value in enumerate(series.values):
                point = (x_for(index), y_for(value))
                cr.line_to(*point) if index else cr.move_to(*point)
            cr.stroke()

        self._draw_legend(cr, margin_left, height - 12)

    def _draw_legend(self, cr, x: float, y: float) -> None:
        cr.set_font_size(11)
        cursor = x
        for position, series in enumerate(self.series):
            colour = series.colour or _PALETTE[position % len(_PALETTE)]
            cr.set_source_rgb(*colour)
            cr.rectangle(cursor, y - 8, 10, 3)
            cr.fill()
            cr.set_source_rgba(0.5, 0.5, 0.5, 0.95)
            cr.move_to(cursor + 16, y - 3)
            cr.show_text(series.label)
            cursor += 26 + cr.text_extents(series.label).width

    def _draw_empty(self, cr, width: int, height: int) -> None:
        cr.set_source_rgba(0.5, 0.5, 0.5, 0.7)
        cr.select_font_face("Sans")
        cr.set_font_size(13)
        text = self.empty_message
        extents = cr.text_extents(text)
        cr.move_to((width - extents.width) / 2, height / 2)
        cr.show_text(text)
