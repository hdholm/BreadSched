"""Manage payees and review payee proposals through the shared payee service.

A payee records who a transaction was with; descriptions are never changed.
Proposals come from exact normalized description keys and are assigned only
when accepted. Every write is one undo step, and a rejected save or accept
leaves the book unchanged.
"""

from __future__ import annotations

from ...gen.db.sqlite import DbSQLite
from ...gen.lib.payee import Payee
from ...gen.services.payees import (
    AppliedPayees,
    SavePayee,
    apply_payee_proposals,
    delete_payee,
    preview_payee_proposals,
    save_payee,
)
from ...presentation import service_error_message
from ..gi_setup import Gtk

__all__ = ["PayeesDialog"]


class PayeesDialog(Gtk.Window):
    """Payee list and editor above the proposals awaiting acceptance."""

    def __init__(self, parent: Gtk.Window | None, db: DbSQLite) -> None:
        super().__init__(title="Payees", transient_for=parent, modal=True)
        self.set_destroy_with_parent(True)
        self.set_default_size(860, 620)
        self.db = db
        #: The payee being edited, or ``None`` when the form adds a new one.
        self.editing: str | None = None
        self.proposal_checks: dict[str, Gtk.CheckButton] = {}

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(18)
        self.set_child(box)
        box.append(
            Gtk.Label(
                label=(
                    "A payee records who a transaction was with; descriptions are never "
                    "changed. Matching ignores case, punctuation, and words containing "
                    "digits, and is otherwise exact. Nothing is assigned until you accept, "
                    "and a transaction that already has a payee is never changed."
                ),
                xalign=0,
                wrap=True,
            )
        )

        # Both lists grow with the window; their minimums keep it on a laptop
        # screen (#148 dialog audit).
        payee_scroller = Gtk.ScrolledWindow(min_content_height=110, vexpand=True)
        self.payee_rows = Gtk.Grid(column_spacing=14, row_spacing=4)
        payee_scroller.set_child(self.payee_rows)
        box.append(payee_scroller)

        form = Gtk.Grid(column_spacing=10, row_spacing=6)
        form.attach(Gtk.Label(label="Name", xalign=0), 0, 0, 1, 1)
        self.name_entry = Gtk.Entry(hexpand=True, placeholder_text="Corner Grocer")
        form.attach(self.name_entry, 1, 0, 1, 1)
        form.attach(Gtk.Label(label="Matching descriptions", xalign=0, yalign=0), 0, 1, 1, 1)
        self.matches_view = Gtk.TextView(accepts_tab=False)
        self.matches_view.set_size_request(-1, 60)
        form.attach(self.matches_view, 1, 1, 1, 1)
        actions = Gtk.Box(spacing=8)
        self.save_button = Gtk.Button(label="Add payee")
        self.save_button.add_css_class("suggested-action")
        self.save_button.connect("clicked", lambda _b: self.save())
        new_button = Gtk.Button(label="New payee")
        new_button.connect("clicked", lambda _b: self.edit(None))
        actions.append(self.save_button)
        actions.append(new_button)
        form.attach(actions, 1, 2, 1, 1)
        box.append(form)

        box.append(Gtk.Label(label="Proposals", xalign=0, css_classes=["heading"]))
        proposal_scroller = Gtk.ScrolledWindow(min_content_height=110, vexpand=True)
        self.proposal_rows = Gtk.Grid(column_spacing=14, row_spacing=4)
        proposal_scroller.set_child(self.proposal_rows)
        box.append(proposal_scroller)
        self.accept_button = Gtk.Button(label="Accept selected")
        self.accept_button.connect("clicked", lambda _b: self.accept_selected())
        box.append(self.accept_button)

        self.status = Gtk.Label(xalign=0, wrap=True, selectable=True)
        box.append(self.status)
        self.refresh()

    # ------------------------------------------------------------------ views

    def refresh(self) -> None:
        """Reload payees, their transaction counts, and the current proposals."""
        counts: dict[str, int] = {}
        for transaction in self.db.iter_transactions():
            if transaction.payee is not None:
                counts[transaction.payee] = counts.get(transaction.payee, 0) + 1
        self.payees = list(self.db.iter_payees())
        _clear(self.payee_rows)
        for column, heading in enumerate(("Payee", "Matches", "Transactions")):
            label = Gtk.Label(label=heading, xalign=1 if column == 2 else 0)
            label.add_css_class("dim")
            self.payee_rows.attach(label, column, 0, 1, 1)
        if not self.payees:
            self.payee_rows.attach(Gtk.Label(label="No payees yet.", xalign=0), 0, 1, 3, 1)
        for row, payee in enumerate(self.payees, start=1):
            self.payee_rows.attach(Gtk.Label(label=payee.name, xalign=0), 0, row, 1, 1)
            self.payee_rows.attach(
                Gtk.Label(label=", ".join(payee.match_keys) or "—", xalign=0, wrap=True),
                1,
                row,
                1,
                1,
            )
            self.payee_rows.attach(
                Gtk.Label(label=str(counts.get(payee.handle, 0)), xalign=1), 2, row, 1, 1
            )
            edit = Gtk.Button(label="Edit")
            edit.connect("clicked", lambda _b, handle=payee.handle: self.edit(handle))
            delete = Gtk.Button(label="Delete")
            delete.connect("clicked", lambda _b, handle=payee.handle: self.delete(handle))
            self.payee_rows.attach(edit, 3, row, 1, 1)
            self.payee_rows.attach(delete, 4, row, 1, 1)

        proposals = preview_payee_proposals(self.db).value or ()
        self.proposal_checks = {}
        _clear(self.proposal_rows)
        for column, heading in enumerate(("Accept", "Date", "Description", "Payee", "Key")):
            label = Gtk.Label(label=heading, xalign=0)
            label.add_css_class("dim")
            self.proposal_rows.attach(label, column, 0, 1, 1)
        if not proposals:
            self.proposal_rows.attach(
                Gtk.Label(label="No transactions without a payee match a payee.", xalign=0),
                0,
                1,
                5,
                1,
            )
        for row, item in enumerate(proposals, start=1):
            check = Gtk.CheckButton(active=True)
            self.proposal_checks[item.transaction] = check
            self.proposal_rows.attach(check, 0, row, 1, 1)
            for column, text in enumerate(
                (item.when.isoformat(), item.description, item.payee_name, item.key), start=1
            ):
                self.proposal_rows.attach(
                    Gtk.Label(label=text, xalign=0, selectable=True), column, row, 1, 1
                )
        self.accept_button.set_sensitive(bool(proposals))

    def edit(self, handle: str | None) -> None:
        """Load a payee into the form, or clear it to add a new one."""
        payee = self.db.get_payee(handle) if handle is not None else None
        self.editing = payee.handle if payee is not None else None
        self.name_entry.set_text(payee.name if payee is not None else "")
        self.matches_view.get_buffer().set_text(
            "\n".join(payee.match_keys) if payee is not None else ""
        )
        self.save_button.set_label("Save changes" if payee is not None else "Add payee")

    # ----------------------------------------------------------------- writes

    def save(self) -> Payee | None:
        """Add or update the payee in the form; None when refused."""
        buffer = self.matches_view.get_buffer()
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
        lines = tuple(line.strip() for line in text.splitlines() if line.strip())
        result = save_payee(self.db, SavePayee(self.name_entry.get_text(), lines, self.editing))
        if result.value is None:
            self._message(service_error_message(result.errors[0]), True)
            return None
        self.edit(None)
        self.refresh()
        self._message(f"Saved {result.value.name}.", False)
        return result.value

    def delete(self, handle: str) -> int | None:
        """Delete a payee and clear it from its transactions; Edit → Undo restores it."""
        payee = self.db.get_payee(handle)
        result = delete_payee(self.db, handle)
        if result.value is None:
            self._message(service_error_message(result.errors[0]), True)
            return None
        if self.editing == handle:
            self.edit(None)
        self.refresh()
        name = payee.name if payee is not None else "the payee"
        self._message(
            f"Deleted {name}; cleared from {result.value} transaction(s). Edit → Undo restores it.",
            False,
        )
        return result.value

    def accept_selected(self) -> AppliedPayees | None:
        """Accept the checked proposals as one undo step."""
        chosen = tuple(
            handle for handle, check in self.proposal_checks.items() if check.get_active()
        )
        result = apply_payee_proposals(self.db, chosen)
        if result.value is None:
            self._message(service_error_message(result.errors[0]), True)
            return None
        self.refresh()
        self._message(
            f"Assigned {result.value.assigned} payee(s); {result.value.unchanged} left unchanged.",
            False,
        )
        return result.value

    def _message(self, text: str, error: bool) -> None:
        self.status.set_text(text)
        if error:
            self.status.add_css_class("negative")
        else:
            self.status.remove_css_class("negative")


def _clear(grid: Gtk.Grid) -> None:
    child = grid.get_first_child()
    while child is not None:
        following = child.get_next_sibling()
        grid.remove(child)
        child = following
