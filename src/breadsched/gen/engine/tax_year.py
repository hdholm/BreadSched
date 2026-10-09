"""One calendar tax year (US-oriented): gains by term, tax-relevant totals, income.

Everything here is derived from the ledger for the year's dates; nothing is stored
but the marks that say what is tax-relevant.

* **Realized gains by term.** Each sale's lots (from ``cost_basis``) are split into
  short-term and long-term parts. A lot is long-term when it is sold after the
  anniversary of its purchase (held more than one year: the holding period starts
  the day after the purchase, so a lot bought on 2024-02-29 is long-term from
  2025-03-01). A transferred lot keeps its purchase date. The sale's proceeds are
  shared between its short- and long-term parts by shares, rounded to the cent,
  with the long-term part taking the remainder so the two add up exactly.
* **Tax-relevant accounts.** An account is tax-relevant when BreadSched marks it so
  (``Account.tax_relevant_override``) or, without a BreadSched mark, when GnuCash
  marks it *tax related*. Its total is the year's change in the account and the
  accounts beneath it, as the account shows it (income and spending positive), per
  commodity.
* **Tax-relevant tags.** A tag is tax-relevant when listed in book metadata
  (``TAX_TAGS_KEY``). Its total is what the tagged transactions of the year spent
  (their expense splits) and received (their income splits), per currency.
* **Income by source.** The year's total of every income account with activity,
  each on its own row, per commodity.

Amounts in different currencies are never added together.
"""

from __future__ import annotations

import calendar
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from enum import Enum

from ..db.sqlite import DbSQLite
from ..lib.account import Account, AccountClass
from ..lib.money import Money
from .cost_basis import Lot, RealizedGain, holdings_cost_basis, shares_text

__all__ = [
    "TAX_TAGS_KEY",
    "GainTerm",
    "IncomeSource",
    "TaxAccountTotal",
    "TaxTagTotal",
    "TaxYearReport",
    "TermSale",
    "TermTotal",
    "currency_labels",
    "gnucash_tax_related",
    "holding_term",
    "is_tax_relevant",
    "tax_relevant_tags",
    "tax_year",
    "tax_years",
]

#: Book metadata: the tags whose transactions are tax-relevant, as the book spells them.
TAX_TAGS_KEY = "tax.tags"


class GainTerm(str, Enum):
    SHORT = "short"
    LONG = "long"

    @property
    def label(self) -> str:
        return "Short-term" if self is GainTerm.SHORT else "Long-term"


def _anniversary(acquired: date) -> date:
    year = acquired.year + 1
    day = min(acquired.day, calendar.monthrange(year, acquired.month)[1])
    return date(year, acquired.month, day)


def holding_term(acquired: date, sold: date) -> GainTerm:
    """Long-term when sold after the anniversary of the purchase (held over a year)."""
    return GainTerm.LONG if sold > _anniversary(acquired) else GainTerm.SHORT


def gnucash_tax_related(account: Account) -> bool:
    """Whether GnuCash marked the account *tax related* (its ``tax-related`` slot)."""
    return any(
        field.name == "slot:tax-related" and field.value.strip().lower() in ("1", "true")
        for field in account.source_fields
    )


def is_tax_relevant(account: Account) -> bool:
    """BreadSched's mark when there is one, otherwise GnuCash's."""
    if account.tax_relevant_override is not None:
        return account.tax_relevant_override
    return gnucash_tax_related(account)


def tax_relevant_tags(db: DbSQLite) -> tuple[str, ...]:
    raw = db.get_metadata(TAX_TAGS_KEY, [])
    if not isinstance(raw, list):
        return ()
    return tuple(str(tag) for tag in raw if isinstance(tag, str) and tag.strip())


@dataclass(frozen=True, slots=True)
class TermSale:
    """The short- or long-term part of one sale: a line of a capital-gains form."""

    account: str
    account_name: str
    currency: str | None
    sold: date
    #: The purchase date, or None when the part's lots were bought on several dates.
    acquired: date | None
    term: GainTerm
    quantity: Money
    proceeds: Money
    cost: Money
    transaction: str
    lots: tuple[Lot, ...]

    @property
    def gain(self) -> Money:
        return self.proceeds - self.cost

    def as_dict(self, currency_label: str | None) -> dict[str, object]:
        return {
            "account": self.account,
            "account_name": self.account_name,
            "currency": currency_label,
            "sold": self.sold,
            "acquired": self.acquired,
            "term": self.term.value,
            "quantity": shares_text(self.quantity),
            "proceeds": self.proceeds,
            "cost": self.cost,
            "gain": self.gain,
            "transaction": self.transaction,
        }


@dataclass(frozen=True, slots=True)
class TermTotal:
    term: GainTerm
    currency: str | None
    proceeds: Money
    cost: Money
    lines: int

    @property
    def gain(self) -> Money:
        return self.proceeds - self.cost


@dataclass(frozen=True, slots=True)
class TaxAccountTotal:
    """The year's change in one tax-relevant account and those beneath it."""

    account: str
    full_name: str
    account_class: AccountClass
    currency: str | None
    amount: Money
    transactions: int
    #: Where the mark came from: "BreadSched" or "GnuCash".
    marked_by: str


@dataclass(frozen=True, slots=True)
class TaxTagTotal:
    tag: str
    currency: str | None
    spent: Money
    received: Money
    transactions: int


@dataclass(frozen=True, slots=True)
class IncomeSource:
    account: str
    full_name: str
    currency: str | None
    amount: Money
    transactions: int


@dataclass(frozen=True, slots=True)
class TaxYearReport:
    year: int
    gains: tuple[TermSale, ...]
    gain_totals: tuple[TermTotal, ...]
    accounts: tuple[TaxAccountTotal, ...]
    tags: tuple[TaxTagTotal, ...]
    income: tuple[IncomeSource, ...]
    #: Income totals per commodity, in ``income`` order of first appearance.
    income_totals: tuple[tuple[str | None, Money], ...]
    problems: tuple[str, ...]

    def currencies(self) -> set[str | None]:
        found: set[str | None] = set()
        found.update(item.currency for item in self.gains)
        found.update(item.currency for item in self.accounts)
        found.update(item.currency for item in self.tags)
        found.update(item.currency for item in self.income)
        return found


def _cents(value: Money) -> Money:
    return value.quantize(100)


def _term_parts(
    account: str, name: str, currency: str | None, sale: RealizedGain
) -> list[TermSale]:
    groups: dict[GainTerm, list[Lot]] = {}
    for lot in sale.lots:
        groups.setdefault(holding_term(lot.acquired, sale.sold), []).append(lot)
    covered = sum((lot.quantity for lot in sale.lots), Money(0))
    parts: list[TermSale] = []
    short_lots = groups.get(GainTerm.SHORT, [])
    short_quantity = sum((lot.quantity for lot in short_lots), Money(0))
    if short_lots and short_quantity == covered:
        short_proceeds = sale.proceeds
    elif short_lots:
        short_proceeds = _cents(sale.proceeds * (short_quantity / covered))
    else:
        short_proceeds = Money(0)
    shares = {
        GainTerm.SHORT: short_proceeds,
        GainTerm.LONG: sale.proceeds - short_proceeds,
    }
    for term in (GainTerm.SHORT, GainTerm.LONG):
        lots = groups.get(term)
        if not lots:
            continue
        dates = {lot.acquired for lot in lots}
        parts.append(
            TermSale(
                account,
                name,
                currency,
                sale.sold,
                next(iter(dates)) if len(dates) == 1 else None,
                term,
                sum((lot.quantity for lot in lots), Money(0)),
                shares[term],
                sum((lot.cost for lot in lots), Money(0)),
                sale.transaction,
                tuple(lots),
            )
        )
    return parts


def _gains(db: DbSQLite, year: int) -> tuple[list[TermSale], list[str]]:
    lines: list[TermSale] = []
    problems: list[str] = []
    for holding in holdings_cost_basis(db):
        name = db.full_name(holding.account)
        sold = [sale for sale in holding.sales if sale.sold.year == year]
        for sale in sold:
            lines.extend(_term_parts(holding.account.handle, name, holding.currency, sale))
            if sale.uncovered:
                problems.append(
                    f"{name}: the sale on {sale.sold.isoformat()} sold "
                    f"{shares_text(sale.uncovered)} shares beyond the recorded purchases; "
                    "their cost, gain, and term are unknown and not included"
                )
        if sold:
            problems.extend(
                f"{name}: {problem}"
                for problem in holding.problems
                if "unrealized" not in problem
                and "quote" not in problem
                and "beyond the recorded purchases" not in problem
            )
    lines.sort(key=lambda item: (item.sold, item.account_name.casefold(), item.term.value))
    return lines, problems


def _term_totals(lines: Iterable[TermSale]) -> list[TermTotal]:
    totals: dict[tuple[GainTerm, str | None], list] = {}
    for line in lines:
        entry = totals.setdefault((line.term, line.currency), [Money(0), Money(0), 0])
        entry[0] = entry[0] + line.proceeds
        entry[1] = entry[1] + line.cost
        entry[2] += 1
    order = {GainTerm.SHORT: 0, GainTerm.LONG: 1}
    return [
        TermTotal(term, currency, proceeds, cost, count)
        for (term, currency), (proceeds, cost, count) in sorted(
            totals.items(), key=lambda item: (order[item[0][0]], str(item[0][1]))
        )
    ]


def _descendants(accounts: dict[str, Account], handle: str) -> set[str]:
    children: dict[str | None, list[str]] = defaultdict(list)
    for account in accounts.values():
        children[account.parent].append(account.handle)
    found = {handle}
    pending = [handle]
    while pending:
        for child in children.get(pending.pop(), ()):
            if child not in found:
                found.add(child)
                pending.append(child)
    return found


def tax_year(db: DbSQLite, year: int) -> TaxYearReport:
    """Everything one calendar year's taxes draw on, from the ledger."""
    accounts = {account.handle: account for account in db.iter_accounts()}
    lines, problems = _gains(db, year)

    marked = [
        account for account in accounts.values() if not account.is_root and is_tax_relevant(account)
    ]
    covered = {account.handle: _descendants(accounts, account.handle) for account in marked}

    # Per account: (commodity -> total quantity, transaction handles).
    own: dict[str, dict[str | None, Money]] = defaultdict(lambda: defaultdict(lambda: Money(0)))
    own_transactions: dict[str, set[str]] = defaultdict(set)
    wanted_tags = {tag.casefold(): tag for tag in tax_relevant_tags(db)}
    tag_spent: dict[tuple[str, str | None], Money] = defaultdict(lambda: Money(0))
    tag_received: dict[tuple[str, str | None], Money] = defaultdict(lambda: Money(0))
    tag_transactions: dict[tuple[str, str | None], set[str]] = defaultdict(set)

    for transaction in db.iter_transactions(start=date(year, 1, 1), end=date(year, 12, 31)):
        for split in transaction.splits:
            account = accounts.get(split.account)
            if account is None:
                continue
            own[account.handle][account.commodity] += split.quantity
            own_transactions[account.handle].add(transaction.handle)
        tags = {
            wanted_tags[tag.casefold()] for tag in transaction.tags if tag.casefold() in wanted_tags
        }
        if not tags:
            continue
        for split in transaction.splits:
            account = accounts.get(split.account)
            if account is None:
                continue
            for tag in tags:
                key = (tag, transaction.currency)
                if account.account_class is AccountClass.EXPENSE:
                    tag_spent[key] += split.value
                    tag_transactions[key].add(transaction.handle)
                elif account.account_class is AccountClass.INCOME:
                    tag_received[key] -= split.value
                    tag_transactions[key].add(transaction.handle)

    account_totals: list[TaxAccountTotal] = []
    for account in sorted(marked, key=lambda item: db.full_name(item).casefold()):
        sums: dict[str | None, Money] = defaultdict(lambda: Money(0))
        transactions: set[str] = set()
        for handle in covered[account.handle]:
            for commodity, amount in own.get(handle, {}).items():
                sums[commodity] += amount
            transactions |= own_transactions.get(handle, set())
        marked_by = "BreadSched" if account.tax_relevant_override is not None else "GnuCash"
        if not sums:
            sums[account.commodity] = Money(0)
        for commodity, amount in sorted(sums.items(), key=lambda item: str(item[0])):
            account_totals.append(
                TaxAccountTotal(
                    account.handle,
                    db.full_name(account),
                    account.account_class,
                    commodity,
                    amount * account.sign(),
                    len(transactions),
                    marked_by,
                )
            )

    tag_totals = [
        TaxTagTotal(
            tag,
            currency,
            tag_spent.get((tag, currency), Money(0)),
            tag_received.get((tag, currency), Money(0)),
            len(tag_transactions[(tag, currency)]),
        )
        for tag, currency in sorted(
            tag_transactions, key=lambda key: (key[0].casefold(), str(key[1]))
        )
    ]
    for tag in sorted(wanted_tags.values(), key=str.casefold):
        if not any(item.tag == tag for item in tag_totals):
            tag_totals.append(TaxTagTotal(tag, None, Money(0), Money(0), 0))
    tag_totals.sort(key=lambda item: (item.tag.casefold(), str(item.currency)))

    income: list[IncomeSource] = []
    income_totals: dict[str | None, Money] = {}
    for account in sorted(accounts.values(), key=lambda item: db.full_name(item).casefold()):
        if account.account_class is not AccountClass.INCOME or account.handle not in own:
            continue
        for commodity, amount in sorted(own[account.handle].items(), key=lambda i: str(i[0])):
            if not amount:
                continue
            shown = amount * account.sign()
            income.append(
                IncomeSource(
                    account.handle,
                    db.full_name(account),
                    commodity,
                    shown,
                    len(own_transactions[account.handle]),
                )
            )
            income_totals[commodity] = income_totals.get(commodity, Money(0)) + shown

    return TaxYearReport(
        year,
        tuple(lines),
        tuple(_term_totals(lines)),
        tuple(account_totals),
        tuple(tag_totals),
        tuple(income),
        tuple(income_totals.items()),
        tuple(problems),
    )


def tax_years(db: DbSQLite) -> tuple[int, ...]:
    """Every year with a transaction, newest first."""
    years: set[int] = set()
    for transaction in db.iter_transactions():
        years.add(transaction.post_date.year)
    return tuple(sorted(years, reverse=True))


def currency_labels(db: DbSQLite, report: TaxYearReport) -> dict[str | None, str]:
    """The mnemonic of each commodity the report uses ("" when none is set)."""
    labels: dict[str | None, str] = {}
    for handle in report.currencies() | {currency for currency, _total in report.income_totals}:
        commodity = db.get_commodity(handle) if handle else None
        labels[handle] = commodity.mnemonic if commodity is not None else (handle or "")
    return labels
