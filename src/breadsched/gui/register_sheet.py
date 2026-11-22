"""The register as a sheet of cells, behind the GnuCash-style grid (``widgets/register_grid``).

Nothing here imports GTK: the grid draws what this model says and passes keys and
clicks to it, so every register behavior is testable without a display. The
model is one account's ledger rows (``engine.ledger.register``), a blank
transaction at the end, and a cursor: the transaction under the cursor is held
as a :class:`Draft` whose cells are plain text until it is saved through the
ordinary ``save_transaction`` service (it is never a parallel transaction model).

As in GnuCash's basic ledger, each transaction is one line: date, number,
description, transfer account, reconcile state, the two amount columns named for
the account type, and the running balance. A transaction with more than two
splits, or one the user splits, opens its split lines (memo, account, amounts)
beneath it while the cursor is on it; until it balances, its empty last line shows
the rest and takes it when given an account (#369).
Typing reads the shared rules in ``engine/entry_input``: date and number keys,
arithmetic amounts, accounts completed ``:`` segment by segment, and
descriptions quick-filled from earlier ones, which then propose the earlier
transaction's transfer and amount (``services/autocomplete``).

An account that holds a security (``ledger.security_of``) adds GnuCash's stock
register columns (#323): the shares each split adds, the price it implies, and
the shares held after it in place of the amount balance. Any two of shares,
price, and amount give the third (``entry_input.fill_trade``), and the shares
are saved as the split's quantity.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date

from ..api import ApiError
from ..api.local import book_server
from ..gen.engine import ledger
from ..gen.engine.cost_basis import shares_text
from ..gen.engine.entry_input import (
    DATE_KEYS,
    EntryInputError,
    is_arithmetic,
    parse_entry_date,
    quickfill_account,
    quickfill_description,
    trade_price,
)
from ..gen.lib.account import Account, AccountClass, AccountType
from ..gen.lib.commodity import Commodity
from ..gen.lib.money import Money
from ..gen.lib.transaction import ReconcileState, Transaction
from ..gen.services import (
    SaveTransaction,
    ToggleCleared,
    TransactionInput,
)
from ..gen.services.autocomplete import SuggestEntry, complete_note
from ..gen.services.register_view import RegisterView
from ..gen.services.transactions import reconciled_changes
from ..presentation.investments import trade_price_text
from . import hidden_accounts
from .split_entry import (
    _SECURITY_SPLIT_FIELDS,
    _SECURITY_TRANSACTION_FIELDS,
    _SPLIT_FIELDS,
    _TRADE_COLUMNS,
    _TRADE_FIELDS,
    _TRANSACTION_FIELDS,
    BLANK,
    IMBALANCE,
    NOTES,
    SPLIT,
    TRANSACTION,
    Draft,
    Line,
    SheetRow,
    SplitEntry,
    TradeQuestion,
    _frac,
    selectable_names,
)

__all__ = [
    "COLUMNS",
    "SECURITY_COLUMNS",
    "BLANK",
    "IMBALANCE",
    "SPLIT",
    "TRANSACTION",
    "Draft",
    "Line",
    "NOTES",
    "RegisterSheet",
    "SheetRow",
]

#: Column keys, in display order.
COLUMNS = ("date", "num", "description", "transfer", "reconcile", "increase", "decrease", "balance")
#: The columns of an account that holds a security, as in GnuCash's stock register.
SECURITY_COLUMNS = (
    "date",
    "num",
    "description",
    "transfer",
    "reconcile",
    "shares",
    "price",
    "increase",
    "decrease",
    "share_balance",
)
#: The columns of an account kept in a currency once a security moves through it:
#: a security's split shows and takes its shares and price there too (#407).
TRADE_COLUMNS = (
    "date",
    "num",
    "description",
    "transfer",
    "reconcile",
    "shares",
    "price",
    "increase",
    "decrease",
    "balance",
)
#: What the transfer cell shows for a transaction of more than two splits.
SPLIT_LABEL = "-- Split Transaction --"


class RegisterSheet(SplitEntry):
    """One account's register: rows, the cursor, and the transaction being typed."""

    #: Tab never leaves a register (past its last cell it saves); see ``SplitSheet``.
    leaving = 0

    def __init__(
        self, db, account: str, *, today: date | None = None, view: RegisterView | None = None
    ) -> None:
        self.db = db
        self.account = account
        self.today = today
        self.filter_text = ""
        self.rows: list[SheetRow] = []
        #: For each row, which transaction (counted from the top) it belongs to.
        self.ordinals: list[int] = []
        self.ledger_rows: list[ledger.RegisterRow] = []
        #: Double Line mode: each transaction has a second line for its notes.
        self.double_line = False
        #: True while a save is being written, so change signals wait for it.
        self.saving = False
        #: The transaction the last successful save wrote (#371).
        self.last_saved: str | None = None
        #: The cursor's row index and column.
        self.cursor = 0
        self.column = "date"
        self.draft = Draft(None, None)
        #: The date the blank transaction offers: the last one entered, else today.
        self.last_date: date | None = None
        #: The last message for the line under the register, and whether it is an error.
        self.message = ""
        self.error = False
        #: Accounts a split may use (full name → handle), and the transfer choices.
        self.account_names: dict[str, str] = {}
        self.transfer_names: dict[str, str] = {}
        #: Every account's full name by handle, and the accounts that hold securities.
        self.full_names: dict[str, str] = {}
        #: Every account as the book server listed it, by handle.
        self.accounts: dict[str, Account] = {}
        self.security_accounts: frozenset[str] = frozenset()
        #: Earlier descriptions in this register, most recent first.
        self.descriptions: list[str] = []
        #: Earlier notes or memos for one description, as last read (#407).
        self._note_choices: tuple[tuple[str, bool], tuple[str, ...]] | None = None
        self.debit_heading, self.credit_heading = "Increase", "Decrease"
        #: The security this account holds, or None for an account kept in a currency.
        self.security: Commodity | None = None
        #: The smallest share unit the account records.
        self.share_fraction = 1
        self.enabled = True
        self.disabled_reason = ""
        #: Asked when shares, price, and amount were all filled and one changed.
        self.trade_question: TradeQuestion | None = None
        #: The accounts of reconciled splits a save would change, waiting on a yes.
        self.reconciled_question: tuple[str, ...] | None = None
        self._reconciled_confirmed = False
        if view is not None:
            # Read in the background through the book server (#389).
            self.apply_view(view, keep_cursor=False)
        else:
            self.reload(keep_cursor=False)

    # ----------------------------------------------------------------- loading

    @property
    def account_object(self) -> Account | None:
        try:
            return book_server(self.db).account(self.account).account
        except ApiError:
            return None

    def _stored(self, handle: str) -> Transaction | None:
        """A transaction as the book now stores it, or ``None`` once it is gone."""
        try:
            return book_server(self.db).find_transaction(handle)
        except ApiError:
            return None

    @property
    def columns(self) -> tuple[str, ...]:
        """The columns this register shows, in display order."""
        if self.security is not None:
            return SECURITY_COLUMNS
        return TRADE_COLUMNS if self.shows_trades() else COLUMNS

    def shows_trades(self) -> bool:
        """Whether a security moves through this cash register: a stored transaction
        here has a split in one, or the transaction being entered does."""
        if not self.security_accounts:
            return False
        if any(self._holds_security(line) for line in self.draft.lines):
            return True
        return any(
            split.account in self.security_accounts
            for row in self.ledger_rows
            for split in row.transaction.splits
        )

    def _security_leg(self, transaction: Transaction):
        """The one split of ``transaction`` in a security, or None."""
        legs = [s for s in transaction.splits if s.account in self.security_accounts]
        return legs[0] if len(legs) == 1 else None

    def reload(self, *, keep_cursor: bool = True, changed: Iterable[str] | None = None) -> None:
        """Re-read the ledger, keeping the cursor on its transaction where it still is.

        With ``changed`` (the handles of transactions just saved or removed) only
        those are read again and the running balances recomputed in memory, so a
        save in a long register stays quick (#319); otherwise the whole ledger is
        read. A draft with unsaved typing survives a reload; one whose transaction
        has gone (deleted elsewhere) returns the cursor to the blank transaction.
        """
        if changed is not None and self.ledger_rows:
            # Only the changed transactions are read again (#319).
            try:
                changes = book_server(self.db).register_changes(self.account, tuple(changed))
            except ApiError:
                self._lose_account()
                return
            self._refresh_draft(keep_cursor)
            self._load_accounts()
            self.ledger_rows = changes.apply(self.ledger_rows)
            self._finish_load(changes.account)
            return
        try:
            view = book_server(self.db).register_view(self.account, None)
        except ApiError:
            self._lose_account()
            return
        self.apply_view(view, keep_cursor=keep_cursor)

    def apply_view(self, view: RegisterView, *, keep_cursor: bool = True) -> None:
        """Show every row of a register read through the book server.

        The view carries the rows with their running balances, the headings, and
        the security the account holds, so the sheet reads them in one request,
        which the register can make off GTK's loop (#389).
        """
        self.debit_heading, self.credit_heading = view.debit_label, view.credit_label
        holding = view.holding
        self.security = holding.security if holding is not None else None
        self.share_fraction = (
            holding.share_fraction if holding is not None else (view.account.commodity_scu or 1)
        )
        self.security_accounts = frozenset(view.security_accounts)
        self._refresh_draft(keep_cursor)
        self._load_accounts()
        self.ledger_rows = list(view.rows)
        self._finish_load(view.account)

    def _lose_account(self) -> None:
        self.ledger_rows = []
        self.rows = [SheetRow(BLANK, current=True)]
        self.enabled = False

    def _refresh_draft(self, keep_cursor: bool) -> None:
        source = self.draft.source
        if not keep_cursor:
            self.draft = self._blank_draft()
        elif source is not None:
            fresh = self._stored(source.handle)
            if fresh is None:
                self.draft = self._blank_draft()
            elif not self.draft.dirty():
                self.draft = self._load_draft(fresh, self.draft.split_handle)

    def _finish_load(self, account) -> None:
        seen: dict[str, None] = {}
        for row in reversed(self.ledger_rows):
            if row.transaction.description.strip():
                seen.setdefault(row.transaction.description, None)
        self.descriptions = list(seen)
        self.enabled, self.disabled_reason = True, ""
        if account.hidden and not hidden_accounts.shown():
            self.enabled, self.disabled_reason = (
                False,
                "Hidden accounts take no new transactions; show hidden accounts to use it",
            )
        elif account.placeholder:
            self.enabled, self.disabled_reason = False, "Placeholder accounts take no transactions"
        elif not self.transfer_names:
            self.enabled, self.disabled_reason = False, "No other visible account to transfer to"
        self._rebuild()

    def _load_accounts(self) -> None:
        referenced = set()
        if self.draft.source is not None:
            referenced = {split.account for split in self.draft.source.splits}
        records = book_server(self.db).accounts()
        self.full_names = {record.account.handle: record.full_name for record in records}
        self.accounts = {record.account.handle: record.account for record in records}
        self.account_names = selectable_names(records, referenced)
        self.transfer_names = {
            name: handle for name, handle in self.account_names.items() if handle != self.account
        }

    def _name(self, handle: str) -> str:
        """An account's full name, as the book server listed it."""
        name = self.full_names.get(handle)
        if name is None:
            # An account made since the list was read.
            self._load_accounts()
            name = self.full_names.get(handle, handle)
        return name

    def set_double_line(self, on: bool) -> None:
        """Show or hide each transaction's notes line, keeping the cursor's place."""
        if on == self.double_line:
            return
        if not on and 0 <= self.cursor < len(self.rows) and self.rows[self.cursor].kind == NOTES:
            self.cursor = self.header_index()
        self.double_line = on
        self._rebuild()

    def set_filter(self, text: str) -> None:
        self.filter_text = text
        self._rebuild()

    def _matches(self, row: ledger.RegisterRow) -> bool:
        return ledger.transaction_matches_named(
            row.transaction, self.filter_text, lambda handle: self.full_names.get(handle, "")
        )

    def _rebuild(self) -> None:
        """Lay the rows out again around the draft, keeping the cursor's place."""
        anchor = self._anchor()
        rows: list[SheetRow] = []
        draft = self.draft
        # Reuse the lines of the last layout that are unchanged: a new object for
        # every line of a long register on every save set off full garbage
        # collections (#319).
        earlier = {
            (id(row.register_row), row.kind, row.current): row
            for row in self.rows
            if row.register_row is not None
        }

        def line(kind: str, row: ledger.RegisterRow, mine: bool) -> SheetRow:
            found = earlier.get((id(row), kind, mine))
            if found is not None and found.register_row is row:
                return found
            return SheetRow(kind, row, current=mine)

        for row in self.ledger_rows:
            mine = (
                draft.source is not None
                and row.transaction.handle == draft.source.handle
                and row.split.handle == draft.split_handle
            )
            if not mine and not self._matches(row):
                continue
            rows.append(line(TRANSACTION, row, mine))
            if self.double_line:
                rows.append(line(NOTES, row, mine))
            if mine and draft.expanded:
                rows.extend(self._line_rows())
        blank_current = draft.source is None
        rows.append(SheetRow(BLANK, current=blank_current))
        if self.double_line:
            rows.append(SheetRow(NOTES, current=blank_current))
        if blank_current and draft.expanded:
            rows.extend(self._line_rows())
        self.rows = rows
        # Each row's transaction, counted from the top, so shading can alternate by
        # transaction: a notes or split line shares its transaction's band (#325).
        ordinal, ordinals = -1, []
        for item in rows:
            if item.kind in (TRANSACTION, BLANK):
                ordinal += 1
            ordinals.append(max(ordinal, 0))
        self.ordinals = ordinals
        self._restore(anchor)

    def _line_rows(self) -> list[SheetRow]:
        # The rest of an unbalanced entry is shown on its empty last line, ready
        # for an account (#369), not on a separate imbalance line.
        return [SheetRow(SPLIT, line=index, current=True) for index in range(len(self.draft.lines))]

    def _anchor(self) -> tuple[str, int | None]:
        """What the cursor is on: the draft's header line, or one of its split lines."""
        if 0 <= self.cursor < len(self.rows):
            row = self.rows[self.cursor]
            if row.kind == SPLIT:
                return ("split", row.line)
            if row.kind == NOTES and row.current:
                return ("notes", None)
        return ("header", None)

    def _restore(self, anchor: tuple[str, int | None]) -> None:
        header = self.header_index()
        kind, line = anchor
        if kind == "notes" and self.double_line:
            self.cursor = header + 1
            self.column = "description"
            return
        if kind == "split" and line is not None:
            for index in range(header + 1, len(self.rows)):
                if self.rows[index].kind == SPLIT and self.rows[index].line == line:
                    self.cursor = index
                    return
        self.cursor = header
        editable = self.editable_columns(self.cursor)
        if editable and self.column not in editable:
            self.column = editable[0]

    def header_index(self) -> int:
        """The row index of the transaction under the cursor (its own line)."""
        for index, row in enumerate(self.rows):
            if row.current and row.kind in (TRANSACTION, BLANK):
                return index
        return len(self.rows) - 1

    # ------------------------------------------------------------------ drafts

    def _blank_date(self) -> date:
        """The date a new entry offers: the last one entered here, else today."""
        return self.last_date or self.today or date.today()

    def _blank_draft(self) -> Draft:
        draft = Draft(None, None, date=self._blank_date().isoformat())
        draft.snapshot = draft.state()
        return draft

    def _load_draft(self, transaction, split_handle: str | None) -> Draft:
        mine = next((s for s in transaction.splits if s.handle == split_handle), None)
        others = [s for s in transaction.splits if s is not mine]
        draft = Draft(
            transaction,
            split_handle,
            date=transaction.post_date.isoformat(),
            num=transaction.num,
            description=transaction.description,
            notes=transaction.notes or "",
        )
        name = self._name(others[0].account) if len(others) == 1 else ""
        if mine is not None and len(others) == 1 and others[0].account != mine.account and name:
            draft.transfer = name
            draft.simple_handles = (mine.handle, others[0].handle)
            target = "increase" if mine.value > 0 else "decrease"
            if mine.value:
                setattr(draft, target, abs(mine.value).format())
            draft.transfer_touched = True
            if self.security is not None:
                self._load_trade(draft, mine)
        else:
            # More than two splits (or an unusual pair) can only be edited split by split.
            ordered = ([mine] if mine is not None else []) + others
            draft.expanded = True
            draft.lines = [self._line_of(split) for split in ordered]
            draft.lines.append(Line())
            draft.transfer = SPLIT_LABEL
        draft.snapshot = draft.state()
        return draft

    # ------------------------------------------------------------ the display

    def headings(self) -> dict[str, str]:
        return {
            "date": "Date",
            "num": "Num",
            "description": "Description",
            "transfer": "Transfer",
            "reconcile": "R",
            "increase": self.debit_heading,
            "decrease": self.credit_heading,
            "balance": "Balance",
            "shares": "Shares",
            "price": "Price",
            "share_balance": "Shares Held",
        }

    def text(self, index: int, column: str) -> str:
        """What cell (``index``, ``column``) shows."""
        row = self.rows[index]
        draft = self.draft
        if row.kind == SPLIT:
            assert row.line is not None
            if row.line == self.balancing_line() and column in ("increase", "decrease"):
                increase, decrease = self._balancing_cells()
                return increase if column == "increase" else decrease
            return draft.lines[row.line].cell(column)
        if row.kind == NOTES:
            if column != "description":
                return ""
            if row.current:
                return draft.notes
            return row.register_row.transaction.notes or "" if row.register_row else ""
        if row.kind == IMBALANCE:
            if column == "description":
                return "Imbalance"
            if column == "balance":
                return self.residual().format(parens_negative=True)
            return ""
        if row.current:
            # A cash register's collapsed row shows the security leg's shares and
            # price as stored (they are typed in its split lines, #407).
            stored_trade = self.security is None and column in _TRADE_FIELDS
            if column in _SECURITY_TRANSACTION_FIELDS and not stored_trade:
                if draft.expanded and column in ("transfer", *_TRADE_COLUMNS):
                    return SPLIT_LABEL if column == "transfer" else ""
                return draft.cell(column)
        ledger_row = row.register_row
        if ledger_row is None:
            if row.current:
                return draft.cell(column)
            # The blank transaction waits at the end with the date it will offer.
            return self._blank_date().isoformat() if column == "date" else ""
        if column == "date":
            return ledger_row.post_date.isoformat()
        if column == "num":
            return ledger_row.transaction.num
        if column == "description":
            return ledger_row.description
        if column == "transfer":
            others = [
                s for s in ledger_row.transaction.splits if s.handle != ledger_row.split.handle
            ]
            if len(others) == 1:
                return self._name(others[0].account)
            return SPLIT_LABEL
        if column == "reconcile":
            return ledger_row.split.reconcile.value
        if column == "increase":
            return ledger_row.debit.format() if ledger_row.debit else ""
        if column == "decrease":
            return ledger_row.credit.format() if ledger_row.credit else ""
        if column == "balance":
            return ledger_row.running.format(parens_negative=True)
        if column in ("shares", "price") and self.security is None:
            leg = self._security_leg(ledger_row.transaction)
            if leg is None or not leg.quantity:
                return ""
            if column == "shares":
                return shares_text(abs(leg.quantity))
            return trade_price_text(trade_price(_frac(leg.value), _frac(leg.quantity)))
        if column == "shares":
            return shares_text(ledger_row.shares) if ledger_row.shares else ""
        if column == "price":
            price = trade_price(_frac(ledger_row.split.value), _frac(ledger_row.shares))
            return trade_price_text(price)
        if column == "share_balance":
            held = ledger_row.share_balance
            return shares_text(held) if held is not None else ""
        return ""

    def negative(self, index: int, column: str) -> bool:
        """Whether the cell shows a negative figure (drawn in the negative colour)."""
        row = self.rows[index]
        if column == "share_balance":
            held = row.register_row.share_balance if row.register_row is not None else None
            return held is not None and held < 0
        if column != "balance":
            return False
        if row.kind == IMBALANCE:
            return True
        return row.register_row is not None and row.register_row.running < 0

    def future(self, index: int) -> bool:
        """Whether the row's transaction is dated after today (GnuCash rules them off)."""
        row = self.rows[index]
        today = self.today or date.today()
        return row.register_row is not None and row.register_row.post_date > today

    def editable_columns(self, index: int) -> tuple[str, ...]:
        row = self.rows[index] if 0 <= index < len(self.rows) else None
        if row is None or row.kind == IMBALANCE:
            return ()
        if not self.enabled and row.kind == BLANK:
            return ()
        if row.kind == SPLIT:
            assert row.line is not None
            line = self.draft.lines[row.line]
            trades = self.security is not None or self._holds_security(line)
            return _SECURITY_SPLIT_FIELDS if trades else _SPLIT_FIELDS
        if row.kind == NOTES:
            if not self.enabled and row.register_row is None:
                return ()
            return ("description",)
        if self.draft.expanded and row.current:
            return ("date", "num", "description")
        return _SECURITY_TRANSACTION_FIELDS if self.security is not None else _TRANSACTION_FIELDS

    def cell_count(self) -> int:
        return len(self.rows)

    def summary(self, index: int) -> str:
        """A row read aloud: date, description, amount, and balance."""
        parts = [self.text(index, column) for column in ("date", "description", "transfer")]
        amount = self.text(index, "increase") or (
            f"-{self.text(index, 'decrease')}" if self.text(index, "decrease") else ""
        )
        if self.security is not None:
            shares = self.text(index, "shares")
            price = self.text(index, "price")
            held = self.text(index, "share_balance")
            trade = (shares and f"{shares} shares", price and f"at {price}")
            return ", ".join(
                part for part in (*parts, *trade, amount, held and f"{held} shares held") if part
            )
        balance = self.text(index, "balance")
        return ", ".join(
            part for part in (*parts, amount, balance and f"balance {balance}") if part
        )

    def closing_balance(self) -> Money | None:
        return self.ledger_rows[-1].running if self.ledger_rows else None

    def closing_shares(self) -> Money | None:
        """The shares held after the last row, in an account that holds a security."""
        if self.security is None:
            return None
        return self.ledger_rows[-1].share_balance if self.ledger_rows else Money(0)

    # --------------------------------------------------------------- editing

    def set_text(self, text: str) -> None:
        """Put typed text in the cursor's cell."""
        row = self.rows[self.cursor]
        if row.kind == SPLIT:
            assert row.line is not None
            self._set_line_text(row.line, self.column, text)
            return
        if row.kind == NOTES:
            self.draft.notes = text
            return
        self.draft.set_cell(self.column, text)

    def cell_text(self) -> str:
        """The text in the cursor's cell, for the editor to start from."""
        return self.text(self.cursor, self.column)

    def quickfill(self, typed: str) -> tuple[str, list[str]]:
        """What the cursor's cell shows for ``typed`` and the accounts it may mean.

        An account cell completes the segment being typed; the description cell
        completes from earlier descriptions in this register. The caller selects
        the part beyond ``typed``.
        """
        kind = self.rows[self.cursor].kind
        if kind == SPLIT:
            return self._quickfill_line(self.column, typed)
        if self.column == "transfer":
            return quickfill_account(typed, self.transfer_names)
        if self.column == "description" and kind != NOTES:
            if self.draft.source is None:
                found = quickfill_description(typed, self.descriptions)
                if found is not None:
                    return found, []
        elif self.column == "description":
            # A note completes from those written for the same description before,
            # beginning with what is typed, or with a word that does (#407).
            found = complete_note(typed, self._earlier_notes(memos=False))
            if found is not None:
                return found, []
        return typed, []

    def date_key(self, text: str, key: str) -> str | None:
        """A date shortcut applied to ``text``; None lets the key be typed."""
        if key not in DATE_KEYS:
            return None
        try:
            base = parse_entry_date(text, self._date_base())
            return parse_entry_date(key, base).isoformat()
        except EntryInputError:
            return None

    def num_key(self, text: str, step: int) -> str | None:
        """The number ``step`` on; None lets the key be typed into other text."""
        if text.strip() and not text.strip()[-1].isdigit():
            return None
        try:
            return book_server(self.db).next_num(self.account, text, step)
        except ApiError:
            return None

    def _date_base(self) -> date:
        source = self.draft.source
        if source is not None:
            return source.post_date
        return self._blank_date()

    def settle(self) -> bool:
        """Read the cursor's cell before leaving it; False (with a message) when unreadable.

        A date shows as ``YYYY-MM-DD``, arithmetic as its result (a negative
        result moves to the other amount column), and an account as its full
        name. Empty cells are fine until the transaction is saved.
        """
        column = self.column
        row = self.rows[self.cursor]
        if row.kind == SPLIT:
            return self._settle_line(row)
        text = self.cell_text().strip()
        if column == "date" and text:
            try:
                self.draft.date = parse_entry_date(text, self._date_base()).isoformat()
            except EntryInputError:
                return self._fail("Enter a date such as 2026-03-15, 3/15, 15, or t.")
        elif column == "transfer" and text and text != SPLIT_LABEL:
            resolved = self._resolve_account(text, self.transfer_names)
            if resolved is None:
                return self._fail(f"No account matches “{text}”.")
            self.set_text(resolved)
        elif column in ("increase", "decrease") and text and is_arithmetic(text):
            if not self._settle_arithmetic(column, text):
                return False
        elif column == "description" and row.kind != NOTES and text:
            self.propose()
        if column in _TRADE_COLUMNS and self.security is not None and row.kind != NOTES:
            return self._fill_trade(row, column)
        return True

    # ------------------------------------------------------------------ trades

    def _trade_holder(self, row: SheetRow) -> Draft | Line | None:
        """What holds the trade cells of ``row``: the draft, or a split line in a security."""
        if row.kind != SPLIT:
            if self.security is None:
                return None
            return None if self.draft.expanded else self.draft
        assert row.line is not None
        line = self.draft.lines[row.line]
        return line if self._holds_security(line) else None

    def propose(self) -> bool:
        """Fill a new entry's untouched cells from the latest matching transaction."""
        draft = self.draft
        if draft.source is not None or not self.enabled or not draft.description.strip():
            return False
        try:
            proposal = book_server(self.db).suggest_entry(
                SuggestEntry(description=draft.description, account=self.account)
            )
        except ApiError:
            return False
        suggestion = proposal.suggestion
        if suggestion is None:
            return False
        source = f"{suggestion.when.isoformat()} “{suggestion.description}”"
        if draft.expanded or len(suggestion.splits) > 2:
            if draft.transfer_touched or any(line.has_input() for line in draft.lines):
                return False
            draft.expanded = True
            draft.lines = [
                Line(
                    account=proposal.name(split.account),
                    memo=split.memo,
                    increase=split.value.format() if split.value > 0 else "",
                    decrease=(-split.value).format() if split.value < 0 else "",
                )
                for split in suggestion.splits
            ]
            draft.lines.append(Line())
            noted = self._propose_notes(suggestion.notes)
            self._rebuild()
            self._say(
                f"Proposed {len(suggestion.splits)} splits"
                + (" and its notes" if noted else "")
                + f" from {source}."
            )
            return True
        filled = []
        if not draft.transfer_touched and suggestion.transfer_account is not None:
            name = proposal.name(suggestion.transfer_account)
            if name in self.transfer_names:
                draft.transfer = name
                filled.append(name)
        if not (draft.increase.strip() or draft.decrease.strip()) and suggestion.amount is not None:
            target = "increase" if suggestion.amount > 0 else "decrease"
            setattr(draft, target, abs(suggestion.amount).format())
            filled.append(abs(suggestion.amount).format())
        if self._propose_notes(suggestion.notes):
            filled.append("its notes")
            self._rebuild()
        if filled:
            self._say(
                f"Proposed from {source}: {', '.join(filled)}. Edit anything, then press Enter."
            )
        return bool(filled)

    def _propose_notes(self, notes: str) -> bool:
        """Take an earlier transaction's notes into a new entry whose notes are empty."""
        if not notes.strip() or self.draft.notes.strip():
            return False
        self.draft.notes = notes
        return True

    # ----------------------------------------------------------------- splits

    def toggle_split(self) -> bool:
        """Open the transaction under the cursor into split lines, or close it again.

        Closing is possible while at most two lines hold a split, one of them in
        this account; otherwise the lines stay, with the reason in the message.
        """
        draft = self.draft
        # Showing an unchanged transaction another way is not an edit of it.
        clean = not draft.dirty()
        done = self._toggle_split()
        if done and clean:
            draft.snapshot = draft.state()
        return done

    def _toggle_split(self) -> bool:
        draft = self.draft
        if not draft.expanded:
            try:
                amount = self._simple_amount()
            except ValueError:
                amount = None
            mine, other = draft.simple_handles
            memo = {split.handle: split.memo for split in getattr(draft.source, "splits", ())}
            own = self._name(self.account)
            draft.lines = [
                Line(
                    account=own,
                    memo=memo.get(mine, "") if mine else "",
                    increase=amount.format() if amount is not None and amount > 0 else "",
                    decrease=(-amount).format() if amount is not None and amount < 0 else "",
                    handle=mine,
                    shares=draft.shares,
                    price=draft.price,
                    settled=dict(draft.settled),
                ),
                Line(
                    account=draft.transfer if draft.transfer != SPLIT_LABEL else "",
                    memo=memo.get(other, "") if other else "",
                    increase=(-amount).format() if amount is not None and amount < 0 else "",
                    decrease=amount.format() if amount is not None and amount > 0 else "",
                    handle=other,
                ),
                Line(),
            ]
            draft.expanded = True
            self.column = "description"
            self._rebuild()
            self.cursor = self.header_index() + 1
            return True
        filled = [line for line in draft.lines if line.has_input()]
        own = self._name(self.account)
        own_line = next((line for line in filled if line.account == own), None)
        if len(filled) > 2 or (filled and own_line is None):
            return self._fail(
                "A transaction with more than two splits, or none in this account, stays split."
            )
        other_line = next((line for line in filled if line is not own_line), None)
        draft.expanded = False
        draft.lines = []
        draft.transfer = other_line.account if other_line is not None else ""
        draft.increase = own_line.increase if own_line is not None else ""
        draft.decrease = own_line.decrease if own_line is not None else ""
        draft.shares = own_line.shares if own_line is not None else ""
        draft.price = own_line.price if own_line is not None else ""
        draft.settled = dict(own_line.settled) if own_line is not None else {}
        draft.simple_handles = (
            own_line.handle if own_line is not None else None,
            other_line.handle if other_line is not None else None,
        )
        self.cursor = self.header_index()
        self._rebuild()
        self.column = "transfer"
        return True

    def _simple_amount(self) -> Money | None:
        draft = self.draft
        for text, sign in ((draft.increase, 1), (draft.decrease, -1)):
            if text.strip():
                value = self.read_amount(text)
                return value if sign > 0 else -value
        return None

    # ------------------------------------------------------------- the cursor

    def move_to(self, index: int, column: str | None = None) -> bool:
        """Put the cursor on another row (saving the transaction it leaves first)."""
        index = max(0, min(index, len(self.rows) - 1))
        target = self.rows[index]
        same = target.current
        if not same:
            handle, split = (
                (target.register_row.transaction.handle, target.register_row.split.handle)
                if target.register_row is not None
                else (None, None)
            )
            if not self.leave():
                return False
            if handle is None:
                self.draft = self._blank_draft()
            else:
                fresh = self._stored(handle)
                if fresh is None:
                    return self._fail("That transaction is no longer in the book.")
                self.draft = self._load_draft(fresh, split)
            self._load_accounts()
            self.cursor = 0
            self._rebuild()
            # Clicking another transaction's notes line opens it there.
            index = self.header_index() + (1 if target.kind == NOTES else 0)
            self.cursor = index
        else:
            if self.rows[self.cursor].kind == SPLIT and not self.settle():
                return False
            self.cursor = index
        editable = self.editable_columns(self.cursor)
        if column in editable:
            self.column = column
        elif self.column not in editable and editable:
            self.column = editable[0]
        return True

    def move(self, step: int) -> bool:
        """Up/Down: the row above or below, saving what the cursor leaves."""
        if not self.settle():
            return False
        target = self.cursor + step
        if target < 0 or target >= len(self.rows):
            return True
        # Skip the imbalance line, which takes no typing.
        while 0 <= target < len(self.rows) and self.rows[target].kind == IMBALANCE:
            target += step
        if not 0 <= target < len(self.rows):
            return True
        return self.move_to(target, self.column)

    def tab(self, backwards: bool = False) -> bool:
        """Tab: the next cell of this transaction; past its last, save and go on."""
        if not self.settle():
            return False
        order = self._tab_order()
        position = (
            order.index((self.cursor, self.column))
            if (
                self.cursor,
                self.column,
            )
            in order
            else 0
        )
        step = -1 if backwards else 1
        target = position + step
        if not backwards and 0 <= target < len(order):
            target = self._expense_hop(order, position, target)
        if 0 <= target < len(order):
            self.cursor, self.column = order[target]
            return True
        if backwards:
            return True
        # Past the last cell, like Enter: save and move to the next transaction.
        return self.enter()

    def _expense_hop(self, order: list[tuple[int, str]], position: int, target: int) -> int:
        """Tab from an expense transfer goes to the money-out column (#369).

        Money spent on an expense leaves this account, so it belongs in Withdrawal
        or Charge, not Deposit or Payment: from the transfer cell of a two-split
        entry whose transfer is an expense, Tab skips to that column. Only this one
        step changes; every other Tab follows the usual order.
        """
        cursor, column = order[position]
        if column != "transfer" or self.security is not None or order[target][1] != "increase":
            return target
        row = self.rows[cursor] if 0 <= cursor < len(self.rows) else None
        if row is None or row.kind == SPLIT or self.draft.expanded:
            return target
        name = self._resolve_account(self.draft.transfer.strip(), self.account_names)
        account = self.accounts.get(self.account_names[name]) if name is not None else None
        if account is None or account.account_class is not AccountClass.EXPENSE:
            return target
        return next(
            (i for i in range(target, len(order)) if order[i] == (cursor, "decrease")), target
        )

    def _tab_order(self) -> list[tuple[int, str]]:
        header = self.header_index()
        order = [(header, column) for column in self.editable_columns(header)]
        for index in range(header + 1, len(self.rows)):
            row = self.rows[index]
            if not row.current or row.kind not in (NOTES, SPLIT, IMBALANCE):
                break
            order.extend((index, column) for column in self.editable_columns(index))
        return order

    def enter(self) -> bool:
        """Enter: save the transaction under the cursor and move to the next one.

        Saving the blank transaction leaves the cursor on a new blank one; an
        unchanged transaction just moves on.
        """
        if not self.settle():
            return False
        blank = self.draft.source is None
        if blank and not self.draft.dirty():
            return True
        if not self.save():
            return False
        if blank:
            self.cursor = self.header_index()
            self.column = "date"
            return True
        # The saved transaction may have moved (a new date); go on from where it is
        # now to the next transaction, past its own notes and split lines (#325).
        header = self.header_index()
        following = next(
            (
                index
                for index in range(header + 1, len(self.rows))
                if self.rows[index].kind in (TRANSACTION, BLANK)
            ),
            len(self.rows) - 1,
        )
        self.move_to(following, "date")
        return True

    def _reconciled_changes(self, splits) -> tuple[str, ...]:
        """The names of reconciled splits that ``splits`` would change or drop."""
        source = self.draft.source
        if source is None:
            return ()
        return tuple(self._name(account) for account in reconciled_changes(source, splits))

    def confirm_reconciled(self) -> bool:
        """Save a change to a reconciled split once it was confirmed, as Enter does."""
        self.reconciled_question = None
        self._reconciled_confirmed = True
        try:
            return self.enter()
        finally:
            self._reconciled_confirmed = False

    def escape(self) -> None:
        """Escape: put the transaction back as it was (the blank one empties)."""
        source = self.draft.source
        if source is None:
            self.draft = self._blank_draft()
        else:
            fresh = self._stored(source.handle)
            self.draft = (
                self._load_draft(fresh, self.draft.split_handle)
                if fresh is not None
                else self._blank_draft()
            )
        self.cursor = 0
        self._rebuild()
        self.cursor = self.header_index()
        self.column = (
            self.editable_columns(self.cursor)[0] if self.editable_columns(self.cursor) else "date"
        )
        self._say("")

    def leave(self) -> bool:
        """Save the transaction under the cursor if it changed; False keeps the cursor."""
        if not self.draft.dirty():
            return True
        return self.save()

    def to_blank(self) -> bool:
        """Put the cursor on the blank transaction at the end, ready for a new entry."""
        if not self.move_to(len(self.rows) - 1, "date"):
            return False
        self.cursor = self.header_index()
        self.column = "date"
        return True

    def select_transaction(self, handle: str) -> bool:
        """Put the cursor on ``handle``'s line in this register."""
        for index, row in enumerate(self.rows):
            if row.register_row is not None and row.register_row.transaction.handle == handle:
                return self.move_to(index)
        return False

    # ------------------------------------------------------------------- saving

    def save(self) -> bool:
        """Save the transaction under the cursor as one balanced transaction."""
        draft = self.draft
        account = self.account_object
        if account is None or (draft.source is None and not self.enabled):
            return self._fail(self.disabled_reason or "This register takes no new transactions.")
        try:
            when = parse_entry_date(draft.date, self._date_base())
        except EntryInputError:
            return self._fail("Enter a date such as 2026-03-15, 3/15, 15, or t.", "date")
        description = draft.description.strip()
        if not description:
            return self._fail("Enter a description.", "description")
        currency = (draft.source.currency if draft.source is not None else None) or book_server(
            self.db
        ).transaction_currency()
        if draft.expanded:
            splits = self._line_splits(currency)
            if not splits:
                return False
            amount = sum(
                (split.value.value for split in splits if split.account == self.account), Money(0)
            )
        else:
            built = self._two_splits(currency)
            if not built:
                return False
            splits, amount = built
        reconciled = self._reconciled_changes(splits)
        if reconciled and not self._reconciled_confirmed and not self.reconciled_allowed():
            # A reconciled split's account or amount changes: ask first (#403).
            self.reconciled_question = reconciled
            return self._fail(
                "This changes a reconciled split ("
                + ", ".join(reconciled)
                + "); confirm to save it anyway."
            )
        self.saving = True
        try:
            saved = self._save_transaction(when, description, currency, splits)
        except ApiError as error:
            return self._fail(str(error))
        finally:
            self.saving = False
        if draft.source is None:
            self.last_date = when
            heading = self.debit_heading if amount > 0 else self.credit_heading
            self._say(
                f"Posted {description}: {abs(amount).format()} {heading}."
                if amount
                else f"Posted {description}."
            )
        elif not any(split.account == self.account for split in splits):
            moved = ", ".join(
                sorted(
                    {
                        self._name(split.account)
                        for split in splits
                        if split.account not in {item.account for item in draft.source.splits}
                    }
                )
            )
            self._say(
                f"Moved {description} to {moved}; it no longer shows in this register."
                if moved
                else f"Saved {description}; it no longer shows in this register."
            )
        else:
            self._say(f"Saved changes to {description}.")
        self.draft = self._blank_draft()
        self.last_saved = saved.handle
        changed = {saved.handle}
        if draft.source is not None:
            changed.add(draft.source.handle)
        self.reload(keep_cursor=False, changed=changed)
        if draft.source is not None:
            self.select_transaction(draft.source.handle)
        return True

    def _save_transaction(self, when, description: str, currency: str, splits):
        """Write the draft as one balanced transaction through the book server (#359)."""
        draft = self.draft
        return book_server(self.db).save_transaction(
            SaveTransaction(
                TransactionInput(
                    post_date=when,
                    description=description,
                    num=draft.num.strip(),
                    # Typed on the notes line in Double Line mode; otherwise kept.
                    notes=draft.notes,
                    currency=currency,
                    splits=tuple(splits),
                ),
                existing_handle=draft.source.handle if draft.source is not None else None,
                source=draft.source,
            ),
        )

    def _two_splits(self, currency: str):
        draft = self.draft
        transfer = self._resolve_account(draft.transfer.strip(), self.transfer_names)
        if not draft.transfer.strip() or transfer is None:
            return self._fail("Choose a transfer account: type part of its name.", "transfer")
        column = "decrease" if draft.decrease.strip() else "increase"
        text = getattr(draft, column).strip()
        mine, other = draft.simple_handles if draft.source is not None else (None, None)
        quantity = None
        if self.security is not None:
            try:
                quantity = self._quantity(draft, mine)
            except EntryInputError:
                return self._fail("Enter shares such as 12.345.", "shares")
            if not quantity:
                return self._fail(
                    "Enter the shares bought or sold, or a price and an amount.", "shares"
                )
        if not text and quantity:
            # Shares with no amount: a share split, or shares moved in kind.
            amount = Money(0)
        elif not text:
            return self._fail(
                f"Enter an amount under {self.debit_heading} or {self.credit_heading}.", "increase"
            )
        else:
            try:
                value = self.read_amount(text)
            except ValueError:
                return self._fail("Enter an amount, or arithmetic such as 12.50+3.", column)
            if value <= 0:
                return self._fail("Amount must be greater than zero.", column)
            amount = value if column == "increase" else -value
        handle = self.transfer_names[transfer]
        return (
            self._split_input(self.account, amount, currency, mine, quantity=quantity),
            self._split_input(handle, -amount, currency, other),
        ), amount

    # ---------------------------------------------------------------- reconcile

    def _needs_own_split(self) -> bool:
        # An existing transaction may move out of this account entirely, such as a
        # purchase entered on the wrong card (#364); a new one must belong here.
        return self.draft.source is None

    def toggle_reconcile(self, index: int) -> bool:
        """Mark a transaction's split in this account cleared, or not cleared."""
        row = self.rows[index]
        if row.kind != TRANSACTION or row.register_row is None:
            return False
        try:
            toggled = book_server(self.db).toggle_cleared(
                ToggleCleared(row.register_row.transaction.handle, row.register_row.split.handle),
            )
        except ApiError as error:
            return self._fail(str(error))
        state = "cleared" if toggled is ReconcileState.CLEARED else "not cleared"
        self._say(f"Marked {row.register_row.description} {state}.")
        self.reload(changed={row.register_row.transaction.handle})
        return True

    # ----------------------------------------------------------------- messages

    def _fail(self, text: str, column: str | None = None, line: int | None = None) -> bool:
        """Report why the cursor stays, moving it to the cell to correct."""
        self.message, self.error = text, True
        if line is not None:
            header = self.header_index()
            for index in range(header + 1, len(self.rows)):
                if self.rows[index].kind == SPLIT and self.rows[index].line == line:
                    self.cursor = index
                    break
        elif column is not None:
            self.cursor = self.header_index()
        if column is not None and column in self.editable_columns(self.cursor):
            self.column = column
        return False

    @property
    def account_type(self) -> AccountType | None:
        account = self.account_object
        return account.atype if account is not None else None
