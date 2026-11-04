"""The Match windows: match actuals to plans by split and amount (decision 0005).

Matching works like reconciling a statement. **From a planned occurrence**, the
window shows its expected, applied, and remaining amounts and, below, the splits in
its period that could count toward it, none checked: checking a row counts the
amount beside it (all that is left of the split, or what you type) and the totals
update. **Close occurrence** lets what remains lapse now. **From a transaction**,
the window lists its plan legs, each with the occurrences it could count toward,
ranked as Review ranks them, and a **Not planned** choice.

Both windows are modeless and write through the book server
(``services.plan_matching``), so a shared book's host does the writing.
"""

from __future__ import annotations

from decimal import InvalidOperation

from ...api.contract import ApiError
from ...api.local import book_server
from ...gen.lib.money import Money
from ...gen.services.plan_matching import (
    AllocateSplit,
    CandidateSplit,
    MatchingLeg,
    OccurrenceMatching,
    SplitNotPlanned,
    TransactionMatching,
)
from ...gen.utils.amount_input import parse_user_amount
from ..gi_setup import Gtk

__all__ = ["OccurrenceMatchWindow", "TransactionMatchWindow"]


def _amount(text: str) -> Money | None:
    try:
        return Money(parse_user_amount(text)) if text.strip() else None
    except (InvalidOperation, ValueError):
        return None


def _padded(child: Gtk.Widget) -> Gtk.Widget:
    for side in ("top", "bottom", "start", "end"):
        getattr(child, f"set_margin_{side}")(12)
    return child


class _MatchWindow(Gtk.Window):
    def __init__(self, parent: Gtk.Window | None, db, title: str) -> None:
        super().__init__(title=title, transient_for=parent, modal=False)
        self.set_default_size(720, 520)
        self.db = db
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.set_child(_padded(self.box))
        self.heading = Gtk.Label(xalign=0, wrap=True)
        self.heading.add_css_class("heading")
        self.totals = Gtk.Label(xalign=0, wrap=True)
        self.message = Gtk.Label(xalign=0, wrap=True)
        self.message.add_css_class("error")
        self.rows = Gtk.ListBox()
        self.rows.set_selection_mode(Gtk.SelectionMode.NONE)
        scroll = Gtk.ScrolledWindow(child=self.rows, vexpand=True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.actions = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        done = Gtk.Button(label="Done")
        done.connect("clicked", lambda _b: self.close())
        self.actions.append(done)
        for widget in (self.heading, self.totals, scroll, self.message, self.actions):
            self.box.append(widget)

    def _clear(self) -> None:
        while (row := self.rows.get_row_at_index(0)) is not None:
            self.rows.remove(row)

    def _say(self, error: ApiError | str | None) -> None:
        text = error if isinstance(error, str) or error is None else error.message or error.code
        self.message.set_text(text or "")


class OccurrenceMatchWindow(_MatchWindow):
    """Match splits to one planned occurrence, as a statement is reconciled."""

    def __init__(self, parent: Gtk.Window | None, db, key: str) -> None:
        super().__init__(parent, db, "Match to Plan")
        self.key = key
        self.close_button = Gtk.Button(label="Close occurrence")
        self.close_button.set_tooltip_text(
            "What remains of it is no longer expected; what was matched still counts."
        )
        self.close_button.connect("clicked", self._on_close_toggle)
        self.actions.prepend(self.close_button)
        self.view: OccurrenceMatching | None = None
        self.refresh()

    def refresh(self) -> None:
        try:
            view = book_server(self.db).occurrence_matching(self.key)
        except ApiError as refused:
            self._say(refused)
            return
        self.view = view
        period = (
            f"{view.planned_date.isoformat()} to {view.period_end.isoformat()}"
            if view.period_end is not None
            else view.planned_date.isoformat()
        )
        kind = "Estimate" if view.estimate else "Scheduled"
        self.heading.set_text(f"{kind}: {view.description} · {period}")
        state = " · closed" if view.closed else ""
        self.totals.set_text(
            f"Expected {view.expected.format()} · Applied {view.applied.format()} · "
            f"Remaining {view.remaining.format()}{state}"
        )
        self.close_button.set_label(
            "Reopen occurrence" if view.closed_early else "Close occurrence"
        )
        self.close_button.set_sensitive(view.closed_early or not view.closed)
        self._clear()
        if not view.candidates:
            self.rows.append(Gtk.Label(label="No splits in its period could count toward it."))
        for candidate in view.candidates:
            self.rows.append(self._row(candidate))

    def _row(self, candidate: CandidateSplit) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.candidate = candidate
        line = Gtk.Box(spacing=8)
        check = Gtk.CheckButton(active=bool(candidate.allocated))
        line.append(check)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        number = f"  {candidate.num}" if candidate.num else ""
        for label, dim in (
            (f"{candidate.date.isoformat()}{number}  {candidate.description}", False),
            (f"{candidate.account_name} ← {', '.join(candidate.other_accounts)}", True),
            (" · ".join(part for part in (candidate.memo, candidate.notes) if part), True),
        ):
            if label:
                widget = Gtk.Label(label=label, xalign=0, wrap=True)
                if dim:
                    widget.add_css_class("dim")
                text.append(widget)
        line.append(text)
        line.append(Gtk.Label(label=candidate.amount.format(), xalign=1))
        entry = Gtk.Entry(width_chars=10, xalign=1)
        entry.set_text((candidate.allocated or candidate.unallocated).format())
        entry.set_tooltip_text("How much of the split counts toward it")
        line.append(entry)
        row.check, row.entry = check, entry
        check.connect("toggled", lambda _b, r=row: self.toggle(r))
        row.set_child(line)
        return row

    def toggle(self, row: Gtk.ListBoxRow) -> None:
        candidate: CandidateSplit = row.candidate
        amount = _amount(row.entry.get_text()) if row.check.get_active() else Money(0)
        if amount is None:
            self._say("Type an amount to count toward it.")
            return
        try:
            book_server(self.db).allocate(
                AllocateSplit(candidate.transaction, candidate.split, self.key, amount)
            )
        except ApiError as refused:
            self._say(refused)
            self.refresh()
            return
        self._say(None)
        self.refresh()

    def _on_close_toggle(self, _button) -> None:
        server = book_server(self.db)
        try:
            if self.view is not None and self.view.closed_early:
                server.reopen_occurrence(self.key)
            else:
                server.close_occurrence(self.key)
        except ApiError as refused:
            self._say(refused)
        self.refresh()


class TransactionMatchWindow(_MatchWindow):
    """Match each plan leg of one transaction to the occurrences it counts toward."""

    def __init__(self, parent: Gtk.Window | None, db, transaction: str) -> None:
        super().__init__(parent, db, "Match to Plan")
        self.transaction = transaction
        self.view: TransactionMatching | None = None
        self.refresh()

    def refresh(self) -> None:
        try:
            view = book_server(self.db).transaction_matching(self.transaction)
        except ApiError as refused:
            self._say(refused)
            return
        self.view = view
        self.heading.set_text(f"{view.date.isoformat()} · {view.description}")
        state = {"matched": "Matched", "unexpected": "Not planned"}.get(
            view.resolution.value, "Still to match"
        )
        self.totals.set_text(state + (f" · Notes: {view.notes}" if view.notes else ""))
        self._clear()
        if not view.legs:
            self.rows.append(Gtk.Label(label="This transaction has nothing to match."))
        for leg in view.legs:
            self.rows.append(self._row(leg))

    def _row(self, leg: MatchingLeg) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.leg = leg
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        memo = f" ({leg.memo})" if leg.memo else ""
        box.append(
            Gtk.Label(
                label=f"{leg.account_name}{memo}  {leg.amount.format()} · "
                f"unallocated {leg.unallocated.format()}",
                xalign=0,
                wrap=True,
            )
        )
        for allocation in leg.allocations:
            line = Gtk.Box(spacing=8)
            name = next(
                (item.description for item in leg.candidates if item.key == allocation.occurrence),
                allocation.occurrence,
            )
            line.append(
                Gtk.Label(label=f"→ {name}  {allocation.amount.format()}", xalign=0, hexpand=True)
            )
            remove = Gtk.Button(label="Remove")
            remove.connect(
                "clicked", lambda _b, key=allocation.occurrence: self.allocate(leg, key, Money(0))
            )
            line.append(remove)
            box.append(line)
        choose = Gtk.Box(spacing=8)
        labels = [
            f"{item.planned_date.isoformat()} {item.description} "
            f"({item.remaining.format()} remains)"
            for item in leg.candidates
        ]
        dropdown = Gtk.DropDown.new_from_strings(labels or ["No open planned item"])
        dropdown.set_sensitive(bool(labels) and not leg.not_planned)
        dropdown.set_hexpand(True)
        entry = Gtk.Entry(width_chars=10, xalign=1)
        entry.set_text(leg.unallocated.format())
        match = Gtk.Button(label="Match")
        match.set_sensitive(bool(labels) and not leg.not_planned and bool(leg.unallocated))
        match.connect(
            "clicked",
            lambda _b: self.allocate(
                leg, leg.candidates[dropdown.get_selected()].key, _amount(entry.get_text())
            ),
        )
        for widget in (dropdown, entry, match):
            choose.append(widget)
        box.append(choose)
        not_planned = Gtk.CheckButton(label="Not planned", active=leg.not_planned)
        not_planned.connect(
            "toggled", lambda button: self.set_not_planned(leg, button.get_active())
        )
        box.append(not_planned)
        row.dropdown, row.entry, row.match, row.not_planned = dropdown, entry, match, not_planned
        row.set_child(box)
        return row

    def allocate(self, leg: MatchingLeg, key: str, amount: Money | None) -> None:
        try:
            book_server(self.db).allocate(AllocateSplit(self.transaction, leg.split, key, amount))
        except ApiError as refused:
            self._say(refused)
            return
        self._say(None)
        self.refresh()

    def set_not_planned(self, leg: MatchingLeg, value: bool) -> None:
        if value == leg.not_planned:
            return
        try:
            book_server(self.db).mark_split_not_planned(
                SplitNotPlanned(self.transaction, leg.split, value)
            )
        except ApiError as refused:
            self._say(refused)
        self.refresh()
