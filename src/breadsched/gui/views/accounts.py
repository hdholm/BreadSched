"""The chart of accounts, as a GnuCash-style tree with balances.

Uses ``Gtk.TreeListModel``, GTK4's replacement for ``GtkTreeStore``: the model is
built lazily from a callback that supplies each node's children, so a chart with a
thousand accounts only materialises the branches that are actually expanded.

Parent rows show a recursive total.  That is the number people mean when they ask
what is in "Assets", and showing a placeholder's own (always zero) balance instead
would be technically correct and useless.
"""

from __future__ import annotations

from ...gen.engine import ledger, valuation  # noqa: E402
from ...gen.lib.account import Account, AccountClass  # noqa: E402
from ...gen.lib.money import Money  # noqa: E402
from ..gi_setup import Gio, Gtk
from ._base import (
    BaseView,
    Row,
    _compare_by,
    column,
    column_menu,
    sorted_model,
    unwrap,
)  # noqa: E402

__all__ = ["AccountTreeView"]


class AccountTreeView(BaseView):
    """Hierarchical account list with running totals."""

    WATCHES = (
        "database-changed",
        "account-add",
        "account-update",
        "account-delete",
        "transaction-add",
        "transaction-update",
        "transaction-delete",
        "commodity-add",
        "commodity-update",
        "commodity-delete",
        "price-add",
        "price-update",
        "price-delete",
    )

    def __init__(self, manager) -> None:
        super().__init__(manager)
        self._show_hidden = False
        self._show_zero = True
        self._build()

    def _build(self) -> None:
        header = Gtk.Box(spacing=8)
        for side in ("top", "bottom", "start", "end"):
            getattr(header, f"set_margin_{side}")(8)

        title = Gtk.Label(label="Accounts", xalign=0)
        title.add_css_class("category-title")
        title.set_hexpand(True)
        header.append(title)

        zero_toggle = Gtk.ToggleButton(label="Hide empty")
        zero_toggle.connect("toggled", self._on_zero_toggled)
        header.append(zero_toggle)

        hidden_toggle = Gtk.ToggleButton(label="Show hidden")
        hidden_toggle.connect("toggled", self._on_hidden_toggled)
        header.append(hidden_toggle)

        price_button = Gtk.Button(label="Security price…")
        price_button.set_tooltip_text("Create a security or record a dated market price")
        price_button.connect("clicked", self._on_security_price)
        header.append(price_button)

        new_button = Gtk.Button(label="New account…", icon_name="list-add-symbolic")
        new_button.connect("clicked", lambda *_: self.edit_account(None))
        header.append(new_button)

        # Double-clicking opens the register, as GnuCash does, so editing gets the
        # explicit control. One gesture cannot mean both, and the register is the
        # thing people open dozens of times a day.
        edit_button = Gtk.Button(label="Edit account…")
        edit_button.set_tooltip_text("Change the selected account's settings")
        edit_button.connect("clicked", self._on_edit_selected)
        header.append(edit_button)
        self._header = header

        self.summary = Gtk.Box(spacing=24)
        for side in ("start", "end", "bottom"):
            getattr(self.summary, f"set_margin_{side}")(12)

        self.column_view = Gtk.ColumnView()
        self.column_view.set_show_row_separators(False)
        self.column_view.add_css_class("data-table")
        self.column_view.append_column(self._name_column())
        self.column_view.append_column(column("Type", lambda a: a.atype.value))
        self.column_view.append_column(column("Description", lambda a: a.description, expand=True))
        self.column_view.append_column(column("Balance", self._format_balance, numeric=True))
        self.column_view.append_column(column("Quote evidence", self._quote_evidence))
        self.column_view.connect("activate", self._on_activated)

        self._header.append(
            column_menu(
                "accounts",
                self.column_view,
                getattr(self.manager.get_application(), "view_settings", None),
            )
        )
        self.append(self._header)
        self.append(self.summary)

        scroller = Gtk.ScrolledWindow(child=self.column_view)
        scroller.set_vexpand(True)
        self.append(scroller)

    # ----------------------------------------------------------------- columns

    def _name_column(self) -> Gtk.ColumnViewColumn:
        """The name cell carries the expander, so the tree structure is visible."""
        factory = Gtk.SignalListItemFactory()

        def on_setup(_factory, item) -> None:
            expander = Gtk.TreeExpander()
            label = Gtk.Label(xalign=0)
            expander.set_child(label)
            item.set_child(expander)

        def on_bind(_factory, item) -> None:
            tree_row = item.get_item()
            expander = item.get_child()
            expander.set_list_row(tree_row)
            account = tree_row.get_item().payload
            label = expander.get_child()
            label.set_text(account.name)
            if account.placeholder:
                label.add_css_class("dim")
            else:
                label.remove_css_class("dim")

        factory.connect("setup", on_setup)
        factory.connect("bind", on_bind)
        col = Gtk.ColumnViewColumn(title="Account", factory=factory)
        col.set_expand(True)
        col.set_resizable(True)
        col.set_sorter(
            Gtk.CustomSorter.new(_compare_by(lambda account: account.name.casefold(), None, False))
        )
        return col

    def _format_balance(self, account) -> str:
        if self.db is None:
            return ""
        try:
            total = valuation.value_recursive(self.db, account.handle)
        except TypeError as exc:
            if "cannot combine unlike commodities" not in str(exc):
                raise
            return "Mixed currencies"
        return total.format(parens_negative=True)

    def _quote_evidence(self, account) -> str:
        if self.db is None:
            return ""
        valued = valuation.account_value(self.db, account)
        if valued.missing_quote:
            unit = f" ({valued.currency.mnemonic})" if valued.currency is not None else ""
            return f"No reporting-currency quote; ledger value{unit}"
        if valued.source in {"market", "currency"} and valued.price_date is not None:
            return f"{valued.price_date.isoformat()} · {valued.price_source or 'Unknown source'}"
        return ""

    # ------------------------------------------------------------------ model

    def refresh(self) -> None:
        if self.db is None:
            return
        root = self.db.root_account()
        model = Gtk.TreeListModel.new(
            self._children_store(root.handle if root else None),
            False,  # passthrough: rows are our Row objects, not raw items
            True,  # autoexpand the first level
            self._create_child_model,
        )
        self.column_view.set_model(Gtk.SingleSelection(model=sorted_model(self.column_view, model)))
        self._refresh_summary()

    def _children_store(self, parent: str | None) -> Gio.ListStore:
        store = Gio.ListStore.new(Row)
        if self.db is None:
            return store
        for account in self.db.child_accounts(parent):
            if account.hidden and not self._show_hidden:
                continue
            if not self._show_zero and not self._has_value(account):
                continue
            store.append(Row(account))
        return store

    def _has_value(self, account: Account) -> bool:
        if self.db is None:
            return False
        try:
            has_value = bool(valuation.value_recursive(self.db, account.handle))
        except TypeError as exc:
            if "cannot combine unlike commodities" not in str(exc):
                raise
            has_value = True
        return has_value or bool(self.db.child_accounts(account.handle))

    def _create_child_model(self, row: Row):
        """Return a child model, or ``None`` for a leaf so no expander is drawn."""
        if self.db is None:
            return None
        account = row.payload
        if not isinstance(account, Account):
            return None
        children = self.db.child_accounts(account.handle)
        if not children:
            return None
        return self._children_store(account.handle)

    # ---------------------------------------------------------------- summary

    def _refresh_summary(self) -> None:
        child = self.summary.get_first_child()
        while child is not None:
            self.summary.remove(child)
            child = self.summary.get_first_child()

        if self.db is None:
            return
        try:
            totals = valuation.totals_by_class(self.db)
            net_worth = valuation.net_worth(self.db)
        except TypeError as exc:
            if "cannot combine unlike commodities" not in str(exc):
                raise
            totals = {}
            net_worth = None
        cards = [
            ("Cash on hand", ledger.cash_on_hand(self.db)),
            ("Assets", totals.get(AccountClass.ASSET, Money(0)) if net_worth is not None else None),
            (
                "Liabilities",
                totals.get(AccountClass.LIABILITY, Money(0)) if net_worth is not None else None,
            ),
            ("Net worth", net_worth),
        ]
        for label, amount in cards:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            box.add_css_class("card")
            caption = Gtk.Label(label=label, xalign=0)
            caption.add_css_class("summary-label")
            value = Gtk.Label(
                label=amount.format(parens_negative=True)
                if amount is not None
                else "Mixed currencies",
                xalign=0,
            )
            value.add_css_class("summary-value")
            value.add_css_class("numeric")
            if amount is not None and amount < 0:
                value.add_css_class("negative")
            box.append(caption)
            box.append(value)
            self.summary.append(box)

    # ---------------------------------------------------------------- actions

    def edit_account(self, account) -> None:
        if self.db is None:
            return
        from ..dialogs.account_dialog import AccountDialog

        root = self.db.root_account()
        dialog = AccountDialog(
            self.get_root(),
            self.db,
            account,
            default_parent=root.handle if root else None,
        )
        dialog.connect("close-request", self.refresh_on_close)
        dialog.present()

    def selected_account(self):
        """The account under the cursor, or None."""
        selection = self.column_view.get_model()
        if selection is None:
            return None
        item = selection.get_selected_item()
        if item is None:
            return None
        return unwrap(item)

    def _on_edit_selected(self, _button) -> None:
        account = self.selected_account()
        if account is not None:
            self.edit_account(account)

    def _on_security_price(self, _button) -> None:
        if self.db is None:
            return
        from ..dialogs.security_price_dialog import SecurityPriceDialog

        dialog = SecurityPriceDialog(self.get_root(), self.db)
        dialog.connect("close-request", self.refresh_on_close)
        dialog.present()

    def _on_activated(self, _view, position: int) -> None:
        """Double-clicking an account opens its register, as GnuCash does.

        A placeholder holds no entries of its own, so activating one expands or
        collapses it instead: the only thing to see is what sits underneath.
        """
        selection = self.column_view.get_model()
        tree_row = selection.get_item(position)
        if tree_row is None:
            return
        account = tree_row.get_item().payload
        if account.placeholder:
            tree_row.set_expanded(not tree_row.get_expanded())
            return
        self.manager.open_register(account.handle)

    def _on_zero_toggled(self, button: Gtk.ToggleButton) -> None:
        self._show_zero = not button.get_active()
        self.refresh()

    def _on_hidden_toggled(self, button: Gtk.ToggleButton) -> None:
        self._show_hidden = button.get_active()
        self.refresh()
