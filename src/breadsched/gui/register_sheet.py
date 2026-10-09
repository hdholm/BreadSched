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
beneath it while the cursor is on it, with an imbalance line until it balances.
Typing reads the shared rules in ``engine/entry_input``: date and number keys,
arithmetic amounts, accounts completed ``:`` segment by segment, and
descriptions quick-filled from earlier ones, which then propose the earlier
transaction's transfer and amount (``services/autocomplete``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from ..gen.engine import ledger
from ..gen.engine.entry_input import (
    DATE_KEYS,
    EntryInputError,
    is_arithmetic,
    parse_entry_date,
    quickfill_account,
    quickfill_description,
)
from ..gen.lib.account import AccountType
from ..gen.lib.amount import Amount
from ..gen.lib.money import Money
from ..gen.lib.transaction import ReconcileState, Transaction
from ..gen.services import (
    SaveTransaction,
    ToggleCleared,
    TransactionInput,
    TransactionSplitInput,
    save_transaction,
    toggle_cleared,
    transaction_currency,
)
from ..gen.services.autocomplete import SuggestEntry, suggest_entry
from ..gen.services.entry_input import next_entry_num, read_entry_amount
from ..presentation import service_error_message

__all__ = [
    "COLUMNS",
    "BLANK",
    "IMBALANCE",
    "SPLIT",
    "TRANSACTION",
    "Draft",
    "Line",
    "RegisterSheet",
    "SheetRow",
]

#: Row kinds.
TRANSACTION = "transaction"
SPLIT = "split"
IMBALANCE = "imbalance"
BLANK = "blank"

#: Column keys, in display order.
COLUMNS = ("date", "num", "description", "transfer", "reconcile", "increase", "decrease", "balance")
#: What the transfer cell shows for a transaction of more than two splits.
SPLIT_LABEL = "-- Split Transaction --"
_TRANSACTION_FIELDS = ("date", "num", "description", "transfer", "increase", "decrease")
_SPLIT_FIELDS = ("description", "transfer", "increase", "decrease")


@dataclass
class Line:
    """One editable split beneath an opened transaction, in the split's own direction."""

    account: str = ""
    memo: str = ""
    increase: str = ""
    decrease: str = ""
    #: The stored split this line edits, or None for a new one.
    handle: str | None = None

    def has_input(self) -> bool:
        return bool(self.account.strip() or self.memo.strip() or self.amount_text()[0])

    def amount_text(self) -> tuple[str, int]:
        if self.increase.strip():
            return self.increase.strip(), 1
        return self.decrease.strip(), -1

    def cell(self, column: str) -> str:
        return {
            "description": self.memo,
            "transfer": self.account,
            "increase": self.increase,
            "decrease": self.decrease,
        }.get(column, "")


@dataclass
class Draft:
    """The transaction under the cursor, as the text in its cells."""

    #: The stored transaction, or None for the blank one.
    source: Transaction | None
    #: This register's split of it.
    split_handle: str | None
    date: str = ""
    num: str = ""
    description: str = ""
    transfer: str = ""
    increase: str = ""
    decrease: str = ""
    expanded: bool = False
    lines: list[Line] = field(default_factory=list)
    #: Split handles behind the register and transfer sides of a two-split entry.
    simple_handles: tuple[str | None, str | None] = (None, None)
    #: Whether the user chose the transfer (a proposal never overwrites it).
    transfer_touched: bool = False
    snapshot: tuple = ()

    def state(self) -> tuple:
        lines = tuple(
            (line.handle, line.account.strip(), line.memo.strip(), *line.amount_text())
            for line in self.lines
            if line.has_input()
        )
        simple = (
            ()
            if self.expanded
            else (self.transfer.strip(), self.increase.strip(), self.decrease.strip())
        )
        return (
            self.date.strip(),
            self.num.strip(),
            self.description.strip(),
            self.expanded,
            simple,
            lines,
        )

    def dirty(self) -> bool:
        return self.state() != self.snapshot

    def cell(self, column: str) -> str:
        return getattr(self, column) if column in _TRANSACTION_FIELDS else ""

    def set_cell(self, column: str, text: str) -> None:
        if column in _TRANSACTION_FIELDS:
            setattr(self, column, text)
            if column == "transfer":
                self.transfer_touched = True
            elif column in ("increase", "decrease") and text.strip():
                setattr(self, "decrease" if column == "increase" else "increase", "")


@dataclass(frozen=True, slots=True)
class SheetRow:
    kind: str
    #: The ledger row of a transaction line; None for the blank transaction.
    register_row: ledger.RegisterRow | None = None
    #: For a split line, its index in the draft's lines.
    line: int | None = None
    #: Whether this row belongs to the transaction under the cursor.
    current: bool = False

    @property
    def handle(self) -> str | None:
        return self.register_row.transaction.handle if self.register_row is not None else None


class RegisterSheet:
    """One account's register: rows, the cursor, and the transaction being typed."""

    def __init__(self, db, account: str, *, today: date | None = None) -> None:
        self.db = db
        self.account = account
        self.today = today
        self.filter_text = ""
        self.rows: list[SheetRow] = []
        self.ledger_rows: list[ledger.RegisterRow] = []
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
        #: Earlier descriptions in this register, most recent first.
        self.descriptions: list[str] = []
        self.debit_heading, self.credit_heading = "Increase", "Decrease"
        self.enabled = True
        self.disabled_reason = ""
        self.reload(keep_cursor=False)

    # ----------------------------------------------------------------- loading

    @property
    def account_object(self):
        return self.db.get_account(self.account)

    def reload(self, *, keep_cursor: bool = True) -> None:
        """Re-read the ledger, keeping the cursor on its transaction where it still is.

        A draft with unsaved typing survives a reload; one whose transaction has
        gone (deleted elsewhere) returns the cursor to the blank transaction.
        """
        account = self.account_object
        if account is None:
            self.ledger_rows = []
            self.rows = [SheetRow(BLANK, current=True)]
            self.enabled = False
            return
        self.debit_heading, self.credit_heading = ledger.register_headings(account.atype)
        source = self.draft.source
        if not keep_cursor:
            self.draft = self._blank_draft()
        elif source is not None:
            fresh = self.db.get_transaction(source.handle)
            if fresh is None:
                self.draft = self._blank_draft()
            elif not self.draft.dirty():
                self.draft = self._load_draft(fresh, self.draft.split_handle)
        self._load_accounts()
        self.ledger_rows = ledger.register(self.db, self.account)
        seen: dict[str, None] = {}
        for row in reversed(self.ledger_rows):
            if row.transaction.description.strip():
                seen.setdefault(row.transaction.description, None)
        self.descriptions = list(seen)
        self.enabled, self.disabled_reason = True, ""
        if account.hidden:
            self.enabled, self.disabled_reason = False, "Hidden accounts take no new transactions"
        elif account.placeholder:
            self.enabled, self.disabled_reason = False, "Placeholder accounts take no transactions"
        elif not self.transfer_names:
            self.enabled, self.disabled_reason = False, "No other visible account to transfer to"
        self._rebuild()

    def _load_accounts(self) -> None:
        referenced = set()
        if self.draft.source is not None:
            referenced = {split.account for split in self.draft.source.splits}
        names: dict[str, str] = {}
        for item in self.db.iter_accounts():
            if item.is_root or item.placeholder or item.parent is None:
                continue
            if item.hidden and item.handle not in referenced:
                continue
            names.setdefault(self.db.full_name(item), item.handle)
        self.account_names = dict(sorted(names.items(), key=lambda pair: pair[0].casefold()))
        self.transfer_names = {
            name: handle for name, handle in self.account_names.items() if handle != self.account
        }

    def set_filter(self, text: str) -> None:
        self.filter_text = text
        self._rebuild()

    def _matches(self, row: ledger.RegisterRow) -> bool:
        needle = self.filter_text.strip().casefold()
        if not needle:
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

    def _rebuild(self) -> None:
        """Lay the rows out again around the draft, keeping the cursor's place."""
        anchor = self._anchor()
        rows: list[SheetRow] = []
        draft = self.draft
        for row in self.ledger_rows:
            mine = (
                draft.source is not None
                and row.transaction.handle == draft.source.handle
                and row.split.handle == draft.split_handle
            )
            if not mine and not self._matches(row):
                continue
            rows.append(SheetRow(TRANSACTION, row, current=mine))
            if mine and draft.expanded:
                rows.extend(self._line_rows())
        blank_current = draft.source is None
        rows.append(SheetRow(BLANK, current=blank_current))
        if blank_current and draft.expanded:
            rows.extend(self._line_rows())
        self.rows = rows
        self._restore(anchor)

    def _line_rows(self) -> list[SheetRow]:
        rows = [SheetRow(SPLIT, line=index, current=True) for index in range(len(self.draft.lines))]
        if self.residual():
            rows.append(SheetRow(IMBALANCE, current=True))
        return rows

    def _anchor(self) -> tuple[str, int | None]:
        """What the cursor is on: the draft's header line, or one of its split lines."""
        if 0 <= self.cursor < len(self.rows):
            row = self.rows[self.cursor]
            if row.kind == SPLIT:
                return ("split", row.line)
        return ("header", None)

    def _restore(self, anchor: tuple[str, int | None]) -> None:
        header = self.header_index()
        kind, line = anchor
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
        )
        name = self.db.full_name(others[0].account) if len(others) == 1 else ""
        if mine is not None and len(others) == 1 and others[0].account != mine.account and name:
            draft.transfer = name
            draft.simple_handles = (mine.handle, others[0].handle)
            target = "increase" if mine.value > 0 else "decrease"
            setattr(draft, target, abs(mine.value).format())
            draft.transfer_touched = True
        else:
            # More than two splits (or an unusual pair) can only be edited split by split.
            ordered = ([mine] if mine is not None else []) + others
            draft.expanded = True
            draft.lines = [self._line_of(split) for split in ordered]
            draft.lines.append(Line())
            draft.transfer = SPLIT_LABEL
        draft.snapshot = draft.state()
        return draft

    def _line_of(self, split) -> Line:
        value = split.value
        return Line(
            account=self.db.full_name(split.account),
            memo=split.memo,
            increase=value.format() if value > 0 else "",
            decrease=(-value).format() if value < 0 else "",
            handle=split.handle,
        )

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
        }

    def text(self, index: int, column: str) -> str:
        """What cell (``index``, ``column``) shows."""
        row = self.rows[index]
        draft = self.draft
        if row.kind == SPLIT:
            assert row.line is not None
            return draft.lines[row.line].cell(column)
        if row.kind == IMBALANCE:
            if column == "description":
                return "Imbalance"
            if column == "balance":
                return self.residual().format(parens_negative=True)
            return ""
        if row.current:
            if column in _TRANSACTION_FIELDS:
                if draft.expanded and column in ("increase", "decrease", "transfer"):
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
                return self.db.full_name(others[0].account)
            return SPLIT_LABEL
        if column == "reconcile":
            return ledger_row.split.reconcile.value
        if column == "increase":
            return ledger_row.debit.format() if ledger_row.debit else ""
        if column == "decrease":
            return ledger_row.credit.format() if ledger_row.credit else ""
        if column == "balance":
            return ledger_row.running.format(parens_negative=True)
        return ""

    def negative(self, index: int, column: str) -> bool:
        """Whether the cell shows a negative figure (drawn in the negative colour)."""
        row = self.rows[index]
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
            return _SPLIT_FIELDS
        if self.draft.expanded and row.current:
            return ("date", "num", "description")
        return _TRANSACTION_FIELDS

    def cell_count(self) -> int:
        return len(self.rows)

    def summary(self, index: int) -> str:
        """A row read aloud: date, description, amount, and balance."""
        parts = [self.text(index, column) for column in ("date", "description", "transfer")]
        amount = self.text(index, "increase") or (
            f"-{self.text(index, 'decrease')}" if self.text(index, "decrease") else ""
        )
        balance = self.text(index, "balance")
        return ", ".join(
            part for part in (*parts, amount, balance and f"balance {balance}") if part
        )

    def closing_balance(self) -> Money | None:
        return self.ledger_rows[-1].running if self.ledger_rows else None

    # --------------------------------------------------------------- editing

    def set_text(self, text: str) -> None:
        """Put typed text in the cursor's cell."""
        row = self.rows[self.cursor]
        if row.kind == SPLIT:
            assert row.line is not None
            line = self.draft.lines[row.line]
            attribute = {
                "description": "memo",
                "transfer": "account",
                "increase": "increase",
                "decrease": "decrease",
            }[self.column]
            setattr(line, attribute, text)
            if self.column in ("increase", "decrease") and text.strip():
                setattr(line, "decrease" if self.column == "increase" else "increase", "")
            self._grow_lines()
            return
        self.draft.set_cell(self.column, text)

    def _grow_lines(self) -> None:
        """Keep an empty split line at the end, ready for the next split."""
        lines = self.draft.lines
        if not lines or lines[-1].has_input():
            lines.append(Line())
            self._rebuild()
        elif self.residual() and not any(row.kind == IMBALANCE for row in self.rows):
            self._rebuild()
        elif not self.residual() and any(row.kind == IMBALANCE for row in self.rows):
            self._rebuild()

    def cell_text(self) -> str:
        """The text in the cursor's cell, for the editor to start from."""
        return self.text(self.cursor, self.column)

    def quickfill(self, typed: str) -> tuple[str, list[str]]:
        """What the cursor's cell shows for ``typed`` and the accounts it may mean.

        An account cell completes the segment being typed; the description cell
        completes from earlier descriptions in this register. The caller selects
        the part beyond ``typed``.
        """
        if self.column == "transfer":
            names = (
                self.account_names if self.rows[self.cursor].kind == SPLIT else self.transfer_names
            )
            return quickfill_account(typed, names)
        if self.column == "description" and self.rows[self.cursor].kind != SPLIT:
            if self.draft.source is None:
                found = quickfill_description(typed, self.descriptions)
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
        result = next_entry_num(self.db, self.account, text, step)
        return result.value if result.value is not None else None

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
        text = self.cell_text().strip()
        if column == "date" and row.kind != SPLIT and text:
            try:
                self.draft.date = parse_entry_date(text, self._date_base()).isoformat()
            except EntryInputError:
                return self._fail("Enter a date such as 2026-03-15, 3/15, 15, or t.")
        elif column == "transfer" and text and text != SPLIT_LABEL:
            names = self.account_names if row.kind == SPLIT else self.transfer_names
            resolved = self._resolve_account(text, names)
            if resolved is None:
                return self._fail(f"No account matches “{text}”.")
            self.set_text(resolved)
        elif column in ("increase", "decrease") and text and is_arithmetic(text):
            try:
                value = self.read_amount(text)
            except ValueError:
                return self._fail("Enter an amount, or arithmetic such as 12.50+3.")
            other = "decrease" if column == "increase" else "increase"
            self.set_text("")
            saved = self.column
            self.column = column if value >= 0 else other
            self.set_text(abs(value).format())
            self.column = saved
        elif column == "description" and row.kind != SPLIT and text:
            self.propose()
        return True

    @staticmethod
    def _resolve_account(text: str, names: dict[str, str]) -> str | None:
        exact = {name.casefold(): name for name in names}
        if text.casefold() in exact:
            return exact[text.casefold()]
        _shown, matches = quickfill_account(text, names)
        return matches[0] if matches else None

    def read_amount(self, text: str) -> Money:
        """Typed text as an amount, arithmetic evaluated; ``ValueError`` if unreadable."""
        source = self.draft.source
        currency = transaction_currency(self.db, source.currency if source is not None else None)
        result = read_entry_amount(self.db, text, currency=currency)
        if result.value is None or result.value.value is None:
            raise ValueError(text)
        return Money(result.value.value)

    def propose(self) -> bool:
        """Fill a new entry's untouched cells from the latest matching transaction."""
        draft = self.draft
        if draft.source is not None or not self.enabled or not draft.description.strip():
            return False
        result = suggest_entry(
            self.db, SuggestEntry(description=draft.description, account=self.account)
        )
        suggestion = result.value.suggestion if result.value is not None else None
        if suggestion is None:
            return False
        source = f"{suggestion.when.isoformat()} “{suggestion.description}”"
        if draft.expanded or len(suggestion.splits) > 2:
            if draft.transfer_touched or any(line.has_input() for line in draft.lines):
                return False
            draft.expanded = True
            draft.lines = [
                Line(
                    account=self.db.full_name(split.account),
                    memo=split.memo,
                    increase=split.value.format() if split.value > 0 else "",
                    decrease=(-split.value).format() if split.value < 0 else "",
                )
                for split in suggestion.splits
            ]
            draft.lines.append(Line())
            self._rebuild()
            self._say(f"Proposed {len(suggestion.splits)} splits from {source}.")
            return True
        filled = []
        if not draft.transfer_touched and suggestion.transfer_account is not None:
            name = self.db.full_name(suggestion.transfer_account)
            if name in self.transfer_names:
                draft.transfer = name
                filled.append(name)
        if not (draft.increase.strip() or draft.decrease.strip()) and suggestion.amount is not None:
            target = "increase" if suggestion.amount > 0 else "decrease"
            setattr(draft, target, abs(suggestion.amount).format())
            filled.append(abs(suggestion.amount).format())
        if filled:
            self._say(
                f"Proposed from {source}: {', '.join(filled)}. Edit anything, then press Enter."
            )
        return bool(filled)

    # ----------------------------------------------------------------- splits

    def residual(self) -> Money:
        total = Money(0)
        for line in self.draft.lines:
            text, sign = line.amount_text()
            if not text:
                continue
            try:
                value = self.read_amount(text)
            except ValueError:
                continue
            total = total + (value if sign > 0 else -value)
        return total

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
            own = self.db.full_name(self.account)
            draft.lines = [
                Line(
                    account=own,
                    memo=memo.get(mine, "") if mine else "",
                    increase=amount.format() if amount is not None and amount > 0 else "",
                    decrease=(-amount).format() if amount is not None and amount < 0 else "",
                    handle=mine,
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
        own = self.db.full_name(self.account)
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
                fresh = self.db.get_transaction(handle)
                if fresh is None:
                    return self._fail("That transaction is no longer in the book.")
                self.draft = self._load_draft(fresh, split)
            self._load_accounts()
            self.cursor = 0
            self._rebuild()
            index = self.header_index()
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
        if 0 <= target < len(order):
            self.cursor, self.column = order[target]
            return True
        if backwards:
            return True
        # Past the last cell, like Enter: save and move to the next transaction.
        return self.enter()

    def _tab_order(self) -> list[tuple[int, str]]:
        header = self.header_index()
        order = [(header, column) for column in self.editable_columns(header)]
        for index in range(header + 1, len(self.rows)):
            row = self.rows[index]
            if not row.current or row.kind not in (SPLIT, IMBALANCE):
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
        # The saved transaction may have moved (a new date); go on from where it is now.
        header = self.header_index()
        self.move_to(min(header + 1, len(self.rows) - 1), "date")
        return True

    def escape(self) -> None:
        """Escape: put the transaction back as it was (the blank one empties)."""
        source = self.draft.source
        if source is None:
            self.draft = self._blank_draft()
        else:
            fresh = self.db.get_transaction(source.handle)
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
        currency = transaction_currency(
            self.db, draft.source.currency if draft.source is not None else None
        )
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
        result = save_transaction(
            self.db,
            SaveTransaction(
                TransactionInput(
                    post_date=when,
                    description=description,
                    num=draft.num.strip(),
                    # Notes are not shown in the register, so an edit keeps them.
                    notes=draft.source.notes if draft.source is not None else "",
                    currency=currency,
                    splits=tuple(splits),
                ),
                existing_handle=draft.source.handle if draft.source is not None else None,
                source=draft.source,
            ),
        )
        if not result.ok:
            return self._fail(service_error_message(result.errors[0]))
        if draft.source is None:
            self.last_date = when
            heading = self.debit_heading if amount > 0 else self.credit_heading
            self._say(
                f"Posted {description}: {abs(amount).format()} {heading}."
                if amount
                else f"Posted {description}."
            )
        else:
            self._say(f"Saved changes to {description}.")
        self.draft = self._blank_draft()
        self.ledger_rows = ledger.register(self.db, self.account)
        self.reload(keep_cursor=False)
        if result.value is not None and draft.source is not None:
            self.select_transaction(draft.source.handle)
        return True

    def _two_splits(self, currency: str):
        draft = self.draft
        transfer = self._resolve_account(draft.transfer.strip(), self.transfer_names)
        if not draft.transfer.strip() or transfer is None:
            return self._fail("Choose a transfer account: type part of its name.", "transfer")
        column = "decrease" if draft.decrease.strip() else "increase"
        text = getattr(draft, column).strip()
        if not text:
            return self._fail(
                f"Enter an amount under {self.debit_heading} or {self.credit_heading}.", "increase"
            )
        try:
            value = self.read_amount(text)
        except ValueError:
            return self._fail("Enter an amount, or arithmetic such as 12.50+3.", column)
        if value <= 0:
            return self._fail("Amount must be greater than zero.", column)
        amount = value if column == "increase" else -value
        mine, other = draft.simple_handles if draft.source is not None else (None, None)
        handle = self.transfer_names[transfer]
        return (
            self._split_input(self.account, amount, currency, mine),
            self._split_input(handle, -amount, currency, other),
        ), amount

    def _line_splits(self, currency: str):
        splits = []
        for index, line in enumerate(self.draft.lines):
            if not line.has_input():
                continue
            name = self._resolve_account(line.account.strip(), self.account_names)
            if not line.account.strip() or name is None:
                return self._fail("Choose an account for this split.", "transfer", index)
            text, sign = line.amount_text()
            try:
                value = self.read_amount(text) if text else None
            except ValueError:
                return self._fail(
                    "Enter an amount, or arithmetic such as 12.50+3.", "increase", index
                )
            if value is None or value == 0:
                return self._fail("Enter an amount for this split.", "increase", index)
            splits.append(
                self._split_input(
                    self.account_names[name],
                    value if sign > 0 else -value,
                    currency,
                    line.handle,
                    line.memo,
                )
            )
        if len(splits) < 2:
            return self._fail("A transaction needs at least two splits.", "transfer", 0)
        residual = self.residual()
        if residual:
            return self._fail(
                f"The splits are out of balance by {residual.format(parens_negative=True)}."
            )
        if not any(split.account == self.account for split in splits):
            return self._fail("One split must be in this register's account.", "transfer", 0)
        return tuple(splits)

    def _split_input(
        self, account: str, value: Money, currency: str, handle: str | None, memo: str | None = None
    ) -> TransactionSplitInput:
        """One split, keeping what the register does not show from its stored split."""
        source = None
        if self.draft.source is not None and handle is not None:
            source = next((s for s in self.draft.source.splits if s.handle == handle), None)
        return TransactionSplitInput(
            account,
            Amount(value, currency),
            handle=handle if source is not None else None,
            memo=(
                memo if memo is not None else (source.memo if source is not None else "")
            ).strip(),
            planning_flow=source.planning_flow if source is not None else None,
            investment_activity=source.investment_activity if source is not None else None,
        )

    # ---------------------------------------------------------------- reconcile

    def toggle_reconcile(self, index: int) -> bool:
        """Mark a transaction's split in this account cleared, or not cleared."""
        row = self.rows[index]
        if row.register_row is None:
            return False
        result = toggle_cleared(
            self.db,
            ToggleCleared(row.register_row.transaction.handle, row.register_row.split.handle),
        )
        if result.value is None:
            return self._fail(service_error_message(result.errors[0]))
        state = "cleared" if result.value is ReconcileState.CLEARED else "not cleared"
        self._say(f"Marked {row.register_row.description} {state}.")
        self.reload()
        return True

    # ----------------------------------------------------------------- messages

    def _say(self, text: str) -> None:
        self.message, self.error = text, False

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
