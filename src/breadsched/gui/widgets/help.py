"""Contextual help: a **Help** button that opens the guide at a window's section.

Each topic comes from ``user_guide.HELP_TOPICS``, the table the browser's Help
buttons also use; the desktop opens the topic's heading in the desktop part of the
packaged guide.
"""

from __future__ import annotations

from ..gi_setup import Gio, Gtk

__all__ = ["help_button", "help_row", "show_help"]


def _application(widget: Gtk.Widget) -> Gio.Application | None:
    window = widget.get_root()
    while isinstance(window, Gtk.Window):
        application = window.get_application()
        if application is not None:
            return application
        window = window.get_transient_for()
    return Gio.Application.get_default()


def show_help(widget: Gtk.Widget, topic: str) -> bool:
    """Open the guide at ``topic`` from ``widget``'s application; False without one."""
    application = _application(widget)
    show_guide = getattr(application, "show_guide", None)
    if show_guide is None:
        return False
    root = widget.get_root()
    show_guide(topic, root if isinstance(root, Gtk.Window) else None)
    return True


def help_button(topic: str) -> Gtk.Button:
    button = Gtk.Button(label="Help")
    button.set_tooltip_text("Open the user guide at the section for this window")
    button.connect("clicked", lambda widget: show_help(widget, topic))
    return button


def help_row(topic: str) -> Gtk.Box:
    """A right-aligned row holding the Help button, for the top of a dialog."""
    row = Gtk.Box(halign=Gtk.Align.END)
    row.append(help_button(topic))
    return row
