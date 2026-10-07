"""Cost basis and gains of security holdings, derived from their ledger splits.

Lots are never stored: like a receivable's status, they are recomputed from the
splits of each investment or retirement account that holds a security. Each split
that adds shares opens a lot at its value (what was paid, in the transaction's
currency); each split that removes shares sells first-in, first-out from the open
lots, and the gain realized is the proceeds less the cost of the shares it took.
What is still held is the open lots; its unrealized gain is its market value (from
``valuation.account_value``, with the quote it used) less that cost.

Nothing is approximated. Shares that arrive with no recorded value (a transfer in
or a share split) open a lot at zero cost and are named; a sale of more shares than
the account's history bought is named and its uncovered part has no realized gain;
an account whose splits use more than one currency, or whose quote is missing or in
another currency than its cost, has no unrealized gain.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from ..db.sqlite import DbSQLite
from ..lib.account import Account, AccountType
from ..lib.money import Money
from .currency import reporting_currency_handle
from .valuation import AccountValuation, account_value

__all__ = [
    "HoldingCostBasis",
    "Lot",
    "RealizedGain",
    "cost_basis",
    "holdings_cost_basis",
    "shares_text",
]


def shares_text(quantity: Money) -> str:
    """A share count as plain decimal text, without currency formatting."""
    return f"{quantity.to_decimal(8).normalize():f}"


_SECURITY_TYPES = {AccountType.INVESTMENT, AccountType.RETIREMENT}


@dataclass(frozen=True, slots=True)
class Lot:
    """Shares still held from one acquisition, and what they cost."""

    acquired: date
    quantity: Money
    cost: Money
    transaction: str


@dataclass(frozen=True, slots=True)
class RealizedGain:
    """One sale: shares sold, what they brought, what they cost, and the gain."""

    sold: date
    quantity: Money
    proceeds: Money
    cost: Money
    transaction: str
    #: Shares sold beyond the recorded purchases; their cost and gain are unknown.
    uncovered: Money = field(default_factory=lambda: Money(0))

    @property
    def gain(self) -> Money:
        return self.proceeds - self.cost


@dataclass(frozen=True, slots=True)
class HoldingCostBasis:
    """One security account's open lots, realized gains, and unrealized gain."""

    account: Account
    currency: str | None
    lots: tuple[Lot, ...]
    sales: tuple[RealizedGain, ...]
    valuation: AccountValuation
    problems: tuple[str, ...] = ()

    @property
    def quantity(self) -> Money:
        return sum((lot.quantity for lot in self.lots), Money(0))

    @property
    def cost(self) -> Money:
        return sum((lot.cost for lot in self.lots), Money(0))

    @property
    def market_value(self) -> Money | None:
        """Market value in the cost's currency, or ``None`` when it cannot be compared."""
        valued = self.valuation
        if valued.source != "market" or valued.missing_quote or valued.total_amount is None:
            return None
        if self.currency is None or valued.total_amount.commodity != self.currency:
            return None
        return valued.total_amount.value

    @property
    def unrealized_gain(self) -> Money | None:
        market = self.market_value
        return None if market is None else market - self.cost

    def realized_by_year(self) -> dict[int, Money]:
        totals: dict[int, Money] = defaultdict(lambda: Money(0))
        for sale in self.sales:
            totals[sale.sold.year] = totals[sale.sold.year] + sale.gain
        return dict(sorted(totals.items()))

    def as_dict(self) -> dict[str, object]:
        return {
            "account": self.account.handle,
            "currency": self.currency,
            # Share counts as exact decimal text; amounts are money.
            "quantity": shares_text(self.quantity),
            "cost": self.cost,
            "market_value": self.market_value,
            "unrealized_gain": self.unrealized_gain,
            "price": self.valuation.price,
            "price_date": self.valuation.price_date,
            "lots": [
                {
                    "acquired": lot.acquired,
                    "quantity": shares_text(lot.quantity),
                    "cost": lot.cost,
                    "transaction": lot.transaction,
                }
                for lot in self.lots
            ],
            "sales": [
                {
                    "sold": sale.sold,
                    "quantity": shares_text(sale.quantity),
                    "proceeds": sale.proceeds,
                    "cost": sale.cost,
                    "gain": sale.gain,
                    "uncovered": shares_text(sale.uncovered),
                    "transaction": sale.transaction,
                }
                for sale in self.sales
            ],
            "realized_by_year": {str(year): gain for year, gain in self.realized_by_year().items()},
            "problems": list(self.problems),
        }


def _is_security(db: DbSQLite, account: Account) -> bool:
    if account.atype not in _SECURITY_TYPES or account.placeholder or not account.commodity:
        return False
    commodity = db.get_commodity(account.commodity)
    return commodity is not None and not commodity.is_currency


def cost_basis(
    db: DbSQLite, account: Account | str, *, as_of: date | None = None
) -> HoldingCostBasis | None:
    """First-in, first-out lots of one security account; ``None`` if it holds cash."""
    obj = db.get_account(account) if isinstance(account, str) else account
    if obj is None or not _is_security(db, obj):
        return None
    book = reporting_currency_handle(db)
    open_lots: list[Lot] = []
    sales: list[RealizedGain] = []
    currencies: set[str] = set()
    zero_cost = Money(0)
    uncovered_total = Money(0)
    for row in db.split_rows(obj.handle, end=as_of):
        quantity = Money(row["quantity_num"], row["quantity_den"])
        value = Money(row["value_num"], row["value_den"])
        when = date.fromisoformat(row["post_date"])
        currencies.add(row["currency"] or book)
        if quantity > 0:
            if value == 0:
                zero_cost = zero_cost + quantity
            open_lots.append(Lot(when, quantity, value, row["txn"]))
        elif quantity < 0:
            remaining = -quantity
            cost = Money(0)
            while remaining > 0 and open_lots:
                lot = open_lots[0]
                if lot.quantity <= remaining:
                    cost = cost + lot.cost
                    remaining = remaining - lot.quantity
                    open_lots.pop(0)
                else:
                    share = remaining / lot.quantity
                    taken = lot.cost * share
                    cost = cost + taken
                    open_lots[0] = Lot(
                        lot.acquired, lot.quantity - remaining, lot.cost - taken, lot.transaction
                    )
                    remaining = Money(0)
            sold = -quantity
            covered = sold - remaining
            proceeds = -value * (covered / sold) if sold else Money(0)
            uncovered_total = uncovered_total + remaining
            sales.append(RealizedGain(when, sold, proceeds, cost, row["txn"], remaining))
    problems: list[str] = []
    if zero_cost:
        problems.append(
            f"{shares_text(zero_cost)} shares arrived with no recorded cost (a transfer in "
            "or a share split); their cost basis is zero"
        )
    if uncovered_total:
        problems.append(
            f"{shares_text(uncovered_total)} shares were sold beyond the recorded purchases; "
            "their cost and gain are unknown"
        )
    currency = next(iter(currencies)) if len(currencies) == 1 else None
    if len(currencies) > 1:
        problems.append("the account's transactions use more than one currency")
    valued = account_value(db, obj, as_of=as_of)
    if open_lots and currency is not None:
        if valued.missing_quote or valued.source != "market":
            problems.append("no market quote, so there is no unrealized gain")
        elif valued.total_amount is not None and valued.total_amount.commodity != currency:
            problems.append("its quote is in another currency than its cost")
    return HoldingCostBasis(
        obj,
        currency,
        tuple(open_lots),
        tuple(sales),
        valued,
        tuple(problems),
    )


def holdings_cost_basis(db: DbSQLite, *, as_of: date | None = None) -> list[HoldingCostBasis]:
    """Every security account with any activity, ordered by full name."""
    found = [
        result
        for account in db.iter_accounts()
        if (result := cost_basis(db, account, as_of=as_of)) is not None
        and (result.lots or result.sales)
    ]
    return sorted(found, key=lambda item: db.full_name(item.account).casefold())
