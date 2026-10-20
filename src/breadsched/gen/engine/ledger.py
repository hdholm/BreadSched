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
from ..lib.commodity import Commodity
from ..lib.money import Money
from ..lib.transaction import Split, Transaction
from .currency import reporting_currency_handle

__all__ = [
    "balance_amount",
    "balance_recursive_amount",
    "register_headings",
    "register",
    "RegisterRow",
    "security_of",
    "share_fraction",
    "update_register",
    "totals_by_class_amount",
    "net_worth_amount",
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


def security_of(db: DbSQLite, account: str | Account | None) -> Commodity | None:
    """The security an account holds, or ``None`` for an account kept in a currency.

    Such an account's splits carry shares as their quantity, so its register
    shows shares, a price, and a share balance beside the amounts (#323).
    """
    obj = db.get_account(account) if isinstance(account, str) else account
    if obj is None or not obj.commodity:
        return None
    commodity = db.get_commodity(obj.commodity)
    return commodity if commodity is not None and not commodity.is_currency else None


def share_fraction(db: DbSQLite, account: str | Account) -> int:
    """The smallest share unit an account records (its SCU, else the security's)."""
    obj = db.get_account(account) if isinstance(account, str) else account
    if obj is not None and obj.commodity_scu:
        return obj.commodity_scu
    security = security_of(db, obj)
    return security.fraction if security is not None and security.fraction else 1


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


@dataclass(slots=True)
class RegisterRow:
    """One line of a GnuCash-style register."""

    transaction: Transaction
    split: Split
    running: Money
    #: Shares held after this row, in an account that holds a security (#323).
    share_balance: Money | None = None

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
    def shares(self) -> Money:
        """The shares this row adds (negative for shares sold or moved out)."""
        return self.split.quantity

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

    shares: Money | None = None
    if security_of(db, obj) is not None:
        shares = Money(0)
        if start is not None:
            for row in db.split_rows(handle, end=_day_before(start)):
                shares += Money(row["quantity_num"], row["quantity_den"])

    rows: list[RegisterRow] = []
    running = opening
    for txn in db.iter_transactions(account=handle, start=start, end=end):
        for split in txn.splits:
            if split.account != handle:
                continue
            amount = Amount(split.value, txn.currency or currency)
            running = _accumulate(running, amount)
            if shares is not None:
                shares = shares + split.quantity
            rows.append(
                RegisterRow(
                    txn,
                    split,
                    (running * sign).value,
                    shares * sign if shares is not None else None,
                )
            )
    return rows


def update_register(
    db: DbSQLite,
    account: str | Account,
    rows: list[RegisterRow],
    changed: Iterable[str],
) -> list[RegisterRow]:
    """``register(db, account)`` after ``changed`` transactions were saved or removed.

    Only the changed transactions are read from the book; the other rows are
    kept, and every running balance is recomputed in memory. Entering one
    transaction in a long register therefore no longer decodes the account's
    whole history (#319). ``rows`` must be a full-range register of ``account``;
    the result equals rebuilding it.
    """
    handle = account if isinstance(account, str) else account.handle
    gone = set(changed)
    kept = [row for row in rows if row.transaction.handle not in gone]
    added: list[RegisterRow] = []
    for txn_handle in sorted(gone):
        txn = db.get_transaction(txn_handle)
        if txn is None:
            continue
        added.extend(
            RegisterRow(txn, split, Money(0)) for split in txn.splits if split.account == handle
        )
    merged = sorted(
        kept + added, key=lambda row: (row.transaction.post_date, row.transaction.handle)
    )
    obj = db.get_account(handle)
    sign = obj.sign() if obj else 1
    currency = reporting_currency_handle(db)
    running: Amount | None = None
    shares: Money | None = Money(0) if security_of(db, obj) is not None else None
    for row in merged:
        amount = Amount(row.split.value, row.transaction.currency or currency)
        running = _accumulate(running, amount)
        value = (running * sign).value
        # Rows keep their objects and only a changed balance is written: making
        # thousands of new rows per save set off full garbage collections, which
        # made an occasional save several times slower than the others.
        if row.running != value:
            row.running = value
        if shares is not None:
            shares = shares + row.split.quantity
            held = shares * sign
            if row.share_balance != held:
                row.share_balance = held
        elif row.share_balance is not None:
            row.share_balance = None
    return merged


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


def cash_on_hand_amount(db: DbSQLite, as_of: date | None = None) -> Amount:
    """Tagged spendable balance in bank and cash accounts."""
    total: Amount | None = None
    for account in db.iter_accounts():
        if account.atype.is_cash_like and not account.placeholder:
            total = _accumulate(total, balance_amount(db, account.handle, as_of))
    return total or Amount(Money(0), reporting_currency_handle(db))


def accounts_of_class(db: DbSQLite, classes: Iterable[AccountClass]) -> list[Account]:
    wanted = set(classes)
    return [a for a in db.iter_accounts() if a.account_class in wanted and not a.is_root]
