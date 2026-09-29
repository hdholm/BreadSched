"""Reimbursable expenses in GTK: what a payer owes back on an expense you paid.

This dialog only gathers input and shows results. Every rule and every write is in
``gen/services/receivables``: a receivable never rewrites or removes the expense
splits it references, a reimbursement is an ordinary credit to the same expense
account (never income), and what is still owed is held in a Receivable account by
reclassification transactions the service keeps up to date (#170); a dispute posts
nothing. Status and remaining balance are always recomputed, never stored.
"""

from __future__ import annotations

from datetime import date

from ...gen.db.sqlite import DbSQLite
from ...gen.lib.money import Money
from ...gen.lib.receivable import Receivable
from ...gen.services.receivables import (
    RecordWriteOff,
    SaveReceivable,
    accept_reimbursements,
    attach_expense_split,
    attach_reimbursement_split,
    clear_dispute,
    delete_receivable,
    detach_split,
    list_receivables,
    mark_disputed,
    receivable_accounts,
    receivable_candidates,
    record_write_off,
    reimbursement_proposals,
    save_receivable,
)
from ...gen.utils.amount_input import parse_user_amount
from ...presentation import service_error_message
from ..gi_setup import Gtk
from ..widgets.bounded import scroll_body

__all__ = ["ReceivablesDialog"]


def _clear(grid: Gtk.Grid) -> None:
    child = grid.get_first_child()
    while child is not None:
        following = child.get_next_sibling()
        grid.remove(child)
        child = following


def _heading(grid: Gtk.Grid, titles: tuple[str, ...], numeric: set[int]) -> None:
    for column, title in enumerate(titles):
        label = Gtk.Label(label=title, xalign=1 if column in numeric else 0)
        label.add_css_class("dim")
        grid.attach(label, column, 0, 1, 1)


def _amount(value: Money) -> Gtk.Label:
    label = Gtk.Label(label=value.format(parens_negative=True), xalign=1)
    label.add_css_class("numeric")
    if value < 0:
        label.add_css_class("negative")
    return label


class ReceivablesDialog(Gtk.Window):
    """List, create, link, dispute, and write off reimbursable expenses."""

    def __init__(
        self,
        parent: Gtk.Window | None,
        db: DbSQLite,
        *,
        expense: tuple[str, str] | None = None,
    ) -> None:
        """``expense`` is a (transaction, split) cost to track as a new receivable."""
        super().__init__(title="Reimbursable expenses", transient_for=parent, modal=True)
        self.set_destroy_with_parent(True)
        self.set_default_size(960, 680)
        self.db = db
        #: The receivable loaded in the form, or None while adding one.
        self.editing: str | None = None
        #: A cost split to link once the new receivable in the form is saved.
        self.pending_expense = expense
        self._costs: list[tuple[str, str]] = []
        self._credits: list[tuple[str, str]] = []

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(18)
        self.set_child(box)
        box.append(
            Gtk.Label(
                label=(
                    "Track an expense you paid that an insurer, employer, or other payer "
                    "owes back. What is owed moves from the expense into a Receivable "
                    "account: part of net worth, never of liquidity, because it cannot "
                    "be spent yet. A reimbursement is an ordinary credit to the same "
                    "expense account, never income; a write-off returns the balance to "
                    "the expense, and a dispute posts nothing. The original expense is "
                    "never changed."
                ),
                xalign=0,
                wrap=True,
            )
        )

        scroller = Gtk.ScrolledWindow(min_content_height=120, vexpand=True)
        self.rows = Gtk.Grid(column_spacing=14, row_spacing=4)
        scroller.set_child(self.rows)
        box.append(scroller)

        form = Gtk.Grid(column_spacing=10, row_spacing=6)
        self.payer_entry = Gtk.Entry(hexpand=True, placeholder_text="Acme Insurance")
        self.description_entry = Gtk.Entry(hexpand=True, placeholder_text="What it was for")
        self.incurred_entry = Gtk.Entry(placeholder_text="YYYY-MM-DD")
        self.expected_entry = Gtk.Entry(placeholder_text="Optional", xalign=1)
        self.expected_entry.add_css_class("numeric")
        self.cash_date_entry = Gtk.Entry(placeholder_text="Optional YYYY-MM-DD")
        self.account_picker = Gtk.DropDown()
        self.account_picker.set_tooltip_text(
            "The Receivable account holding what is owed; by default one per currency, "
            "created when the book has none"
        )
        #: The account handle behind each picker entry; None is the default.
        self._accounts: list[str | None] = []
        for row, (label, widget) in enumerate(
            (
                ("Payer", self.payer_entry),
                ("Description", self.description_entry),
                ("Incurred", self.incurred_entry),
                ("Expected back", self.expected_entry),
                ("Expected by", self.cash_date_entry),
                ("Held in", self.account_picker),
            )
        ):
            form.attach(Gtk.Label(label=label, xalign=0), 0, row, 1, 1)
            form.attach(widget, 1, row, 1, 1)
        actions = Gtk.Box(spacing=8)
        self.save_button = Gtk.Button(label="Add receivable")
        self.save_button.add_css_class("suggested-action")
        self.save_button.connect("clicked", lambda _b: self.save())
        new_button = Gtk.Button(label="New")
        new_button.connect("clicked", lambda _b: self.edit(None))
        self.delete_button = Gtk.Button(label="Delete")
        self.delete_button.add_css_class("destructive-action")
        self.delete_button.connect("clicked", lambda _b: self.delete())
        for button in (self.save_button, new_button, self.delete_button):
            actions.append(button)
        form.attach(actions, 1, 6, 1, 1)
        box.append(form)

        # Credits that clearly reimburse one open receivable; nothing links until
        # the user accepts.
        proposals_heading = Gtk.Label(label="Proposed reimbursements", xalign=0)
        proposals_heading.add_css_class("heading")
        box.append(proposals_heading)
        self.proposal_rows = Gtk.Grid(column_spacing=14, row_spacing=4)
        box.append(self.proposal_rows)
        self.accept_button = Gtk.Button(label="Accept selected")
        self.accept_button.set_halign(Gtk.Align.START)
        self.accept_button.connect("clicked", lambda _b: self.accept_selected())
        box.append(self.accept_button)
        self.proposal_checks: dict[tuple[str, str, str], Gtk.CheckButton] = {}

        # Links, dispute, and write-off apply to the receivable loaded in the form.
        self.detail = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        heading = Gtk.Label(label="Linked splits", xalign=0)
        heading.add_css_class("heading")
        self.detail.append(heading)
        self.links = Gtk.Grid(column_spacing=14, row_spacing=4)
        self.detail.append(self.links)
        self.warning = Gtk.Label(xalign=0, wrap=True)
        self.warning.add_css_class("negative")
        self.detail.append(self.warning)

        link_row = Gtk.Box(spacing=8)
        self.cost_picker = Gtk.DropDown()
        self.cost_picker.set_hexpand(True)
        self.cost_picker.set_tooltip_text("An expense split recording money spent")
        link_cost = Gtk.Button(label="Link expense")
        link_cost.connect("clicked", lambda _b: self.link("expense"))
        link_row.append(self.cost_picker)
        link_row.append(link_cost)
        self.detail.append(link_row)
        credit_row = Gtk.Box(spacing=8)
        self.credit_picker = Gtk.DropDown()
        self.credit_picker.set_hexpand(True)
        self.credit_picker.set_tooltip_text(
            "An expense split crediting money back, as an ordinary refund"
        )
        link_credit = Gtk.Button(label="Link reimbursement")
        link_credit.connect("clicked", lambda _b: self.link("reimbursement"))
        credit_row.append(self.credit_picker)
        credit_row.append(link_credit)
        self.detail.append(credit_row)

        dispute_row = Gtk.Box(spacing=8)
        self.dispute_date = Gtk.Entry(placeholder_text="YYYY-MM-DD")
        self.dispute_note = Gtk.Entry(hexpand=True, placeholder_text="Why the payer contests it")
        mark = Gtk.Button(label="Mark disputed")
        mark.connect("clicked", lambda _b: self.dispute())
        clear = Gtk.Button(label="Clear dispute")
        clear.connect("clicked", lambda _b: self.clear_dispute())
        for widget in (Gtk.Label(label="Dispute"), self.dispute_date, self.dispute_note):
            dispute_row.append(widget)
        dispute_row.append(mark)
        dispute_row.append(clear)
        self.detail.append(dispute_row)

        write_row = Gtk.Box(spacing=8)
        self.write_off_amount = Gtk.Entry(placeholder_text="0.00", xalign=1)
        self.write_off_amount.add_css_class("numeric")
        self.write_off_date = Gtk.Entry(placeholder_text="YYYY-MM-DD")
        self.write_off_reason = Gtk.Entry(hexpand=True, placeholder_text="Reason")
        record = Gtk.Button(label="Record write-off")
        record.connect("clicked", lambda _b: self.write_off())
        for widget in (
            Gtk.Label(label="Write off"),
            self.write_off_amount,
            self.write_off_date,
            self.write_off_reason,
        ):
            write_row.append(widget)
        write_row.append(record)
        self.detail.append(write_row)
        box.append(self.detail)

        self.status = Gtk.Label(xalign=0, wrap=True, selectable=True)
        box.append(self.status)
        # The form scrolls; the status line stays in view (#148 dialog audit).
        scroll_body(self)

        self.edit(None)
        if expense is not None:
            self._prefill_from(expense)
        self.refresh()

    # ------------------------------------------------------------------ views

    def _message(self, text: str, error: bool) -> None:
        self.status.set_text(text)
        if error:
            self.status.add_css_class("negative")
        else:
            self.status.remove_css_class("negative")

    def _prefill_from(self, expense: tuple[str, str]) -> None:
        transaction = self.db.get_transaction(expense[0])
        split = (
            next((item for item in transaction.splits if item.handle == expense[1]), None)
            if transaction is not None
            else None
        )
        if transaction is None or split is None:
            self.pending_expense = None
            return
        self.incurred_entry.set_text(transaction.post_date.isoformat())
        self.description_entry.set_text(transaction.description)
        self.expected_entry.set_text(split.value.format())
        self._message(
            f"Enter who owes this back; saving links the {split.value.format()} expense "
            f"from {transaction.post_date.isoformat()} “{transaction.description}”.",
            False,
        )

    def refresh(self) -> None:
        """Reload every receivable's recomputed standing and the loaded one's links."""
        summaries = list_receivables(self.db, as_of=date.today()).value or ()
        _clear(self.rows)
        titles = (
            "Payer",
            "Description",
            "Incurred",
            "Expense",
            "Reimbursed",
            "Written off",
            "Remaining",
            "Status",
            "Age",
            "Expected by",
        )
        _heading(self.rows, titles, {3, 4, 5, 6, 8})
        if not summaries:
            self.rows.attach(Gtk.Label(label="No reimbursable expenses yet.", xalign=0), 0, 1, 4, 1)
        for row, summary in enumerate(summaries, start=1):
            item = summary.receivable
            texts = (item.payer, item.description or "—", item.incurred_date.isoformat())
            for column, text in enumerate(texts):
                label = Gtk.Label(label=text, xalign=0, selectable=True)
                self.rows.attach(label, column, row, 1, 1)
            for column, value in enumerate(
                (summary.expense_total, summary.reimbursed, summary.written_off, summary.remaining),
                start=3,
            ):
                self.rows.attach(_amount(value), column, row, 1, 1)
            self.rows.attach(Gtk.Label(label=summary.status.label, xalign=0), 7, row, 1, 1)
            self.rows.attach(Gtk.Label(label=f"{summary.age_days} d", xalign=1), 8, row, 1, 1)
            expected_by = item.expected_cash_date.isoformat() if item.expected_cash_date else "—"
            self.rows.attach(Gtk.Label(label=expected_by, xalign=0), 9, row, 1, 1)
            open_button = Gtk.Button(label="Open")
            open_button.connect("clicked", lambda _b, handle=item.handle: self.edit(handle))
            self.rows.attach(open_button, 10, row, 1, 1)
        self._refresh_proposals()
        self._refresh_detail()

    def _refresh_proposals(self) -> None:
        proposals = reimbursement_proposals(self.db, as_of=date.today()).value or ()
        _clear(self.proposal_rows)
        self.proposal_checks = {}
        _heading(
            self.proposal_rows,
            ("Accept", "Date", "Description", "Payer", "Amount", "Remaining after", "Why"),
            {4, 5},
        )
        if not proposals:
            self.proposal_rows.attach(
                Gtk.Label(
                    label="No unlinked credits clearly reimburse an open receivable.", xalign=0
                ),
                0,
                1,
                7,
                1,
            )
        for row, item in enumerate(proposals, start=1):
            check = Gtk.CheckButton(active=True)
            check.update_property(
                [Gtk.AccessibleProperty.LABEL], [f"Accept {item.description} for {item.payer}"]
            )
            self.proposal_checks[(item.receivable, item.transaction, item.split)] = check
            self.proposal_rows.attach(check, 0, row, 1, 1)
            for column, text in enumerate(
                (item.when.isoformat(), item.description, item.payer), start=1
            ):
                self.proposal_rows.attach(Gtk.Label(label=text, xalign=0), column, row, 1, 1)
            self.proposal_rows.attach(_amount(item.amount), 4, row, 1, 1)
            self.proposal_rows.attach(_amount(item.remaining_after), 5, row, 1, 1)
            reason = Gtk.Label(label=item.reason, xalign=0, wrap=True)
            reason.add_css_class("dim")
            self.proposal_rows.attach(reason, 6, row, 1, 1)
        self.accept_button.set_sensitive(bool(proposals))

    def accept_selected(self):
        """Link the checked proposals that are still on offer."""
        chosen = tuple(key for key, check in self.proposal_checks.items() if check.get_active())
        result = accept_reimbursements(self.db, chosen)
        if result.value is None:
            self._message(service_error_message(result.errors[0]), True)
            return None
        self.refresh()
        self._message(
            f"Linked {result.value.linked} reimbursement(s); "
            f"{result.value.unchanged} left unchanged.",
            False,
        )
        return result.value

    def _refresh_detail(self) -> None:
        receivable = self.db.get_receivable(self.editing) if self.editing else None
        self.detail.set_visible(receivable is not None)
        self.delete_button.set_sensitive(receivable is not None)
        _clear(self.links)
        self.warning.set_visible(False)
        if receivable is None:
            return
        overlaps = next(
            (
                summary.fsa_claims
                for summary in list_receivables(self.db).value or ()
                if summary.receivable.handle == receivable.handle
            ),
            (),
        )
        if overlaps:
            self.warning.set_text(
                "An expense linked here is also on an FSA claim. Check that the same "
                "cost is not expected back from both."
            )
            self.warning.set_visible(True)
        _heading(self.links, ("Role", "Date", "Description", "Account", "Amount"), {4})
        linked = [("Expense", link) for link in receivable.expenses] + [
            ("Reimbursement", link) for link in receivable.reimbursements
        ]
        if not linked:
            self.links.attach(Gtk.Label(label="Nothing linked yet.", xalign=0), 0, 1, 5, 1)
        for row, (role, link) in enumerate(linked, start=1):
            transaction = self.db.get_transaction(link.transaction)
            split = (
                next((item for item in transaction.splits if item.handle == link.split), None)
                if transaction is not None
                else None
            )
            self.links.attach(Gtk.Label(label=role, xalign=0), 0, row, 1, 1)
            if transaction is None or split is None:
                missing = Gtk.Label(label="Linked split no longer exists", xalign=0)
                missing.add_css_class("negative")
                self.links.attach(missing, 1, row, 4, 1)
            else:
                for column, text in enumerate(
                    (
                        transaction.post_date.isoformat(),
                        transaction.description,
                        self.db.full_name(split.account),
                    ),
                    start=1,
                ):
                    self.links.attach(Gtk.Label(label=text, xalign=0), column, row, 1, 1)
                self.links.attach(_amount(split.value), 4, row, 1, 1)
            unlink = Gtk.Button(label="Unlink")
            unlink.connect("clicked", lambda _b, item=link: self.unlink(item))
            self.links.attach(unlink, 5, row, 1, 1)
        self._fill_pickers(receivable)
        self.dispute_date.set_text(
            receivable.disputed_on.isoformat() if receivable.disputed_on else ""
        )
        self.dispute_note.set_text(receivable.dispute_note)

    def _fill_pickers(self, receivable: Receivable) -> None:
        """Offer recent unlinked expense-account splits, costs and credits apart."""
        costs, credits = receivable_candidates(self.db, receivable.handle).value or ((), ())
        for picker, items, attribute in (
            (self.cost_picker, costs, "_costs"),
            (self.credit_picker, credits, "_credits"),
        ):
            setattr(self, attribute, [(item.transaction, item.split) for item in items])
            names = Gtk.StringList()
            for item in items:
                names.append(
                    f"{item.when.isoformat()} · {item.description} · "
                    f"{self.db.full_name(item.account)} · {item.value.format()}"
                )
            if not items:
                names.append("(no unlinked expense-account splits)")
            picker.set_model(names)

    def _fill_accounts(self, current: str | None) -> None:
        accounts = receivable_accounts(self.db)
        self._accounts = [None, *(account.handle for account in accounts)]
        names = Gtk.StringList()
        names.append("Default Receivable account")
        for account in accounts:
            names.append(self.db.full_name(account) or account.name)
        self.account_picker.set_model(names)
        self.account_picker.set_selected(
            self._accounts.index(current) if current in self._accounts else 0
        )

    def edit(self, handle: str | None) -> None:
        """Load a receivable into the form, or clear the form to add one."""
        receivable = self.db.get_receivable(handle) if handle is not None else None
        self.editing = receivable.handle if receivable is not None else None
        today = date.today().isoformat()
        self.payer_entry.set_text(receivable.payer if receivable else "")
        self.description_entry.set_text(receivable.description if receivable else "")
        self.incurred_entry.set_text(receivable.incurred_date.isoformat() if receivable else today)
        expected = receivable.expected_amount if receivable else None
        self.expected_entry.set_text(expected.format() if expected is not None else "")
        cash_date = receivable.expected_cash_date if receivable else None
        self.cash_date_entry.set_text(cash_date.isoformat() if cash_date else "")
        self.write_off_amount.set_text("")
        self.write_off_date.set_text(today)
        self.write_off_reason.set_text("")
        self._fill_accounts(receivable.account if receivable else None)
        self.save_button.set_label("Save changes" if receivable else "Add receivable")
        if receivable is not None:
            self.pending_expense = None
        self._refresh_detail()

    # ----------------------------------------------------------------- parsing

    def _date(self, entry: Gtk.Entry, label: str, *, optional: bool = False) -> date | None:
        text = entry.get_text().strip()
        if not text and optional:
            return None
        try:
            return date.fromisoformat(text)
        except ValueError:
            raise ValueError(f"Enter the {label} as YYYY-MM-DD.") from None

    def _money(self, entry: Gtk.Entry, label: str, *, optional: bool = False) -> Money | None:
        text = entry.get_text().strip()
        if not text and optional:
            return None
        try:
            return Money(parse_user_amount(text))
        except (ValueError, ArithmeticError):
            raise ValueError(f"Enter a valid {label}.") from None

    # ----------------------------------------------------------------- writes

    def save(self) -> Receivable | None:
        """Add or update the receivable in the form; None when refused."""
        try:
            request = SaveReceivable(
                incurred_date=self._date(self.incurred_entry, "incurred date") or date.today(),
                payer=self.payer_entry.get_text(),
                description=self.description_entry.get_text(),
                expected_amount=self._money(self.expected_entry, "expected amount", optional=True),
                expected_cash_date=self._date(self.cash_date_entry, "expected date", optional=True),
                handle=self.editing,
                account=self._accounts[self.account_picker.get_selected()]
                if self._accounts
                else None,
            )
        except ValueError as error:
            self._message(str(error), True)
            return None
        result = save_receivable(self.db, request)
        if result.value is None:
            self._message(service_error_message(result.errors[0]), True)
            return None
        receivable = result.value
        message = f"Saved {receivable.payer}."
        if self.pending_expense is not None and self.editing is None:
            linked = attach_expense_split(self.db, receivable.handle, *self.pending_expense)
            if linked.value is None:
                message += f" The expense was not linked: {service_error_message(linked.errors[0])}"
            else:
                message += " The expense is linked."
            self.pending_expense = None
        self.edit(receivable.handle)
        self.refresh()
        self._message(message, False)
        return receivable

    def delete(self) -> bool:
        """Delete the loaded receivable; its transactions are untouched."""
        if self.editing is None:
            return False
        result = delete_receivable(self.db, self.editing)
        if result.value is None:
            self._message(service_error_message(result.errors[0]), True)
            return False
        self.edit(None)
        self.refresh()
        self._message("Deleted the receivable; its transactions are unchanged.", False)
        return True

    def link(self, role: str) -> bool:
        """Link the chosen cost or credit split to the loaded receivable."""
        if self.editing is None:
            return False
        picker, choices = (
            (self.cost_picker, self._costs)
            if role == "expense"
            else (self.credit_picker, self._credits)
        )
        index = picker.get_selected()
        if not 0 <= index < len(choices):
            self._message("There is no split to link.", True)
            return False
        attach = attach_expense_split if role == "expense" else attach_reimbursement_split
        result = attach(self.db, self.editing, *choices[index])
        if result.value is None:
            self._message(service_error_message(result.errors[0]), True)
            return False
        self.refresh()
        self._message(f"Linked the {role}.", False)
        return True

    def unlink(self, link) -> bool:
        if self.editing is None:
            return False
        result = detach_split(self.db, self.editing, link.transaction, link.split)
        if result.value is None:
            self._message(service_error_message(result.errors[0]), True)
            return False
        self.refresh()
        self._message("Unlinked; the transaction itself is unchanged.", False)
        return True

    def dispute(self) -> bool:
        if self.editing is None:
            return False
        try:
            when = self._date(self.dispute_date, "dispute date") or date.today()
        except ValueError as error:
            self._message(str(error), True)
            return False
        result = mark_disputed(self.db, self.editing, when, self.dispute_note.get_text())
        if result.value is None:
            self._message(service_error_message(result.errors[0]), True)
            return False
        self.refresh()
        self._message("Marked disputed.", False)
        return True

    def clear_dispute(self) -> bool:
        if self.editing is None:
            return False
        result = clear_dispute(self.db, self.editing)
        if result.value is None:
            self._message(service_error_message(result.errors[0]), True)
            return False
        self.refresh()
        self._message("Dispute cleared.", False)
        return True

    def write_off(self) -> bool:
        if self.editing is None:
            return False
        try:
            amount = self._money(self.write_off_amount, "write-off amount") or Money(0)
            when = self._date(self.write_off_date, "write-off date") or date.today()
        except ValueError as error:
            self._message(str(error), True)
            return False
        result = record_write_off(
            self.db,
            RecordWriteOff(self.editing, amount, when, self.write_off_reason.get_text()),
        )
        if result.value is None:
            self._message(service_error_message(result.errors[0]), True)
            return False
        self.edit(self.editing)
        self.refresh()
        self._message(f"Wrote off {amount.format()}; it is back in the expense account.", False)
        return True
