"""The register: one account's transactions, in GnuCash's ledger layout.

Columns follow GnuCash's basic ledger — date, number, description, transfer, then
separate debit and credit columns whose headings change with the account type
(``Deposit``/``Withdrawal`` for a bank account, ``Charge``/``Payment`` for a credit
card).  Those labels are not decoration: "debit" and "credit" are the single most
common source of data-entry errors in a household ledger, and naming the columns
after what the account actually does removes the translation step.

The running balance is computed by the engine, not accumulated in the widget, so a
back-dated entry re-sorts and re-totals correctly.
"""

from __future__ import annotations

from datetime import date

from ...gen.engine import ledger  # noqa: E402
from ...gen.lib.account import AccountClass, AccountType  # noqa: E402
from ..gi_setup import Gio, Gtk, Pango
from ._base import BaseView, Row, column, column_menu, sorted_model, unwrap  # noqa: E402

__all__ = ["RegisterView", "column_headings"]

#: Account type -> (debit heading, credit heading), as GnuCash labels them.
_HEADINGS = {
    AccountType.BANK: ("Deposit", "Withdrawal"),
    AccountType.CASH: ("Receive", "Spend"),
    AccountType.CREDIT: ("Payment", "Charge"),
    AccountType.LIABILITY: ("Payment", "Increase"),
    AccountType.INCOME: ("Charge", "Income"),
    AccountType.EXPENSE: ("Expense", "Rebate"),
    AccountType.INVESTMENT: ("Buy", "Sell"),
    AccountType.RETIREMENT: ("Contribution", "Distribution"),
    AccountType.EQUITY: ("Decrease", "Increase"),
}


def _is_parent(payload) -> bool:
    return not isinstance(payload, SplitRow)


class SplitRow:
    """One leg of a transaction, shown as a child row beneath it."""

    __slots__ = ("split", "transaction", "is_this_account", "db")

    def __init__(self, split, transaction, is_this_account: bool, db) -> None:
        self.split = split
        self.transaction = transaction
        self.is_this_account = is_this_account
        self.db = db

    @property
    def account_name(self) -> str:
        return self.db.full_name(self.split.account) or "(unknown account)"

    @property
    def description(self) -> str:
        return self.split.memo or self.account_name

    @property
    def debit(self):
        return self.split.value if self.split.value > 0 else None

    @property
    def credit(self):
        return -self.split.value if self.split.value < 0 else None


def _amount(value) -> Gtk.Label:
    label = Gtk.Label(label=value.format() if value is not None else "", xalign=1)
    label.add_css_class("numeric")
    return label


def column_headings(atype: AccountType) -> tuple[str, str]:
    """Debit and credit column headings for an account type."""
    return _HEADINGS.get(atype, ("Increase", "Decrease"))


class RegisterView(BaseView):
    """A scrollable ledger for one account at a time."""

    WATCHES = (
        "database-changed",
        "transaction-add",
        "transaction-update",
        "transaction-delete",
        "account-update",
    )

    def __init__(self, manager) -> None:
        super().__init__(manager)
        self.account_handle: str | None = None
        self._rows: list = []
        self._pickable: list = []
        # Repopulating the picker resets its selection, which fires
        # notify::selected and would otherwise overwrite the account the caller
        # just asked for.
        self._updating = False
        self._build()

    def _build(self) -> None:
        bar = Gtk.Box(spacing=8)
        for side in ("top", "bottom", "start", "end"):
            getattr(bar, f"set_margin_{side}")(8)

        self.account_picker = Gtk.DropDown()
        self.account_picker.set_hexpand(True)
        self.account_picker.connect("notify::selected", self._on_account_changed)
        bar.append(self.account_picker)

        self.balance_label = Gtk.Label(xalign=1)
        self.balance_label.add_css_class("summary-value")
        self.balance_label.add_css_class("numeric")
        bar.append(self.balance_label)

        add_button = Gtk.Button(icon_name="list-add-symbolic")
        add_button.set_tooltip_text("Add a transaction")
        add_button.connect("clicked", self._on_add_clicked)
        bar.append(add_button)

        self.reconcile_button = Gtk.Button(label="Reconcile…")
        self.reconcile_button.set_tooltip_text("Compare this account with a statement")
        self.reconcile_button.connect("clicked", self._on_reconcile_clicked)
        bar.append(self.reconcile_button)

        self.column_view = Gtk.ColumnView()
        self.column_view.set_show_row_separators(True)
        self.column_view.connect("activate", self._on_activated)
        self.column_view.append_column(
            column(
                "Date",
                lambda r: r.post_date.isoformat() if _is_parent(r) else "",
                sort_key=lambda r: r.transaction.post_date,
            )
        )
        self.column_view.append_column(
            column("Num", lambda r: r.transaction.num if _is_parent(r) else "")
        )
        # The description carries the expander, so splits appear indented directly
        # beneath the transaction they belong to.
        self.column_view.append_column(self._description_column())
        self.column_view.append_column(
            column(
                "Transfer",
                lambda r: r.transfer_label(self.db) if _is_parent(r) else r.account_name,
                expand=True,
            )
        )
        self.debit_column = column(
            "Increase", lambda r: r.debit.format() if r.debit else "", numeric=True
        )
        self.credit_column = column(
            "Decrease", lambda r: r.credit.format() if r.credit else "", numeric=True
        )
        self.column_view.append_column(self.debit_column)
        self.column_view.append_column(self.credit_column)
        self.column_view.append_column(
            column(
                "Balance",
                lambda r: r.running.format(parens_negative=True) if _is_parent(r) else "",
                numeric=True,
            )
        )

        # The menu needs the columns to exist, so the toolbar is added last.
        bar.append(
            column_menu(
                "register",
                self.column_view,
                getattr(self.manager.get_application(), "view_settings", None),
            )
        )
        self.append(bar)

        scroller = Gtk.ScrolledWindow(child=self.column_view)
        scroller.set_vexpand(True)
        self.append(scroller)

    # ------------------------------------------------------------------ model

    def refresh(self) -> None:
        if self.db is None:
            return
        self._updating = True
        try:
            self._populate_picker()
        finally:
            self._updating = False
        if self.account_handle is None:
            return

        account = self.db.get_account(self.account_handle)
        if account is None:
            return
        self.reconcile_button.set_sensitive(
            not account.placeholder
            and account.account_class in {AccountClass.ASSET, AccountClass.LIABILITY}
        )
        debit, credit = column_headings(account.atype)
        self.debit_column.set_title(debit)
        self.credit_column.set_title(credit)

        self._rows = ledger.register(self.db, self.account_handle)
        store = Gio.ListStore.new(Row)
        for row in self._rows:
            store.append(Row(row))
        tree = Gtk.TreeListModel.new(store, False, False, self._children_of)
        selection = Gtk.SingleSelection(model=sorted_model(self.column_view, tree))
        selection.connect("notify::selected", self._on_selection_changed)
        self.column_view.set_model(selection)

        closing = self._rows[-1].running if self._rows else None
        self.balance_label.set_text(closing.format(parens_negative=True) if closing else "0.00")
        if closing is not None and closing < 0:
            self.balance_label.add_css_class("negative")
        else:
            self.balance_label.remove_css_class("negative")

    def _populate_picker(self) -> None:
        db = self.db
        if db is None:
            return
        accounts = sorted(
            (a for a in db.iter_accounts() if not a.is_root and not a.placeholder),
            key=db.full_name,
        )
        self._pickable = accounts
        model = Gtk.StringList()
        for account in accounts:
            model.append(db.full_name(account))
        self.account_picker.set_model(model)

        if self.account_handle is None and accounts:
            self.account_handle = accounts[0].handle
        for index, account in enumerate(accounts):
            if account.handle == self.account_handle:
                self.account_picker.set_selected(index)
                break

    def _description_column(self) -> Gtk.ColumnViewColumn:
        factory = Gtk.SignalListItemFactory()

        def on_setup(_factory, item) -> None:
            expander = Gtk.TreeExpander()
            label = Gtk.Label(xalign=0)
            label.set_ellipsize(Pango.EllipsizeMode.END)
            expander.set_child(label)
            item.set_child(expander)

        def on_bind(_factory, item) -> None:
            tree_row = item.get_item()
            expander = item.get_child()
            expander.set_list_row(tree_row)
            payload = unwrap(tree_row)
            label = expander.get_child()
            label.set_text(payload.description or "(no description)")
            if _is_parent(payload):
                label.remove_css_class("dim")
            else:
                label.add_css_class("dim")

        factory.connect("setup", on_setup)
        factory.connect("bind", on_bind)
        col = Gtk.ColumnViewColumn(title="Description", factory=factory)
        col.set_expand(True)
        col.set_resizable(True)
        return col

    def _children_of(self, item):
        """The splits of a transaction row, or None for a split row itself."""
        payload = item.payload if isinstance(item, Row) else item
        if not isinstance(payload, ledger.RegisterRow) or self.db is None:
            return None
        txn = payload.transaction
        store = Gio.ListStore.new(Row)
        for split in txn.splits:
            store.append(Row(SplitRow(split, txn, split.handle == payload.split.handle, self.db)))
        return store

    def _on_selection_changed(self, selection, _param) -> None:
        """Expand the selected transaction in place, collapsing the last one.

        The splits belong underneath the transaction they came from, where the
        surrounding ledger stays visible — which is usually why the split was
        opened. Only one is expanded at a time; leaving them open pushes the
        register apart until the running balance is unreadable.
        """
        position = selection.get_selected()
        model = selection.get_model()
        for index in range(model.get_n_items()):
            tree_row = model.get_item(index)
            if tree_row is None or tree_row.get_depth() != 0:
                continue
            tree_row.set_expanded(index == position)

    def show_account(self, handle: str) -> None:
        self.account_handle = handle
        self.refresh()

    # ---------------------------------------------------------------- actions

    def _on_account_changed(self, picker, _param) -> None:
        if self._updating:
            return
        index = picker.get_selected()
        if 0 <= index < len(self._pickable):
            handle = self._pickable[index].handle
            if handle != self.account_handle:
                self.account_handle = handle
                self.refresh()

    def _on_activated(self, _view, position: int) -> None:
        """Double-clicking a row opens that transaction for editing.

        Activating one of a transaction's split rows opens the transaction it
        belongs to: the splits are part of the same record, and editing them
        separately is what would let a ledger drift out of balance.
        """
        selection = self.column_view.get_model()
        tree_row = selection.get_item(position)
        if tree_row is None:
            return
        payload = unwrap(tree_row)
        self.edit_transaction(payload.transaction)

    def edit_transaction(self, transaction) -> None:
        if self.db is None:
            return
        from ..dialogs.transaction_dialog import TransactionDialog

        dialog = TransactionDialog(
            self.get_root(),
            self.db,
            default_account=self.account_handle,
            transaction=transaction,
        )
        dialog.connect("close-request", self.refresh_on_close)
        dialog.present()

    def _on_add_clicked(self, _button) -> None:
        if self.db is None or self.account_handle is None:
            return
        from ..dialogs.transaction_dialog import TransactionDialog

        dialog = TransactionDialog(
            self.get_root(),
            self.db,
            default_account=self.account_handle,
            default_date=date.today(),
        )
        dialog.connect("close-request", self.refresh_on_close)
        dialog.present()

    def _on_reconcile_clicked(self, _button) -> None:
        if self.db is None or self.account_handle is None:
            return
        account = self.db.get_account(self.account_handle)
        if account is None:
            return
        from ..dialogs.reconciliation_dialog import ReconciliationDialog

        dialog = ReconciliationDialog(self.get_root(), self.db, account)
        dialog.connect("close-request", self.refresh_on_close)
        dialog.present()
