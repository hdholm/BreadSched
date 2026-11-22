"""Split lines as typed: the logic the register and every transaction editor share.

The register (``register_sheet.RegisterSheet``) opens a transaction into split
lines beneath it; the transaction and schedule editors show the same lines on a
grid of their own (``split_sheet.SplitSheet``). Both are :class:`SplitEntry`, so a
split line behaves the same wherever it is typed: the account completes ``:``
segment by segment, an amount may be arithmetic, the empty last line shows the
rest of an unbalanced entry and takes it when given an account (#369), and a
split in an account that holds a security takes shares and a price, any two
giving the third (#323, #407). Nothing here imports GTK.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from typing import Any, Literal

from ..api import ApiError
from ..api.local import book_server
from ..gen.engine import ledger
from ..gen.engine.cost_basis import shares_text
from ..gen.engine.entry_input import (
    EntryInputError,
    is_arithmetic,
    parse_entry_amount,
    quickfill_account,
    trade_price,
)
from ..gen.lib.account import Account
from ..gen.lib.amount import Amount
from ..gen.lib.money import Money
from ..gen.lib.transaction import (
    InvestmentActivityKind,
    PlanningFlowKind,
    ReconcileState,
    Transaction,
)
from ..gen.services import TransactionSplitInput
from ..gen.services.autocomplete import EarlierNotes, complete_note
from ..gen.services.entry_input import ReadEntryTrade
from ..presentation.investments import trade_price_text
from . import hidden_accounts

#: Row kinds.
TRANSACTION = "transaction"
SPLIT = "split"
IMBALANCE = "imbalance"
BLANK = "blank"
#: The second line of a transaction in Double Line mode: its notes (#319).
NOTES = "notes"

_TRANSACTION_FIELDS = ("date", "num", "description", "transfer", "increase", "decrease")
_SPLIT_FIELDS = ("description", "transfer", "increase", "decrease")
_TRADE_FIELDS = ("shares", "price")
_SECURITY_TRANSACTION_FIELDS = ("date", "num", "description", "transfer", *_TRADE_FIELDS) + (
    "increase",
    "decrease",
)
_SECURITY_SPLIT_FIELDS = ("description", "transfer", *_TRADE_FIELDS, "increase", "decrease")
_TRADE_COLUMNS = (*_TRADE_FIELDS, "increase", "decrease")


def _frac(value: Money) -> Fraction:
    return Fraction(value.numerator, value.denominator)


def _trade_cells(holder) -> dict[str, str]:
    return {column: getattr(holder, column).strip() for column in _TRADE_COLUMNS}


TradeField = Literal["shares", "price", "amount"]
#: What changing one of shares, price, or amount may work out again.
TRADE_CHOICES: tuple[TradeField, ...] = ("shares", "price", "amount")


@dataclass
class Line:
    """One editable split beneath an opened transaction, in the split's own direction."""

    account: str = ""
    memo: str = ""
    increase: str = ""
    decrease: str = ""
    #: The stored split this line edits, or None for a new one.
    handle: str | None = None
    #: Shares and price, for a split in an account that holds a security.
    shares: str = ""
    price: str = ""
    #: The trade cells as last read, so leaving one unchanged recomputes nothing.
    settled: dict[str, str] = field(default_factory=dict)
    #: Planning purpose and investment activity, as their labels; blank is ordinary.
    purpose: str = ""
    activity: str = ""

    def has_input(self) -> bool:
        return bool(
            self.account.strip()
            or self.memo.strip()
            or self.amount_text()[0]
            or self.shares.strip()
        )

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
            "shares": self.shares,
            "price": self.price,
            "purpose": self.purpose,
            "activity": self.activity,
        }.get(column, "")


#: The cells whose change alters a split's account, amount, or shares.
_PROTECTED = ("transfer", "increase", "decrease", "shares", "price")

#: What each split-line column is stored as on a :class:`Line`.
LINE_ATTRIBUTES = {
    "description": "memo",
    "transfer": "account",
    "increase": "increase",
    "decrease": "decrease",
    "shares": "shares",
    "price": "price",
    "purpose": "purpose",
    "activity": "activity",
}
#: The planning purposes a split may name, by label; blank leaves it ordinary.
PURPOSES: dict[str, PlanningFlowKind] = {kind.label: kind for kind in PlanningFlowKind}
#: The investment activities a split may name, by label; blank is ordinary.
ACTIVITIES: dict[str, InvestmentActivityKind] = {
    kind.label: kind for kind in InvestmentActivityKind
}
#: The columns that take a choice from a list, and the choices.
CHOICES: dict[str, dict] = {"purpose": PURPOSES, "activity": ACTIVITIES}


def choose(typed: str, labels) -> tuple[str, list[str]]:
    """Complete ``typed`` to the first label beginning with it, listing every label
    that begins with or contains it (like an account cell)."""
    folded = typed.casefold()
    if not folded:
        return typed, list(labels)
    starting = [label for label in labels if label.casefold().startswith(folded)]
    containing = [label for label in labels if folded in label.casefold() and label not in starting]
    shown = starting[0] if starting else typed
    return shown, starting + containing


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
    #: Shares and price, in an account that holds a security (#323).
    shares: str = ""
    price: str = ""
    notes: str = ""
    expanded: bool = False
    lines: list[Line] = field(default_factory=list)
    #: Split handles behind the register and transfer sides of a two-split entry.
    simple_handles: tuple[str | None, str | None] = (None, None)
    #: Whether the user chose the transfer (a proposal never overwrites it).
    transfer_touched: bool = False
    #: The trade cells as last read, so leaving one unchanged recomputes nothing.
    settled: dict[str, str] = field(default_factory=dict)
    snapshot: tuple = ()

    def state(self) -> tuple:
        lines = tuple(
            (
                line.handle,
                line.account.strip(),
                line.memo.strip(),
                *line.amount_text(),
                line.shares.strip(),
            )
            for line in self.lines
            if line.has_input()
        )
        simple = (
            ()
            if self.expanded
            else (
                self.transfer.strip(),
                self.increase.strip(),
                self.decrease.strip(),
                self.shares.strip(),
            )
        )
        return (
            self.date.strip(),
            self.num.strip(),
            self.description.strip(),
            self.notes.strip(),
            self.expanded,
            simple,
            lines,
        )

    def dirty(self) -> bool:
        return self.state() != self.snapshot

    def cell(self, column: str) -> str:
        return getattr(self, column) if column in _SECURITY_TRANSACTION_FIELDS else ""

    def set_cell(self, column: str, text: str) -> None:
        if column in _SECURITY_TRANSACTION_FIELDS:
            setattr(self, column, text)
            if column == "transfer":
                self.transfer_touched = True
            elif column in ("increase", "decrease") and text.strip():
                setattr(self, "decrease" if column == "increase" else "increase", "")


@dataclass(frozen=True)
class TradeQuestion:
    """Shares, price, and amount were all filled and one changed: which other one
    should change to match (#407)?"""

    holder: Any
    #: The cell that changed, and what it is.
    column: str
    changed: TradeField
    #: The two that could be worked out again, in the order offered.
    choices: tuple[TradeField, ...]


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


def selectable_names(records: Iterable[Any], referenced: Iterable[str] = ()) -> dict[str, str]:
    """The accounts a split may use, full name → handle, sorted as the register sorts
    them: never the root, and a placeholder, an account outside the tree, or a
    hidden one (while hidden accounts are not shown) only when the transaction
    already uses it."""
    keep = set(referenced)
    names: dict[str, str] = {}
    for record in records:
        item = record.account
        if item.is_root:
            continue
        # A placeholder, an account outside the tree (GnuCash's template accounts),
        # or a hidden one while hidden accounts are not shown is offered only where
        # the transaction already uses it, so it can be saved unchanged.
        if item.handle not in keep and (
            item.placeholder or item.parent is None or (item.hidden and not hidden_accounts.shown())
        ):
            continue
        names.setdefault(record.full_name, item.handle)
    return dict(sorted(names.items(), key=lambda pair: pair[0].casefold()))


class SplitEntry:
    """What a sheet of split lines does with them, whatever else the sheet shows.

    A subclass provides ``db``, ``draft`` (whose ``lines`` are typed here),
    ``account`` (the account a two-split entry belongs to, if any),
    ``account_names`` (full name → handle), ``accounts`` (handle → account),
    ``security_accounts``, ``share_fraction``, ``rows``, ``_rebuild``,
    ``_trade_holder``, ``_fail`` and ``_name``.
    """

    db: Any
    draft: Draft
    account: str
    column: str
    account_names: dict[str, str]
    accounts: dict[str, Account]
    security_accounts: frozenset[str]
    share_fraction: int
    rows: list[SheetRow]
    message: str
    error: bool
    trade_question: TradeQuestion | None
    _note_choices: tuple[tuple[str, bool], tuple[str, ...]] | None
    #: Whether the lines show (and save) planning purpose and investment activity.
    edits_detail: bool = False

    # What each sheet provides.

    def _rebuild(self) -> None:
        raise NotImplementedError

    def _name(self, handle: str) -> str:
        raise NotImplementedError

    def _trade_holder(self, row: SheetRow) -> Draft | Line | None:
        raise NotImplementedError

    def _fail(self, text: str, column: str | None = None, line: int | None = None) -> bool:
        raise NotImplementedError

    def set_text(self, text: str) -> None:
        raise NotImplementedError

    def cell_text(self) -> str:
        raise NotImplementedError

    def balancing_line(self) -> int | None:
        """The empty last split line, when it shows what is left to balance (#369)."""
        lines = self.draft.lines
        if not self.draft.expanded or not lines or lines[-1].has_input() or not self.residual():
            return None
        return len(lines) - 1

    def _balancing_cells(self) -> tuple[str, str]:
        """(increase, decrease) text that would balance the entry, as the line shows it."""
        rest = -self.residual()
        if rest > 0:
            return rest.format(), ""
        return "", (-rest).format()

    def _line_of(self, split) -> Line:
        value = split.value
        line = Line(
            account=self._name(split.account),
            memo=split.memo,
            increase=value.format() if value > 0 else "",
            decrease=(-value).format() if value < 0 else "",
            handle=split.handle,
            purpose=split.planning_flow.label if split.planning_flow is not None else "",
            activity=(
                split.investment_activity.label if split.investment_activity is not None else ""
            ),
        )
        if split.account in self.security_accounts:
            self._load_trade(line, split)
        return line

    @staticmethod
    def _load_trade(holder: Draft | Line, split) -> None:
        """Shares and price of a stored split: a size with a value, else signed shares."""
        quantity = split.quantity
        if quantity:
            holder.shares = shares_text(abs(quantity) if split.value else quantity)
            price = trade_price(_frac(split.value), _frac(quantity))
            holder.price = trade_price_text(price)
        holder.settled = _trade_cells(holder)

    def _grow_lines(self) -> None:
        """Keep an empty split line at the end, ready for the next split."""
        lines = self.draft.lines
        if not lines or lines[-1].has_input():
            lines.append(Line())
            self._rebuild()

    def _earlier_notes(self, *, memos: bool) -> tuple[str, ...]:
        """Notes (or memos) of earlier transactions with the draft's description,
        read once for each description."""
        description = self.draft.description.strip()
        key = (description, memos)
        cached = self._note_choices
        if cached is not None and cached[0] == key:
            return cached[1]
        source = self.draft.source
        try:
            found = book_server(self.db).earlier_notes(
                EarlierNotes(
                    description=description,
                    memos=memos,
                    exclude=source.handle if source is not None else None,
                )
            )
        except ApiError:
            found = ()
        self._note_choices = (key, found)
        return found

    def _holds_security(self, line: Line) -> bool:
        handle = self.account_names.get(line.account.strip())
        return handle is not None and handle in self.security_accounts

    def _read_shares(self, text: str, holder: Draft | Line | None = None) -> Decimal | None:
        """Typed shares, arithmetic evaluated; ``EntryInputError`` when unreadable.

        A split line's shares round to its own security's unit, which in a cash
        register is not the register's (#407).
        """
        fraction = self.share_fraction
        if isinstance(holder, Line):
            account = self.accounts.get(self.account_names.get(holder.account.strip(), ""))
            if account is not None and account.commodity_scu:
                fraction = account.commodity_scu
        return parse_entry_amount(text, "auto", fraction)

    def _fill_trade(self, row: SheetRow, column: str, recompute: TradeField | None = None) -> bool:
        """Work out the third of shares, price, and amount from the two typed (#323).

        When all three were already filled, changing one changes neither of the
        others until the question in :attr:`trade_question` is answered (#407).
        """
        holder = self._trade_holder(row)
        if holder is None:
            return True
        if getattr(holder, column).strip() == holder.settled.get(column, ""):
            return True
        changed: TradeField = "amount" if column in ("increase", "decrease") else column  # type: ignore[assignment]
        settled = holder.settled
        was_full = bool(
            settled.get("shares")
            and settled.get("price")
            and (settled.get("increase") or settled.get("decrease"))
        )
        if recompute is None and was_full and getattr(holder, column).strip():
            self.trade_question = TradeQuestion(
                holder, column, changed, tuple(f for f in TRADE_CHOICES if f != changed)
            )
            return True
        amount_text = holder.increase.strip() or holder.decrease.strip()
        source = self.draft.source
        account = (
            self.account_names[holder.account.strip()] if isinstance(holder, Line) else self.account
        )
        try:
            filled = book_server(self.db).entry_trade(
                ReadEntryTrade(
                    account,
                    shares=holder.shares,
                    price=holder.price,
                    amount=amount_text,
                    changed=changed,
                    currency=(source.currency if source else None)
                    or book_server(self.db).transaction_currency(),
                    recompute=recompute,
                )
            )
        except ApiError as error:
            return self._fail(str(error))
        selling = holder.shares.strip().startswith("-")
        if filled.shares is not None and (amount_text or filled.amount):
            holder.shares = shares_text(Money(filled.shares))
        if filled.price is not None:
            holder.price = trade_price_text(filled.price)
        if filled.amount is not None:
            target = (
                ("increase" if holder.increase.strip() else "decrease")
                if amount_text
                else ("decrease" if selling else "increase")
            )
            other = "decrease" if target == "increase" else "increase"
            setattr(holder, target, Money(filled.amount).format())
            setattr(holder, other, "")
        holder.settled = _trade_cells(holder)
        if isinstance(holder, Line):
            self._grow_lines()
        return True

    def answer_trade(self, choice: TradeField | None) -> None:
        """Answer :attr:`trade_question`: work out ``choice`` again from the other
        two, or with ``None`` put the changed cell back as it was."""
        question = self.trade_question
        self.trade_question = None
        if question is None:
            return
        holder = question.holder
        if choice is None:
            for name, text in holder.settled.items():
                setattr(holder, name, text)
            self._rebuild()
            return
        row = next(
            (candidate for candidate in self.rows if self._trade_holder(candidate) is holder),
            None,
        )
        if row is not None:
            self._fill_trade(row, question.column, recompute=choice)
            self._rebuild()

    def _quantity(self, holder: Draft | Line, handle: str | None) -> Money | None:
        """The shares a draft or line saves, signed like its amount; None when blank.

        Shares left as loaded keep the stored quantity exactly; without an amount
        (a share split, or shares moved in kind) the typed sign gives the direction.
        """
        text = holder.shares.strip()
        if not text:
            return None
        source = None
        if self.draft.source is not None and handle is not None:
            source = next((s for s in self.draft.source.splits if s.handle == handle), None)
        if (
            source is not None
            and source.quantity
            and text
            in (
                shares_text(abs(source.quantity)),
                shares_text(source.quantity),
            )
        ):
            size = abs(source.quantity)
            typed_sign = -1 if text.startswith("-") else 1
        else:
            value = self._read_shares(text, holder)
            if value is None:
                return None
            size = abs(Money(value))
            typed_sign = -1 if value < 0 else 1
        if holder.increase.strip():
            return size
        if holder.decrease.strip():
            return -size
        return size * typed_sign

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
        currency = (source.currency if source is not None else None) or book_server(
            self.db
        ).transaction_currency()
        try:
            value = book_server(self.db).entry_amount(text, currency).value
        except ApiError:
            raise ValueError(text) from None
        if value is None:
            raise ValueError(text)
        return Money(value)

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

    def _split_input(
        self,
        account: str,
        value: Money,
        currency: str,
        handle: str | None,
        memo: str | None = None,
        *,
        quantity: Money | None = None,
        line: Line | None = None,
    ) -> TransactionSplitInput:
        """One split, keeping what the sheet does not show from its stored split.

        A sheet that shows planning detail (``edits_detail``) saves each line's
        purpose and activity as typed; one that does not keeps the stored ones.
        """
        source = None
        if self.draft.source is not None and handle is not None:
            source = next((s for s in self.draft.source.splits if s.handle == handle), None)
        if line is not None and self.edits_detail:
            return TransactionSplitInput(
                account,
                Amount(value, currency),
                handle=handle if source is not None else None,
                memo=(memo if memo is not None else line.memo).strip(),
                planning_flow=PURPOSES.get(line.purpose.strip()),
                investment_activity=ACTIVITIES.get(line.activity.strip()),
                quantity=self._shares_amount(account, quantity),
            )
        return TransactionSplitInput(
            account,
            Amount(value, currency),
            handle=handle if source is not None else None,
            memo=(
                memo if memo is not None else (source.memo if source is not None else "")
            ).strip(),
            planning_flow=source.planning_flow if source is not None else None,
            investment_activity=source.investment_activity if source is not None else None,
            quantity=self._shares_amount(account, quantity),
        )

    def _shares_amount(self, account: str, quantity: Money | None) -> Amount | None:
        """Shares as an amount of the account's security, for the shared service."""
        if quantity is None:
            return None
        obj = self.accounts.get(account)
        return Amount(quantity, obj.commodity) if obj is not None and obj.commodity else None

    def _say(self, text: str) -> None:
        self.message, self.error = text, False

    # ------------------------------------------------- reconciled splits (#403)

    def reconciled_accounts(self, row: SheetRow, column: str) -> tuple[str, ...]:
        """The accounts of reconciled splits that typing in ``column`` of ``row``
        would change, so the grid asks before the first keystroke, as GnuCash does;
        empty when none would, or once changes were allowed for this entry.

        A split line's account, amount, shares, and price belong to its own split;
        a register's two-split line moves both splits with its amount, and the
        other split with its transfer account.
        """
        source = self.draft.source
        if source is None or column not in _PROTECTED or self.reconciled_allowed():
            return ()
        reconciled = {
            split.handle: split
            for split in source.splits
            if split.reconcile is ReconcileState.RECONCILED
        }
        if not reconciled:
            return ()
        if row.kind == SPLIT:
            if row.line is None or row.line >= len(self.draft.lines):
                return ()
            handles = [self.draft.lines[row.line].handle]
        elif self.draft.expanded:
            return ()
        else:
            mine, other = self.draft.simple_handles
            handles = [other] if column == "transfer" else [mine, other]
        return tuple(
            self._name(reconciled[handle].account) for handle in handles if handle in reconciled
        )

    def allow_reconciled_changes(self) -> None:
        """Allow changing this entry's reconciled splits until it is saved or put back."""
        self._allowed_reconciled = id(self.draft)

    def reconciled_allowed(self) -> bool:
        return getattr(self, "_allowed_reconciled", None) == id(self.draft)

    # ------------------------------------------------------- typing split lines

    def line_values(self) -> list[tuple[str | None, Money | None, str]]:
        """Each split line with input as (account handle, signed amount, memo): an
        increase (a debit) positive; None for an account or amount not readable."""
        found: list[tuple[str | None, Money | None, str]] = []
        for line in self.draft.lines:
            if not line.has_input():
                continue
            text, sign = line.amount_text()
            try:
                value = self.read_amount(text) if text else None
            except ValueError:
                value = None
            found.append(
                (
                    self.account_names.get(line.account.strip()),
                    value if value is None or sign > 0 else -value,
                    line.memo.strip(),
                )
            )
        return found

    def _needs_own_split(self) -> bool:
        """Whether the lines must include a split in :attr:`account` to be saved."""
        return False

    def _set_line_text(self, index: int, column: str, text: str) -> None:
        """Put typed text in a split line's cell."""
        line = self.draft.lines[index]
        # Choosing an account (or a memo) for the line holding the rest takes
        # the rest as its amount, as GnuCash does (#369).
        balancing = (
            self._balancing_cells()
            if index == self.balancing_line()
            and column in ("description", "transfer")
            and text.strip()
            else None
        )
        setattr(line, LINE_ATTRIBUTES[column], text)
        if balancing is not None:
            line.increase, line.decrease = balancing
        if column in ("increase", "decrease") and text.strip():
            setattr(line, "decrease" if column == "increase" else "increase", "")
        self._grow_lines()

    def _quickfill_line(self, column: str, typed: str) -> tuple[str, list[str]]:
        """What a split line's cell shows for ``typed``, and the choices it may mean.

        The account completes the segment being typed; a memo completes from the
        memos written for the same description before (#407); planning purpose and
        investment activity complete from their lists.
        """
        if column == "transfer":
            return quickfill_account(typed, self.account_names)
        if column == "description":
            found = complete_note(typed, self._earlier_notes(memos=True))
            return (found, []) if found is not None else (typed, [])
        if column in CHOICES:
            return choose(typed, CHOICES[column])
        return typed, []

    def _settle_arithmetic(self, column: str, text: str) -> bool:
        """Show arithmetic typed in an amount cell as its result; a negative result
        moves to the other amount column."""
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
        return True

    def _settle_line(self, row: SheetRow) -> bool:
        """Read a split line's cell before leaving it; False (with a message) when
        unreadable. Empty cells are fine until the transaction is saved."""
        column = self.column
        text = self.cell_text().strip()
        if column == "transfer" and text:
            resolved = self._resolve_account(text, self.account_names)
            if resolved is None:
                return self._fail(f"No account matches “{text}”.")
            self.set_text(resolved)
        elif column in ("increase", "decrease") and text and is_arithmetic(text):
            if not self._settle_arithmetic(column, text):
                return False
        elif column in CHOICES and text:
            _shown, found = choose(text, CHOICES[column])
            exact = next((label for label in found if label.casefold() == text.casefold()), None)
            if exact is None and not found:
                return self._fail(
                    f"No {'purpose' if column == 'purpose' else 'activity'} matches “{text}”."
                )
            self.set_text(exact or found[0])
        if column in _TRADE_COLUMNS:
            return self._fill_trade(row, column)
        return True

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
            quantity = None
            if self._holds_security(line):
                try:
                    quantity = self._quantity(line, line.handle)
                except EntryInputError:
                    return self._fail("Enter shares such as 12.345.", "shares", index)
                if not quantity:
                    return self._fail("Enter the shares for this split.", "shares", index)
            if (value is None or value == 0) and not quantity:
                return self._fail("Enter an amount for this split.", "increase", index)
            value = value or Money(0)
            splits.append(
                self._split_input(
                    self.account_names[name],
                    value if sign > 0 else -value,
                    currency,
                    line.handle,
                    line.memo,
                    quantity=quantity,
                    line=line,
                )
            )
        if len(splits) < 2:
            return self._fail("A transaction needs at least two splits.", "transfer", 0)
        residual = self.residual()
        if residual:
            rest_line = self.balancing_line()
            return self._fail(
                f"The splits are out of balance by {residual.format(parens_negative=True)}; "
                "choose an account for the last line, which holds the rest.",
                "transfer",
                rest_line if rest_line is not None else 0,
            )
        # An existing transaction may move out of this account entirely, such as a
        # purchase entered on the wrong card (#364); a new one must belong here.
        if self._needs_own_split() and not any(split.account == self.account for split in splits):
            return self._fail("One split must be in this register's account.", "transfer", 0)
        return tuple(splits)
