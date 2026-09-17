"""Enter or edit a transaction.

One dialog for both, because a transaction being corrected needs exactly the
controls a transaction being created does. It opens on a table of splits rather
than two account pickers: a two-split entry is the common case, but a transaction
that cannot be edited once it has three legs is a dead end, and the split table
collapses to the simple case without a second code path.

Two rules the dialog enforces rather than explains. The residual is shown live and
Save stays disabled until the splits sum to zero, so an impossible transaction
cannot be recorded and repaired later. And when exactly one amount is blank, it is
filled with whatever balances the rest — which is what makes the ordinary case
"pick two accounts, type one number".
"""

from __future__ import annotations

from datetime import date

from ...gen.db.sqlite import DbSQLite
from ...gen.engine import escrow, fsa_claims
from ...gen.lib import (
    InvestmentActivityKind,
    Money,
    PlanningFlowKind,
    ReconcileState,
    Split,
    Transaction,
)
from ...gen.services import (
    ClaimAttachment,
    DeleteTransaction,
    SaveTransaction,
    TransactionInput,
    TransactionSplitInput,
    build_transaction,
    delete_transaction,
    save_transaction,
)
from ...gen.utils.amount_input import parse_user_amount
from ...presentation import service_error_message
from ..gi_setup import Gtk

__all__ = ["TransactionDialog"]

_INVESTMENT_ACTIVITIES = [
    ("Ordinary", None),
    *[(kind.label, kind) for kind in InvestmentActivityKind],
]

_PLANNING_FLOWS = [
    ("Ordinary / infer from account", None),
    *[(kind.label, kind) for kind in PlanningFlowKind],
]


class SplitEditor:
    """One row of the split table."""

    def __init__(self, dialog: TransactionDialog, split: Split | None = None) -> None:
        self.dialog = dialog
        self.handle = split.handle if split else None
        self.original_account = split.account if split else None
        self.quantity = split.quantity if split else None
        self.action = split.action if split else ""
        self.reconcile = split.reconcile if split else ReconcileState.NOT_RECONCILED
        self.reconcile_date = split.reconcile_date if split else None
        self.fsa_year_start = split.fsa_year_start if split else None

        self.box = Gtk.Box(spacing=8)
        self.account = Gtk.DropDown.new_from_strings(dialog.account_names or ["(none)"])
        self.account.set_hexpand(True)
        self.account.connect("notify::selected", dialog.revalidate)
        self.box.append(self.account)

        self.memo = Gtk.Entry(placeholder_text="Memo")
        self.memo.set_size_request(140, -1)
        self.box.append(self.memo)

        self.amount = Gtk.Entry(placeholder_text="0.00", xalign=1)
        self.amount.set_size_request(110, -1)
        self.amount.add_css_class("numeric")
        self.amount.connect("changed", dialog.revalidate)
        self.box.append(self.amount)

        self.planning_purpose = Gtk.DropDown.new_from_strings(
            [label for label, _kind in _PLANNING_FLOWS]
        )
        self.planning_purpose.set_size_request(180, -1)
        self.planning_purpose.set_tooltip_text(
            "Override the planning purpose; Ordinary lets the account type and context decide"
        )
        self.planning_purpose.connect("notify::selected", dialog.revalidate)
        self.box.append(self.planning_purpose)

        self.investment_activity = Gtk.DropDown.new_from_strings(
            [label for label, _kind in _INVESTMENT_ACTIVITIES]
        )
        self.investment_activity.set_tooltip_text(
            "Classify a contribution, distribution, investment income, fee, or rollover"
        )
        self.investment_activity.connect("notify::selected", dialog.revalidate)
        self.box.append(self.investment_activity)

        self.remove = Gtk.Button(icon_name="list-remove-symbolic")
        self.remove.set_tooltip_text("Remove this split")
        self.remove.connect("clicked", lambda *_: dialog.remove_split(self))
        self.box.append(self.remove)

        if split is not None:
            for index, account in enumerate(dialog.accounts):
                if account.handle == split.account:
                    self.account.set_selected(index)
                    break
            self.memo.set_text(split.memo)
            self.amount.set_text(str(split.value.to_decimal()))
            self.planning_purpose.set_selected(
                next(
                    (
                        index
                        for index, (_label, kind) in enumerate(_PLANNING_FLOWS)
                        if kind is split.planning_flow
                    ),
                    0,
                )
            )
            self.investment_activity.set_selected(
                next(
                    (
                        index
                        for index, (_label, kind) in enumerate(_INVESTMENT_ACTIVITIES)
                        if kind is split.investment_activity
                    ),
                    0,
                )
            )

    @property
    def account_handle(self) -> str | None:
        index = self.account.get_selected()
        if 0 <= index < len(self.dialog.accounts):
            return self.dialog.accounts[index].handle
        return None

    def value(self) -> Money | None:
        """The typed amount, or None when the field is blank or unreadable."""
        text = self.amount.get_text().strip()
        if not text:
            return None
        try:
            return Money(parse_user_amount(text))
        except (ValueError, ArithmeticError):
            return None

    @property
    def is_blank(self) -> bool:
        return not self.amount.get_text().strip()

    @property
    def activity(self) -> InvestmentActivityKind | None:
        return _INVESTMENT_ACTIVITIES[self.investment_activity.get_selected()][1]

    @property
    def purpose(self) -> PlanningFlowKind | None:
        return _PLANNING_FLOWS[self.planning_purpose.get_selected()][1]


class TransactionDialog(Gtk.Window):
    """Create a transaction, or change one that already exists."""

    def __init__(
        self,
        parent: Gtk.Window | None,
        db: DbSQLite,
        default_account: str | None = None,
        default_date: date | None = None,
        transaction: Transaction | None = None,
    ) -> None:
        editing = transaction is not None
        super().__init__(
            title="Edit transaction" if transaction is not None else "New transaction",
            transient_for=parent,
            modal=True,
        )
        self.db = db
        self.transaction = transaction
        self.editing = editing
        self.set_default_size(640, 460)

        referenced = {split.account for split in transaction.splits} if transaction else set()
        self.accounts = sorted(
            (
                account
                for account in db.iter_accounts()
                if not account.is_root
                and not account.placeholder
                and (not account.hidden or account.handle in referenced)
            ),
            key=db.full_name,
        )
        self.account_names = [
            f"{db.full_name(account)} (hidden)" if account.hidden else db.full_name(account)
            for account in self.accounts
        ]
        self.splits: list[SplitEditor] = []

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(18)
        self.set_child(box)

        header = Gtk.Grid(column_spacing=10, row_spacing=8)
        box.append(header)

        when = (transaction.post_date if transaction is not None else default_date) or date.today()
        self.date_entry = Gtk.Entry(text=when.isoformat())
        self.date_entry.connect("changed", self.revalidate)
        header.attach(Gtk.Label(label="Date", xalign=0), 0, 0, 1, 1)
        header.attach(self.date_entry, 1, 0, 1, 1)

        self.description_entry = Gtk.Entry(placeholder_text="What was it for")
        self.description_entry.set_hexpand(True)
        if transaction is not None:
            self.description_entry.set_text(transaction.description)
        header.attach(Gtk.Label(label="Description", xalign=0), 0, 1, 1, 1)
        header.attach(self.description_entry, 1, 1, 1, 1)

        self.num_entry = Gtk.Entry(placeholder_text="Cheque or reference")
        if transaction is not None:
            self.num_entry.set_text(transaction.num)
        header.attach(Gtk.Label(label="Number", xalign=0), 0, 2, 1, 1)
        header.attach(self.num_entry, 1, 2, 1, 1)

        self.notes_view = Gtk.TextView()
        self.notes_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.notes_view.set_size_request(-1, 64)
        if transaction is not None and transaction.notes:
            self.notes_view.get_buffer().set_text(transaction.notes)
        notes_scroll = Gtk.ScrolledWindow(child=self.notes_view)
        notes_scroll.set_min_content_height(64)
        header.attach(Gtk.Label(label="BreadSched notes", xalign=0), 0, 3, 1, 1)
        header.attach(notes_scroll, 1, 3, 1, 1)

        if transaction is not None and transaction.source_notes:
            imported_notes = Gtk.Label(
                label=transaction.source_notes,
                xalign=0,
                wrap=True,
                selectable=True,
            )
            imported_notes.add_css_class("dim")
            imported_notes.set_tooltip_text(
                "Read-only notes from the import source; refreshed on re-import"
            )
            header.attach(Gtk.Label(label="Imported notes", xalign=0), 0, 4, 1, 1)
            header.attach(imported_notes, 1, 4, 1, 1)

        self.escrow_treatment: Gtk.Label | None = None
        if transaction is not None:
            accounts = {account.handle: account for account in db.iter_accounts()}
            explanation = escrow.recognition(
                ((split.account, split.value) for split in transaction.splits), accounts
            ).explanations(accounts)
            if explanation:
                self.escrow_treatment = Gtk.Label(
                    label="\n".join(explanation),
                    xalign=0,
                    wrap=True,
                    selectable=True,
                )
                header.attach(Gtk.Label(label="Escrow treatment", xalign=0), 0, 5, 1, 1)
                header.attach(self.escrow_treatment, 1, 5, 1, 1)

        caption = Gtk.Box(spacing=8)
        splits_label = Gtk.Label(label="Splits", xalign=0)
        splits_label.add_css_class("total-row")
        splits_label.set_hexpand(True)
        caption.append(splits_label)
        add_button = Gtk.Button(label="Add split", icon_name="list-add-symbolic")
        add_button.connect("clicked", lambda *_: self.add_split())
        caption.append(add_button)
        box.append(caption)

        self.split_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        scroller = Gtk.ScrolledWindow(child=self.split_box)
        scroller.set_vexpand(True)
        box.append(scroller)

        self.fsa_claims = [
            claim
            for claim in fsa_claims.iter_claims(db)
            if fsa_claims.claim_summary(db, claim).status
            is not fsa_claims.FsaClaimStatus.FULLY_REIMBURSED
        ]
        self.fsa_claim = None
        self.fsa_role = None
        if self.fsa_claims and not editing:
            fsa_row = Gtk.Box(spacing=8)
            fsa_row.append(Gtk.Label(label="FSA claim (optional)", xalign=0))
            self.fsa_claim = Gtk.DropDown.new_from_strings(
                ["Do not attach"]
                + [
                    f"{claim.service_date} {claim.provider or claim.description or 'FSA claim'}"
                    for claim in self.fsa_claims
                ]
            )
            fsa_row.append(self.fsa_claim)
            self.fsa_role = Gtk.DropDown.new_from_strings(
                ["Healthcare payment", "Provider refund", "FSA reimbursement"]
            )
            fsa_row.append(self.fsa_role)
            box.append(fsa_row)

        self.status = Gtk.Label(xalign=0)
        box.append(self.status)

        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        if transaction is not None:
            schedule_button = Gtk.Button(label="Make scheduled…")
            schedule_button.set_tooltip_text(
                "Create a new scheduled transaction draft from every split"
            )
            schedule_button.connect("clicked", self._on_make_scheduled)
            buttons.append(schedule_button)
            delete = Gtk.Button(label="Delete")
            delete.add_css_class("destructive-action")
            delete.connect("clicked", self._on_delete)
            buttons.append(delete)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        buttons.append(cancel)
        self.save_button = Gtk.Button(label="Save")
        self.save_button.add_css_class("suggested-action")
        self.save_button.set_sensitive(False)
        self.save_button.connect("clicked", self._on_save)
        buttons.append(self.save_button)
        box.append(buttons)

        if transaction is not None:
            for split in transaction.splits:
                self.add_split(split)
        else:
            first = self.add_split()
            second = self.add_split()
            # Both rows would otherwise open on the same account, which is never a
            # valid transaction and makes a fresh dialog look broken.
            here = 0
            if default_account:
                here = next(
                    (
                        index
                        for index, account in enumerate(self.accounts)
                        if account.handle == default_account
                    ),
                    0,
                )
            second.account.set_selected(here)
            first.account.set_selected(1 if here == 0 and len(self.accounts) > 1 else 0)
        self.revalidate()

    def _on_make_scheduled(self, _button) -> None:
        if self.transaction is None:
            return
        from ...gen.engine import schedule
        from .schedule_dialog import ScheduleDialog

        draft = schedule.from_transaction(self.transaction)
        dialog = ScheduleDialog(self, self.db, source=draft, creating=True)
        dialog.present()

    # ------------------------------------------------------------------ splits

    def add_split(self, split: Split | None = None) -> SplitEditor:
        editor = SplitEditor(self, split)
        self.splits.append(editor)
        self.split_box.append(editor.box)
        self.revalidate()
        return editor

    def remove_split(self, editor: SplitEditor) -> None:
        if len(self.splits) <= 2:
            # Two legs is the floor: fewer cannot describe a movement of money.
            self.status.set_text("A transaction needs at least two splits")
            return
        self.splits.remove(editor)
        self.split_box.remove(editor.box)
        self.revalidate()

    # -------------------------------------------------------------- validation

    def residual(self) -> Money:
        """How far the typed amounts are from balancing, blanks counting as zero."""
        total = Money(0)
        for editor in self.splits:
            value = editor.value()
            if value is not None:
                total = total + value
        return total

    def revalidate(self, *_args) -> None:
        problems: list[str] = []

        if len({e.account_handle for e in self.splits}) < 2:
            problems.append("use at least two different accounts")
        if any(e.value() is None and not e.is_blank for e in self.splits):
            problems.append("check the amounts")
        try:
            date.fromisoformat(self.date_entry.get_text().strip())
        except ValueError:
            problems.append("check the date (YYYY-MM-DD)")

        blanks = [e for e in self.splits if e.is_blank]
        residual = self.residual()

        if problems:
            self.status.set_text("; ".join(problems).capitalize())
            self.save_button.set_sensitive(False)
            return

        if len(blanks) == 1:
            # One blank leg is not an error: it is the other side of the entry, and
            # its value is determined by the rest.
            self.status.set_text(
                f"The blank split will be set to {(-residual).format(parens_negative=True)}"
            )
            self.save_button.set_sensitive(bool(residual))
            return
        if blanks:
            self.status.set_text("Fill in the amounts")
            self.save_button.set_sensitive(False)
            return
        if residual:
            self.status.set_text(f"Out of balance by {residual.format(parens_negative=True)}")
            self.status.add_css_class("negative")
            self.save_button.set_sensitive(False)
            return

        self.status.remove_css_class("negative")
        self.status.set_text("Balanced")
        self.save_button.set_sensitive(True)

    # ------------------------------------------------------------------ saving

    def _request(self) -> SaveTransaction:
        """Translate the form into the shared typed service contract."""
        when = date.fromisoformat(self.date_entry.get_text().strip())
        residual = self.residual()
        notes_start, notes_end = self.notes_view.get_buffer().get_bounds()
        splits: list[TransactionSplitInput] = []
        for editor in self.splits:
            value = editor.value()
            if value is None:
                value = -residual
            account_handle = editor.account_handle
            if account_handle is None:
                raise ValueError("Choose an account for every split")
            splits.append(
                TransactionSplitInput(
                    account=account_handle,
                    value=value,
                    handle=editor.handle,
                    memo=editor.memo.get_text().strip(),
                    planning_flow=editor.purpose,
                    investment_activity=editor.activity,
                )
            )
        attachment = None
        if (
            self.fsa_claim is not None
            and self.fsa_role is not None
            and self.fsa_claim.get_selected() > 0
        ):
            claim = self.fsa_claims[self.fsa_claim.get_selected() - 1]
            roles = ("payment", "refund", "reimbursement")
            attachment = ClaimAttachment(claim.handle, roles[self.fsa_role.get_selected()])
        return SaveTransaction(
            TransactionInput(
                post_date=when,
                description=self.description_entry.get_text().strip() or "(no description)",
                num=self.num_entry.get_text().strip(),
                notes=self.notes_view.get_buffer().get_text(notes_start, notes_end, True).strip(),
                splits=tuple(splits),
            ),
            existing_handle=self.transaction.handle if self.transaction is not None else None,
            claim_attachment=attachment,
            source=self.transaction,
        )

    def build(self) -> Transaction:
        """Return the service-built preview used by editor interactions and tests."""
        result = build_transaction(self.db, self._request())
        if result.value is None:
            raise ValueError(service_error_message(result.errors[0]))
        return result.value

    def _on_save(self, _button) -> None:
        try:
            request = self._request()
        except ValueError as exc:
            self.status.set_text(str(exc))
            return
        result = save_transaction(self.db, request)
        if result.value is None:
            self.status.set_text(service_error_message(result.errors[0]))
            self.status.add_css_class("negative")
            return
        self.close()

    def _on_delete(self, _button) -> None:
        if not self.editing or self.transaction is None:
            return
        result = delete_transaction(self.db, DeleteTransaction(self.transaction.handle))
        if not result.ok:
            self.status.set_text(service_error_message(result.errors[0]))
            self.status.add_css_class("negative")
            return
        self.close()
