"""One transaction's split lines as a sheet, for the transaction and schedule editors.

The register opens a transaction into split lines beneath it; an editor window
shows the same lines on a grid of their own (``widgets/register_grid``), so a split
is typed the same way wherever it is entered. Both are a
:class:`~breadsched.gui.split_entry.SplitEntry`: the memo completes from earlier
memos, the account one ``:`` segment at a time, an amount may be arithmetic, the
empty last line shows the rest of an unbalanced entry and takes it when given an
account (#369), and a split in an account that holds a security takes shares and
a price (#323). Shown with planning detail, each line also takes its planning
purpose and investment activity, completed from their lists.

Unlike the register, the sheet saves nothing itself: Enter asks the editor to
save (``on_enter``), Escape asks it to close (``on_escape``), and Tab past the
last cell leaves the grid (``leaving``). :meth:`SplitSheet.splits` gives the
lines as the shared service's split inputs, or reports why they cannot be saved.
Nothing here imports GTK.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from ..gen.engine import ledger
from ..gen.lib.account import Account
from ..gen.lib.money import Money
from ..gen.lib.transaction import Transaction
from ..gen.services import TransactionSplitInput
from .split_entry import (
    _SECURITY_SPLIT_FIELDS,
    _SPLIT_FIELDS,
    _TRADE_FIELDS,
    SPLIT,
    Draft,
    Line,
    SheetRow,
    SplitEntry,
    TradeQuestion,
    selectable_names,
)

__all__ = ["DETAIL_COLUMNS", "SplitSheet"]

#: The planning-detail columns, shown with **Planning detail**.
DETAIL_COLUMNS = ("purpose", "activity")


class SplitSheet(SplitEntry):
    """The split lines of one transaction, with a cursor, as the grid draws them."""

    def __init__(
        self,
        db,
        records: Iterable[Any],
        *,
        security_accounts: Iterable[str] = (),
        transaction: Transaction | None = None,
        account: str | None = None,
        currency: str | None = None,
    ) -> None:
        self.db = db
        records = list(records)
        referenced = {split.account for split in transaction.splits} if transaction else set()
        self.full_names = {record.account.handle: record.full_name for record in records}
        self.accounts: dict[str, Account] = {
            record.account.handle: record.account for record in records
        }
        self.account_names = selectable_names(records, referenced)
        self.security_accounts = frozenset(security_accounts)
        self.share_fraction = 1
        #: The account the editor was opened from (its register), whose headings
        #: name the amount columns as that register does; "" for none.
        self.account = account or ""
        self.currency = currency
        self.edits_detail = False
        self.message = ""
        self.error = False
        self.trade_question: TradeQuestion | None = None
        self._note_choices = None
        self.cursor = 0
        self.column = "description"
        self.ordinals: list[int] = []
        #: Asked to save (Enter) and to close (Escape) by the editor holding the sheet.
        self.on_enter: Callable[[], bool] | None = None
        self.on_escape: Callable[[], None] | None = None
        #: Set by Tab past the last cell (1) or Shift+Tab before the first (-1), so
        #: the grid moves the keyboard on to the editor's next control.
        self.leaving = 0
        self.draft = Draft(transaction, None, expanded=True)
        if transaction is not None:
            self.draft.description = transaction.description
            self.draft.lines = [self._line_of(split) for split in transaction.splits]
        else:
            first = self.full_names.get(account, "") if account else ""
            self.draft.lines = [Line(account=first)]
        self.draft.lines.append(Line())
        self._rebuild()
        self.snapshot = self.state()

    # ----------------------------------------------------------------- layout

    @property
    def rows(self) -> list[SheetRow]:
        """One row per line; the line under the cursor is shaded as the register
        shades the transaction under its cursor."""
        return [
            SheetRow(SPLIT, line=index, current=index == self.cursor)
            for index in range(len(self.lines))
        ]

    @rows.setter
    def rows(self, _rows: list[SheetRow]) -> None:
        pass  # laid out from the lines each time

    def _rebuild(self) -> None:
        # Each line its own band, as the register's Lines banding does.
        self.ordinals = list(range(len(self.lines)))
        self.cursor = max(0, min(self.cursor, len(self.lines) - 1))
        editable = self.editable_columns(self.cursor)
        if editable and self.column not in editable:
            self.column = editable[0]

    @property
    def lines(self) -> list[Line]:
        return self.draft.lines

    def state(self) -> tuple:
        return self.draft.state()

    def dirty(self) -> bool:
        return self.state() != self.snapshot

    def set_detail(self, on: bool) -> None:
        """Show (and save) each line's planning purpose and investment activity."""
        self.edits_detail = on
        self._rebuild()

    def shows_trades(self) -> bool:
        return any(self._holds_security(line) for line in self.lines)

    @property
    def columns(self) -> tuple[str, ...]:
        columns: tuple[str, ...] = ("description", "transfer")
        if self.shows_trades():
            columns += _TRADE_FIELDS
        columns += ("increase", "decrease")
        if self.edits_detail:
            columns += DETAIL_COLUMNS
        return columns

    def headings(self) -> dict[str, str]:
        account = self.accounts.get(self.account)
        debit, credit = (
            ledger.register_headings(account.atype) if account is not None else ("Debit", "Credit")
        )
        return {
            "description": "Memo",
            "transfer": "Account",
            "shares": "Shares",
            "price": "Price",
            "increase": debit,
            "decrease": credit,
            "purpose": "Planning purpose",
            "activity": "Investment activity",
        }

    def text(self, index: int, column: str) -> str:
        row = self.rows[index]
        assert row.line is not None
        if row.line == self.balancing_line() and column in ("increase", "decrease"):
            increase, decrease = self._balancing_cells()
            return increase if column == "increase" else decrease
        return self.lines[row.line].cell(column)

    def negative(self, index: int, column: str) -> bool:
        return False

    def future(self, index: int) -> bool:
        return False

    def editable_columns(self, index: int) -> tuple[str, ...]:
        if not 0 <= index < len(self.rows):
            return ()
        line = self.lines[index]
        columns: tuple[str, ...] = (
            _SECURITY_SPLIT_FIELDS if self._holds_security(line) else _SPLIT_FIELDS
        )
        if self.edits_detail:
            columns += ("purpose",)
            if self._investing(line) or line.activity.strip():
                columns += ("activity",)
        return columns

    def _investing(self, line: Line) -> bool:
        handle = self.account_names.get(line.account.strip())
        account = self.accounts.get(handle) if handle is not None else None
        return account is not None and account.atype.is_investment

    def summary(self, index: int) -> str:
        """A line read aloud: its account, memo, and amount."""
        amount = self.text(index, "increase") or (
            f"-{self.text(index, 'decrease')}" if self.text(index, "decrease") else ""
        )
        parts = (self.text(index, "transfer"), self.text(index, "description"), amount)
        return ", ".join(part for part in parts if part) or "empty split"

    # ----------------------------------------------------------------- typing

    def set_text(self, text: str) -> None:
        before = len(self.lines)
        self._set_line_text(self.cursor, self.column, text)
        if len(self.lines) != before:
            self._rebuild()

    def cell_text(self) -> str:
        return self.text(self.cursor, self.column)

    def quickfill(self, typed: str) -> tuple[str, list[str]]:
        return self._quickfill_line(self.column, typed)

    #: The columns whose choices the grid lists beneath the cell.
    listed_columns = ("transfer", *DETAIL_COLUMNS)
    #: What a screen reader calls the grid.
    grid_label = "Splits"

    def settle(self) -> bool:
        if not self.rows:
            return True
        return self._settle_line(self.rows[self.cursor])

    def date_key(self, text: str, key: str) -> str | None:
        return None

    def num_key(self, text: str, step: int) -> str | None:
        return None

    def toggle_reconcile(self, index: int) -> bool:
        return False

    # ------------------------------------------------------------- the cursor

    def move_to(self, index: int, column: str | None = None) -> bool:
        if index != self.cursor and not self.settle():
            return False
        self.cursor = max(0, min(index, len(self.rows) - 1))
        editable = self.editable_columns(self.cursor)
        if column in editable:
            self.column = column
        elif self.column not in editable and editable:
            self.column = editable[0]
        return True

    def move(self, step: int) -> bool:
        target = self.cursor + step
        if not 0 <= target < len(self.rows):
            return self.settle()
        return self.move_to(target, self.column)

    def _tab_order(self) -> list[tuple[int, str]]:
        return [
            (index, column)
            for index in range(len(self.rows))
            for column in self.editable_columns(index)
        ]

    def tab(self, backwards: bool = False) -> bool:
        """Tab: the next cell; past the last one (or before the first), on to the
        editor's next (or previous) control."""
        if not self.settle():
            return False
        order = self._tab_order()
        here = (self.cursor, self.column)
        position = order.index(here) if here in order else 0
        target = position + (-1 if backwards else 1)
        if 0 <= target < len(order):
            self.cursor, self.column = order[target]
            return True
        self.leaving = -1 if backwards else 1
        return True

    def enter(self) -> bool:
        """Enter: read the cell, then ask the editor to save."""
        if not self.settle():
            return False
        return self.on_enter() if self.on_enter is not None else True

    def escape(self) -> None:
        """Escape: ask the editor to close, as Escape does in any dialog."""
        if self.on_escape is not None:
            self.on_escape()

    def to_blank(self) -> bool:
        return self.move_to(len(self.rows) - 1, "transfer")

    # ------------------------------------------------------------------ lines

    def set_lines(self, lines: Iterable[tuple[str | None, Money | None, str]]) -> None:
        """Start the lines from (account, signed value, memo), as a register's blank
        entry or a proposal gives them; an empty line follows for the next split."""
        self.draft.lines = [
            Line(
                account=self.full_names.get(account, "") if account else "",
                memo=memo,
                increase=value.format() if value is not None and value > 0 else "",
                decrease=(-value).format() if value is not None and value < 0 else "",
            )
            for account, value, memo in lines
        ]
        self.draft.lines.append(Line())
        self.cursor = 0
        self._rebuild()

    def untouched(self) -> bool:
        """Whether nothing beyond the opening account was typed, so a proposal may
        fill the lines."""
        typed = [line for line in self.lines if line.has_input()]
        return all(
            not (line.memo.strip() or line.amount_text()[0] or line.shares.strip())
            for line in typed
        ) and (len(typed) <= 1)

    def splits(self, currency: str) -> tuple[TransactionSplitInput, ...] | None:
        """The lines as split inputs for the shared service, or None with the reason
        in :attr:`message` and the cursor on the line to correct."""
        found = self._line_splits(currency)
        return found if found else None

    # ---------------------------------------------------------------- the base

    def _name(self, handle: str) -> str:
        return self.full_names.get(handle, handle)

    def _trade_holder(self, row: SheetRow) -> Draft | Line | None:
        if row.line is None:
            return None
        line = self.lines[row.line]
        return line if self._holds_security(line) else None

    def check(self, currency: str) -> str | None:
        """Why the lines cannot be saved, or None, leaving the cursor and the message
        where they are (for a window that says so as the user types)."""
        held = (self.cursor, self.column, self.message, self.error)
        self._quiet = True
        try:
            found = self._line_splits(currency)
            problem = None if found else self.message
        finally:
            self._quiet = False
            self.cursor, self.column, self.message, self.error = held
        return problem

    def schedule_lines(self, schedule) -> None:
        """Start the lines from a schedule's splits (signed ledger amounts, with each
        split's memo, planning purpose, and investment activity)."""

        def value(split) -> Money:
            return split.amount if split.amount is not None else Money(0)

        self.draft.lines = [
            Line(
                account=self.full_names.get(split.account, split.account),
                memo=split.memo,
                increase=value(split).format() if value(split) > 0 else "",
                decrease=(-value(split)).format() if value(split) < 0 else "",
                purpose=split.planning_flow.label if split.planning_flow is not None else "",
                activity=(
                    split.investment_activity.label if split.investment_activity is not None else ""
                ),
            )
            for split in schedule.splits
        ]
        self.draft.lines.append(Line())
        self.cursor = 0
        self._rebuild()
        self.snapshot = self.state()

    def _fail(self, text: str, column: str | None = None, line: int | None = None) -> bool:
        self.message, self.error = text, True
        if getattr(self, "_quiet", False):
            return False
        if line is not None and 0 <= line < len(self.rows):
            self.cursor = line
        if column is not None and column in self.editable_columns(self.cursor):
            self.column = column
        return False
