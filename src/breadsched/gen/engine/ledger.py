"""Reading the books: balances and registers.

Everything here is a pure function of the database.  Balances are never cached on
the account object, because a cached balance is a bug waiting for the first
back-dated transaction; the ``split_index`` table makes recomputation cheap enough
that caching buys nothing.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from ..db.sqlite import DbSQLite
from ..lib.account import Account, AccountClass
from ..lib.money import Money
from ..lib.transaction import Split, Transaction

__all__ = [
    "balance",
    "balance_recursive",
    "register",
    "RegisterRow",
    "totals_by_class",
    "net_worth",
    "cash_on_hand",
]


def balance(
    db: DbSQLite,
    account: str | Account,
    as_of: date | None = None,
    since: date | None = None,
    natural_sign: bool = True,
) -> Money:
    """Balance of one account, excluding its children.

    With ``natural_sign`` the result is what a user expects to read: a credit card
    with 500 owed reports 500, not -500.  Set it False when summing raw split
    values across account classes, where the signed form is what balances to zero.
    """
    handle = account if isinstance(account, str) else account.handle
    total = Money(0)
    for row in db.split_rows(handle, start=since, end=as_of):
        total = total + Money(row["value_num"], row["value_den"])
    if natural_sign:
        obj = db.get_account(handle)
        if obj is not None:
            total = total * obj.sign()
    return total


def balance_recursive(
    db: DbSQLite,
    account: str | Account,
    as_of: date | None = None,
    since: date | None = None,
    natural_sign: bool = True,
) -> Money:
    """Balance including every descendant, which is what an account tree shows."""
    handle = account if isinstance(account, str) else account.handle
    total = balance(db, handle, as_of, since, natural_sign)
    for child in db.descendants(handle):
        total = total + balance(db, child.handle, as_of, since, natural_sign)
    return total


@dataclass(slots=True)
class RegisterRow:
    """One line of a GnuCash-style register."""

    transaction: Transaction
    split: Split
    running: Money

    @property
    def post_date(self) -> date:
        return self.transaction.post_date

    @property
    def description(self) -> str:
        return self.transaction.description

    @property
    def amount(self) -> Money:
        return self.split.value

    @property
    def debit(self) -> Money | None:
        return self.split.value if self.split.value > 0 else None

    @property
    def credit(self) -> Money | None:
        return -self.split.value if self.split.value < 0 else None

    def transfer_label(self, db: DbSQLite) -> str:
        """The 'other side' column: one account name, or ``-- Split --``."""
        others = [s for s in self.transaction.splits if s.handle != self.split.handle]
        if len(others) == 1:
            return db.full_name(others[0].account)
        return "-- Split --"


def register(
    db: DbSQLite,
    account: str | Account,
    start: date | None = None,
    end: date | None = None,
) -> list[RegisterRow]:
    """Ordered rows with a running balance, one per split touching the account.

    A transaction with two splits against the same account (a transfer within one
    account, rare but legal) correctly produces two rows.
    """
    handle = account if isinstance(account, str) else account.handle
    obj = db.get_account(handle)
    sign = obj.sign() if obj else 1
    opening = Money(0)
    if start is not None:
        opening = balance(db, handle, as_of=_day_before(start), natural_sign=False)

    rows: list[RegisterRow] = []
    running = opening
    for txn in db.iter_transactions(account=handle, start=start, end=end):
        for split in txn.splits:
            if split.account != handle:
                continue
            running = running + split.value
            rows.append(RegisterRow(txn, split, running * sign))
    return rows


def _day_before(when: date) -> date:
    return date.fromordinal(when.toordinal() - 1)


def totals_by_class(
    db: DbSQLite,
    as_of: date | None = None,
    since: date | None = None,
) -> dict[AccountClass, Money]:
    """Aggregate natural-sign balances by account class."""
    totals: dict[AccountClass, Money] = {}
    for account in db.iter_accounts():
        if account.is_root:
            continue
        cls = account.account_class
        totals[cls] = totals.get(cls, Money(0)) + balance(db, account.handle, as_of, since)
    return totals


def net_worth(db: DbSQLite, as_of: date | None = None) -> Money:
    """Assets less liabilities, ignoring the income and expense flows."""
    totals = totals_by_class(db, as_of=as_of)
    return totals.get(AccountClass.ASSET, Money(0)) - totals.get(AccountClass.LIABILITY, Money(0))


def cash_on_hand(db: DbSQLite, as_of: date | None = None) -> Money:
    """Only what is spendable now: bank and cash accounts."""
    total = Money(0)
    for account in db.iter_accounts():
        if account.atype.is_cash_like and not account.placeholder:
            total = total + balance(db, account.handle, as_of)
    return total


def accounts_of_class(db: DbSQLite, classes: Iterable[AccountClass]) -> list[Account]:
    wanted = set(classes)
    return [a for a in db.iter_accounts() if a.account_class in wanted and not a.is_root]
