"""Keep dialogs within the screen however much they hold.

A tall form in a plain box sets the window's minimum height, and GTK will not make
a window smaller than that: on a laptop screen the Save button ends up below the
bottom edge. :func:`scroll_body` moves everything but the dialog's last row (its
buttons) into a vertical scroller, so the form scrolls and the buttons stay put.

A window also opens at its natural size, and a wrapping label's natural width is
its whole text on one line, so a dialog with a long note opened wider than the
screen. :class:`BoundedWindow` caps the size a dialog opens at to the monitor it
opens on (:func:`fit_to_screen`); the content then wraps or scrolls inside.

A dialog you have resized opens at that size next time (#349; see
``widgets.window_sizes``): each kind of dialog is remembered under its class
name, or under :attr:`BoundedWindow.size_key` when one is set.

Every dialog also answers the keyboard the same way (#335): Escape closes it, as
its Cancel or Close button does, and Enter in a one-line field activates its main
button, the ``suggested-action`` button in its last row (or one named with
:meth:`BoundedWindow.set_primary`).
"""

from __future__ import annotations

import weakref

from ..gi_setup import GLib, Gtk
from .window_sizes import remember_size, restore_size

__all__ = ["BoundedWindow", "fit_to_screen", "keeps_enter", "scroll_body"]

#: One-line fields whose Enter does their own job (fetch, propose), not the dialog's.
_OWN_ENTER: weakref.WeakSet = weakref.WeakSet()


def keeps_enter(entry: Gtk.Entry) -> Gtk.Entry:
    """Leave Enter in ``entry`` to its own ``activate`` handler."""
    _OWN_ENTER.add(entry)
    return entry


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


def _descendants(widget: Gtk.Widget):
    child = widget.get_first_child()
    while child is not None:
        yield child
        yield from _descendants(child)
        child = child.get_next_sibling()


class BoundedWindow(Gtk.Window):
    """A dialog window that opens within the screen and answers Escape and Enter."""

    #: The key a resized size is remembered under; None for a plain BoundedWindow
    #: unless its creator names one (a subclass defaults to its class name).
    size_key: str | None = None

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._primary: Gtk.Widget | None = None
        #: The size the window opened at, to tell whether it was resized (#349).
        self._opened_size: tuple[int, int] | None = None
        #: Remember the size on every close, not only after a resize (a register
        #: window, whose size is not set by what it shows).
        self.always_remember_size = False
        self.connect("close-request", self._on_close_remember)
        # The size GTK settles on once the window is shown is the baseline, so
        # only a resize the user makes afterwards counts.
        self.connect("map", lambda *_: GLib.idle_add(self._note_opened_size))
        keys = Gtk.ShortcutController()
        keys.set_scope(Gtk.ShortcutScope.LOCAL)
        keys.add_shortcut(
            Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string("Escape"),
                Gtk.CallbackAction.new(lambda *_: self._on_escape()),
            )
        )
        self.add_controller(keys)

    def _note_opened_size(self) -> bool:
        if self.get_mapped():
            self._opened_size = self.get_default_size()
        return False

    def _size_key(self) -> str | None:
        if self.size_key is not None:
            return self.size_key
        return None if type(self) is BoundedWindow else type(self).__name__

    def _on_close_remember(self, *_args) -> bool:
        key = self._size_key()
        if key is not None and self._opened_size is not None:
            resized = self.get_default_size() != self._opened_size or self.is_maximized()
            if resized or self.always_remember_size:
                remember_size(self, key)
        return False

    def _on_escape(self) -> bool:
        self.close()
        return True

    def set_primary(self, button: Gtk.Widget) -> None:
        """Make ``button`` the one Enter activates."""
        self._primary = button

    def primary(self) -> Gtk.Widget | None:
        """The button Enter activates: the one named, else the last row's suggested one."""
        if self._primary is not None:
            return self._primary
        child = self.get_child()
        footer = child.get_last_child() if child is not None else None
        if footer is None:
            return None
        candidates = [footer, *_descendants(footer)]
        return next(
            (
                widget
                for widget in candidates
                if isinstance(widget, Gtk.Button) and widget.has_css_class("suggested-action")
            ),
            None,
        )

    def present(self) -> None:
        if not self.get_mapped():
            key = self._size_key()
            if key is not None:
                restore_size(self, key)
            self._opened_size = fit_to_screen(self)
            primary = self.primary()
            if primary is not None:
                self.set_default_widget(primary)
                for widget in _descendants(self):
                    if (
                        isinstance(widget, Gtk.Entry)
                        and not isinstance(widget, Gtk.SearchEntry)
                        and widget not in _OWN_ENTER
                    ):
                        widget.set_activates_default(True)
        super().present()
