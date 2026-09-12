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
from ...gen.engine import fsa_claims
from ...gen.lib import Money, ReconcileState, Split, Transaction, UnbalancedError
from ...gen.utils.amount_input import parse_user_amount
from ..gi_setup import Gtk

__all__ = ["TransactionDialog"]


class SplitEditor:
    """One row of the split table."""

    def __init__(self, dialog: TransactionDialog, split: Split | None = None) -> None:
        self.dialog = dialog
        self.handle = split.handle if split else None
        self.reconcile = split.reconcile if split else ReconcileState.NOT_RECONCILED

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
            self.amount.set_text(f"{split.value.to_decimal():.2f}")

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
            title="Edit transaction" if editing else "New transaction",
            transient_for=parent,
            modal=True,
        )
        self.db = db
        self.transaction = transaction
        self.editing = editing
        self.set_default_size(640, 460)

        self.accounts = sorted(
            (a for a in db.iter_accounts() if not a.is_root and not a.placeholder),
            key=db.full_name,
        )
        self.account_names = [db.full_name(a) for a in self.accounts]
        self.splits: list[SplitEditor] = []

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(18)
        self.set_child(box)

        header = Gtk.Grid(column_spacing=10, row_spacing=8)
        box.append(header)

        when = (transaction.post_date if editing else default_date) or date.today()
        self.date_entry = Gtk.Entry(text=when.isoformat())
        self.date_entry.connect("changed", self.revalidate)
        header.attach(Gtk.Label(label="Date", xalign=0), 0, 0, 1, 1)
        header.attach(self.date_entry, 1, 0, 1, 1)

        self.description_entry = Gtk.Entry(placeholder_text="What was it for")
        self.description_entry.set_hexpand(True)
        if editing:
            self.description_entry.set_text(transaction.description)
        header.attach(Gtk.Label(label="Description", xalign=0), 0, 1, 1, 1)
        header.attach(self.description_entry, 1, 1, 1, 1)

        self.num_entry = Gtk.Entry(placeholder_text="Cheque or reference")
        if editing:
            self.num_entry.set_text(transaction.num)
        header.attach(Gtk.Label(label="Number", xalign=0), 0, 2, 1, 1)
        header.attach(self.num_entry, 1, 2, 1, 1)

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
            claim for claim in fsa_claims.iter_claims(db)
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
        if editing:
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

        if editing:
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
                        index for index, account in enumerate(self.accounts)
                        if account.handle == default_account
                    ),
                    0,
                )
            second.account.set_selected(here)
            first.account.set_selected(1 if here == 0 and len(self.accounts) > 1 else 0)
        self.revalidate()

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
        if any(
            e.value() is None and not e.is_blank for e in self.splits
        ):
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
            self.status.set_text(
                f"Out of balance by {residual.format(parens_negative=True)}"
            )
            self.status.add_css_class("negative")
            self.save_button.set_sensitive(False)
            return

        self.status.remove_css_class("negative")
        self.status.set_text("Balanced")
        self.save_button.set_sensitive(True)

    # ------------------------------------------------------------------ saving

    def build(self) -> Transaction:
        """The transaction the form describes, balanced."""
        when = date.fromisoformat(self.date_entry.get_text().strip())
        residual = self.residual()

        target = self.transaction if self.editing else Transaction()
        target.post_date = when
        target.description = (
            self.description_entry.get_text().strip() or "(no description)"
        )
        target.num = self.num_entry.get_text().strip()

        splits: list[Split] = []
        for editor in self.splits:
            value = editor.value()
            if value is None:
                value = -residual
            splits.append(
                Split(
                    account=editor.account_handle,
                    value=value,
                    memo=editor.memo.get_text().strip(),
                    reconcile=editor.reconcile,
                    handle=editor.handle,
                )
            )
        target.splits = splits
        return target

    def _on_save(self, _button) -> None:
        try:
            target = self.build()
        except ValueError as exc:
            self.status.set_text(str(exc))
            return
        try:
            with self.db.transaction(
                f"{'Edit' if self.editing else 'Add'} {target.description}"
            ) as txn:
                if self.editing:
                    self.db.commit_transaction(target, txn)
                else:
                    self.db.add_transaction(target, txn)
        except UnbalancedError as exc:  # pragma: no cover - guarded above
            self.status.set_text(str(exc))
            self.status.add_css_class("negative")
            return
        if self.fsa_claim is not None and self.fsa_claim.get_selected() > 0:
            claim = self.fsa_claims[self.fsa_claim.get_selected() - 1]
            roles = ("payment", "refund", "reimbursement")
            role = roles[self.fsa_role.get_selected()]
            try:
                fsa_claims.attach_transaction_to_claim(
                    self.db, claim.handle, target.handle, role=role
                )
            except (KeyError, ValueError) as exc:
                self.transaction = target
                self.editing = True
                self.status.set_text(f"Transaction saved; FSA claim not attached: {exc}")
                self.status.add_css_class("negative")
                return
        self.close()

    def _on_delete(self, _button) -> None:
        if not self.editing or self.transaction is None:
            return
        with self.db.transaction(f"Delete {self.transaction.description}") as txn:
            self.db.remove_transaction(self.transaction.handle, txn)
        self.close()
