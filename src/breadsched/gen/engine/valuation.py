"""As-of valuation of security quantities without changing ledger history."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from fractions import Fraction

from ..db.sqlite import DbSQLite
from ..lib.account import Account, AccountClass, AccountType
from ..lib.commodity import Commodity, CommodityPrice
from ..lib.money import Money
from . import ledger

__all__ = [
    "AccountValuation",
    "account_value",
    "book_currency",
    "latest_price",
    "net_worth",
    "quantity_balance",
    "save_security_price",
    "totals_by_class",
    "value_recursive",
]


@dataclass(frozen=True, slots=True)
class AccountValuation:
    """One account's value and the evidence used to calculate it."""

    total: Money
    source: str = "ledger"
    quantity: Money | None = None
    price: Money | None = None
    price_date: date | None = None
    commodity: Commodity | None = None
    currency: Commodity | None = None


def book_currency(db: DbSQLite) -> Commodity | None:
    """The configured reporting currency, then USD, then the first currency."""
    configured = db.get_metadata("default_currency")
    if isinstance(configured, str):
        found = db.get_commodity(configured) or db.get_commodity_by_mnemonic(configured)
        if found is not None and found.is_currency:
            return found
    usd = db.get_commodity_by_mnemonic("USD")
    if usd is not None and usd.is_currency:
        return usd
    return next((commodity for commodity in db.iter_commodities() if commodity.is_currency), None)


def latest_price(
    db: DbSQLite,
    commodity: str | Commodity,
    *,
    as_of: date | None = None,
    currency: str | Commodity | None = None,
) -> CommodityPrice | None:
    """Latest quote on or before ``as_of`` in the requested/reporting currency."""
    commodity_handle = commodity if isinstance(commodity, str) else commodity.handle
    quote_currency = currency
    if quote_currency is None:
        quote_currency = book_currency(db)
    currency_handle = (
        quote_currency
        if isinstance(quote_currency, str)
        else quote_currency.handle
        if quote_currency is not None
        else None
    )
    return next(
        db.iter_prices(commodity=commodity_handle, currency=currency_handle, through=as_of),
        None,
    )


def quantity_balance(db: DbSQLite, account: str | Account, as_of: date | None = None) -> Money:
    """Account-commodity units held as of a date, excluding descendants."""
    obj = db.get_account(account) if isinstance(account, str) else account
    if obj is None:
        return Money(0)
    total = Money(0)
    for row in db.split_rows(obj.handle, end=as_of):
        total = total + Money(row["quantity_num"], row["quantity_den"])
    return total * obj.sign()


def account_value(
    db: DbSQLite, account: str | Account, as_of: date | None = None
) -> AccountValuation:
    """Market-value a security account, falling back explicitly to ledger value."""
    obj = db.get_account(account) if isinstance(account, str) else account
    if obj is None:
        return AccountValuation(Money(0))
    ledger_total = ledger.balance(db, obj, as_of=as_of)
    if obj.atype not in {AccountType.INVESTMENT, AccountType.RETIREMENT}:
        return AccountValuation(ledger_total)
    commodity = db.get_commodity(obj.commodity) if obj.commodity else None
    if commodity is None or commodity.is_currency:
        return AccountValuation(ledger_total)
    price = latest_price(db, commodity, as_of=as_of)
    if price is None:
        return AccountValuation(ledger_total, commodity=commodity)
    quantity = quantity_balance(db, obj, as_of=as_of)
    scalar = Fraction(quantity.numerator, quantity.denominator)
    currency = db.get_commodity(price.currency)
    fraction = currency.fraction if currency is not None else 100
    total = (price.value * scalar).quantize(fraction)
    return AccountValuation(
        total=total,
        source="market",
        quantity=quantity,
        price=price.value,
        price_date=price.quote_date,
        commodity=commodity,
        currency=currency,
    )


def value_recursive(db: DbSQLite, account: str | Account, as_of: date | None = None) -> Money:
    """Ledger or market value of an account and each descendant exactly once."""
    obj = db.get_account(account) if isinstance(account, str) else account
    if obj is None:
        return Money(0)
    total = account_value(db, obj, as_of=as_of).total
    for child in db.descendants(obj.handle):
        total = total + account_value(db, child, as_of=as_of).total
    return total


def totals_by_class(db: DbSQLite, as_of: date | None = None) -> dict[AccountClass, Money]:
    """Top-level accounting totals with security accounts marked to market."""
    totals: dict[AccountClass, Money] = {}
    for account in db.iter_accounts():
        if account.is_root:
            continue
        cls = account.account_class
        totals[cls] = totals.get(cls, Money(0)) + account_value(db, account, as_of=as_of).total
    return totals


def net_worth(db: DbSQLite, as_of: date | None = None) -> Money:
    """Market-valued assets less liabilities."""
    totals = totals_by_class(db, as_of=as_of)
    return totals.get(AccountClass.ASSET, Money(0)) - totals.get(AccountClass.LIABILITY, Money(0))


def save_security_price(
    db: DbSQLite,
    *,
    security_handle: str | None,
    currency_handle: str | None,
    quote_date: date,
    value: Money,
    mnemonic: str = "",
    fullname: str = "",
    namespace: str = "FUND",
    fraction: int = 10000,
) -> tuple[Commodity, CommodityPrice]:
    """Create a security when needed and save one user-entered dated quote."""
    security = db.get_commodity(security_handle) if security_handle else None
    creating = security_handle is None
    if security_handle is not None and security is None:
        raise ValueError("security no longer exists")
    if security is not None and security.is_currency:
        raise ValueError("choose a security, not a currency")
    if value <= 0:
        raise ValueError("price must be greater than zero")

    currency = db.get_commodity(currency_handle) if currency_handle else None
    creating_currency = False
    if currency is None and not currency_handle:
        currency = db.get_commodity_by_mnemonic("USD")
        if currency is None:
            currency = Commodity(mnemonic="USD", fullname="US Dollar", fraction=100)
            creating_currency = True
    if currency is None or not currency.is_currency:
        raise ValueError("choose a quote currency")

    if creating:
        symbol = mnemonic.strip().upper()
        if not symbol:
            raise ValueError("security symbol is required")
        if db.get_commodity_by_mnemonic(symbol) is not None:
            raise ValueError("that security symbol already exists")
        security_namespace = namespace.strip().upper() or "FUND"
        if security_namespace in {"CURRENCY", "ISO4217"}:
            raise ValueError("security namespace cannot be a currency namespace")
        if fraction < 1:
            raise ValueError("security fraction must be positive")
        security = Commodity(
            namespace=security_namespace,
            mnemonic=symbol,
            fullname=fullname.strip() or symbol,
            fraction=fraction,
        )

    assert security is not None
    with db.transaction("Save security price") as txn:
        if creating_currency:
            db.add_commodity(currency, txn)
        if creating:
            db.add_commodity(security, txn)
        existing = next(
            (
                item
                for item in db.iter_prices(
                    commodity=security.handle,
                    currency=currency.handle,
                    through=quote_date,
                )
                if item.quote_date == quote_date and item.source == "breadsched"
            ),
            None,
        )
        price = CommodityPrice(
            handle=existing.handle if existing is not None else None,
            commodity=security.handle,
            currency=currency.handle,
            quote_date=quote_date,
            value=value,
        )
        if existing is None:
            db.add_price(price, txn)
        else:
            db.commit_price(price, txn)
    return security, price
