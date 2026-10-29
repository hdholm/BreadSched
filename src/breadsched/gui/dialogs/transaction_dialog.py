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

A new transaction whose splits are still untouched is proposed from the latest
earlier entry with the same description (``services.autocomplete``) when the
description is left. The proposal only fills the form,
with a visible note; nothing is saved until the user chooses Save, typed splits
are never replaced, and a transaction being edited is never rewritten.

A split in an account that holds a security also shows its shares, saved as the
split's quantity; shares left as loaded keep the stored quantity exactly (#323).

Tags are saved with the transaction. Linked documents are changed at once through
``services.attachments`` (each change is its own undoable step) and only on a
transaction that has been saved; a missing file is marked, never removed.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from ...gen.db.sqlite import DbSQLite
from ...gen.engine import attachments as attachment_engine
from ...gen.engine import escrow, fsa_claims, ledger
from ...gen.engine.cost_basis import shares_text
from ...gen.engine.entry_input import parse_amount_text
from ...gen.lib import (
    Amount,
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
    attach_file,
    attach_location,
    build_transaction,
    delete_transaction,
    detach,
    relink,
    save_transaction,
    transaction_currency,
)
from ...gen.services.autocomplete import EntrySuggestion, SuggestEntry, suggest_entry
from ...gen.utils.amount_input import parse_user_amount
from ...presentation import claim_role_label, service_error_message
from .. import hidden_accounts
from ..gi_setup import Gio, GLib, Gtk
from ..widgets.bounded import BoundedWindow, keeps_enter
from ..widgets.choice import bounded_dropdown
from ..widgets.confirm import delete_button
from ..widgets.date_entry import attach_date_keys, parse_date_text
from ..widgets.form_pages import FormPages
from ..widgets.help import help_row

__all__ = ["MORE", "TransactionDialog"]

#: The second page: tags, documents, and escrow detail; its tab counts them (#383).
MORE = "Tags & documents"

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

        #: Whether the dialog added this row to hold the rest of an unbalanced
        #: entry, so it can take it away again once the entry balances (#369).
        self.auto: bool = False
        self.box = Gtk.Box(spacing=8)
        self.account = bounded_dropdown(dialog.account_names or ["(none)"])
        self.account.set_hexpand(True)
        self.account.connect("notify::selected", dialog.revalidate)
        self.account.connect("notify::selected", lambda *_: self._show_shares())
        self.account.connect("notify::selected", lambda *_: self._label_columns())
        # Choosing an account for a row that holds the rest keeps the row.
        self.account.connect(
            "notify::selected",
            lambda *_: setattr(self, "auto", self.auto and self.account_handle is None),
        )
        self.box.append(self.account)

        self.memo = Gtk.Entry(placeholder_text="Memo")
        self.memo.set_size_request(140, -1)
        self.box.append(self.memo)

        # Two amount columns, as in the register (#369): each named for this row's
        # account (Payment/Charge, Deposit/Withdrawal, Expense/Rebate), so which
        # way money moves is never a matter of the sign. Typing in one clears the
        # other; either may be arithmetic.
        self.increase = Gtk.Entry(xalign=1)
        self.decrease = Gtk.Entry(xalign=1)
        for entry, other in ((self.increase, self.decrease), (self.decrease, self.increase)):
            entry.set_size_request(100, -1)
            entry.add_css_class("numeric")
            entry.connect("changed", self._amount_changed, other)
            self.box.append(entry)

        # Shares, for a split in an account that holds a security (#323).
        self.shares = Gtk.Entry(placeholder_text="Shares", xalign=1)
        self.shares.set_size_request(100, -1)
        self.shares.add_css_class("numeric")
        self.shares.set_tooltip_text(
            "Shares this split adds; negative for shares sold or moved out"
        )
        self.shares.update_property([Gtk.AccessibleProperty.LABEL], ["Shares"])
        self.shares.connect("changed", dialog.revalidate)
        self.box.append(self.shares)
        self._loaded_shares = ""

        self.planning_purpose = bounded_dropdown([label for label, _kind in _PLANNING_FLOWS])
        self.planning_purpose.set_size_request(180, -1)
        self.planning_purpose.set_tooltip_text(
            "Override the planning purpose; Ordinary lets the account type and context decide"
        )
        self.planning_purpose.connect("notify::selected", dialog.revalidate)
        self.planning_purpose.connect("notify::selected", lambda *_: self.show_detail())
        self.box.append(self.planning_purpose)

        self.investment_activity = bounded_dropdown(
            [label for label, _kind in _INVESTMENT_ACTIVITIES]
        )
        self.investment_activity.set_tooltip_text(
            "Classify a contribution, distribution, investment income, fee, or rollover"
        )
        self.investment_activity.connect("notify::selected", dialog.revalidate)
        self.investment_activity.connect("notify::selected", lambda *_: self.show_detail())
        self.box.append(self.investment_activity)
        self.account.connect("notify::selected", lambda *_: self.show_detail())

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
            self.set_value(split.value)
            if dialog.holds_security(split.account):
                self._loaded_shares = shares_text(split.quantity)
                self.shares.set_text(self._loaded_shares)
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

        self._show_shares()
        self._label_columns()
        self.show_detail()

    def show_detail(self) -> None:
        """Show planning purpose and investment activity only where they apply (#383).

        Planning purpose shows with the dialog's **Planning detail**; investment
        activity also needs an investment or retirement account. Either shows
        whenever it already holds a choice, so nothing set is hidden.
        """
        detail = self.dialog.show_planning_detail
        handle = self.account_handle
        account = self.dialog.db.get_account(handle) if handle is not None else None
        investing = account is not None and account.atype.is_investment
        self.planning_purpose.set_visible(detail or self.planning_purpose.get_selected() != 0)
        self.investment_activity.set_visible(
            (detail and investing) or self.investment_activity.get_selected() != 0
        )

    def _amount_changed(self, entry: Gtk.Entry, other: Gtk.Entry) -> None:
        if entry.get_text().strip() and other.get_text():
            other.set_text("")
        self.auto = False
        self.dialog.revalidate()

    def _label_columns(self) -> None:
        """Name the two amount columns for this row's account, as its register does."""
        handle = self.account_handle
        account = self.dialog.db.get_account(handle) if handle is not None else None
        up, down = ledger.register_headings(account.atype) if account is not None else ("In", "Out")
        self._labels = (up, down)
        for entry, label in ((self.increase, up), (self.decrease, down)):
            entry.update_property([Gtk.AccessibleProperty.LABEL], [label])
        self.show_rest(None)

    def show_rest(self, rest: Money | None) -> None:
        """Show what this blank row would take to balance the entry, or the labels."""
        up, down = getattr(self, "_labels", ("In", "Out"))
        self.increase.set_placeholder_text(up)
        self.decrease.set_placeholder_text(down)
        if rest is not None and rest:
            target = self.increase if rest > 0 else self.decrease
            target.set_placeholder_text(abs(rest).format())

    def set_value(self, value: Money | None) -> None:
        """Show a signed value in its column: positive as an increase (a debit)."""
        if value is None:
            self.increase.set_text("")
            self.decrease.set_text("")
        elif value >= 0:
            self.increase.set_text(str(value.to_decimal()))
        else:
            self.decrease.set_text(str((-value).to_decimal()))

    def set_amount_text(self, text: str) -> None:
        """Type a signed amount: a leading minus puts it in the decrease column."""
        text = text.strip()
        if text.startswith("-"):
            self.increase.set_text("")
            self.decrease.set_text(text[1:])
        else:
            self.decrease.set_text("")
            self.increase.set_text(text)

    def _show_shares(self) -> None:
        self.shares.set_visible(self.dialog.holds_security(self.account_handle))

    def quantity_amount(self) -> Amount | None:
        """The typed shares as an amount of the account's security; None when not given.

        Shares left as loaded keep the stored quantity exactly. ``ValueError`` for
        shares that cannot be read.
        """
        handle = self.account_handle
        if handle is None or not self.dialog.holds_security(handle):
            return None
        text = self.shares.get_text().strip()
        if not text:
            return None
        account = self.dialog.db.get_account(handle)
        assert account is not None and account.commodity
        if text == self._loaded_shares and self.quantity is not None:
            return Amount(self.quantity, account.commodity)
        try:
            return Amount(Money(parse_user_amount(text)), account.commodity)
        except (ValueError, ArithmeticError):
            raise ValueError("check the shares") from None

    @property
    def account_handle(self) -> str | None:
        index = self.account.get_selected()
        if 0 <= index < len(self.dialog.accounts):
            return self.dialog.accounts[index].handle
        return None

    def value(self) -> Money | None:
        """The signed amount (an increase positive), or None when blank or unreadable."""
        for entry, sign in ((self.increase, 1), (self.decrease, -1)):
            text = entry.get_text().strip()
            if not text:
                continue
            try:
                amount = Money(parse_amount_text(text))
            except (ValueError, ArithmeticError):
                return None
            return amount if sign > 0 else -amount
        return None

    @property
    def is_blank(self) -> bool:
        return not self.increase.get_text().strip() and not self.decrease.get_text().strip()

    @property
    def activity(self) -> InvestmentActivityKind | None:
        return _INVESTMENT_ACTIVITIES[self.investment_activity.get_selected()][1]

    @property
    def purpose(self) -> PlanningFlowKind | None:
        return _PLANNING_FLOWS[self.planning_purpose.get_selected()][1]


class TransactionDialog(BoundedWindow):
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
        self.default_account = default_account
        #: True once Save has written the transaction, so openers can tell a
        #: saved close from a cancelled one.
        self.saved = False
        self.set_default_size(640, 460)

        referenced = {split.account for split in transaction.splits} if transaction else set()
        self.accounts = sorted(
            (
                account
                for account in db.iter_accounts()
                if not account.is_root
                and not account.placeholder
                and (not account.hidden or hidden_accounts.shown() or account.handle in referenced)
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
        box.prepend(help_row("transactions"))

        self.show_planning_detail = False
        # The entry itself first; tags, documents, and escrow detail a tab away (#383).
        self.pages = FormPages()
        box.append(self.pages)
        header = self.pages.page("Entry")
        more = self.pages.page(MORE)

        when = (transaction.post_date if transaction is not None else default_date) or date.today()
        self.date_entry = attach_date_keys(Gtk.Entry(text=when.isoformat()))
        self.date_entry.connect("changed", self.revalidate)
        header.add("Date", self.date_entry)

        self.description_entry = Gtk.Entry(placeholder_text="What was it for")
        self.description_entry.set_hexpand(True)
        if transaction is not None:
            self.description_entry.set_text(transaction.description)
        header.add("Description", self.description_entry)
        if transaction is None:
            # Leaving the description proposes the latest matching entry's splits.
            keeps_enter(self.description_entry)
            self.description_entry.connect("activate", lambda *_: self.propose_from_entry())
            leave = Gtk.EventControllerFocus()
            leave.connect("leave", lambda *_: self.propose_from_entry())
            self.description_entry.add_controller(leave)

        self.num_entry = Gtk.Entry(placeholder_text="Cheque or reference")
        if transaction is not None:
            self.num_entry.set_text(transaction.num)
        header.add("Number", self.num_entry)

        self.notes_view = Gtk.TextView()
        self.notes_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.notes_view.set_size_request(-1, 64)
        if transaction is not None and transaction.notes:
            self.notes_view.get_buffer().set_text(transaction.notes)
        notes_scroll = Gtk.ScrolledWindow(child=self.notes_view)
        notes_scroll.set_min_content_height(64)
        # One note, imported ones included (#385).
        header.add("Notes", notes_scroll)

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
                more.add("Escrow treatment", self.escrow_treatment)

        self.tags_entry = Gtk.Entry(placeholder_text="Comma-separated, e.g. Tax, Home repair")
        if transaction is not None:
            self.tags_entry.set_text(", ".join(transaction.tags))
        self.tags_entry.set_tooltip_text(
            "BreadSched's own labels; kept when a GnuCash book is imported again"
        )
        self.tags_entry.connect("changed", lambda *_: self._count_more())
        more.add("Tags", self.tags_entry)

        self.documents = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        more.add("Documents", self.documents)
        #: (status, row) for each linked document shown, in order.
        self.document_rows: list[tuple[attachment_engine.AttachmentStatus, Gtk.Widget]] = []
        self.link_entry: Gtk.Entry | None = None
        self._show_documents()
        self._count_more()

        caption = Gtk.Box(spacing=8)
        splits_label = Gtk.Label(label="Splits", xalign=0)
        splits_label.add_css_class("total-row")
        splits_label.set_hexpand(True)
        caption.append(splits_label)
        # Planning purpose and investment activity, per split, only when wanted.
        self.detail_toggle = Gtk.ToggleButton(label="Planning detail")
        self.detail_toggle.set_tooltip_text(
            "Show each split's planning purpose, and investment activity for "
            "investment and retirement accounts"
        )
        self.detail_toggle.connect("toggled", self._on_detail_toggled)
        caption.append(self.detail_toggle)
        add_button = Gtk.Button(label="Add split", icon_name="list-add-symbolic")
        add_button.connect("clicked", lambda *_: self.add_split())
        caption.append(add_button)
        box.append(caption)

        self.proposal_note = Gtk.Label(xalign=0, wrap=True, visible=False)
        self.proposal_note.add_css_class("dim")
        box.append(self.proposal_note)

        # Each split row names its two amount columns for its own account (#369).
        self.split_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        scroller = Gtk.ScrolledWindow(child=self.split_box)
        scroller.set_vexpand(True)
        box.append(scroller)

        self.fsa_claims = [
            claim
            for claim in fsa_claims.iter_claims(db)
            if not fsa_claims.claim_summary(db, claim).status.settled
        ]
        self.fsa_claim = None
        self.fsa_role = None
        if self.fsa_claims and not editing:
            fsa_row = Gtk.Box(spacing=8)
            fsa_row.append(Gtk.Label(label="FSA claim (optional)", xalign=0))
            self.fsa_claim = bounded_dropdown(
                ["Do not attach"]
                + [
                    f"{claim.service_date} {claim.provider or claim.description or 'FSA claim'}"
                    for claim in self.fsa_claims
                ]
            )
            fsa_row.append(self.fsa_claim)
            self.fsa_role = bounded_dropdown(
                [claim_role_label(role) for role in fsa_claims.ATTACHMENT_ROLES]
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
            delete = delete_button(
                f"the transaction {transaction.description}",
                lambda: self._on_delete(None),
            )
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

    # --------------------------------------------------------------- documents

    def _show_documents(self) -> None:
        while (child := self.documents.get_first_child()) is not None:
            self.documents.remove(child)
        self.document_rows = []
        self.link_entry = None
        if self.transaction is None:
            note = Gtk.Label(label="Save the transaction to link documents.", xalign=0)
            note.add_css_class("dim")
            self.documents.append(note)
            return
        for item in attachment_engine.statuses(self.db, self.transaction):
            row = Gtk.Box(spacing=6)
            label = Gtk.Label(
                label=item.location + (" — missing" if item.missing else ""),
                xalign=0,
                selectable=True,
                wrap=True,
            )
            label.set_hexpand(True)
            if item.missing:
                label.add_css_class("negative")
                label.set_tooltip_text(
                    f"Not found at {item.path}. Restore the file, or relink it."
                    if item.path is not None
                    else "Not found"
                )
            elif item.owner == "source":
                label.set_tooltip_text("Linked in GnuCash; refreshed on re-import")
            if item.owner == "source":
                label.add_css_class("dim")
            row.append(label)
            open_button = Gtk.Button(label="Open")
            open_button.set_sensitive(not item.missing)
            open_button.connect("clicked", lambda _b, status=item: self.open_document(status))
            row.append(open_button)
            if item.owner == "breadsched":
                again = Gtk.Button(label="Relink…")
                again.connect("clicked", lambda _b, status=item: self._choose_relink(status))
                row.append(again)
                remove = Gtk.Button(icon_name="list-remove-symbolic")
                remove.set_tooltip_text("Unlink; the file itself is kept")
                remove.connect(
                    "clicked", lambda _b, status=item: self.remove_document(status.location)
                )
                row.append(remove)
            self.documents.append(row)
            self.document_rows.append((item, row))
        folder = attachment_engine.attachment_folder(self.db)
        controls = Gtk.Box(spacing=6)
        attach = Gtk.Button(label="Attach file…")
        attach.set_tooltip_text(
            f"Copy a file into {folder} and link it" if folder else "Link a file where it is"
        )
        attach.connect("clicked", self._choose_attachment)
        controls.append(attach)
        address = Gtk.Entry(placeholder_text="https://…")
        address.set_hexpand(True)
        keeps_enter(address)
        address.connect("activate", lambda entry: self.link_location(entry.get_text()))
        controls.append(address)
        self.link_entry = address
        link = Gtk.Button(label="Link address")
        link.connect("clicked", lambda *_: self.link_location(address.get_text()))
        controls.append(link)
        self.documents.append(controls)
        self._count_more()

    def _document_changed(self, result) -> bool:
        if result.value is None:
            self.status.set_text(service_error_message(result.errors[0]))
            self.status.add_css_class("negative")
            return False
        # Later saves build on the stored transaction, including its documents.
        self.transaction = result.value
        self._show_documents()
        return True

    def attach_path(self, path: Path) -> bool:
        """Copy a file into the attachment folder (or link it in place) and link it."""
        if self.transaction is None:
            return False
        copy = attachment_engine.attachment_folder(self.db) is not None
        return self._document_changed(
            attach_file(self.db, self.transaction.handle, path, copy=copy)
        )

    def link_location(self, location: str) -> bool:
        if self.transaction is None:
            return False
        return self._document_changed(attach_location(self.db, self.transaction.handle, location))

    def remove_document(self, location: str) -> bool:
        if self.transaction is None:
            return False
        return self._document_changed(detach(self.db, self.transaction.handle, location))

    def relink_document(self, location: str, path: Path) -> bool:
        if self.transaction is None:
            return False
        new = attachment_engine.location_for(self.db, path)
        return self._document_changed(relink(self.db, self.transaction.handle, location, new))

    def open_document(self, item: attachment_engine.AttachmentStatus) -> str | None:
        """Open a document with the desktop's default application."""
        if item.kind == "web":
            uri = item.location
        elif item.path is not None and item.path.is_file():
            uri = item.path.as_uri()
        else:
            self.status.set_text(f"{item.location} cannot be found")
            self.status.add_css_class("negative")
            return None
        try:
            Gio.AppInfo.launch_default_for_uri(uri, None)
        except GLib.Error as exc:
            self.status.set_text(f"Could not open {item.location}: {exc.message}")
            self.status.add_css_class("negative")
        return uri

    def _choose_attachment(self, _button) -> None:
        dialog = Gtk.FileDialog(title="Attach a document")

        def chosen(dialog, result) -> None:
            try:
                file = dialog.open_finish(result)
            except GLib.Error:
                return
            if file.get_path():
                self.attach_path(Path(file.get_path()))

        dialog.open(self, None, chosen)

    def _choose_relink(self, item: attachment_engine.AttachmentStatus) -> None:
        dialog = Gtk.FileDialog(title=f"Where is {item.location} now?")

        def chosen(dialog, result) -> None:
            try:
                file = dialog.open_finish(result)
            except GLib.Error:
                return
            if file.get_path():
                self.relink_document(item.location, Path(file.get_path()))

        dialog.open(self, None, chosen)

    def _on_make_scheduled(self, _button) -> None:
        if self.transaction is None:
            return
        from ...gen.engine import schedule
        from .schedule_dialog import ScheduleDialog

        draft = schedule.from_transaction(self.transaction)
        dialog = ScheduleDialog(self, self.db, source=draft, creating=True)
        dialog.present()

    def prefill(
        self,
        *,
        description: str = "",
        num: str = "",
        transfer: str | None = None,
        amount: Money | None = None,
        splits: list[tuple[str | None, Money | None, str]] | None = None,
    ) -> None:
        """Start a new transaction from a register's blank entry row (#158).

        ``amount`` is signed as it moves the register account; the transfer split
        balances it. With no amount the splits stay blank, so the ordinary proposal
        from the description can still fill them. ``splits`` instead gives
        (account, value, memo) for each split.
        """
        if self.transaction is not None:
            return
        self.description_entry.set_text(description)
        self.num_entry.set_text(num)
        accounts = [account.handle for account in self.accounts]
        if splits:
            # In-place split lines (#158 slice 2) carry over one editor row each.
            while len(self.splits) < len(splits):
                self.add_split()
            for editor, (account, value, memo) in zip(self.splits, splits, strict=False):
                if account in accounts:
                    editor.account.set_selected(accounts.index(account))
                editor.memo.set_text(memo)
                editor.set_value(value)
            self.revalidate()
            return
        other, here = self.splits[0], self.splits[1]
        if transfer in accounts:
            other.account.set_selected(accounts.index(transfer))
        if amount is not None:
            here.set_value(amount)
            other.set_value(-amount)
        elif description:
            self.propose_from_entry()
        self.revalidate()

    # ---------------------------------------------------------------- proposal

    def propose_from_entry(self) -> EntrySuggestion | None:
        """Fill untouched splits of a new transaction from the latest matching entry."""
        if self.transaction is not None:
            return None  # an existing transaction is never rewritten
        if any(not e.is_blank or e.memo.get_text().strip() for e in self.splits):
            return None  # never overwrite what the user typed
        result = suggest_entry(
            self.db,
            SuggestEntry(
                description=self.description_entry.get_text(),
                account=self.default_account,
                currency=transaction_currency(self.db),
            ),
        )
        suggestion = result.value.suggestion if result.value is not None else None
        if suggestion is None or len(suggestion.splits) < 2:
            return None
        handles = [account.handle for account in self.accounts]
        if any(split.account not in handles for split in suggestion.splits):
            return None
        while len(self.splits) > len(suggestion.splits):
            self.split_box.remove(self.splits.pop().box)
        while len(self.splits) < len(suggestion.splits):
            self.add_split()
        for editor, split in zip(self.splits, suggestion.splits, strict=True):
            editor.account.set_selected(handles.index(split.account))
            editor.memo.set_text(split.memo)
            editor.set_value(split.value)
        notes = self.notes_view.get_buffer()
        if (
            suggestion.notes.strip()
            and not notes.get_text(notes.get_start_iter(), notes.get_end_iter(), False).strip()
        ):
            notes.set_text(suggestion.notes)  # #363: only notes left empty
        self.proposal_note.set_text(
            f"Proposed from {suggestion.when.isoformat()} “{suggestion.description}”. "
            "Edit anything before saving."
        )
        self.proposal_note.set_visible(True)
        self.revalidate()
        return suggestion

    # ------------------------------------------------------------------ splits

    def _on_detail_toggled(self, button: Gtk.ToggleButton) -> None:
        self.show_planning_detail = button.get_active()
        for editor in self.splits:
            editor.show_detail()

    def _count_more(self) -> None:
        """Count what the second page holds on its tab."""
        tags = [part for part in self.tags_entry.get_text().split(",") if part.strip()]
        self.pages.set_count(MORE, len(tags) + len(self.document_rows))

    def holds_security(self, account: str | None) -> bool:
        """Whether ``account`` holds a security, so its splits carry shares."""
        return account is not None and ledger.security_of(self.db, account) is not None

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
        if getattr(self, "_revalidating", False):
            return
        self._revalidating = True
        try:
            self._revalidate()
        finally:
            self._revalidating = False

    def _rest_row(self, residual: Money) -> None:
        """Keep one blank row holding the rest while the entry is out of balance (#369).

        GnuCash puts the rest of an unbalanced entry on a new split, waiting for its
        account; this does the same. A row added for it goes again, untouched, once
        the entry balances.
        """
        typed = [e for e in self.splits if not e.is_blank]
        blanks = [e for e in self.splits if e.is_blank]
        if residual and not blanks and len(typed) >= 2:
            editor = self.add_split()
            editor.auto = True
            editor.account.set_selected(Gtk.INVALID_LIST_POSITION)
            blanks = [editor]
        elif not residual:
            for editor in [e for e in blanks if e.auto and len(self.splits) > 2]:
                self.splits.remove(editor)
                self.split_box.remove(editor.box)
            blanks = [e for e in self.splits if e.is_blank]
        for editor in self.splits:
            editor.show_rest(-residual if editor in blanks and len(blanks) == 1 else None)

    def _revalidate(self) -> None:
        problems: list[str] = []
        self._rest_row(self.residual())

        if any(e.account_handle is None for e in self.splits):
            problems.append("choose the account for the last split, which holds the rest")
        elif len({e.account_handle for e in self.splits}) < 2:
            problems.append("use at least two different accounts")
        if any(e.value() is None and not e.is_blank for e in self.splits):
            problems.append("check the amounts")
        for editor in self.splits:
            try:
                editor.quantity_amount()
            except ValueError:
                problems.append("check the shares")
                break
        try:
            parse_date_text(self.date_entry.get_text().strip())
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
        when = parse_date_text(self.date_entry.get_text().strip())
        residual = self.residual()
        currency = transaction_currency(
            self.db, self.transaction.currency if self.transaction is not None else None
        )
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
                    value=Amount(value, currency),
                    handle=editor.handle,
                    memo=editor.memo.get_text().strip(),
                    planning_flow=editor.purpose,
                    investment_activity=editor.activity,
                    quantity=editor.quantity_amount(),
                )
            )
        attachment = None
        if (
            self.fsa_claim is not None
            and self.fsa_role is not None
            and self.fsa_claim.get_selected() > 0
        ):
            claim = self.fsa_claims[self.fsa_claim.get_selected() - 1]
            role = fsa_claims.ATTACHMENT_ROLES[self.fsa_role.get_selected()]
            attachment = ClaimAttachment(claim.handle, role)
        return SaveTransaction(
            TransactionInput(
                post_date=when,
                description=self.description_entry.get_text().strip() or "(no description)",
                num=self.num_entry.get_text().strip(),
                notes=self.notes_view.get_buffer().get_text(notes_start, notes_end, True).strip(),
                currency=currency,
                splits=tuple(splits),
                tags=tuple(self.tags_entry.get_text().split(",")),
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
        self.saved = True
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
