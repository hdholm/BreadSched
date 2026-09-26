"""As-of valuation of security quantities without changing ledger history."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..db.sqlite import DbSQLite
from ..lib.account import Account, AccountClass, AccountType
from ..lib.amount import Amount
from ..lib.commodity import Commodity, CommodityPrice
from ..lib.money import Money
from . import ledger
from .currency import book_currency, commodity_fraction, reporting_currency_handle

__all__ = [
    "AccountValuation",
    "CurrencyConversion",
    "ValuationAggregate",
    "aggregate_value",
    "account_value",
    "book_currency",
    "convert_currency",
    "latest_price",
    "net_worth",
    "quantity_balance",
    "quote_age_label",
    "save_currency_quote",
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
    price_source: str | None = None
    quote_age_days: int | None = None
    conversion_path: str | None = None
    missing_quote: bool = False
    commodity: Commodity | None = None
    currency: Commodity | None = None
    total_amount: Amount | None = None
    quantity_amount: Amount | None = None


@dataclass(frozen=True, slots=True)
class CurrencyConversion:
    """One as-of currency conversion, or explicit evidence of a missing quote."""

    amount: Amount | None
    source_currency: str
    target_currency: str
    quote_date: date | None = None
    quote_source: str | None = None
    quote_type: str | None = None
    path: str | None = None

    @property
    def missing_quote(self) -> bool:
        return self.amount is None


@dataclass(frozen=True, slots=True)
class ValuationAggregate:
    """Exact reporting-currency total, or accounts preventing a complete total."""

    amount: Amount | None
    missing_quotes: tuple[str, ...] = ()
    incompatible_accounts: tuple[str, ...] = ()


def quote_age_label(days: int) -> str:
    """Describe quote age relative to the valuation date without imposing a cutoff."""
    if days < 0:
        return f"dated {-days} day{'s' if days != -1 else ''} ahead"
    if days == 0:
        return "dated today"
    return f"{days} day{'s' if days != 1 else ''} old"


def aggregate_value(
    db: DbSQLite,
    *,
    accounts: list[Account] | None = None,
    as_of: date | None = None,
    net_worth: bool = False,
) -> ValuationAggregate:
    """Sum only complete reporting-currency valuations, without rounding or fallback."""
    reporting = reporting_currency_handle(db)
    total = Amount(Money(0), reporting)
    missing: list[str] = []
    incompatible: list[str] = []
    for account in accounts if accounts is not None else db.iter_accounts():
        if account.is_root or (
            net_worth and account.account_class not in {AccountClass.ASSET, AccountClass.LIABILITY}
        ):
            continue
        valued = account_value(db, account, as_of=as_of)
        amount = valued.total_amount
        if amount is None or not amount:
            continue
        if valued.missing_quote:
            missing.append(account.handle)
        elif amount.commodity != reporting:
            incompatible.append(account.handle)
        else:
            sign = -1 if net_worth and account.account_class == AccountClass.LIABILITY else 1
            total += amount * sign
    return ValuationAggregate(
        None if missing or incompatible else total,
        tuple(missing),
        tuple(incompatible),
    )


def convert_currency(
    db: DbSQLite,
    amount: Amount,
    *,
    as_of: date | None = None,
    currency: str | Commodity | None = None,
) -> CurrencyConversion:
    """Prefer an applicable direct quote, then invert an applicable reverse quote.

    Keep the result exact so a caller can sum converted values before rounding
    once for presentation. An absent quote returns no converted amount; callers
    must choose and disclose any fallback themselves.
    """
    source = db.get_commodity(amount.commodity)
    if currency is None:
        target = book_currency(db)
    elif isinstance(currency, str):
        target = db.get_commodity(currency)
    else:
        target = db.get_commodity(currency.handle)
    if source is None or not source.is_currency:
        raise ValueError("source must be a known currency")
    if target is None or not target.is_currency or db.get_commodity(target.handle) is None:
        raise ValueError("target must be a known currency")
    if source.handle == target.handle:
        return CurrencyConversion(amount, source.handle, target.handle, path="identity")
    quote = latest_price(db, source, as_of=as_of, currency=target)
    if quote is None:
        reverse = latest_price(db, target, as_of=as_of, currency=source)
        if reverse is None:
            return CurrencyConversion(None, source.handle, target.handle)
        return CurrencyConversion(
            Amount(amount.value * (Money(1) / reverse.value), target.handle),
            source.handle,
            target.handle,
            quote_date=reverse.quote_date,
            quote_source=reverse.source,
            quote_type=reverse.quote_type,
            path="inverse",
        )
    return CurrencyConversion(
        quote.convert(amount),
        source.handle,
        target.handle,
        quote_date=quote.quote_date,
        quote_source=quote.source,
        quote_type=quote.quote_type,
        path="direct",
    )


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
        total_amount = Amount(Money(0), reporting_currency_handle(db))
        return AccountValuation(Money(0), total_amount=total_amount)
    ledger_amount = ledger.balance_amount(db, obj, as_of=as_of)
    ledger_total = ledger_amount.value
    if obj.atype not in {AccountType.INVESTMENT, AccountType.RETIREMENT}:
        reporting = reporting_currency_handle(db)
        source_currency = db.get_commodity(ledger_amount.commodity)
        if ledger_amount.commodity != reporting and source_currency is not None:
            if source_currency.is_currency:
                converted = convert_currency(db, ledger_amount, as_of=as_of)
                if converted.amount is None:
                    return AccountValuation(
                        ledger_total,
                        total_amount=ledger_amount,
                        missing_quote=True,
                        currency=source_currency,
                    )
                return AccountValuation(
                    converted.amount.value,
                    source="currency",
                    total_amount=converted.amount,
                    price_date=converted.quote_date,
                    price_source=converted.quote_source,
                    quote_age_days=(
                        ((as_of or date.today()) - converted.quote_date).days
                        if converted.quote_date is not None
                        else None
                    ),
                    conversion_path=converted.path,
                    currency=db.get_commodity(reporting),
                )
        return AccountValuation(ledger_total, total_amount=ledger_amount)
    commodity = db.get_commodity(obj.commodity) if obj.commodity else None
    if commodity is None or commodity.is_currency:
        return AccountValuation(ledger_total, total_amount=ledger_amount)
    price = latest_price(db, commodity, as_of=as_of)
    if price is None:
        return AccountValuation(
            ledger_total, commodity=commodity, total_amount=ledger_amount, missing_quote=True
        )
    quantity = quantity_balance(db, obj, as_of=as_of)
    currency = db.get_commodity(price.currency)
    fraction = commodity_fraction(db, price.currency)
    quantity_amount = Amount(quantity, commodity.handle)
    total_amount = price.convert(quantity_amount, fraction=fraction)
    return AccountValuation(
        total=total_amount.value,
        source="market",
        quantity=quantity,
        price=price.value,
        price_date=price.quote_date,
        price_source=price.source,
        quote_age_days=((as_of or date.today()) - price.quote_date).days,
        commodity=commodity,
        currency=currency,
        total_amount=total_amount,
        quantity_amount=quantity_amount,
    )


def value_recursive(db: DbSQLite, account: str | Account, as_of: date | None = None) -> Money:
    """Ledger or market value of an account and each descendant exactly once."""
    obj = db.get_account(account) if isinstance(account, str) else account
    if obj is None:
        return Money(0)
    own = account_value(db, obj, as_of=as_of)
    total = own.total_amount or Amount(own.total, reporting_currency_handle(db))
    for child in db.descendants(obj.handle):
        valued = account_value(db, child, as_of=as_of)
        amount = valued.total_amount or Amount(valued.total, total.commodity)
        if amount:
            total = amount if not total else total + amount
    return total.value


def totals_by_class(db: DbSQLite, as_of: date | None = None) -> dict[AccountClass, Money]:
    """Top-level accounting totals with security accounts marked to market."""
    tagged: dict[AccountClass, Amount] = {}
    for account in db.iter_accounts():
        if account.is_root:
            continue
        cls = account.account_class
        valued = account_value(db, account, as_of=as_of)
        amount = valued.total_amount or Amount(valued.total, reporting_currency_handle(db))
        tagged[cls] = tagged[cls] + amount if cls in tagged else amount
    return {cls: amount.value for cls, amount in tagged.items()}


def net_worth(db: DbSQLite, as_of: date | None = None) -> Money:
    """Market-valued assets less liabilities."""
    totals = totals_by_class(db, as_of=as_of)
    return totals.get(AccountClass.ASSET, Money(0)) - totals.get(AccountClass.LIABILITY, Money(0))


def save_currency_quote(
    db: DbSQLite,
    *,
    source_handle: str,
    target_handle: str,
    quote_date: date,
    value: Money,
) -> CommodityPrice:
    """Save an exact manual FX quote without changing imported price evidence.

    The pair is directional: ``value`` is target units per one source unit.
    Repeated entry on the same date updates only the BreadSched-owned quote.
    """
    source = db.get_commodity(source_handle)
    target = db.get_commodity(target_handle)
    if source is None or not source.is_currency:
        raise ValueError("source must be a known currency")
    if target is None or not target.is_currency:
        raise ValueError("target must be a known currency")
    if source.handle == target.handle:
        raise ValueError("choose two different currencies")
    if value <= 0:
        raise ValueError("rate must be greater than zero")

    with db.transaction("Save currency quote") as txn:
        existing = next(
            (
                item
                for item in db.iter_prices(
                    commodity=source.handle,
                    currency=target.handle,
                    through=quote_date,
                )
                if item.quote_date == quote_date and item.source == "breadsched"
            ),
            None,
        )
        quote = CommodityPrice(
            handle=existing.handle if existing is not None else None,
            commodity=source.handle,
            currency=target.handle,
            quote_date=quote_date,
            value=value,
        )
        if existing is None:
            db.add_price(quote, txn)
        else:
            db.commit_price(quote, txn)
    return quote


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
