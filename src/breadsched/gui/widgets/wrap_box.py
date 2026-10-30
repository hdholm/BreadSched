"""A container that lays its children out in lines, wrapping as the width allows.

``Gtk.FlowBox`` aligns its children in columns across every line, so one wide child
(the Dashboard's bills) sets the first column's width for the lines below it, and
narrower children never sit beside each other there (#397). A child it hides still
keeps its place. Here each line takes as many visible children, at their natural
widths, as fit, so narrower ones float up beside each other, and a hidden child
takes no room.
"""

from __future__ import annotations

from ..gi_setup import Gdk, Gtk

__all__ = ["WrapBox"]


class WrapBox(Gtk.Widget):
    """Children left to right, wrapping to a new line when the next does not fit."""

    __gtype_name__ = "BreadSchedWrapBox"

    def __init__(self, *, column_spacing: int = 0, row_spacing: int = 0) -> None:
        super().__init__()
        self.column_spacing = column_spacing
        self.row_spacing = row_spacing
        self._children: list[Gtk.Widget] = []

    def append(self, child: Gtk.Widget) -> None:
        self._children.append(child)
        child.set_parent(self)
        self.queue_resize()

    def remove(self, child: Gtk.Widget) -> None:
        if child in self._children:
            self._children.remove(child)
            child.unparent()
            self.queue_resize()

    def children(self) -> list[Gtk.Widget]:
        return list(self._children)

    def do_dispose(self) -> None:
        for child in self._children:
            child.unparent()
        self._children.clear()

    def _visible(self) -> list[Gtk.Widget]:
        return [child for child in self._children if child.get_visible()]

    def _lines(self, width: int) -> list[list[tuple[Gtk.Widget, int]]]:
        """The visible children by line, each with the width it is given."""
        lines: list[list[tuple[Gtk.Widget, int]]] = []
        line: list[tuple[Gtk.Widget, int]] = []
        used = 0
        for child in self._visible():
            minimum, natural, _, _ = child.measure(Gtk.Orientation.HORIZONTAL, -1)
            given = max(minimum, min(natural, width))
            needed = given + (self.column_spacing if line else 0)
            if line and used + needed > width:
                lines.append(line)
                line, used, needed = [], 0, given
            line.append((child, given))
            used += needed
        if line:
            lines.append(line)
        return lines

    def _line_height(self, line: list[tuple[Gtk.Widget, int]]) -> tuple[int, int]:
        sizes = [child.measure(Gtk.Orientation.VERTICAL, width)[:2] for child, width in line]
        return max(size[0] for size in sizes), max(size[1] for size in sizes)

    def do_get_request_mode(self) -> Gtk.SizeRequestMode:
        return Gtk.SizeRequestMode.HEIGHT_FOR_WIDTH

    def do_measure(self, orientation: Gtk.Orientation, for_size: int):
        visible = self._visible()
        if not visible:
            return 0, 0, -1, -1
        if orientation == Gtk.Orientation.HORIZONTAL:
            sizes = [child.measure(orientation, -1)[:2] for child in visible]
            minimum = max(size[0] for size in sizes)
            natural = sum(size[1] for size in sizes) + self.column_spacing * (len(sizes) - 1)
            return minimum, natural, -1, -1
        width = for_size if for_size >= 0 else self.measure(Gtk.Orientation.HORIZONTAL, -1)[1]
        lines = self._lines(width)
        heights = [self._line_height(line) for line in lines]
        spacing = self.row_spacing * (len(lines) - 1)
        return (
            sum(height[0] for height in heights) + spacing,
            sum(height[1] for height in heights) + spacing,
            -1,
            -1,
        )

    def do_size_allocate(self, width: int, height: int, baseline: int) -> None:
        y = 0
        for line in self._lines(width):
            line_height = self._line_height(line)[1]
            x = 0
            for child, given in line:
                child_height = child.measure(Gtk.Orientation.VERTICAL, given)[1]
                allocation = Gdk.Rectangle()
                allocation.x, allocation.y = x, y
                allocation.width, allocation.height = given, min(child_height, line_height)
                child.size_allocate(allocation, -1)
                x += given + self.column_spacing
            y += line_height + self.row_spacing
