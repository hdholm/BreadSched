"""Setting up a loan, in the manner of GnuCash's mortgage assistant.

A loan is not one number, it is a *shape*: a level payment whose interest and
principal parts move against each other every month. Recording it as a fixed
transfer would show a household paying off a mortgage in a straight line, which is
wrong from the first month and badly wrong for a decade.

So the schedule is stored with **formula** splits — ``ipmt`` and ``ppmt`` over the
loan's variables — rather than fixed amounts. Each occurrence resolves against its
own period number, so the split between interest and principal is correct for that
month and the balance falls the way it really does. This is the same technique
GnuCash uses, which is also why an imported GnuCash mortgage now evaluates instead
of reading as zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ..db.sqlite import DbSQLite
from ..lib.account import Account, AccountType
from ..lib.finance import amortisation_schedule, pmt
from ..lib.money import Money
from ..lib.recurrence import PeriodType, Recurrence
from ..lib.scheduled import ScheduledSplit, ScheduledTransaction
from ..lib.transaction import PlanningFlowKind

__all__ = ["LoanTerms", "build_schedule", "create_loan", "schedule_preview"]

#: Payments per year for each supported frequency.
_PER_YEAR = {
    PeriodType.MONTH: 12,
    PeriodType.WEEK: 52,
    PeriodType.YEAR: 1,
    PeriodType.SEMI_MONTH: 24,
}


@dataclass
class LoanTerms:
    """What a lender tells you, and what the accounts are called here."""

    name: str
    principal: Money
    annual_rate: Decimal
    years: int
    start: date
    #: The liability account the debt sits in.
    liability: str
    #: Where the interest is expensed.
    interest_account: str
    #: The account the payment comes out of.
    payment_account: str
    #: An escrow or fee amount added to every payment, if any.
    escrow: Money | None = None
    escrow_account: str | None = None
    frequency: PeriodType = PeriodType.MONTH

    @property
    def per_year(self) -> int:
        return _PER_YEAR.get(self.frequency, 12)

    @property
    def periods(self) -> int:
        return self.years * self.per_year

    @property
    def period_rate(self) -> Decimal:
        """The rate per payment, which is what every formula here needs."""
        return self.annual_rate / Decimal(self.per_year)

    def payment(self) -> Money:
        """The level payment, as a positive amount of money leaving the household."""
        return Money(-pmt(self.period_rate, self.periods, self.principal.rate())).quantize(100)

    def total_interest(self) -> Money:
        total = Money(0)
        for row in amortisation_schedule(self.period_rate, self.periods, self.principal.rate()):
            total = total + Money(-row["interest"]).quantize(100)
        return total


def build_schedule(terms: LoanTerms) -> ScheduledTransaction:
    """A scheduled transaction whose splits recalculate every period.

    The variables travel with the schedule, so the formulas stay readable and a
    user can change the rate in one place rather than editing three expressions.
    ``period`` is supplied per occurrence by :meth:`instantiate`.
    """
    schedule = ScheduledTransaction(
        name=terms.name,
        description=f"{terms.name} payment",
        recurrence=Recurrence(period=terms.frequency, interval=1, start=terms.start),
        auto_create=False,
    )
    schedule.variables = {
        "principal": str(terms.principal.to_decimal(2)),
        "rate": str(terms.period_rate),
        "periods": str(terms.periods),
        "period": "1",
    }

    # In a formula these return positive amounts, as GnuCash's do, so the
    # liability and expense legs read directly and only the funding leg is
    # negated. Writing them the way GnuCash writes them means an imported
    # mortgage and one created here behave identically.
    splits = [
        ScheduledSplit(
            terms.liability,
            formula="ppmt(rate, period, periods, principal)",
            memo="Principal",
            planning_flow=PlanningFlowKind.DEBT_PRINCIPAL,
        ),
        ScheduledSplit(
            terms.interest_account,
            formula="ipmt(rate, period, periods, principal)",
            memo="Interest",
        ),
    ]
    funding = "-pmt(rate, periods, principal)"
    if terms.escrow and terms.escrow_account:
        splits.append(ScheduledSplit(terms.escrow_account, amount=terms.escrow, memo="Escrow"))
        funding = f"{funding} - {terms.escrow.to_decimal(2)}"
    splits.append(ScheduledSplit(terms.payment_account, formula=funding, memo="Payment"))

    schedule.splits = splits
    return schedule


def schedule_preview(terms: LoanTerms, rows: int = 12) -> list[dict[str, object]]:
    """The first ``rows`` payments, split into interest and principal."""
    table = amortisation_schedule(terms.period_rate, terms.periods, terms.principal.rate())
    return [
        {
            "period": int(row["period"]),
            "payment": Money(-row["payment"]).quantize(100),
            "interest": Money(-row["interest"]).quantize(100),
            "principal": Money(-row["principal"]).quantize(100),
            "balance": Money(row["balance"]).quantize(100),
        }
        for row in table[:rows]
    ]


def create_loan(
    db: DbSQLite,
    terms: LoanTerms,
    opening_balance: bool = True,
) -> ScheduledTransaction:
    """Store the schedule, and optionally record what is currently owed.

    The opening balance matters for a forecast: without it the liability account
    reads zero and the projection shows a payment leaving every month against a
    debt that does not exist.
    """
    schedule = build_schedule(terms)
    with db.transaction(f"Set up loan {terms.name}") as txn:
        db.add_scheduled(schedule, txn)

        # The schedule's ipmt leg *is* this loan's interest. Leaving a rate on the
        # account as well makes a projection charge interest twice, which turns a
        # repaying mortgage into a growing one -- slowly enough to look plausible.
        liability_account = db.get_account(terms.liability)
        if liability_account is not None and liability_account.annual_interest:
            liability_account.annual_interest = Decimal(0)
            liability_account.notes = (
                (liability_account.notes + "\n" if liability_account.notes else "")
                + f"Interest is calculated by the {terms.name!r} scheduled "
                "transaction, so no rate is applied to this account."
            ).strip()
            db.commit_account(liability_account, txn)

        if opening_balance:
            from ..lib.transaction import Transaction

            liability = db.get_account(terms.liability)
            equity = _opening_balances_account(db, txn)
            if liability is not None and equity is not None:
                # Dated the day before the first payment: you owe the money before
                # you start repaying it, and a forecast beginning on the first
                # payment date would otherwise see no debt at all.
                opening_date = date.fromordinal(terms.start.toordinal() - 1)
                posting = Transaction.simple(
                    post_date=opening_date,
                    description=f"{terms.name} opening balance",
                    debit_account=equity,
                    credit_account=terms.liability,
                    amount=terms.principal,
                )
                db.add_transaction(posting, txn)
    return schedule


def _opening_balances_account(db: DbSQLite, txn) -> str | None:
    existing = db.get_account_by_name("Equity:Opening Balances") or (
        db.get_account_by_name("Opening Balances")
    )
    if existing is not None:
        return existing.handle
    root = db.root_account()
    if root is None:
        return None
    equity = db.get_account_by_name("Equity")
    if equity is None:
        equity = Account(
            name="Equity", atype=AccountType.EQUITY, parent=root.handle, placeholder=True
        )
        db.add_account(equity, txn)
    account = Account(name="Opening Balances", atype=AccountType.EQUITY, parent=equity.handle)
    db.add_account(account, txn)
    return account.handle
