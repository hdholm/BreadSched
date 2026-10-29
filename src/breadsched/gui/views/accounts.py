"""The chart of accounts, as a GnuCash-style tree with balances.

Uses ``Gtk.TreeListModel``, GTK4's replacement for ``GtkTreeStore``: the model is
built lazily from a callback that supplies each node's children, so a chart with a
thousand accounts only materialises the branches that are actually expanded.

Parent rows show a recursive total.  That is the number people mean when they ask
what is in "Assets", and showing a placeholder's own (always zero) balance instead
would be technically correct and useless.
"""

from __future__ import annotations

from ...api.local import book_server  # noqa: E402
from ...gen.lib.account import Account  # noqa: E402
from ...gen.services import BalanceSummary, ChartLine  # noqa: E402
from .. import hidden_accounts  # noqa: E402
from ..gi_setup import Gio, Gtk, Pango
from ._base import (
    BaseView,
    Row,
    _compare_by,
    column,
    sorted_model,
    table_section,
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
        #: The chart as last read through the book server, by handle, and each
        #: account's children in the server's order (#358).
        self._lines: dict[str, ChartLine] = {}
        self._children: dict[str | None, list[Account]] = {}
        self._build()

    def _build(self) -> None:
        header = Gtk.Box(spacing=8)
        for side in ("top", "bottom", "start", "end"):
            getattr(header, f"set_margin_{side}")(8)

        title = Gtk.Label(label="Accounts", xalign=0)
        title.add_css_class("category-title")
        title.set_hexpand(True)
        header.append(title)

        # New/Edit account, Security price, and Exchange rate are toolbar icons and
        # Actions menu items while Accounts is shown; Hide empty and Show hidden are
        # View menu check items (#156, #182). Double-clicking opens the register, as GnuCash
        # does, so editing is the explicit Edit Account command.
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

        self.append(self._header)
        self.append(self.summary)

        # The column chooser sits in the table's own header, not the toolbar (#153).
        self.table = table_section(
            self.column_view,
            "accounts",
            getattr(self.manager.get_application(), "view_settings", None),
            table_label="account",
        )
        for side in ("start", "end"):
            getattr(self.table, f"set_margin_{side}")(8)
        self.table.scroller.set_vexpand(True)
        self.table.set_vexpand(True)
        self.append(self.table)

    # ----------------------------------------------------------------- columns

    def _name_column(self) -> Gtk.ColumnViewColumn:
        """The name cell carries the expander, so the tree structure is visible."""
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
        line = self._lines.get(account.handle)
        if line is None:
            return ""
        if line.balance is None:
            return "Missing reporting-currency quote" if line.missing_quotes else "Mixed currencies"
        return line.balance.format(parens_negative=True)

    def _quote_evidence(self, account) -> str:
        # The quote date already says how current it is (#151).
        line = self._lines.get(account.handle)
        return line.quote_evidence if line is not None else ""

    # ------------------------------------------------------------------ model

    def refresh(self) -> None:
        if self.db is None:
            return
        book = book_server(self.db)
        self._show_chart(book.chart(include_hidden=True), book.balance_summary())

    def background_refresh(self):
        """The chart is valued through the book server off GTK's loop (#389)."""
        if self.db is None:
            return None

        def read(book):
            return book.chart(include_hidden=True), book.balance_summary()

        return read, lambda answer: self._show_chart(*answer)

    def _show_chart(self, lines: list[ChartLine], summary: BalanceSummary) -> None:
        self._lines = {line.record.account.handle: line for line in lines}
        children: dict[str | None, list[Account]] = {}
        top = {line.record.account.handle for line in lines if line.depth == 0}
        for line in lines:
            account = line.record.account
            parent = None if account.handle in top else account.parent
            children.setdefault(parent, []).append(account)
        self._children = children
        self._lay_out()
        self._refresh_summary(summary)

    def _lay_out(self) -> None:
        """The tree from the chart already read, as the view's filters choose."""
        model = Gtk.TreeListModel.new(
            self._children_store(None),
            False,  # passthrough: rows are our Row objects, not raw items
            True,  # autoexpand the first level
            self._create_child_model,
        )
        self.column_view.set_model(Gtk.SingleSelection(model=sorted_model(self.column_view, model)))

    def _children_store(self, parent: str | None) -> Gio.ListStore:
        store = Gio.ListStore.new(Row)
        for account in self._children.get(parent, ()):
            if account.hidden and not self._show_hidden:
                continue
            if not self._show_zero and not self._has_value(account):
                continue
            store.append(Row(account))
        return store

    def _has_value(self, account: Account) -> bool:
        line = self._lines.get(account.handle)
        has_value = line is None or line.balance is None or bool(line.balance)
        return has_value or bool(self._children.get(account.handle))

    def _create_child_model(self, row: Row):
        """Return a child model, or ``None`` for a leaf so no expander is drawn."""
        account = row.payload
        if not isinstance(account, Account) or not self._children.get(account.handle):
            return None
        return self._children_store(account.handle)

    # ---------------------------------------------------------------- summary

    def _refresh_summary(self, summary: BalanceSummary) -> None:
        child = self.summary.get_first_child()
        while child is not None:
            self.summary.remove(child)
            child = self.summary.get_first_child()
        cards = [
            ("Cash on hand", summary.cash),
            ("Assets", summary.assets),
            ("Liabilities", summary.liabilities),
            ("Net worth", summary.net_worth),
        ]
        for label, amount in cards:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            box.add_css_class("card")
            caption = Gtk.Label(label=label, xalign=0)
            caption.add_css_class("summary-label")
            value = Gtk.Label(
                label=amount.format(parens_negative=True)
                if amount is not None
                else "Missing reporting-currency quote",
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

        root = next(
            (
                record.account
                for record in book_server(self.db).accounts()
                if record.account.is_root
            ),
            None,
        )
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

    def _on_online_quotes(self, *_args):
        if self.db is None:
            return None
        from ..dialogs.online_quotes_dialog import OnlineQuotesDialog

        application = self.manager.get_application() if self.manager is not None else None
        dialog = OnlineQuotesDialog(
            self.get_root(), self.db, getattr(application, "settings", None)
        )
        dialog.connect("close-request", self.refresh_on_close)
        dialog.present()
        return dialog

    def _on_holdings(self, _button) -> None:
        if self.db is None:
            return
        from ..dialogs.holdings_dialog import HoldingsDialog

        HoldingsDialog(self.get_root(), self.db).present()

    def _on_realized_gains(self, _button) -> None:
        if self.db is None:
            return
        from ..dialogs.realized_gains_dialog import RealizedGainsDialog

        RealizedGainsDialog(self.get_root(), self.db).present()

    def _on_tax_year(self, _button) -> None:
        if self.db is None:
            return
        from ..dialogs.tax_year_dialog import TaxYearDialog

        TaxYearDialog(self.get_root(), self.db).present()

    def _on_exchange_rate(self, _button) -> None:
        if self.db is None:
            return
        from ..dialogs.exchange_rate_dialog import ExchangeRateDialog

        dialog = ExchangeRateDialog(self.get_root(), self.db)
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

    def _on_new_account(self) -> None:
        self.edit_account(None)

    def set_hide_empty(self, hide: bool) -> None:
        self._show_zero = not hide
        self._lay_out()

    def set_show_hidden(self, show: bool) -> None:
        """Show hidden accounts here and make them usable everywhere (registers,
        dialogs), or put them away again."""
        self._show_hidden = show
        hidden_accounts.set_shown(show)
        root = self.get_root()
        refresh = getattr(root, "refresh_views", None)
        if callable(refresh):
            refresh()
        self._lay_out()
