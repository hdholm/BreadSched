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
from ..gi_setup import Gdk, Gio, GLib, Gtk, Pango
from ..widgets.choice import bounded_dropdown
from ._base import (  # noqa: E402
    BaseView,
    Row,
    column,
    host_widget,
    sorted_model,
    table_section,
    unwrap,
)
from .blank_entry import BLANK, BLANK_PAYLOADS, BlankEntryRow, ImbalanceLine, SplitLine

__all__ = ["RegisterView", "column_headings"]


def _is_parent(payload) -> bool:
    return not isinstance(payload, (SplitRow, *BLANK_PAYLOADS))


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
    return ledger.register_headings(atype)


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
        #: Set when the register should open on its most recent entry (#157).
        self._scroll_to_end = False
        self._build()

    def _build(self) -> None:
        bar = Gtk.Box(spacing=8)
        for side in ("top", "bottom", "start", "end"):
            getattr(bar, f"set_margin_{side}")(8)

        self.account_picker = bounded_dropdown()
        self.account_picker.set_hexpand(True)
        self.account_picker.connect("notify::selected", self._on_account_changed)
        bar.append(self.account_picker)

        self.filter_entry = Gtk.SearchEntry(placeholder_text="Filter this register")
        self.filter_entry.set_tooltip_text(
            "Filter by description, number, notes, tag, memo, or account"
        )
        self.filter_entry.connect("search-changed", lambda *_: self.schedule_refresh())
        bar.append(self.filter_entry)

        self.balance_label = Gtk.Label(xalign=1)
        self.balance_label.add_css_class("summary-value")
        self.balance_label.add_css_class("numeric")
        bar.append(self.balance_label)

        add_button = Gtk.Button(icon_name="list-add-symbolic")
        # The blank row at the bottom takes an ordinary two-split transaction
        # directly (#158); this opens the full editor for anything it cannot do.
        add_button.set_tooltip_text(
            "Open the full transaction editor (extra splits, notes, reconciliation)"
        )
        add_button.connect("clicked", self._on_add_clicked)
        bar.append(add_button)

        new_window = Gtk.Button(icon_name="window-new-symbolic")
        new_window.set_tooltip_text("Open this account in an independent register window")
        new_window.connect("clicked", self._on_open_window_clicked)
        bar.append(new_window)

        self.reconcile_button = Gtk.Button(label="Reconcile…")
        self.reconcile_button.set_tooltip_text("Compare this account with a statement")
        self.reconcile_button.connect("clicked", self._on_reconcile_clicked)
        bar.append(self.reconcile_button)

        # The blank entry row's widgets live as long as the view, so typing in
        # them survives the register repainting around them (#158).
        self.blank = BlankEntryRow(self)
        #: The stored transaction being edited in place (#158 slice 3), if any, and
        #: the child store holding its split lines.
        self.editor: BlankEntryRow | None = None
        self._edit_children = Gio.ListStore.new(Row)
        cell = self._blank_cell

        self.column_view = Gtk.ColumnView()
        self.column_view.add_css_class("data-table")
        self.column_view.set_show_row_separators(True)
        self.column_view.connect("activate", self._on_activated)
        # F2 edits the selected transaction in place; double-click and Enter keep
        # opening the full editor.
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_register_key)
        self.column_view.add_controller(keys)
        self.column_view.append_column(
            column(
                "Date",
                lambda r: r.post_date.isoformat() if _is_parent(r) else "",
                sort_key=lambda r: r.transaction.post_date,
                cell=cell("Date"),
            )
        )
        self.column_view.append_column(
            column("Num", lambda r: r.transaction.num if _is_parent(r) else "", cell=cell("Num"))
        )
        # The description carries the expander, so splits appear indented directly
        # beneath the transaction they belong to.
        self.column_view.append_column(self._description_column())
        self.column_view.append_column(
            column(
                "Transfer",
                lambda r: r.transfer_label(self.db) if _is_parent(r) else r.account_name,
                expand=True,
                cell=cell("Transfer"),
            )
        )
        self.debit_column = column(
            "Increase",
            lambda r: r.debit.format() if r.debit else "",
            numeric=True,
            cell=cell("Increase"),
        )
        self.credit_column = column(
            "Decrease",
            lambda r: r.credit.format() if r.credit else "",
            numeric=True,
            cell=cell("Decrease"),
        )
        self.column_view.append_column(self.debit_column)
        self.column_view.append_column(self.credit_column)
        self.column_view.append_column(
            column(
                "Balance",
                lambda r: r.running.format(parens_negative=True) if _is_parent(r) else "",
                numeric=True,
                cell=cell("Balance"),
            )
        )

        self.append_toolbar(bar)

        # The column chooser sits in the table's own header, not the toolbar (#153).
        self.table = table_section(
            self.column_view,
            "register",
            getattr(self.manager.get_application(), "view_settings", None),
            table_label="register",
        )
        for side in ("start", "end"):
            getattr(self.table, f"set_margin_{side}")(8)
        self.table.scroller.set_vexpand(True)
        self.table.set_vexpand(True)
        self.append(self.table)

        # One line under the register reports what the blank row did or why not.
        self.entry_status = Gtk.Label(xalign=0, wrap=True)
        self.entry_status.add_css_class("dim")
        for side in ("start", "end"):
            getattr(self.entry_status, f"set_margin_{side}")(8)
        self.entry_status.set_margin_bottom(4)
        self.append(self.entry_status)

    def _blank_cell(self, title: str):
        """A cell hook that shows the blank row's widget for ``title`` (#158)."""
        return lambda payload: self._blank_widget(payload, title)

    def _blank_widget(self, payload, title: str):
        """The blank row's, or one of its split lines', widget for a column.

        The row of a transaction being edited in place hosts the editor's
        widgets the same way.
        """
        if payload is BLANK:
            return self.blank.cells.get(title)
        if self._is_edited(payload):
            assert self.editor is not None
            return self.editor.cells.get(title)
        if isinstance(payload, (SplitLine, ImbalanceLine)):
            # A column a split line does not use shows its (empty) label.
            return payload.blank_cells.get(title)
        return None

    def _is_edited(self, payload) -> bool:
        editor = self.editor
        return (
            editor is not None
            and isinstance(payload, ledger.RegisterRow)
            and payload.split.handle == editor.edit_split
        )

    def update_blank_rows(self, row: BlankEntryRow | None = None) -> None:
        """Show a blank or edited row's current lines without rebuilding the register."""
        if row is not None and row is self.editor:
            lines = [Row(payload) for payload in row.rows()[1:]]
            self._edit_children.splice(0, self._edit_children.get_n_items(), lines)
            return
        store = getattr(self, "blank_store", None)
        if store is None:
            return
        rows = [Row(payload) for payload in self.blank.rows()]
        store.splice(0, store.get_n_items(), rows)

    # ------------------------------------------------------ in-place editing

    def _on_register_key(self, _controller, keyval: int, _keycode: int, _state) -> bool:
        if keyval == Gdk.KEY_F2:
            self.edit_selected_in_place()
            return True
        return False

    def edit_selected_in_place(self, *_args) -> bool:
        """Edit the selected transaction in its own row (#158 slice 3)."""
        selection = self.column_view.get_model()
        if selection is None or self.db is None:
            return False
        payload = unwrap(selection.get_selected_item()) if selection.get_selected_item() else None
        if payload is None or isinstance(payload, BLANK_PAYLOADS):
            return False
        transaction = self.db.get_transaction(payload.transaction.handle)
        if transaction is None:
            return False
        split = payload.split if isinstance(payload, ledger.RegisterRow) else None
        if split is None:
            split = next(
                (item for item in transaction.splits if item.account == self.account_handle),
                None,
            )
        if split is None:
            return False
        self.start_editing(transaction, split.handle)
        return True

    def track_selected_reimbursable(self, *_args):
        """Open reimbursable expenses to track the selected transaction's cost."""
        selection = self.column_view.get_model()
        item = selection.get_selected_item() if selection is not None else None
        payload = unwrap(item) if item is not None else None
        if self.db is None or payload is None or isinstance(payload, BLANK_PAYLOADS):
            self.set_entry_status("Select a transaction to track as reimbursable.", error=True)
            return None
        transaction = self.db.get_transaction(payload.transaction.handle)
        accounts = {account.handle: account for account in self.db.iter_accounts()}
        cost = next(
            (
                split
                for split in (transaction.splits if transaction is not None else ())
                if split.value > 0
                and accounts.get(split.account) is not None
                and accounts[split.account].account_class is AccountClass.EXPENSE
            ),
            None,
        )
        if transaction is None or cost is None:
            self.set_entry_status(
                "Only a transaction with an expense can be tracked as reimbursable.", error=True
            )
            return None
        application = self.manager.get_application() if self.manager is not None else None
        opener = getattr(application, "on_receivables", None)
        if opener is None:
            return None
        return opener(expense=(transaction.handle, cost.handle))

    def start_editing(self, transaction, split_handle: str) -> None:
        """Turn one transaction's row into editable cells, after any pending edit."""

        def begin() -> None:
            if self.db is None or self.account_handle is None:
                return
            editor = BlankEntryRow(self, editing=transaction, split=split_handle)
            account = self.db.get_account(self.account_handle)
            if account is not None:
                editor.set_headings(*column_headings(account.atype))
            editor.populate(self.db, self.account_handle)
            self.editor = editor
            editor.load()
            self.set_entry_status(
                "Editing in place: Enter saves, Escape cancels; the pencil opens the editor."
            )
            self.refresh()
            GLib.idle_add(lambda: editor.description.grab_focus() and GLib.SOURCE_REMOVE)

        if self.editor is not None:
            self.editor.confirm_leave(begin)
        else:
            begin()

    def stop_editing(self) -> None:
        """End an in-place edit without saving; the row shows its stored values."""
        if self.editor is None:
            return
        self.editor = None
        self._edit_children.splice(0, self._edit_children.get_n_items(), [])
        self.schedule_refresh()

    def has_unsaved(self) -> bool:
        return self.blank.has_input() or (self.editor is not None and self.editor.has_input())

    def confirm_leave(self, proceed) -> None:
        """Ask about an unsaved in-place edit, then the blank row, then proceed."""
        editor = self.editor

        def then_blank() -> None:
            self.blank.confirm_leave(proceed)

        def after_edit() -> None:
            self.stop_editing()
            then_blank()

        if editor is not None:
            editor.confirm_leave(after_edit)
        else:
            then_blank()

    def set_entry_status(self, text: str, *, error: bool = False) -> None:
        self.entry_status.set_text(text)
        if error:
            self.entry_status.add_css_class("negative")
        else:
            self.entry_status.remove_css_class("negative")

    def scroll_to_end_on_refresh(self) -> None:
        self._scroll_to_end = True

    def cancel_scroll_to_end(self) -> None:
        self._scroll_to_end = False

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
        self.blank.set_headings(debit, credit)
        self.blank.populate(self.db, account.handle)
        if account.hidden:
            self.blank.set_enabled(False, "Hidden accounts take no new transactions")
        elif account.placeholder:
            self.blank.set_enabled(False, "Placeholder accounts take no transactions")
        elif not self.blank.transfers:
            self.blank.set_enabled(False, "No other visible account to transfer to")
        else:
            self.blank.set_enabled(True)

        if self.editor is not None:
            fresh = self.db.get_transaction(self.editor.editing.handle)
            if fresh is None or self.editor.edit_split not in {s.handle for s in fresh.splits}:
                self.editor = None
                self._edit_children.splice(0, self._edit_children.get_n_items(), [])
            else:
                self.editor.set_headings(debit, credit)
                self.editor.populate(self.db, account.handle)
                self.update_blank_rows(self.editor)

        self._rows = ledger.register(self.db, self.account_handle)
        store = Gio.ListStore.new(Row)
        for row in self._rows:
            if not self._matches_filter(row):
                continue
            store.append(Row(row))
        tree = Gtk.TreeListModel.new(store, False, False, self._children_of)
        # The blank row joins after sorting and filtering, so it stays last (#158).
        self.blank_store = Gio.ListStore.new(Row)
        for payload in self.blank.rows():
            self.blank_store.append(Row(payload))
        parts = Gio.ListStore.new(Gio.ListModel)
        parts.append(sorted_model(self.column_view, tree))
        parts.append(self.blank_store)
        selection = Gtk.SingleSelection(model=Gtk.FlattenListModel.new(parts))
        selection.set_autoselect(False)
        selection.set_selected(Gtk.INVALID_LIST_POSITION)
        selection.connect("notify::selected", self._on_selection_changed)
        adjustment = self.table.scroller.get_vadjustment()
        previous = adjustment.get_value()
        self.column_view.set_model(selection)
        self._expand_edited(selection)
        # Like a check register, a newly shown account opens on its most recent
        # entry at the bottom; any other repaint keeps the reader's place (#157).
        if self._scroll_to_end:
            self._scroll_to_end = False
            count = selection.get_n_items()
            if count and hasattr(self.column_view, "scroll_to"):  # GTK 4.12+
                self.column_view.scroll_to(count - 1, None, Gtk.ListScrollFlags.NONE, None)
            elif count:

                def to_end() -> bool:
                    adjustment.set_value(adjustment.get_upper() - adjustment.get_page_size())
                    return GLib.SOURCE_REMOVE

                GLib.idle_add(to_end)
        else:
            GLib.idle_add(lambda: adjustment.set_value(previous) or GLib.SOURCE_REMOVE)

        closing = self._rows[-1].running if self._rows else None
        self.balance_label.set_text(closing.format(parens_negative=True) if closing else "0.00")
        if closing is not None and closing < 0:
            self.balance_label.add_css_class("negative")
        else:
            self.balance_label.remove_css_class("negative")

    def _matches_filter(self, row) -> bool:
        needle = self.filter_entry.get_text().strip().casefold()
        if not needle or self.db is None:
            return True
        transaction = row.transaction
        text = " ".join(
            [
                transaction.description,
                transaction.num,
                transaction.notes,
                transaction.source_notes,
                *transaction.tags,
                *(split.memo for split in transaction.splits),
                *(self.db.full_name(split.account) for split in transaction.splits),
            ]
        ).casefold()
        return needle in text

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
            expander.set_hexpand(True)
            label = Gtk.Label(xalign=0)
            label.set_ellipsize(Pango.EllipsizeMode.END)
            expander.set_child(label)
            host = Gtk.Box()
            host.label = expander
            host.append(expander)
            item.set_child(host)

        def on_bind(_factory, item) -> None:
            tree_row = item.get_item()
            host = item.get_child()
            expander = host.label
            payload = unwrap(tree_row)
            hosted = self._blank_widget(payload, "Description")
            if hosted is not None:
                expander.set_list_row(None)
                host_widget(host, hosted)
                return
            host_widget(host, None)
            expander.set_list_row(tree_row)
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

    def _expand_edited(self, selection) -> None:
        """Keep the row being edited open, so its split lines stay in view."""
        if self.editor is None:
            return
        for index in range(selection.get_n_items()):
            tree_row = selection.get_item(index)
            if isinstance(tree_row, Gtk.TreeListRow) and self._is_edited(unwrap(tree_row)):
                tree_row.set_expanded(True)
                return

    def _children_of(self, item):
        """The splits of a transaction row, or None for a split row itself.

        A transaction being edited in place shows its editable split lines instead.
        """
        payload = item.payload if isinstance(item, Row) else item
        if not isinstance(payload, ledger.RegisterRow) or self.db is None:
            return None
        if self._is_edited(payload):
            return self._edit_children
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
            if not isinstance(tree_row, Gtk.TreeListRow) or tree_row.get_depth() != 0:
                continue
            if self._is_edited(unwrap(tree_row)):
                continue  # the row being edited keeps its lines open
            tree_row.set_expanded(index == position)

    def open_in_new_tab(self) -> None:
        """Open another register tab beside this one (#228)."""
        opener = getattr(self.manager, "open_register_tab", None)
        if opener is not None and self.db is not None:
            opener()

    def show_account(self, handle: str) -> None:
        if handle == self.account_handle:
            self._scroll_to_end = True
            self.refresh()
            return

        def switch() -> None:
            self.account_handle = handle
            self._scroll_to_end = True
            self.set_entry_status("")
            self.refresh()

        self.confirm_leave(switch)

    # ---------------------------------------------------------------- actions

    def _on_account_changed(self, picker, _param) -> None:
        if self._updating:
            return
        index = picker.get_selected()
        if 0 <= index < len(self._pickable):
            handle = self._pickable[index].handle
            if handle != self.account_handle:
                # Until the user answers, the picker keeps showing this account.
                self._select_in_picker(self.account_handle)
                self.show_account(handle)

    def _select_in_picker(self, handle: str | None) -> None:
        self._updating = True
        try:
            for index, account in enumerate(self._pickable):
                if account.handle == handle:
                    self.account_picker.set_selected(index)
                    break
        finally:
            self._updating = False

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
        if isinstance(payload, BLANK_PAYLOADS):
            return
        self.edit_transaction(payload.transaction)

    def edit_transaction(self, transaction) -> None:
        if self.db is None:
            return
        self.confirm_leave(lambda: self._open_editor(transaction))

    def _open_editor(self, transaction) -> None:
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

    def _on_open_window_clicked(self, _button) -> None:
        if self.account_handle is None:
            return
        opener = getattr(self.manager, "open_register_window", None)
        if callable(opener):
            opener(self.account_handle)

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
