"""Review actual transactions that have not yet been resolved against the plan."""

from __future__ import annotations

from ...gen.engine import planning
from ...gen.lib.money import Money
from ...gen.lib.transaction import PlanningResolution
from ..gi_setup import Gtk, Pango
from ._base import BaseView

__all__ = ["ResolutionView"]


def _gross_amount(transaction) -> Money:
    total = Money(0)
    for split in transaction.splits:
        if split.value > 0:
            total = total + split.value
    return total


class ResolutionView(BaseView):
    """Queue of actuals awaiting Match / Reject / Unexpected decisions."""

    WATCHES = (
        "database-changed",
        "transaction-add",
        "transaction-update",
        "transaction-delete",
        "scheduled-add",
        "scheduled-update",
        "scheduled-delete",
    )

    def __init__(self, manager) -> None:
        super().__init__(manager)
        self._transaction_handle: str | None = None
        self._candidate_key: str | None = None
        self._build()

    def _build(self) -> None:
        title = Gtk.Label(label="Resolve actuals", xalign=0)
        title.add_css_class("category-title")
        title.set_margin_top(12)
        title.set_margin_start(12)
        self.append(title)

        subtitle = Gtk.Label(
            label=(
                "Match imported or entered transactions to their scheduled "
                "expectations, reject bad suggestions, or mark them unexpected."
            ),
            xalign=0,
            wrap=True,
        )
        subtitle.set_margin_start(12)
        subtitle.set_margin_end(12)
        subtitle.set_margin_bottom(8)
        self.append(subtitle)

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_position(480)
        paned.set_vexpand(True)
        self.append(paned)

        self.actual_list = Gtk.ListBox()
        self.actual_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.actual_list.connect("row-selected", self._on_actual_selected)
        actual_scroll = Gtk.ScrolledWindow(child=self.actual_list)
        actual_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        paned.set_start_child(actual_scroll)

        detail = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        detail.set_margin_top(12)
        detail.set_margin_bottom(12)
        detail.set_margin_start(12)
        detail.set_margin_end(12)
        paned.set_end_child(detail)

        self.actual_summary = Gtk.Label(xalign=0, wrap=True)
        self.actual_summary.add_css_class("summary-value")
        detail.append(self.actual_summary)

        candidates_label = Gtk.Label(label="Candidate scheduled occurrences", xalign=0)
        candidates_label.add_css_class("heading")
        detail.append(candidates_label)

        self.candidate_list = Gtk.ListBox()
        self.candidate_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.candidate_list.connect("row-selected", self._on_candidate_selected)
        candidate_scroll = Gtk.ScrolledWindow(child=self.candidate_list)
        candidate_scroll.set_vexpand(True)
        detail.append(candidate_scroll)

        self.variance = Gtk.Label(xalign=0, wrap=True)
        self.variance.add_css_class("dim")
        detail.append(self.variance)

        buttons = Gtk.Box(spacing=8)
        self.match_button = Gtk.Button(label="Match")
        self.match_button.connect("clicked", self._on_match)
        buttons.append(self.match_button)
        self.reject_button = Gtk.Button(label="Reject candidate")
        self.reject_button.connect("clicked", self._on_reject)
        buttons.append(self.reject_button)
        self.skip_button = Gtk.Button(label="Skip scheduled occurrence")
        self.skip_button.connect("clicked", self._on_skip)
        buttons.append(self.skip_button)
        self.unexpected_button = Gtk.Button(label="Mark unexpected")
        self.unexpected_button.connect("clicked", self._on_unexpected)
        buttons.append(self.unexpected_button)
        detail.append(buttons)
        self._set_action_sensitivity()

    def _clear_list(self, widget: Gtk.ListBox) -> None:
        while (row := widget.get_row_at_index(0)) is not None:
            widget.remove(row)

    def _unresolved_transactions(self):
        if self.db is None:
            return []
        return sorted(
            (
                transaction
                for transaction in self.db.iter_transactions()
                if transaction.planning_resolution is PlanningResolution.UNRESOLVED
            ),
            key=lambda transaction: (transaction.post_date, transaction.handle),
        )

    def refresh(self) -> None:
        if self.db is None:
            return
        selected = self._transaction_handle
        self._clear_list(self.actual_list)
        transactions = self._unresolved_transactions()
        selected_row = None
        for transaction in transactions:
            row = Gtk.ListBoxRow()
            row.transaction_handle = transaction.handle
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            box.set_margin_top(6)
            box.set_margin_bottom(6)
            box.set_margin_start(8)
            box.set_margin_end(8)
            description = Gtk.Label(
                label=f"{transaction.post_date.isoformat()}  {transaction.description}",
                xalign=0,
            )
            description.set_ellipsize(Pango.EllipsizeMode.END)
            box.append(description)
            amount = _gross_amount(transaction)
            amount_label = Gtk.Label(label=amount.format(), xalign=0)
            amount_label.add_css_class("numeric")
            amount_label.add_css_class("dim")
            box.append(amount_label)
            row.set_child(box)
            self.actual_list.append(row)
            if transaction.handle == selected:
                selected_row = row

        if selected_row is None and transactions:
            selected_row = self.actual_list.get_row_at_index(0)
        if selected_row is not None:
            self.actual_list.select_row(selected_row)
        else:
            self._transaction_handle = None
            self._candidate_key = None
            self.actual_summary.set_text("No unresolved actual transactions.")
            self._clear_list(self.candidate_list)
            self.variance.set_text("")
            self._set_action_sensitivity()

    def _on_actual_selected(self, _listbox, row) -> None:
        self._transaction_handle = getattr(row, "transaction_handle", None) if row else None
        self._candidate_key = None
        self._refresh_candidates()

    def _refresh_candidates(self) -> None:
        self._clear_list(self.candidate_list)
        if self.db is None or self._transaction_handle is None:
            self.actual_summary.set_text("")
            self.variance.set_text("")
            self._set_action_sensitivity()
            return
        transaction = self.db.get_transaction(self._transaction_handle)
        if transaction is None:
            return
        amount = _gross_amount(transaction)
        self.actual_summary.set_text(
            f"{transaction.post_date.isoformat()} · {transaction.description}\n"
            f"Actual amount: {amount.format()}"
        )
        for candidate in planning.match_candidates(self.db, transaction):
            row = Gtk.ListBoxRow()
            row.candidate = candidate
            event = candidate.event
            text = (
                f"{event.planned_date.isoformat()}  {event.description}\n"
                f"Expected {event.expected_amount.format()} · "
                f"{candidate.date_distance} day(s) · "
                f"amount difference {candidate.amount_difference.format()}"
            )
            label = Gtk.Label(label=text, xalign=0, wrap=True)
            label.set_margin_top(6)
            label.set_margin_bottom(6)
            label.set_margin_start(8)
            label.set_margin_end(8)
            row.set_child(label)
            self.candidate_list.append(row)
        first = self.candidate_list.get_row_at_index(0)
        if first is not None:
            self.candidate_list.select_row(first)
        else:
            self.variance.set_text("No candidate within the matching window.")
            self._set_action_sensitivity()

    def _on_candidate_selected(self, _listbox, row) -> None:
        candidate = getattr(row, "candidate", None) if row else None
        self._candidate_key = candidate.event.key if candidate is not None else None
        if candidate is None:
            self.variance.set_text("")
        else:
            signed_days = 0
            if self.db is not None and self._transaction_handle is not None:
                transaction = self.db.get_transaction(self._transaction_handle)
                if transaction is not None:
                    signed_days = (transaction.post_date - candidate.event.planned_date).days
            amount_variance = Money(0)
            if self.db is not None and self._transaction_handle is not None:
                transaction = self.db.get_transaction(self._transaction_handle)
                if transaction is not None:
                    amount_variance = (
                        _gross_amount(transaction) - candidate.event.expected_amount
                    )
            amount_text = amount_variance.format()
            if amount_variance > 0:
                amount_text = f"+{amount_text}"
            self.variance.set_text(
                f"If matched: amount variance {amount_text} · "
                f"date variance {signed_days:+d} day(s)"
            )
        self._set_action_sensitivity()

    def _set_action_sensitivity(self) -> None:
        has_actual = self._transaction_handle is not None
        has_candidate = self._candidate_key is not None
        self.match_button.set_sensitive(has_actual and has_candidate)
        self.reject_button.set_sensitive(has_actual and has_candidate)
        self.skip_button.set_sensitive(has_candidate)
        self.unexpected_button.set_sensitive(has_actual)

    def _selected_transaction_and_event(self):
        if self.db is None or self._transaction_handle is None:
            return None, None
        transaction = self.db.get_transaction(self._transaction_handle)
        event = (
            planning.event_by_key(self.db, self._candidate_key)
            if self._candidate_key is not None
            else None
        )
        return transaction, event

    def _on_match(self, _button) -> None:
        transaction, event = self._selected_transaction_and_event()
        if transaction is None or event is None or self.db is None:
            return
        planning.actualize_transaction(transaction, event)
        with self.db.transaction("Match transaction to planned occurrence") as txn:
            self.db.commit_transaction(transaction, txn)
        self._transaction_handle = None
        self.refresh()

    def _on_reject(self, _button) -> None:
        transaction, event = self._selected_transaction_and_event()
        if transaction is None or event is None or self.db is None:
            return
        planning.reject_candidate(transaction, event)
        with self.db.transaction("Reject planned occurrence candidate") as txn:
            self.db.commit_transaction(transaction, txn)
        self._candidate_key = None
        self._refresh_candidates()

    def _on_skip(self, _button) -> None:
        _transaction, event = self._selected_transaction_and_event()
        if event is None or self.db is None:
            return
        planning.skip_occurrence(self.db, event)
        self._candidate_key = None
        self._refresh_candidates()

    def _on_unexpected(self, _button) -> None:
        if self.db is None or self._transaction_handle is None:
            return
        transaction = self.db.get_transaction(self._transaction_handle)
        if transaction is None:
            return
        planning.mark_unexpected(transaction)
        with self.db.transaction("Mark transaction as unexpected") as txn:
            self.db.commit_transaction(transaction, txn)
        self._transaction_handle = None
        self.refresh()
