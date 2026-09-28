"""Keep a dialog's height within the screen however much its form holds.

A tall form in a plain box sets the window's minimum height, and GTK will not make
a window smaller than that: on a laptop screen the Save button ends up below the
bottom edge. :func:`scroll_body` moves everything but the dialog's last row (its
buttons) into a vertical scroller, so the form scrolls and the buttons stay put.
"""

from __future__ import annotations

from ..gi_setup import Gtk

__all__ = ["scroll_body"]

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
