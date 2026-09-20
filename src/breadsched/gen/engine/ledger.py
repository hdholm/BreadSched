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
from ..lib.account import Account, AccountClass, AccountType
from ..lib.amount import Amount
from ..lib.money import Money
from ..lib.transaction import Split, Transaction
from .currency import reporting_currency_handle

__all__ = [
    "balance",
    "balance_amount",
    "balance_recursive",
    "balance_recursive_amount",
    "register_headings",
    "register",
    "RegisterRow",
    "totals_by_class",
    "totals_by_class_amount",
    "net_worth",
    "net_worth_amount",
    "cash_on_hand",
    "cash_on_hand_amount",
]


_REGISTER_HEADINGS = {
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


def _accumulate(total: Amount | None, item: Amount) -> Amount:
    """Add a material amount without assigning a commodity to an empty zero."""
    if not item:
        return total or item
    if total is None or not total:
        return item
    return total + item


def register_headings(atype: AccountType) -> tuple[str, str]:
    """Return household-language labels for positive and negative register splits."""
    return _REGISTER_HEADINGS.get(atype, ("Increase", "Decrease"))


def balance_amount(
    db: DbSQLite,
    account: str | Account,
    as_of: date | None = None,
    since: date | None = None,
    natural_sign: bool = True,
) -> Amount:
    """Tagged transaction-currency balance of one account, excluding children.

    With ``natural_sign`` the result is what a user expects to read: a credit card
    with 500 owed reports 500, not -500.  Set it False when summing raw split
    values across account classes, where the signed form is what balances to zero.
    Transactions in unlike currencies are rejected instead of silently netted.
    """
    handle = account if isinstance(account, str) else account.handle
    reporting_currency = reporting_currency_handle(db)
    total: Amount | None = None
    for row in db.split_rows(handle, start=since, end=as_of):
        row_currency = row["currency"]
        commodity = (
            row_currency if isinstance(row_currency, str) and row_currency else reporting_currency
        )
        total = _accumulate(total, Amount(Money(row["value_num"], row["value_den"]), commodity))
    if total is None:
        total = Amount(Money(0), reporting_currency)
    if natural_sign:
        obj = db.get_account(handle)
        if obj is not None:
            total = total * obj.sign()
    return total


def balance(
    db: DbSQLite,
    account: str | Account,
    as_of: date | None = None,
    since: date | None = None,
    natural_sign: bool = True,
) -> Money:
    """Compatibility scalar for :func:`balance_amount`."""
    return balance_amount(db, account, as_of, since, natural_sign).value


def balance_recursive_amount(
    db: DbSQLite,
    account: str | Account,
    as_of: date | None = None,
    since: date | None = None,
    natural_sign: bool = True,
) -> Amount:
    """Tagged balance including every descendant exactly once."""
    handle = account if isinstance(account, str) else account.handle
    total = balance_amount(db, handle, as_of, since, natural_sign)
    for child in db.descendants(handle):
        total = _accumulate(total, balance_amount(db, child.handle, as_of, since, natural_sign))
    return total


def balance_recursive(
    db: DbSQLite,
    account: str | Account,
    as_of: date | None = None,
    since: date | None = None,
    natural_sign: bool = True,
) -> Money:
    """Compatibility scalar for :func:`balance_recursive_amount`."""
    return balance_recursive_amount(db, account, as_of, since, natural_sign).value


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
    currency = reporting_currency_handle(db)
    opening: Amount | None = None
    if start is not None:
        opening = balance_amount(db, handle, as_of=_day_before(start), natural_sign=False)

    rows: list[RegisterRow] = []
    running = opening
    for txn in db.iter_transactions(account=handle, start=start, end=end):
        for split in txn.splits:
            if split.account != handle:
                continue
            amount = Amount(split.value, txn.currency or currency)
            running = _accumulate(running, amount)
            rows.append(RegisterRow(txn, split, (running * sign).value))
    return rows


def _day_before(when: date) -> date:
    return date.fromordinal(when.toordinal() - 1)


def totals_by_class_amount(
    db: DbSQLite,
    as_of: date | None = None,
    since: date | None = None,
) -> dict[AccountClass, Amount]:
    """Aggregate tagged natural-sign balances by account class."""
    totals: dict[AccountClass, Amount] = {}
    for account in db.iter_accounts():
        if account.is_root:
            continue
        cls = account.account_class
        amount = balance_amount(db, account.handle, as_of, since)
        totals[cls] = _accumulate(totals.get(cls), amount)
    return totals


def totals_by_class(
    db: DbSQLite,
    as_of: date | None = None,
    since: date | None = None,
) -> dict[AccountClass, Money]:
    """Compatibility scalars for :func:`totals_by_class_amount`."""
    return {
        cls: amount.value
        for cls, amount in totals_by_class_amount(db, as_of=as_of, since=since).items()
    }


def net_worth_amount(db: DbSQLite, as_of: date | None = None) -> Amount:
    """Tagged assets less liabilities, ignoring income and expense flows."""
    currency = reporting_currency_handle(db)
    totals = totals_by_class_amount(db, as_of=as_of)
    assets = totals.get(AccountClass.ASSET)
    liabilities = totals.get(AccountClass.LIABILITY)
    if assets is None and liabilities is None:
        return Amount(Money(0), currency)
    if assets is None:
        assert liabilities is not None
        assets = Amount(Money(0), liabilities.commodity)
    if liabilities is None:
        liabilities = Amount(Money(0), assets.commodity)
    return assets - liabilities


def net_worth(db: DbSQLite, as_of: date | None = None) -> Money:
    """Compatibility scalar for :func:`net_worth_amount`."""
    return net_worth_amount(db, as_of).value


def cash_on_hand_amount(db: DbSQLite, as_of: date | None = None) -> Amount:
    """Tagged spendable balance in bank and cash accounts."""
    total: Amount | None = None
    for account in db.iter_accounts():
        if account.atype.is_cash_like and not account.placeholder:
            total = _accumulate(total, balance_amount(db, account.handle, as_of))
    return total or Amount(Money(0), reporting_currency_handle(db))


def cash_on_hand(db: DbSQLite, as_of: date | None = None) -> Money:
    """Compatibility scalar for :func:`cash_on_hand_amount`."""
    return cash_on_hand_amount(db, as_of).value


def accounts_of_class(db: DbSQLite, classes: Iterable[AccountClass]) -> list[Account]:
    wanted = set(classes)
    return [a for a in db.iter_accounts() if a.account_class in wanted and not a.is_root]
