"""Read-only investment reports: holdings' cost basis, realized gains, and the tax year.

The lots, gains, and yearly totals are derived in ``engine/cost_basis``,
``engine/realized_gains``, and ``engine/tax_year``. Each view here carries the
report whole with the account names and currency mnemonics its layouts show, so an
interface lays it out without reading the book itself.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date

from ..db.sqlite import DbSQLite
from ..engine import realized_gains as realized_gains_engine
from ..engine import tax_year as tax_year_engine
from ..engine.chart_model import ChartModel
from ..engine.cost_basis import HoldingCostBasis, holdings_charts, holdings_cost_basis
from ..engine.realized_gains import RealizedGainsReport
from ..engine.tax_year import TaxYearReport

__all__ = [
    "HoldingsView",
    "RealizedGainsView",
    "TaxYearView",
    "commodity_mnemonics",
    "holdings_view",
    "realized_gains_view",
    "tax_year_view",
]


@dataclass(frozen=True, slots=True)
class HoldingsView:
    """Every security holding's lots, sales, and cost basis on ``as_of`` (today if none)."""

    as_of: date | None
    holdings: tuple[HoldingCostBasis, ...]
    #: Full names of the holdings' accounts and of the accounts their lots moved to or from.
    names: Mapping[str, str]
    charts: tuple[ChartModel, ...]

    def name(self, handle: str) -> str:
        return self.names.get(handle, handle)

    def other_name(self, handle: str | None) -> str | None:
        """The full name of the account a lot moved to or from, if any."""
        return self.names.get(handle, handle) if handle else None


@dataclass(frozen=True, slots=True)
class RealizedGainsView:
    """Sales for ``year`` (every year when ``None``) of one account or all of them."""

    year: int | None
    account: str | None
    report: RealizedGainsReport
    #: Years with a sale of ``account`` (or any account), oldest first.
    available_years: tuple[int, ...]
    #: Mnemonics of the report's currencies, by handle.
    mnemonics: Mapping[str, str]

    def labels(self) -> dict[str | None, str]:
        return realized_gains_engine.currency_labels(self.report, self.mnemonics)


@dataclass(frozen=True, slots=True)
class TaxYearView:
    """One calendar year's tax outputs."""

    report: TaxYearReport
    #: Years with a transaction, newest first.
    available_years: tuple[int, ...]
    #: Mnemonics of the report's commodities, by handle.
    mnemonics: Mapping[str, str]

    def labels(self) -> dict[str | None, str]:
        return tax_year_engine.currency_labels(self.report, self.mnemonics)


def commodity_mnemonics(db: DbSQLite, handles: Iterable[str]) -> dict[str, str]:
    """The mnemonic of each commodity the book knows among ``handles``."""
    found: dict[str, str] = {}
    for handle in handles:
        commodity = db.get_commodity(handle)
        if commodity is not None:
            found[handle] = commodity.mnemonic
    return found


def _full_names(db: DbSQLite, handles: Iterable[str]) -> dict[str, str]:
    names: dict[str, str] = {}
    for handle in handles:
        account = db.get_account(handle)
        if account is not None:
            names[handle] = db.full_name(account)
    return names


def holdings_view(db: DbSQLite, as_of: date | None = None) -> HoldingsView:
    holdings = tuple(holdings_cost_basis(db, as_of=as_of))
    handles = {item.account.handle for item in holdings}
    handles.update(
        move.other_account for item in holdings for move in item.moves if move.other_account
    )
    names = _full_names(db, handles)
    mnemonics = commodity_mnemonics(db, {item.currency for item in holdings if item.currency})
    return HoldingsView(as_of, holdings, names, holdings_charts(holdings, names, mnemonics))


def realized_gains_view(
    db: DbSQLite, year: int | None = None, account: str | None = None
) -> RealizedGainsView:
    every = realized_gains_engine.realized_gains(db, account=account)
    report = (
        every
        if year is None
        else realized_gains_engine.realized_gains(db, year=year, account=account)
    )
    years = tuple(sorted({sale.sold.year for sale in every.sales}))
    mnemonics = commodity_mnemonics(db, realized_gains_engine.currency_handles(report))
    return RealizedGainsView(year, account, report, years, mnemonics)


def tax_year_view(db: DbSQLite, year: int | None = None) -> TaxYearView:
    """``year``'s outputs; without one, the latest year with a transaction (or this year)."""
    years = tax_year_engine.tax_years(db)
    if year is None:
        year = years[0] if years else date.today().year
    report = tax_year_engine.tax_year(db, year)
    mnemonics = commodity_mnemonics(db, tax_year_engine.currency_handles(report))
    return TaxYearView(report, years, mnemonics)
