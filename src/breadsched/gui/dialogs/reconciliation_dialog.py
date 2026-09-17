"""Reconcile one account against a dated statement balance."""

from __future__ import annotations

from datetime import date

from ...gen.db.sqlite import DbSQLite
from ...gen.engine import reconciliation
from ...gen.lib import Account, Money, ReconciliationStatus
from ...gen.services import (
    ReconciliationAction,
    StartReconciliation,
    UpdateReconciliation,
    cancel_reconciliation,
    complete_reconciliation,
    reopen_reconciliation,
    start_reconciliation,
    update_reconciliation,
)
from ...gen.utils.amount_input import parse_user_amount
from ...presentation import service_error_message
from ..gi_setup import Gtk

__all__ = ["ReconciliationDialog"]


class ReconciliationDialog(Gtk.Window):
    """Thin GTK presentation over the shared reconciliation service."""

    def __init__(self, parent: Gtk.Window | None, db: DbSQLite, account: Account) -> None:
        super().__init__(
            title=f"Reconcile {db.full_name(account)}",
            transient_for=parent,
            modal=True,
        )
        self.db = db
        self.account = account
        self.set_default_size(760, 620)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for side in ("top", "bottom", "start", "end"):
            getattr(outer, f"set_margin_{side}")(16)
        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        outer.append(self.body)
        close = Gtk.Button(label="Close", halign=Gtk.Align.END)
        close.connect("clicked", lambda *_: self.close())
        outer.append(close)
        self.set_child(outer)
        self._render()

    def _clear(self) -> None:
        child = self.body.get_first_child()
        while child is not None:
            self.body.remove(child)
            child = self.body.get_first_child()

    def _render(self) -> None:
        self._clear()
        current = reconciliation.open_for_account(self.db, self.account.handle)
        if current is None:
            self._render_start()
        else:
            self._render_session(current.handle)

    def _render_start(self) -> None:
        explanation = Gtk.Label(
            label=(
                "Enter the statement date and ending balance. Already-cleared entries "
                "start checked; no ledger state changes until the difference is zero "
                "and the reconciliation is finished."
            ),
            xalign=0,
            wrap=True,
        )
        explanation.add_css_class("dim")
        self.body.append(explanation)
        grid = Gtk.Grid(column_spacing=10, row_spacing=8)
        self.statement_date = Gtk.Entry(text=date.today().isoformat())
        self.ending_balance = Gtk.Entry(placeholder_text="0.00", xalign=1)
        grid.attach(Gtk.Label(label="Statement date", xalign=0), 0, 0, 1, 1)
        grid.attach(self.statement_date, 1, 0, 1, 1)
        grid.attach(Gtk.Label(label="Ending balance", xalign=0), 0, 1, 1, 1)
        grid.attach(self.ending_balance, 1, 1, 1, 1)
        self.body.append(grid)
        self.status = Gtk.Label(xalign=0)
        self.body.append(self.status)
        start = Gtk.Button(label="Start reconciliation", halign=Gtk.Align.START)
        start.add_css_class("suggested-action")
        start.connect("clicked", self._on_start)
        self.body.append(start)

        completed = [
            item
            for item in self.db.iter_reconciliations(self.account.handle)
            if item.status is ReconciliationStatus.COMPLETED
        ]
        if completed:
            latest = max(completed, key=lambda item: item.statement_date)
            reopen = Gtk.Button(
                label=f"Reopen statement through {latest.statement_date}",
                halign=Gtk.Align.START,
            )
            reopen.connect("clicked", lambda *_: self._on_reopen(latest.handle))
            self.body.append(reopen)

    def _render_session(self, handle: str) -> None:
        state = reconciliation.summary(self.db, handle)
        session = state.reconciliation
        heading = Gtk.Label(label=f"Statement through {session.statement_date}", xalign=0)
        heading.add_css_class("title-3")
        self.body.append(heading)

        target_row = Gtk.Box(spacing=8)
        target_row.append(Gtk.Label(label="Ending balance", xalign=0))
        self.target_entry = Gtk.Entry(text=f"{session.ending_balance.to_decimal():.2f}", xalign=1)
        target_row.append(self.target_entry)
        update = Gtk.Button(label="Update")
        update.connect("clicked", lambda *_: self._on_update_target(handle))
        target_row.append(update)
        self.body.append(target_row)

        self.candidate_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        self.candidate_checks: list[tuple[str, Gtk.CheckButton]] = []
        for candidate in state.candidates:
            check = Gtk.CheckButton(
                label=(
                    f"{candidate.post_date}  {candidate.description or '(no description)'}"
                    f"    {candidate.amount.format(parens_negative=True)}"
                )
            )
            check.set_active(candidate.selected)
            check.connect("toggled", lambda *_: self._on_selection(handle))
            self.candidate_checks.append((candidate.split, check))
            self.candidate_box.append(check)
        scroller = Gtk.ScrolledWindow(child=self.candidate_box)
        scroller.set_vexpand(True)
        self.body.append(scroller)

        self.totals = Gtk.Label(xalign=0)
        self.body.append(self.totals)
        actions = Gtk.Box(spacing=8)
        cancel = Gtk.Button(label="Cancel reconciliation")
        cancel.connect("clicked", lambda *_: self._on_cancel(handle))
        actions.append(cancel)
        self.finish = Gtk.Button(label="Finish")
        self.finish.add_css_class("suggested-action")
        self.finish.set_sensitive(state.balanced)
        self.finish.connect("clicked", lambda *_: self._on_finish(handle))
        actions.append(self.finish)
        self.body.append(actions)
        self._show_totals(state)

    def _show_totals(self, state: reconciliation.ReconciliationSummary) -> None:
        self.totals.set_text(
            f"Prior reconciled {state.opening_balance.format()}  ·  "
            f"Checked {state.selected_balance.format()}  ·  "
            f"Difference {state.difference.format(parens_negative=True)}"
        )
        self.finish.set_sensitive(state.balanced)

    def _on_start(self, _button) -> None:
        try:
            statement_date = date.fromisoformat(self.statement_date.get_text().strip())
            ending = Money(parse_user_amount(self.ending_balance.get_text().strip()))
            result = start_reconciliation(
                self.db,
                StartReconciliation(self.account.handle, statement_date, ending),
            )
        except (ValueError, ArithmeticError) as exc:
            self.status.set_text(str(exc))
            return
        if result.value is None:
            self.status.set_text(service_error_message(result.errors[0]))
            return
        self._render()

    def _on_selection(self, handle: str) -> None:
        selected = [split for split, check in self.candidate_checks if check.get_active()]
        result = update_reconciliation(
            self.db,
            UpdateReconciliation(handle, selected_splits=tuple(selected)),
        )
        if result.value is not None:
            self._show_totals(result.value)

    def _on_update_target(self, handle: str) -> None:
        try:
            ending = Money(parse_user_amount(self.target_entry.get_text().strip()))
            result = update_reconciliation(
                self.db,
                UpdateReconciliation(handle, ending_balance=ending),
            )
        except (ValueError, ArithmeticError):
            self.totals.set_text("Enter a valid ending balance")
            self.finish.set_sensitive(False)
            return
        if result.value is None:
            self.totals.set_text(service_error_message(result.errors[0]))
            self.finish.set_sensitive(False)
            return
        self._show_totals(result.value)

    def _on_finish(self, handle: str) -> None:
        result = complete_reconciliation(self.db, ReconciliationAction(handle))
        if result.value is None:
            self.totals.set_text(service_error_message(result.errors[0]))
            return
        self.close()

    def _on_cancel(self, handle: str) -> None:
        result = cancel_reconciliation(self.db, ReconciliationAction(handle))
        if result.value is None:
            self.totals.set_text(service_error_message(result.errors[0]))
            return
        self._render()

    def _on_reopen(self, handle: str) -> None:
        result = reopen_reconciliation(self.db, ReconciliationAction(handle))
        if result.value is None:
            self.status.set_text(service_error_message(result.errors[0]))
            return
        self._render()
