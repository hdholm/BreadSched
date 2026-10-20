"""Delete buttons that look alike and always ask first (#335).

Every Delete in the desktop window is styled as destructive and asks "Delete …?"
before it removes anything, even where Ctrl+Z could restore it, so a slip of the
mouse never costs a record. The confirmation is one ``Gtk.AlertDialog`` with
**Cancel** (the default, also Escape) and **Delete**.
"""

from __future__ import annotations

from collections.abc import Callable

from ..gi_setup import GLib, Gtk

__all__ = ["CANCEL", "DELETE", "confirm_delete", "delete_button"]

#: Button positions in the confirmation.
CANCEL, DELETE = 0, 1


def confirm_delete(
    widget: Gtk.Widget,
    what: str,
    on_confirm: Callable[[], object],
    *,
    detail: str = "",
) -> Gtk.AlertDialog:
    """Ask whether to delete ``what``; ``on_confirm`` runs only after **Delete**."""
    alert = Gtk.AlertDialog(
        message=f"Delete {what}?",
        detail=detail,
        modal=True,
        buttons=["Cancel", "Delete"],
        cancel_button=CANCEL,
        default_button=CANCEL,
    )

    def answered(dialog, result) -> None:
        try:
            choice = dialog.choose_finish(result)
        except GLib.Error:
            return  # dismissed: nothing is deleted
        if choice == DELETE:
            on_confirm()

    root = widget.get_root()
    alert.choose(root if isinstance(root, Gtk.Window) else None, None, answered)
    return alert


def delete_button(
    what: str | Callable[[], str],
    on_confirm: Callable[[], object],
    *,
    label: str = "Delete",
    detail: str = "",
) -> Gtk.Button:
    """A destructive button that confirms deleting ``what`` before ``on_confirm``.

    ``what`` names the thing ("the rule for “Rent”"), or is a callable that names
    it when clicked, for a dialog whose loaded record changes.
    """
    button = Gtk.Button(label=label)
    button.add_css_class("destructive-action")

    def clicked(widget: Gtk.Button) -> None:
        name = what() if callable(what) else what
        confirm_delete(widget, name, on_confirm, detail=detail)

    button.connect("clicked", clicked)
    return button
