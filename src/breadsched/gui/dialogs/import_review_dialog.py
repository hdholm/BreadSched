"""GnuCash changes held back from transactions reconciled in BreadSched.

Re-import never silently replaces a locally reconciled transaction. Each held
change is decided on its own, in one batch, like the due-schedule review:

* **Keep BreadSched version** — the local transaction stays as reconciled and
  this GnuCash version is not raised again.
* **Use GnuCash version** — apply the change, keeping BreadSched notes, Plan
  links, and classifications. Unavailable while a completed statement would stop
  balancing; reopen that statement first.
* **Decide later** — the default, because the safe answer to an unread question
  is to ask again.

The batch is validated and applied by the shared service as one undo step.
"""

from __future__ import annotations

from ...gen.db.sqlite import DbSQLite
from ...gen.services import (
    HeldImportChange,
    HeldImportDecision,
    ResolveHeldImports,
    resolve_import_changes,
)
from ...presentation import service_error_message
from ..gi_setup import Gtk

__all__ = ["ImportReviewDialog"]

_LABELS = {
    HeldImportDecision.KEEP_LOCAL: "Keep BreadSched version",
    HeldImportDecision.USE_SOURCE: "Use GnuCash version",
    HeldImportDecision.LATER: "Decide later",
}
#: A held deletion asks whether to delete the transaction here too.
_DELETION_LABELS = {
    HeldImportDecision.KEEP_LOCAL: "Keep the transaction",
    HeldImportDecision.USE_SOURCE: "Delete it here too",
    HeldImportDecision.LATER: "Decide later",
}


class ImportReviewDialog(Gtk.Window):
    """One row per held GnuCash change, each with its own decision."""

    def __init__(
        self, parent: Gtk.Window | None, db: DbSQLite, changes: list[HeldImportChange]
    ) -> None:
        super().__init__(
            title="GnuCash changes to reconciled transactions", transient_for=parent, modal=True
        )
        self.set_destroy_with_parent(True)
        self.db = db
        self.changes = list(changes)
        self.choosers: list[Gtk.DropDown] = []
        self._options: list[list[HeldImportDecision]] = []
        self.set_default_size(820, 480)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(18)
        self.set_child(box)

        box.append(
            Gtk.Label(
                label=(
                    f"GnuCash changed {len(self.changes)} transaction(s) that are reconciled "
                    "in BreadSched. They were left unchanged. Choose what to do with each."
                ),
                xalign=0,
                wrap=True,
            )
        )

        bulk = Gtk.Box(spacing=8)
        for label, decision in (
            ("Keep all BreadSched versions", HeldImportDecision.KEEP_LOCAL),
            ("Use GnuCash where possible", HeldImportDecision.USE_SOURCE),
            ("Decide all later", HeldImportDecision.LATER),
        ):
            button = Gtk.Button(label=label)
            button.connect("clicked", lambda _b, value=decision: self.set_all(value))
            bulk.append(button)
        box.append(bulk)

        grid = Gtk.Grid(column_spacing=14, row_spacing=8)
        for position, heading in enumerate(("Date", "Transaction", "GnuCash change", "Action")):
            label = Gtk.Label(label=heading, xalign=0)
            label.add_css_class("summary-label")
            grid.attach(label, position, 0, 1, 1)

        for index, change in enumerate(self.changes, start=1):
            grid.attach(Gtk.Label(label=change.post_date.isoformat(), xalign=0), 0, index, 1, 1)
            grid.attach(Gtk.Label(label=change.description, xalign=0), 1, index, 1, 1)
            lines = list(change.changes)
            if change.blocked_by:
                prefix = "Still used by: " if change.deleted else "Reopen first: "
                lines.append(prefix + ", ".join(change.blocked_by))
            detail = Gtk.Label(label="\n".join(lines), xalign=0, wrap=True)
            grid.attach(detail, 2, index, 1, 1)

            options = [
                decision
                for decision in (
                    HeldImportDecision.KEEP_LOCAL,
                    HeldImportDecision.USE_SOURCE,
                    HeldImportDecision.LATER,
                )
                if change.can_use_source or decision is not HeldImportDecision.USE_SOURCE
            ]
            labels = _DELETION_LABELS if change.deleted else _LABELS
            chooser = Gtk.DropDown.new_from_strings([labels[item] for item in options])
            # Undecided by default: an unread question is asked again.
            chooser.set_selected(options.index(HeldImportDecision.LATER))
            self.choosers.append(chooser)
            self._options.append(options)
            grid.attach(chooser, 3, index, 1, 1)

        scroller = Gtk.ScrolledWindow(child=grid)
        scroller.set_vexpand(True)
        box.append(scroller)

        self.status = Gtk.Label(xalign=0, wrap=True)
        self.status.add_css_class("dim")
        box.append(self.status)

        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        close = Gtk.Button(label="Decide later")
        close.connect("clicked", lambda *_: self.close())
        buttons.append(close)
        apply_button = Gtk.Button(label="Apply")
        apply_button.add_css_class("suggested-action")
        apply_button.connect("clicked", self._on_apply)
        buttons.append(apply_button)
        box.append(buttons)

    # ------------------------------------------------------------------ choices

    def set_all(self, decision: HeldImportDecision) -> None:
        """Choose ``decision`` for every row that offers it."""
        for chooser, options in zip(self.choosers, self._options, strict=True):
            if decision in options:
                chooser.set_selected(options.index(decision))

    def decision(self, index: int) -> HeldImportDecision:
        return self._options[index][self.choosers[index].get_selected()]

    def select(self, index: int, decision: HeldImportDecision) -> None:
        self.choosers[index].set_selected(self._options[index].index(decision))

    def apply(self) -> tuple[int, int] | None:
        """Carry out the decisions. Returns (kept, applied), or None on refusal."""
        decisions = tuple(
            (change.transaction, self.decision(index))
            for index, change in enumerate(self.changes)
            if self.decision(index) is not HeldImportDecision.LATER
        )
        if not decisions:
            return 0, 0
        result = resolve_import_changes(self.db, ResolveHeldImports(decisions))
        if not result.ok:
            self.status.set_text(service_error_message(result.errors[0]))
            self.status.add_css_class("negative")
            return None
        assert result.value is not None
        return result.value.kept, result.value.applied

    def _on_apply(self, _button) -> None:
        outcome = self.apply()
        if outcome is None:
            return
        kept, applied = outcome
        self.close()
        if kept or applied:
            alert = Gtk.AlertDialog(
                message=(
                    f"GnuCash changes: kept {kept} BreadSched version(s), "
                    f"applied {applied} GnuCash version(s)."
                ),
                detail="Undo with Ctrl+Z if that was not what you wanted.",
            )
            alert.show(self.get_transient_for())
