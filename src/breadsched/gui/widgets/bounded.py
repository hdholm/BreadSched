"""Keep dialogs within the screen however much they hold.

A tall form in a plain box sets the window's minimum height, and GTK will not make
a window smaller than that: on a laptop screen the Save button ends up below the
bottom edge. :func:`scroll_body` moves everything but the dialog's last row (its
buttons) into a vertical scroller, so the form scrolls and the buttons stay put.

A window also opens at its natural size, and a wrapping label's natural width is
its whole text on one line, so a dialog with a long note opened wider than the
screen. :class:`BoundedWindow` caps the size a dialog opens at to the monitor it
opens on (:func:`fit_to_screen`); the content then wraps or scrolls inside.
"""

from __future__ import annotations

from ..gi_setup import Gtk

__all__ = ["BoundedWindow", "fit_to_screen", "scroll_body"]

#: Share of the monitor a dialog may open at, leaving room for panels and docks.
SCREEN_SHARE = 0.9

_SIDES = ("top", "bottom", "start", "end")


def scroll_body(window: Gtk.Window) -> Gtk.ScrolledWindow:
    """Wrap ``window``'s content box, less its last child, in a vertical scroller.

    The content box's margins move to the new outer box, so the dialog looks the
    same; only its minimum height changes. The scroller asks for the form's natural
    height, so a dialog with room to spare opens without a scrollbar.
    """
    body = window.get_child()
    assert isinstance(body, Gtk.Box), "scroll_body expects a vertical content box"
    footer = body.get_last_child()
    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=body.get_spacing())
    for side in _SIDES:
        getattr(outer, f"set_margin_{side}")(getattr(body, f"get_margin_{side}")())
        getattr(body, f"set_margin_{side}")(0)
    window.set_child(None)
    if footer is not None:
        body.remove(footer)
    scroller = Gtk.ScrolledWindow(child=body)
    scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    scroller.set_propagate_natural_height(True)
    scroller.set_vexpand(True)
    outer.append(scroller)
    if footer is not None:
        outer.append(footer)
    window.set_child(outer)
    return scroller


def _screen_size(window: Gtk.Window) -> tuple[int, int] | None:
    """The size of the monitor ``window`` (or its parent) is on, if known."""
    display = window.get_display()
    if display is None:
        return None
    monitor = None
    for candidate in (window, window.get_transient_for()):
        surface = candidate.get_surface() if candidate is not None else None
        if surface is not None:
            monitor = display.get_monitor_at_surface(surface)
            if monitor is not None:
                break
    if monitor is None:
        monitors = display.get_monitors()
        monitor = monitors.get_item(0) if monitors.get_n_items() else None
    if monitor is None:
        return None
    geometry = monitor.get_geometry()
    return geometry.width, geometry.height


def fit_to_screen(window: Gtk.Window, bounds: tuple[int, int] | None = None) -> tuple[int, int]:
    """Open ``window`` no larger than ``bounds`` (by default most of its monitor).

    The window opens at its default size, or its natural size when it has none,
    capped to the bounds but never below its minimum size. Returns the size set.
    """
    if bounds is None:
        screen = _screen_size(window)
        if screen is None:
            return window.get_default_size()
        bounds = (int(screen[0] * SCREEN_SHARE), int(screen[1] * SCREEN_SHARE))
    most_wide, most_tall = bounds
    width, height = window.get_default_size()
    minimum_width, natural_width = Gtk.Widget.measure(window, Gtk.Orientation.HORIZONTAL, -1)[:2]
    if width <= 0:
        width = natural_width
    width = max(minimum_width, min(width, most_wide))
    minimum_height, natural_height = Gtk.Widget.measure(window, Gtk.Orientation.VERTICAL, width)[:2]
    if height <= 0:
        height = natural_height
    height = max(minimum_height, min(height, most_tall))
    window.set_default_size(width, height)
    return width, height


class BoundedWindow(Gtk.Window):
    """A dialog window that opens within the screen (see :func:`fit_to_screen`)."""

    def present(self) -> None:
        if not self.get_mapped():
            fit_to_screen(self)
        super().present()
