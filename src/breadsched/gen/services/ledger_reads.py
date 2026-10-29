"""Ledger reads every interface shares: lookups, accounts, registers, balances.

These are read-only. Each returns a typed result, so the book server can serve the
same answer in process and over HTTP (#354). A missing account or transaction is a
``*.not_found`` failure; a reference that matches more than one is
``*.reference.ambiguous``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..db.sqlite import DbSQLite
from ..engine import ledger, valuation
from ..lib.account import Account, AccountClass
from ..lib.amount import Amount
from ..lib.commodity import Commodity
from ..lib.money import Money
from ..lib.transaction import Split, Transaction
from .contracts import ServiceError, ServiceResult
from .transactions import TransactionInput, TransactionSplitInput, transaction_currency

__all__ = [
    "AccountBalance",
    "AccountRecord",
    "AccountRef",
    "ChartLine",
    "BalanceQuery",
    "BalanceSummary",
    "RegisterLine",
    "RegisterPage",
    "RegisterQuery",
    "OutlineLine",
    "account_balance",
    "account_outline",
    "account_record",
    "account_records",
    "balance_summary",
    "chart_of_accounts",
    "editable_transaction",
    "find_account",
    "find_transaction",
    "register_page",
]


@dataclass(frozen=True, slots=True)
class AccountRef:
    """Who an account is, without its settings."""

    handle: str
    name: str
    full_name: str
    type: str
    commodity: str | None


def _ref(db: DbSQLite, handle: str) -> AccountRef | None:
    account = db.get_account(handle)
    if account is None:
        return None
    return AccountRef(
        account.handle, account.name, db.full_name(account), account.atype.value, account.commodity
    )


def find_account(db: DbSQLite, reference: str) -> ServiceResult[AccountRef]:
    """An account by handle, full name (``Assets:Checking``), or unique bare name."""
    account = db.get_account(reference) or db.get_account_by_name(reference)
    found = _ref(db, account.handle) if account is not None else None
    if found is None:
        return ServiceResult.failure(ServiceError("account.not_found", ("account",)))
    return ServiceResult.success(found)


def find_transaction(db: DbSQLite, reference: str) -> ServiceResult[Transaction]:
    """A transaction by handle or unique handle prefix."""
    exact = db.get_transaction(reference) if reference else None
    if exact is not None:
        return ServiceResult.success(exact)
    matches = (
        [item for item in db.iter_transactions() if item.handle.startswith(reference)]
        if reference
        else []
    )
    if not matches:
        return ServiceResult.failure(ServiceError("transaction.not_found", ("transaction",)))
    if len(matches) > 1:
        return ServiceResult.failure(
            ServiceError("transaction.reference.ambiguous", ("transaction",))
        )
    return ServiceResult.success(matches[0])


def editable_transaction(db: DbSQLite, handle: str) -> ServiceResult[TransactionInput]:
    """The stored transaction in the editors' input form, ready to change and save."""
    transaction = db.get_transaction(handle)
    if transaction is None:
        return ServiceResult.failure(ServiceError("transaction.not_found", ("transaction",)))
    currency = transaction_currency(db, transaction.currency)

    def split_input(split: Split) -> TransactionSplitInput:
        account = db.get_account(split.account)
        quantity_commodity = account.commodity if account is not None else None
        return TransactionSplitInput(
            split.account,
            Amount(split.value, currency),
            quantity=Amount(split.quantity, quantity_commodity or currency),
            handle=split.handle,
            memo=split.memo,
            planning_flow=split.planning_flow,
            investment_activity=split.investment_activity,
        )

    return ServiceResult.success(
        TransactionInput(
            post_date=transaction.post_date,
            description=transaction.description,
            num=transaction.num,
            notes=transaction.notes,
            currency=currency,
            splits=tuple(split_input(split) for split in transaction.splits),
        )
    )


@dataclass(frozen=True, slots=True)
class RegisterQuery:
    account: str
    start: date | None = None
    end: date | None = None
    #: Only transactions carrying this tag (any case).
    tag: str | None = None
    #: Only the last this-many lines.
    limit: int | None = None


@dataclass(frozen=True, slots=True)
class RegisterLine:
    transaction: str
    split: str
    when: date
    description: str
    transfer: str
    amount: Money
    balance: Money
    tags: tuple[str, ...]
    #: Linked documents, counting an external source link.
    attachments: int


@dataclass(frozen=True, slots=True)
class RegisterPage:
    account: AccountRef
    lines: tuple[RegisterLine, ...]


def register_page(db: DbSQLite, query: RegisterQuery) -> ServiceResult[RegisterPage]:
    """An account's register with running balances, oldest first."""
    account = _ref(db, query.account)
    if account is None:
        return ServiceResult.failure(ServiceError("account.not_found", ("account",)))
    if query.limit is not None and query.limit < 0:
        return ServiceResult.failure(ServiceError("register.limit.invalid", ("limit",)))
    rows = ledger.register(db, account.handle, start=query.start, end=query.end)
    if query.tag:
        wanted = " ".join(query.tag.split()).casefold()
        rows = [row for row in rows if any(t.casefold() == wanted for t in row.transaction.tags)]
    if query.limit:
        rows = rows[-query.limit :]
    return ServiceResult.success(
        RegisterPage(
            account,
            tuple(
                RegisterLine(
                    row.transaction.handle,
                    row.split.handle,
                    row.post_date,
                    row.description,
                    row.transfer_label(db),
                    row.amount,
                    row.running,
                    tuple(row.transaction.tags),
                    len(row.transaction.attachments) + (1 if row.transaction.source_link else 0),
                )
                for row in rows
            ),
        )
    )


@dataclass(frozen=True, slots=True)
class BalanceQuery:
    account: str
    as_of: date | None = None
    #: Include every descendant account.
    recursive: bool = False


@dataclass(frozen=True, slots=True)
class AccountBalance:
    account: AccountRef
    amount: Money
    recursive: bool


def account_balance(db: DbSQLite, query: BalanceQuery) -> ServiceResult[AccountBalance]:
    """One account's ledger balance in its own commodity."""
    account = _ref(db, query.account)
    if account is None:
        return ServiceResult.failure(ServiceError("account.not_found", ("account",)))
    amount = (
        ledger.balance_recursive_amount(db, account.handle, as_of=query.as_of).value
        if query.recursive
        else ledger.balance_amount(db, account.handle, as_of=query.as_of).value
    )
    return ServiceResult.success(AccountBalance(account, amount, query.recursive))


@dataclass(frozen=True, slots=True)
class BalanceSummary:
    """Cash and net worth in the reporting currency, with what could not be valued."""

    cash: Money | None
    net_worth: Money | None
    cash_missing_quotes: tuple[str, ...]
    net_worth_missing_quotes: tuple[str, ...]
    net_worth_incompatible_accounts: tuple[str, ...]
    #: Every asset, and every liability, in the reporting currency (None when a
    #: quote one needs is missing), as the desktop's Accounts summary shows them.
    assets: Money | None = None
    liabilities: Money | None = None


def balance_summary(db: DbSQLite, as_of: date | None = None) -> BalanceSummary:
    net_worth = valuation.aggregate_value(db, as_of=as_of, net_worth=True)
    accounts = list(db.iter_accounts())
    cash = valuation.aggregate_value(
        db,
        accounts=[a for a in accounts if a.atype.is_cash_like and not a.placeholder],
        as_of=as_of,
    )
    assets = valuation.aggregate_value(
        db, accounts=[a for a in accounts if a.account_class == AccountClass.ASSET], as_of=as_of
    )
    liabilities = valuation.aggregate_value(
        db,
        accounts=[a for a in accounts if a.account_class == AccountClass.LIABILITY],
        as_of=as_of,
    )
    return BalanceSummary(
        cash.amount.value if cash.amount is not None else None,
        net_worth.amount.value if net_worth.amount is not None else None,
        tuple(cash.missing_quotes),
        tuple(net_worth.missing_quotes),
        tuple(net_worth.incompatible_accounts),
        assets.amount.value if assets.amount is not None else None,
        liabilities.amount.value if liabilities.amount is not None else None,
    )


@dataclass(frozen=True, slots=True)
class AccountRecord:
    """An account as it is stored, with its full name."""

    account: Account
    full_name: str


def account_record(db: DbSQLite, handle: str) -> ServiceResult[AccountRecord]:
    account = db.get_account(handle)
    if account is None:
        return ServiceResult.failure(ServiceError("account.not_found", ("account",)))
    return ServiceResult.success(AccountRecord(account, db.full_name(account)))


def commodities(db: DbSQLite) -> tuple[Commodity, ...]:
    """Every commodity, currencies and securities, by namespace and code."""
    return tuple(sorted(db.iter_commodities(), key=lambda item: (item.namespace, item.mnemonic)))


def account_records(db: DbSQLite) -> tuple[AccountRecord, ...]:
    """Every account, the root included, by full name."""
    records = [AccountRecord(account, db.full_name(account)) for account in db.iter_accounts()]
    return tuple(sorted(records, key=lambda record: record.full_name))


@dataclass(frozen=True, slots=True)
class ChartLine:
    """One account in the chart, with its value and the quotes behind it."""

    record: AccountRecord
    depth: int
    #: The account and its descendants in the reporting currency, or None
    #: when a quote it needs is missing.
    balance: Money | None
    missing_quotes: tuple[str, ...]
    quote_date: date | None
    quote_source: str | None
    quote_age_days: int | None
    conversion_path: str | None
    quote_evidence: str
    #: The ledger balance of the account and its descendants in their own
    #: commodity, or None when they hold unlike commodities.
    book_balance: Money | None
    #: The account's own ledger balance, without its descendants.
    own_balance: Money
    #: How the account alone was valued (``ledger``, ``price``, ...), with the
    #: quantity and price used and whether a quote it needs is missing.
    valuation_source: str
    quantity: Money | None
    price: Money | None
    missing_quote: bool
    #: Mnemonics of the commodity held and the currency it was valued in.
    commodity: str | None
    currency: str | None


@dataclass(frozen=True, slots=True)
class OutlineLine:
    """One account in the tree, with its depth and no value."""

    record: AccountRecord
    depth: int


def account_outline(db: DbSQLite, include_hidden: bool = False) -> tuple[OutlineLine, ...]:
    """The account tree below the root, depth first, as the chart orders it but
    without valuing anything: what choosing an account needs (decision 0004's entry
    role sees only this), and quick to read."""
    lines: list[OutlineLine] = []

    def walk(handle: str | None, depth: int) -> None:
        for account in db.child_accounts(handle):
            if account.hidden and not include_hidden:
                continue
            lines.append(OutlineLine(AccountRecord(account, db.full_name(account)), depth))
            walk(account.handle, depth + 1)

    root = db.root_account()
    walk(root.handle if root else None, 0)
    return tuple(lines)


def chart_of_accounts(
    db: DbSQLite, as_of: date | None = None, include_hidden: bool = False
) -> tuple[ChartLine, ...]:
    """The account tree below the root, depth first, valued as of ``as_of``."""
    lines: list[ChartLine] = []

    def walk(handle: str | None, depth: int) -> None:
        for account in db.child_accounts(handle):
            if account.hidden and not include_hidden:
                continue
            result = valuation.aggregate_value(
                db, accounts=[account, *db.descendants(account.handle)], as_of=as_of
            )
            valued = valuation.account_value(db, account, as_of=as_of)
            try:
                book_balance: Money | None = ledger.balance_recursive_amount(
                    db, account.handle, as_of=as_of
                ).value
            except TypeError as exc:
                if "cannot combine unlike commodities" not in str(exc):
                    raise
                book_balance = None
            lines.append(
                ChartLine(
                    AccountRecord(account, db.full_name(account)),
                    depth,
                    result.amount.value if result.amount is not None else None,
                    tuple(result.missing_quotes),
                    valued.price_date,
                    valued.price_source,
                    valued.quote_age_days,
                    valued.conversion_path,
                    valuation.quote_evidence(db, valued),
                    book_balance,
                    ledger.balance_amount(db, account.handle, as_of=as_of).value,
                    valued.source,
                    valued.quantity,
                    valued.price,
                    valued.missing_quote,
                    valued.commodity.mnemonic if valued.commodity is not None else None,
                    valued.currency.mnemonic if valued.currency is not None else None,
                )
            )
            walk(account.handle, depth + 1)

    root = db.root_account()
    walk(root.handle if root else None, 0)
    return tuple(lines)
