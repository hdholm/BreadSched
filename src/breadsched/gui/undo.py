"""Undo, asking first when the step is large (#405).

A step such as importing a GnuCash book changes thousands of records, and undoing it
takes a while. Undo says what it will undo and how much, asks before it starts, and
then shows its progress, undoing off GTK's loop so the window stays drawn. A small
step is undone at once, as before.
"""

from __future__ import annotations

from ..api.local import book_server
from ..gen.db.base import UndoPreview
from ..gen.db.sqlite import DbSQLite
from .background import BackgroundJob
from .gi_setup import GLib, Gtk

__all__ = ["LARGE_UNDO", "describe_undo", "undo_with_care"]

#: A step with at least this many changes is confirmed first and shows progress.
LARGE_UNDO = 200

_NAMES = {
    "transaction": ("transaction", "transactions"),
    "account": ("account", "accounts"),
    "commodity": ("commodity", "commodities"),
    "price": ("price", "prices"),
    "scheduled": ("scheduled transaction", "scheduled transactions"),
    "scenario": ("scenario", "scenarios"),
    "fsa-claim": ("FSA claim", "FSA claims"),
    "reconciliation": ("reconciliation", "reconciliations"),
    "receivable": ("receivable", "receivables"),
    "savings-goal": ("savings goal", "savings goals"),
}


def describe_undo(preview: UndoPreview) -> str:
    """What undoing ``preview`` changes, kind by kind: "removes 3,180 transactions …"."""
    verbs = {"added": "removes", "removed": "restores", "changed": "puts back"}
    parts = []
    for verb_key in ("added", "changed", "removed"):
        items = [
            f"{count:,} {_NAMES.get(stem, (stem, stem))[0 if count == 1 else 1]}"
            for stem, kind, count in preview.changes
            if kind == verb_key
        ]
        if items:
            parts.append(f"{verbs[verb_key]} {', '.join(items)}")
    return "; ".join(parts) if parts else f"{preview.total:,} changes"


def undo_with_care(parent: Gtk.Window | None, db) -> None:
    """Undo the last step; a large one is confirmed and shown as it goes."""
    preview = book_server(db).undo_preview()
    if preview is None:
        return
    if preview.total < LARGE_UNDO:
        db.undo()
        return
    alert = Gtk.AlertDialog(
        message=f"Undo “{preview.message}”?",
        detail=(
            f"This undoes {preview.total:,} changes: {describe_undo(preview)}. "
            "It may take a while; Redo puts it all back."
        ),
        buttons=["Cancel", "Undo"],
        cancel_button=0,
        default_button=1,
    )

    def answered(dialog, result) -> None:
        try:
            choice = dialog.choose_finish(result)
        except GLib.Error:
            choice = 0
        if choice == 1:
            run_undo(parent, db, preview)

    alert.choose(parent, None, answered)


def run_undo(parent: Gtk.Window | None, db, preview: UndoPreview) -> Gtk.Window | None:
    """Undo with a progress window; a shared book's server undoes it in one request."""
    if not isinstance(db, DbSQLite):
        db.undo()
        return None
    window = Gtk.Window(
        title="Undoing", transient_for=parent, modal=True, deletable=False, resizable=False
    )
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    for side in ("top", "bottom", "start", "end"):
        getattr(box, f"set_margin_{side}")(16)
    label = Gtk.Label(label=f"Undoing “{preview.message}”…", xalign=0, wrap=True)
    bar = Gtk.ProgressBar(show_text=True)
    bar.set_size_request(360, -1)
    box.append(label)
    box.append(bar)
    window.set_child(box)
    window.progress = bar
    window.present()
    job: BackgroundJob[bool, tuple[int, int]] = BackgroundJob()
    window.job = job

    def progress(value: tuple[int, int]) -> None:
        done, total = value
        bar.set_fraction(done / total if total else 1.0)
        bar.set_text(f"{done:,} of {total:,} changes")

    def finished(_undone: bool) -> None:
        db.announce_undo()
        window.destroy()

    def failed(error: BaseException) -> None:
        window.destroy()
        Gtk.AlertDialog(message="Undo did not finish", detail=str(error)).show(parent)

    job.start(
        lambda _cancel, report: db.undo(lambda d, t: report((d, t)), announce=False),
        progress,
        finished,
        failed,
    )
    return window
