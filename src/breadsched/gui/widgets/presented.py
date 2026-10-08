"""Run work once a window is really on screen.

A modal dialog presented while its parent is still unmapped can end up behind the
parent when the parent appears a moment later, and the parent then refuses input
because it is waiting for the hidden dialog (#295). Anything that may run before
the main window is presented -- start-up notices, the due review, the upgrade
of an old book -- goes through :func:`when_presented` instead.
"""

from __future__ import annotations

from collections.abc import Callable

from ..gi_setup import GLib, Gtk

__all__ = ["when_presented"]


def when_presented(window: Gtk.Window, callback: Callable[[], None]) -> Callable[[], None]:
    """Call ``callback`` once ``window`` is mapped and has had a chance to draw.

    The call is always deferred to an idle callback: GTK redraws at a higher
    priority than default idle work, so the window's current contents (for example
    an "Upgrading..." page) reach the screen before ``callback`` can block the main
    loop. Returns a function that cancels the call if it has not happened yet.
    """
    pending: dict[str, int] = {}

    def run() -> bool:
        pending.pop("idle", None)
        callback()
        return GLib.SOURCE_REMOVE

    def on_map(widget: Gtk.Widget) -> None:
        widget.disconnect(pending.pop("map"))
        pending["idle"] = GLib.idle_add(run)

    def cancel() -> None:
        if "map" in pending:
            window.disconnect(pending.pop("map"))
        if "idle" in pending:
            GLib.source_remove(pending.pop("idle"))

    if window.get_mapped():
        pending["idle"] = GLib.idle_add(run)
    else:
        pending["map"] = window.connect("map", on_map)
    return cancel
