"""Realized gains by year across every security holding, from the derived lots.

Each sale in ``cost_basis`` already knows its proceeds, the lot parts it took
(with their purchase dates and cost), and whether it named those lots. This report
gathers the sales of every security account, optionally for one year or one
account, and totals them by year and currency. Amounts in different currencies are
never added together; a sale whose account mixes currencies, or that sold shares
beyond the recorded purchases, is reported with the problem its account names.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

from ..db.sqlite import DbSQLite
from ..lib.money import Money
from .cost_basis import Lot, RealizedGain, holdings_cost_basis, shares_text

__all__ = [
    "RealizedGainsReport",
    "RealizedSale",
    "YearTotal",
    "currency_handles",
    "currency_labels",
    "realized_gains",
]


@dataclass(frozen=True, slots=True)
class RealizedSale:
    """One sale of one security account, with the lots it took."""

    account: str
    account_name: str
    currency: str | None
    sale: RealizedGain

    @property
    def sold(self) -> date:
        return self.sale.sold

    @property
    def lots(self) -> tuple[Lot, ...]:
        return self.sale.lots

    def as_dict(self, mnemonics: Mapping[str, str]) -> dict[str, object]:
        """The sale as JSON, its currency named by ``mnemonics`` (handle when unnamed)."""
        sale = self.sale
        return {
            "account": self.account,
            "account_name": self.account_name,
            "currency": _named(mnemonics, self.currency),
            "sold": sale.sold,
            "quantity": shares_text(sale.quantity),
            "proceeds": sale.proceeds,
            "cost": sale.cost,
            "gain": sale.gain,
            "uncovered": shares_text(sale.uncovered),
            "specific": sale.specific,
            "transaction": sale.transaction,
            "split": sale.split,
            "lots": [
                {
                    "lot": lot.transaction,
                    "acquired": lot.acquired,
                    "quantity": shares_text(lot.quantity),
                    "cost": lot.cost,
                }
                for lot in sale.lots
            ],
        }


@dataclass(frozen=True, slots=True)
class YearTotal:
    """Proceeds, cost, and gain of one year's sales in one currency."""

    year: int
    currency: str | None
    proceeds: Money
    cost: Money
    sales: int

    @property
    def gain(self) -> Money:
        return self.proceeds - self.cost


@dataclass(frozen=True, slots=True)
class RealizedGainsReport:
    sales: tuple[RealizedSale, ...]
    years: tuple[YearTotal, ...]
    #: Problems named by the accounts that sold, as "Account: problem".
    problems: tuple[str, ...]

    def as_dict(self, mnemonics: Mapping[str, str]) -> dict[str, object]:
        """The report as JSON, currencies named by ``mnemonics`` (handle when unnamed)."""
        return {
            "sales": [sale.as_dict(mnemonics) for sale in self.sales],
            "years": [
                {
                    "year": total.year,
                    "currency": _named(mnemonics, total.currency),
                    "proceeds": total.proceeds,
                    "cost": total.cost,
                    "gain": total.gain,
                    "sales": total.sales,
                }
                for total in self.years
            ],
            "problems": list(self.problems),
        }


def realized_gains(
    db: DbSQLite, *, year: int | None = None, account: str | None = None
) -> RealizedGainsReport:
    """Every sale of every security account (or one), oldest first, with yearly totals."""
    sales: list[RealizedSale] = []
    problems: list[str] = []
    for holding in holdings_cost_basis(db):
        if account is not None and holding.account.handle != account:
            continue
        name = db.full_name(holding.account)
        chosen = [sale for sale in holding.sales if year is None or sale.sold.year == year]
        sales.extend(
            RealizedSale(holding.account.handle, name, holding.currency, sale) for sale in chosen
        )
        if chosen:
            problems.extend(
                f"{name}: {problem}"
                for problem in holding.problems
                if "unrealized" not in problem and "quote" not in problem
            )
    sales.sort(key=lambda item: (item.sold, item.account_name.casefold(), item.sale.transaction))
    proceeds: dict[tuple[int, str | None], Money] = defaultdict(lambda: Money(0))
    cost: dict[tuple[int, str | None], Money] = defaultdict(lambda: Money(0))
    count: dict[tuple[int, str | None], int] = defaultdict(int)
    for item in sales:
        key = (item.sold.year, item.currency)
        proceeds[key] = proceeds[key] + item.sale.proceeds
        cost[key] = cost[key] + item.sale.cost
        count[key] += 1
    years = tuple(
        YearTotal(key[0], key[1], proceeds[key], cost[key], count[key])
        for key in sorted(count, key=lambda key: (key[0], key[1] or ""))
    )
    return RealizedGainsReport(tuple(sales), years, tuple(problems))


def currency_handles(report: RealizedGainsReport) -> set[str]:
    """Every currency the report uses."""
    found = {sale.currency for sale in report.sales} | {total.currency for total in report.years}
    return {handle for handle in found if handle}


def currency_labels(
    report: RealizedGainsReport, mnemonics: Mapping[str, str]
) -> dict[str | None, str]:
    """The mnemonic of each currency the report uses, for layouts that never query.

    A sale without one currency, or whose currency ``mnemonics`` does not name, is "mixed".
    """
    handles = {sale.currency for sale in report.sales} | {total.currency for total in report.years}
    return {handle: mnemonics.get(handle, "mixed") if handle else "mixed" for handle in handles}


def _named(mnemonics: Mapping[str, str], handle: str | None) -> str | None:
    return mnemonics.get(handle, handle) if handle else handle
