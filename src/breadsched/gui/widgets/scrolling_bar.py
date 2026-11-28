"""A row too wide for the window, scrolled by arrows at its ends, as GnuCash's tabs are.

The toolbar and the tab row used to sit in horizontal scrolled windows, whose
scrollbar took a line of its own and was easy to miss. ``ScrollingBar`` scrolls its
row without a scrollbar and shows a left and a right arrow only while the row is
wider than the window; each arrow is greyed out at its end, and the mouse wheel
scrolls the row too. ``reveal`` brings one child into view (the current tab), and
``reveal_span`` a run of them (the toolbar's commands for the view shown).
"""

from __future__ import annotations

from ..gi_setup import GLib, Gtk

__all__ = ["ScrollingBar"]


class ScrollingBar(Gtk.Box):
    """``child`` in a scroller with no scrollbar, between two arrows that scroll it."""

    #: How far one arrow click scrolls, as a share of the width shown.
    STEP = 0.5

    def __init__(self, child: Gtk.Widget) -> None:
        super().__init__(spacing=0)
        self.child = child
        self.scroller = Gtk.ScrolledWindow(child=child, hexpand=True)
        # EXTERNAL scrolls without a scrollbar; the window may narrow past the row.
        self.scroller.set_policy(Gtk.PolicyType.EXTERNAL, Gtk.PolicyType.NEVER)
        self.scroller.set_propagate_natural_height(True)
        self.left = self._arrow("pan-start-symbolic", "Scroll left", -1)
        self.right = self._arrow("pan-end-symbolic", "Scroll right", 1)
        self.append(self.left)
        self.append(self.scroller)
        self.append(self.right)
        adjustment = self.scroller.get_hadjustment()
        # The adjustment changes while GTK lays the row out; showing or hiding an
        # arrow then would resize the bar mid-layout, so it waits for idle.
        self._update_source = 0
        #: The run of children to bring into view once the row is laid out.
        self._span: tuple[Gtk.Widget, Gtk.Widget] | None = None
        adjustment.connect("changed", lambda *_: self._queue_update())
        adjustment.connect("value-changed", lambda *_: self._queue_update())
        # A vertical wheel (most mice have no other) scrolls the row sideways.
        wheel = Gtk.EventControllerScroll(
            flags=Gtk.EventControllerScrollFlags.BOTH_AXES | Gtk.EventControllerScrollFlags.DISCRETE
        )
        wheel.connect("scroll", self._on_wheel)
        self.scroller.add_controller(wheel)
        self._update()

    def _arrow(self, icon: str, tooltip: str, direction: int) -> Gtk.Button:
        button = Gtk.Button(icon_name=icon)
        button.add_css_class("flat")
        button.add_css_class("bar-arrow")
        button.set_tooltip_text(tooltip)
        # Scrolling leaves the keyboard where it was (in a register, say).
        button.set_focus_on_click(False)
        button.set_visible(False)
        button.connect("clicked", lambda *_: self.scroll_by(direction))
        return button

    @property
    def overflowing(self) -> bool:
        adjustment = self.scroller.get_hadjustment()
        return adjustment.get_upper() - adjustment.get_page_size() > 0.5

    def _queue_update(self) -> None:
        if not self._update_source:
            self._update_source = GLib.idle_add(self._idle_update)

    def _idle_update(self) -> bool:
        self._update_source = 0
        self._update()
        return False

    def _update(self) -> None:
        adjustment = self.scroller.get_hadjustment()
        if self._span is not None and self._apply_span(*self._span):
            self._span = None
        overflowing = self.overflowing
        for arrow in (self.left, self.right):
            arrow.set_visible(overflowing)
        value = adjustment.get_value()
        end = adjustment.get_upper() - adjustment.get_page_size()
        self.left.set_sensitive(overflowing and value > 0.5)
        self.right.set_sensitive(overflowing and value < end - 0.5)

    def scroll_by(self, direction: int) -> float:
        """Scroll half the shown width left (-1) or right (1); returns the new offset."""
        adjustment = self.scroller.get_hadjustment()
        step = max(adjustment.get_page_size() * self.STEP, 40.0)
        end = max(0.0, adjustment.get_upper() - adjustment.get_page_size())
        value = min(end, max(0.0, adjustment.get_value() + direction * step))
        adjustment.set_value(value)
        return value

    def _on_wheel(self, _controller, dx: float, dy: float) -> bool:
        if not self.overflowing:
            return False
        self.scroll_by(1 if (dx or dy) > 0 else -1)
        return True

    def reveal(self, widget: Gtk.Widget) -> None:
        """Scroll so ``widget`` (a child of the row) is wholly in view."""
        found, bounds = widget.compute_bounds(self.child) if widget is not None else (False, None)
        if not found or bounds is None:
            return
        adjustment = self.scroller.get_hadjustment()
        start, width = bounds.get_x(), bounds.get_width()
        value, page = adjustment.get_value(), adjustment.get_page_size()
        if start < value:
            adjustment.set_value(start)
        elif start + width > value + page:
            adjustment.set_value(start + width - page)

    def reveal_span(self, first: Gtk.Widget, last: Gtk.Widget) -> None:
        """Scroll so the children from ``first`` to ``last`` are in view, or as much
        of them as fits, from ``first`` on. The row may not be laid out yet (its
        children just changed), so it is done again once it is."""
        self._span = (first, last)
        if self._apply_span(first, last):
            self._span = None
        self._queue_update()

    def _apply_span(self, first: Gtk.Widget, last: Gtk.Widget) -> bool:
        found_first, start = first.compute_bounds(self.child)
        found_last, end = last.compute_bounds(self.child)
        if not (found_first and found_last) or end.get_width() <= 0:
            return False
        adjustment = self.scroller.get_hadjustment()
        page = adjustment.get_page_size()
        upper = adjustment.get_upper()
        if page <= 0 or upper < end.get_x() + end.get_width() - 0.5:
            return False  # not laid out with these children yet
        left, right = start.get_x(), end.get_x() + end.get_width()
        value = adjustment.get_value()
        if right > value + page:
            value = right - page
        value = min(value, left)  # the start of the run wins when it cannot all fit
        adjustment.set_value(max(0.0, min(value, upper - page)))
        return True
