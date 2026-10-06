"""The dashboard: what is owned, what is owed, and what has to stay liquid.

Modelled on the household spreadsheet this replaces, but computed from the book
rather than typed in, so the figures cannot drift from the ledger that produced
them.

Four ideas carry the whole view.

**Groups.** Accounts are gathered into colon-delimited paths. Generated parent
headings total their child groups, while selecting a chart parent includes its
account subtree exactly once. A group of a property plus its active loans reports
equity and loan-to-value, which is the pair of numbers that actually answers "how
is the house doing".

**Normalised bills.** A bill's amount means nothing without its cycle: 619 a
quarter and 200 a month are not comparable until both are monthly. Every scheduled
outflow is expressed per month and per year from its own recurrence, so a
quarterly HOA fee and a fortnightly daycare bill can be added together and sorted
against each other.

**Hold.** A bill due in three weeks is not free money today. Its reserve accrues
on the household's actual scheduled income dates, in proportion to the income in
that bill cycle. An overdue bill remains a current liquidity obligation while new
income begins reserving for its next occurrence.

**Liquidity and the emergency fund.** Two different questions, deliberately kept
apart. Liquidity asks whether the bills falling due before the next pay arrives can
be paid from what is in the bank. The emergency fund asks how long the household
could continue with no income at all. A household can be comfortable on one and
short on the other, and averaging them hides exactly that.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, TypedDict

from ..db.sqlite import DbSQLite
from ..lib.account import Account, AccountClass, AccountType
from ..lib.money import Money
from . import fsa, fsa_claim_report, ledger, receivables, savings_goals, schedule, valuation
from .completeness import Completeness, Policy, combine
from .currency import reporting_currency_handle, reporting_fraction
from .dashboard_bills import (
    DAYS_PER_MONTH,
    BillRow,
    MissedGroup,
    group_missed,
    pending_cash_flow,
)

__all__ = [
    "DashboardConfig",
    "GroupConfig",
    "Dashboard",
    "GroupResult",
    "GroupAccountResult",
    "BillRow",
    "MissedGroup",
    "group_missed",
    "build",
    "DAYS_PER_MONTH",
]


# --------------------------------------------------------------- configuration


@dataclass
class GroupConfig:
    """Accounts assigned to a colon-delimited dashboard group path."""

    name: str
    accounts: list[str] = field(default_factory=list)
    #: ``asset``, ``liability``, ``property``, ``retirement`` or ``liquid``.
    #: ``property`` pairs a value with its loans and reports equity and LTV.
    kind: str = "asset"

    def serialize(self) -> dict[str, Any]:
        return {"name": self.name, "accounts": list(self.accounts), "kind": self.kind}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GroupConfig:
        return cls(
            name=data["name"],
            accounts=list(data.get("accounts", [])),
            kind=data.get("kind", "asset"),
        )


@dataclass
class DashboardConfig:
    """What the dashboard shows, and the two horizons it judges against.

    Stored in the book rather than in user settings, because it names accounts:
    carrying it between books would silently point at the wrong ones.
    """

    groups: list[GroupConfig] = field(default_factory=list)
    #: Days of bills that must be covered by cash on hand. Defaults to a month,
    #: but a fortnightly-paid household usually wants its own pay cycle here.
    liquidity_days: int = 30
    #: Months of outgoings the emergency fund should cover.
    emergency_months: int = 6
    #: Cash kept back from the "available" figure — the float you never spend.
    reserve: Money = field(default_factory=lambda: Money(0))
    #: Bills below this are rolled into an "other" line rather than listed.
    minimum_bill: Money = field(default_factory=lambda: Money(0))

    def serialize(self) -> dict[str, Any]:
        return {
            "groups": [group.serialize() for group in self.groups],
            "liquidity_days": self.liquidity_days,
            "emergency_months": self.emergency_months,
            "reserve": [self.reserve.numerator, self.reserve.denominator],
            "minimum_bill": [self.minimum_bill.numerator, self.minimum_bill.denominator],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DashboardConfig:
        reserve = data.get("reserve")
        minimum = data.get("minimum_bill")
        return cls(
            groups=[GroupConfig.from_dict(g) for g in data.get("groups", [])],
            liquidity_days=data.get("liquidity_days", 30),
            emergency_months=data.get("emergency_months", 6),
            reserve=Money(*reserve) if reserve else Money(0),
            minimum_bill=Money(*minimum) if minimum else Money(0),
        )

    # ------------------------------------------------------------- persistence

    KEY = "dashboard"

    @classmethod
    def load(cls, db: DbSQLite) -> DashboardConfig:
        stored = db.get_metadata(cls.KEY)
        if not stored:
            return default_config(db)
        return cls.from_dict(stored)

    def save(self, db: DbSQLite) -> None:
        db.set_metadata(self.KEY, self.serialize())


def default_config(db: DbSQLite) -> DashboardConfig:
    """Return an empty dashboard until accounts are explicitly assigned.

    ``db`` remains in the signature for API compatibility. Account-level group
    assignments are merged by :func:`resolve_groups`; nothing is inferred from
    account type, balance class, or linked-loan relationships.
    """
    del db
    return DashboardConfig()


# -------------------------------------------------------------------- results


@dataclass
class GroupAccountResult:
    """One directly selected account's contribution to a dashboard group."""

    name: str
    total: Money | None
    source: str = "ledger"
    #: The selected account's own valuation note; never its descendants' (#150).
    note: str = ""
    missing_quotes: tuple[str, ...] = ()
    #: Descendants included by inference, one line each with any quote evidence.
    members: tuple[str, ...] = ()


@dataclass
class GroupResult:
    """One group or generated path heading and its aggregate totals."""

    name: str
    kind: str
    total: Money
    path: str = ""
    depth: int = 0
    heading: bool = False
    accounts: list[GroupAccountResult] = field(default_factory=list)
    #: The directly selected accounts' own valuation notes (#150).
    note: str = ""
    #: Every account the row covers, one line each, for a tooltip or detail list.
    members: tuple[str, ...] = ()
    #: Set for property groups: the value, what is owed, and the ratio.
    value: Money | None = None
    debt: Money | None = None
    #: Latest bounded repayment date among active loans in this group.
    loan_end: date | None = None
    #: Aggregate amount from liquid groups at or beneath this node.
    liquid: Money = field(default_factory=lambda: Money(0))
    _assets: Money = field(default_factory=lambda: Money(0), repr=False)
    _debts: Money = field(default_factory=lambda: Money(0), repr=False)
    missing_quotes: tuple[str, ...] = ()
    liquid_missing_quotes: tuple[str, ...] = ()
    #: Withheld when ``missing_quotes`` is set, with each excluded balance (#236).
    completeness: Completeness = field(default_factory=Completeness)

    @property
    def report_total(self) -> Money | None:
        return None if self.missing_quotes else self.total

    @property
    def report_value(self) -> Money | None:
        return None if self.missing_quotes else self.value

    @property
    def report_debt(self) -> Money | None:
        return None if self.missing_quotes else self.debt

    @property
    def report_equity(self) -> Money | None:
        return None if self.missing_quotes else self.equity

    @property
    def report_loan_to_value(self) -> Decimal | None:
        return None if self.missing_quotes else self.loan_to_value

    @property
    def equity(self) -> Money | None:
        if self.value is None or self.debt is None:
            return None
        return self.value - self.debt

    @property
    def loan_to_value(self) -> Decimal | None:
        """Debt as a fraction of value, or None when there is nothing to divide."""
        if self.value is None or self.debt is None or not self.value:
            return None
        return (self.debt.rate() / self.value.rate()).quantize(Decimal("0.0001"))


class DashboardSummary(TypedDict):
    as_of: date
    net_worth: Money
    assets: Money
    debts: Money
    liquid: Money
    required_liquid: Money
    available: Money
    emergency_fund: Money
    emergency_shortfall: Money
    months_covered: Decimal
    monthly_outgoings: Money
    monthly_outgoings_with_estimates: Money
    emergency_monthly_outgoings: Money
    emergency_monthly_outgoings_with_estimates: Money
    annual_outgoings: Money
    annual_outgoings_with_estimates: Money
    income_per_month: Money
    income_per_month_with_estimates: Money
    next_income: date | None
    bills: int
    receivables_owed: Money
    receivables_attention: Money
    goals_set_aside: Money
    goals_held: Money
    fsa_claims_attention: int


@dataclass
class Dashboard:
    """Everything the dashboard shows, computed once."""

    as_of: date
    config: DashboardConfig
    fraction: int = 100
    groups: list[GroupResult] = field(default_factory=list)
    pending: list[BillRow] = field(default_factory=list)
    estimates: list[BillRow] = field(default_factory=list)
    income_per_month: Money = field(default_factory=lambda: Money(0))
    income_per_month_with_estimates: Money = field(default_factory=lambda: Money(0))
    next_income: date | None = None
    _income_events: list[tuple[date, Money]] = field(default_factory=list, repr=False)
    liquid: Money = field(default_factory=lambda: Money(0))
    liquid_missing_quotes: tuple[str, ...] = ()
    #: Whole-book asset and liability totals. Groups are presentation only and
    #: never change these (issue #149).
    book_assets: Money = field(default_factory=lambda: Money(0))
    book_debts: Money = field(default_factory=lambda: Money(0))
    book_missing_quotes: tuple[str, ...] = ()
    #: Net worth, assets and debts: withheld with the excluded balances (#236).
    completeness: Completeness = field(default_factory=Completeness)
    #: Liquid cash and what depends on it.
    liquid_completeness: Completeness = field(default_factory=Completeness)
    coverage_notes: tuple[str, ...] = ()
    #: Reimbursements still owed in the reporting currency (issue #170). Held in
    #: Receivable accounts: part of net worth, never of liquidity.
    receivables_owed: Money = field(default_factory=lambda: Money(0))
    #: The part of ``receivables_owed`` that is disputed or past its expected date.
    receivables_attention: Money = field(default_factory=lambda: Money(0))
    #: Each savings goal's earmark on the as-of date.
    goals: list[savings_goals.GoalProgress] = field(default_factory=list)
    #: FSA claims with something left to do (see ``fsa_claim_report``).
    claim_alerts: tuple[fsa_claim_report.ClaimLine, ...] = ()

    @property
    def missing_quotes(self) -> tuple[str, ...]:
        return self.book_missing_quotes

    def report_summary(self) -> dict[str, object]:
        """Suppress monetary conclusions that depend on unavailable quotes."""
        report: dict[str, object] = dict(self.summary())
        if self.missing_quotes:
            for field in ("assets", "debts", "net_worth"):
                report[field] = None
        if self.liquid_missing_quotes:
            for field in ("liquid", "available", "emergency_shortfall", "months_covered"):
                report[field] = None
        if not self._normalised_bills():
            for field in ("emergency_fund", "emergency_shortfall", "months_covered"):
                report[field] = None
        return report

    def unavailable_reason(self, field: str) -> str:
        """Explain an absent report value without conflating setup with FX gaps."""
        if (
            field in {"emergency_fund", "emergency_shortfall", "months_covered"}
            and not self._normalised_bills()
        ):
            return "No committed outgoings"
        if field == "next_income" and not self._income_events:
            return "No scheduled income"
        return "Missing reporting-currency quote"

    # -------------------------------------------------------------- aggregates

    def group(self, name: str) -> GroupResult | None:
        return next((g for g in self.groups if g.path == name), None) or next(
            (g for g in self.groups if g.name == name), None
        )

    def total_of_kind(self, kind: str) -> Money:
        total = Money(0)
        for group in self.groups:
            if group.depth == 0 and group.kind == kind:
                total = total + (group.equity or group.total)
        return total

    @property
    def assets(self) -> Money:
        """Every asset account in the book, whatever the Dashboard groups say."""
        return self.book_assets

    @property
    def debts(self) -> Money:
        """Every liability account in the book, whatever the Dashboard groups say."""
        return self.book_debts

    @property
    def net_worth(self) -> Money:
        return self.assets - self.debts

    # --------------------------------------------------------------- the bills

    @property
    def bills(self) -> list[BillRow]:
        return [row for row in self.pending if not row.income]

    @property
    def incomes(self) -> list[BillRow]:
        return [row for row in self.pending if row.income]

    @property
    def display_bills(self) -> list[BillRow | MissedGroup]:
        """Bills for presentation, with each schedule's missed dates grouped."""
        return group_missed(self.bills, self.as_of)

    @property
    def display_incomes(self) -> list[BillRow | MissedGroup]:
        return group_missed(self.incomes, self.as_of)

    @property
    def monthly_outgoings(self) -> Money:
        total = Money(0)
        for bill in self._normalised_bills():
            total = total + bill.monthly
        return total

    @property
    def monthly_outgoings_with_estimates(self) -> Money:
        return sum((bill.monthly for bill in self._normalised_bills(True)), Money(0))

    @property
    def annual_outgoings(self) -> Money:
        total = Money(0)
        for bill in self._normalised_bills():
            total = total + bill.annual
        return total

    @property
    def annual_outgoings_with_estimates(self) -> Money:
        return sum((bill.annual for bill in self._normalised_bills(True)), Money(0))

    @property
    def emergency_monthly_outgoings(self) -> Money:
        """Recurring costs explicitly retained when household income stops."""
        return sum((bill.emergency_monthly for bill in self._normalised_bills()), Money(0))

    @property
    def emergency_monthly_outgoings_with_estimates(self) -> Money:
        return sum((bill.emergency_monthly for bill in self._normalised_bills(True)), Money(0))

    def _normalised_bills(self, include_estimates: bool = False) -> list[BillRow]:
        """One representative row per recurring obligation.

        Every missed occurrence remains a pending liquidity obligation, but three
        overdue monthly payments do not turn one monthly bill into three separate
        recurring expenses for emergency-fund sizing.
        """
        recurring: dict[tuple[str, str], BillRow] = {}
        standalone: list[BillRow] = []
        candidates = self.bills
        if include_estimates:
            candidates = [*candidates, *(row for row in self.estimates if not row.income)]
        for bill in candidates:
            if bill.schedule is not None:
                key = ("schedule", bill.schedule.handle)
            elif bill.account is not None:
                key = ("account", bill.account)
            else:
                standalone.append(bill)
                continue
            current = recurring.get(key)
            if current is None or bill.next_due > current.next_due:
                recurring[key] = bill
        return [*recurring.values(), *standalone]

    @property
    def total_hold(self) -> Money:
        total = Money(0)
        for bill in self.bills:
            total = total + bill.held
        return total

    def due_within(self, days: int) -> list[BillRow]:
        return [bill for bill in self.bills if bill.due_within(self.as_of, days)]

    # ------------------------------------------------------------- the verdict

    @property
    def required_liquid(self) -> Money:
        """Near-term bills plus protected reserves, less dated future income.

        A normal bill inside the liquidity window already includes its reserve,
        so that reserve is not counted twice. A reserve attached to an overdue
        bill is for the *next* occurrence and remains additional to the unpaid
        obligation.
        """
        horizon = self.as_of + timedelta(days=self.config.liquidity_days)
        overdue = sum((bill.amount for bill in self.bills if bill.next_due < self.as_of), Money(0))
        upcoming = sum(
            (bill.amount for bill in self.bills if self.as_of <= bill.next_due <= horizon),
            Money(0),
        )
        funded_upcoming = upcoming - self.income_within(self.config.liquidity_days)
        if funded_upcoming < 0:
            funded_upcoming = Money(0)
        reserves = sum(
            (
                bill.held
                for bill in self.bills
                if bill.next_due > horizon or (bill.next_due < self.as_of and not bill.generated)
            ),
            Money(0),
        )
        # Future income can fund future bills. It cannot retroactively erase an
        # obligation whose due date has already passed.
        return overdue + funded_upcoming + reserves

    def income_within(self, days: int) -> Money:
        """Income on actual scheduled dates from today through ``days`` ahead."""
        through = self.as_of + timedelta(days=days)
        return sum(
            (amount for when, amount in self._income_events if self.as_of <= when <= through),
            Money(0),
        )

    @property
    def emergency_fund(self) -> Money:
        """What the household would need to run with no income at all."""
        return (self.emergency_monthly_outgoings * self.config.emergency_months).quantize(
            self.fraction
        )

    @property
    def goals_set_aside(self) -> Money:
        """Everything set aside for open savings goals, wherever it is held."""
        return sum((item.set_aside for item in self.goals), Money(0))

    @property
    def goals_held(self) -> Money:
        """The part of goal earmarks taken out of spendable cash."""
        return savings_goals.spendable_hold(self.goals)

    @property
    def available(self) -> Money:
        """Cash that is genuinely free: liquid, less what is spoken for.

        Savings-goal earmarks are spoken for exactly like bill reserves.
        """
        return self.liquid - self.required_liquid - self.goals_held - self.config.reserve

    @property
    def emergency_shortfall(self) -> Money:
        """How far the liquid balance falls short of the emergency fund."""
        return self.emergency_fund - self.liquid

    @property
    def months_covered(self) -> Decimal:
        """How long the liquid balance would last with no income."""
        monthly = self.emergency_monthly_outgoings
        if not monthly:
            return Decimal(0)
        return (self.liquid.rate() / monthly.rate()).quantize(Decimal("0.01"))

    def summary(self) -> DashboardSummary:
        return {
            "as_of": self.as_of,
            "net_worth": self.net_worth,
            "assets": self.assets,
            "debts": self.debts,
            "liquid": self.liquid,
            "required_liquid": self.required_liquid,
            "available": self.available,
            "emergency_fund": self.emergency_fund,
            "emergency_shortfall": self.emergency_shortfall,
            "months_covered": self.months_covered,
            "monthly_outgoings": self.monthly_outgoings,
            "monthly_outgoings_with_estimates": self.monthly_outgoings_with_estimates,
            "emergency_monthly_outgoings": self.emergency_monthly_outgoings,
            "emergency_monthly_outgoings_with_estimates": (
                self.emergency_monthly_outgoings_with_estimates
            ),
            "annual_outgoings": self.annual_outgoings,
            "annual_outgoings_with_estimates": self.annual_outgoings_with_estimates,
            "income_per_month": self.income_per_month,
            "income_per_month_with_estimates": self.income_per_month_with_estimates,
            "next_income": self.next_income,
            "bills": len(self.bills),
            "receivables_owed": self.receivables_owed,
            "receivables_attention": self.receivables_attention,
            "goals_set_aside": self.goals_set_aside,
            "goals_held": self.goals_held,
            "fsa_claims_attention": len(self.claim_alerts),
        }


# --------------------------------------------------------------------- builder


def build(
    db: DbSQLite,
    config: DashboardConfig | None = None,
    as_of: date | None = None,
    horizon_days: int = 400,
) -> Dashboard:
    """Compute the dashboard from the book."""
    today = as_of or date.today()
    config = config or DashboardConfig.load(db)
    fraction = reporting_fraction(db)
    board = Dashboard(as_of=today, config=config, fraction=fraction)
    paid_off = _paid_off_loans(db, today)
    resolved = resolve_groups(db, config)
    board.groups = _hierarchical_results(db, resolved, today, paid_off)

    # Totals always cover the whole book; groups only arrange rows (issue #149).
    accounts = [account for account in db.iter_accounts() if not account.is_root]
    owned = valuation.aggregate_value(
        db,
        accounts=[a for a in accounts if a.account_class is AccountClass.ASSET],
        as_of=today,
    )
    owed = valuation.aggregate_value(
        db,
        accounts=[a for a in accounts if a.account_class is AccountClass.LIABILITY],
        as_of=today,
    )
    board.book_assets = owned.amount.value if owned.amount is not None else Money(0)
    board.book_debts = owed.amount.value if owed.amount is not None else Money(0)
    board.book_missing_quotes = tuple(
        dict.fromkeys(
            (
                *owned.missing_quotes,
                *owned.incompatible_accounts,
                *owed.missing_quotes,
                *owed.incompatible_accounts,
            )
        )
    )
    cash = valuation.aggregate_value(
        db,
        accounts=[a for a in accounts if a.atype.is_cash_like and not a.placeholder],
        as_of=today,
    )
    board.liquid = cash.amount.value if cash.amount is not None else Money(0)
    board.liquid_missing_quotes = cash.missing_quotes
    board.completeness = combine(owned.completeness, owed.completeness)
    board.liquid_completeness = cash.completeness
    pending, estimates, income_per_month, income_with_estimates, next_income, income_events = (
        pending_cash_flow(db, today, horizon_days, paid_off, fraction)
    )
    board.pending = pending
    board.estimates = estimates
    board.income_per_month = income_per_month
    board.income_per_month_with_estimates = income_with_estimates
    board.next_income = next_income
    board._income_events = income_events
    _receivables(db, board, today)
    try:
        board.claim_alerts = fsa_claim_report.claim_report(db, as_of=today).needing_attention
    except ValueError as exc:  # a claim naming a removed account or year
        board.coverage_notes = (*board.coverage_notes, f"FSA claims could not be checked: {exc}")
    board.goals = [
        item
        for item in savings_goals.goals_progress(db, today)
        if item.status not in {"closed", "not started"} or item.set_aside > 0
    ]
    unpaid_cards = schedule.unconfigured_card_balances(db, today)
    if unpaid_cards:
        count = len(unpaid_cards)
        subject, pronoun = (
            ("1 credit card owes a balance", "it")
            if count == 1
            else (f"{count} credit cards owe balances", "them")
        )
        board.coverage_notes = (
            *board.coverage_notes,
            f"Card payments not set up: {subject} with no payment day or payment "
            f"schedule, so Needed within 30 days excludes {pronoun}. Set payment "
            "behavior on the card account.",
        )
    return board


def _receivables(db: DbSQLite, board: Dashboard, today: date) -> None:
    """What payers still owe back, and how much of it needs attention.

    Only reporting-currency receivables are summed; others are counted in a
    coverage note rather than converted.
    """
    book = reporting_currency_handle(db)
    foreign = 0
    for receivable in receivables.iter_receivables(db):
        try:
            summary = receivables.receivable_summary(db, receivable, as_of=today)
            currency = receivables.posting_currency(db, receivable)
        except receivables.ReceivableError:
            continue
        if summary.remaining <= 0 or summary.status in {
            receivables.ReceivableStatus.SETTLED,
            receivables.ReceivableStatus.WRITTEN_OFF,
        }:
            continue
        if currency is not None and currency != book:
            foreign += 1
            continue
        board.receivables_owed = board.receivables_owed + summary.remaining
        overdue = (
            receivable.expected_cash_date is not None and receivable.expected_cash_date < today
        )
        if summary.status is receivables.ReceivableStatus.DISPUTED or overdue:
            board.receivables_attention = board.receivables_attention + summary.remaining
    if foreign:
        board.coverage_notes = (
            *board.coverage_notes,
            f"Reimbursements due excludes {foreign} receivable(s) in another currency.",
        )


def linked_pairs(db: DbSQLite) -> list[tuple[Account, Account]]:
    """Every loan that names the asset it was borrowed against."""
    pairs: list[tuple[Account, Account]] = []
    for loan in db.iter_accounts():
        if loan.account_class is not AccountClass.LIABILITY or not loan.linked_asset:
            continue
        asset = db.get_account(loan.linked_asset)
        if asset is not None:
            pairs.append((loan, asset))
    return pairs


def resolve_groups(db: DbSQLite, config: DashboardConfig) -> list[GroupConfig]:
    """Resolve only explicitly configured, visible account memberships.

    Dashboard configuration wins over the account's own group field. An account
    claimed by one explicit source is omitted from later sources. Stored
    asset/loan links complete an already requested property group but never create
    one. A selected chart parent wins over selected descendants so no ledger value
    is counted twice. Hidden accounts are never direct dashboard members.
    """
    resolved: list[GroupConfig] = []
    claimed: set[str] = set()

    for group in config.groups:
        remaining = []
        for handle in group.accounts:
            account = db.get_account(handle)
            if account is not None and not account.hidden and handle not in claimed:
                remaining.append(handle)
        if not remaining:
            continue
        resolved.append(GroupConfig(name=group.name, accounts=remaining, kind=group.kind))
        claimed.update(remaining)

    by_name = {group.name: group for group in resolved}
    for account in db.iter_accounts():
        if not account.group or account.handle in claimed or account.is_root or account.hidden:
            continue
        existing_group = by_name.get(account.group)
        if existing_group is None:
            existing_group = GroupConfig(name=account.group, accounts=[], kind=_kind_for(account))
            by_name[account.group] = existing_group
            resolved.append(existing_group)
        existing_group.accounts.append(account.handle)
        claimed.add(account.handle)

    _enrich_linked_properties(db, resolved, claimed)
    return _deduplicate_group_accounts(db, resolved)


def _enrich_linked_properties(db: DbSQLite, groups: list[GroupConfig], claimed: set[str]) -> None:
    """Complete an explicit property group from its stored loan/asset links.

    A relationship is supporting configuration, not an implicit Dashboard group:
    at least one side must already be assigned. Explicit membership elsewhere wins,
    so enrichment never steals an account from another configured group.
    """
    pairs = linked_pairs(db)
    by_asset: dict[str, tuple[Account, list[Account]]] = {}
    for loan, asset in pairs:
        if asset.handle not in by_asset:
            by_asset[asset.handle] = (asset, [])
        by_asset[asset.handle][1].append(loan)

    for group in groups:
        direct = set(group.accounts)
        related_assets = {
            asset.handle for loan, asset in pairs if asset.handle in direct or loan.handle in direct
        }
        if not related_assets:
            continue
        for asset_handle in related_assets:
            asset, loans = by_asset[asset_handle]
            for account in (asset, *loans):
                if account.hidden or account.handle in claimed:
                    continue
                group.accounts.append(account.handle)
                claimed.add(account.handle)
        if any(
            asset.handle in group.accounts and any(loan.handle in group.accounts for loan in loans)
            for asset, loans in by_asset.values()
        ):
            group.kind = "property"


def _deduplicate_group_accounts(db: DbSQLite, groups: list[GroupConfig]) -> list[GroupConfig]:
    """Keep each selected account subtree once, with a selected parent winning.

    Selecting a parent already selects its descendants through recursive balance
    calculation. This normalization applies across groups as well as within one
    group so an explicitly repeated child cannot inflate headings or net worth.
    """
    selected = {handle for group in groups for handle in group.accounts}
    owner: dict[str, int] = {}
    for index, group in enumerate(groups):
        for handle in group.accounts:
            if handle in owner:
                continue
            account = db.get_account(handle)
            parent = account.parent if account is not None else None
            redundant = False
            visited: set[str] = set()
            while parent and parent not in visited:
                if parent in selected:
                    redundant = True
                    break
                visited.add(parent)
                ancestor = db.get_account(parent)
                parent = ancestor.parent if ancestor is not None else None
            if not redundant:
                owner[handle] = index

    normalized: list[GroupConfig] = []
    for index, group in enumerate(groups):
        accounts: list[str] = []
        seen: set[str] = set()
        for handle in group.accounts:
            if owner.get(handle) == index and handle not in seen:
                accounts.append(handle)
                seen.add(handle)
        if accounts:
            normalized.append(GroupConfig(group.name, accounts, group.kind))
    return normalized


def _sides(db: DbSQLite, group: GroupConfig) -> list:
    """The accounts of a group, skipping any that have gone."""
    return [
        account
        for account in (db.get_account(handle) for handle in group.accounts)
        if account is not None and not account.hidden
    ]


def _pairs_a_loan(db: DbSQLite, group: GroupConfig) -> bool:
    """True when a group holds both a liability and something it could secure."""
    accounts = _sides(db, group)
    has_debt = any(a.account_class is AccountClass.LIABILITY for a in accounts)
    has_value = any(a.account_class is AccountClass.ASSET for a in accounts)
    return has_debt and has_value


def _kind_for(account: Account) -> str:
    if account.atype is AccountType.RETIREMENT:
        return "retirement"
    if account.atype in {AccountType.FSA, AccountType.INVESTMENT, AccountType.ESCROW}:
        return "asset"
    if account.account_class is AccountClass.LIABILITY:
        return "liability"
    if account.is_spendable_cash:
        return "liquid"
    if account.atype.is_investment:
        return "retirement"
    return "asset"


@dataclass
class _GroupNode:
    name: str
    path: str
    groups: list[GroupConfig] = field(default_factory=list)
    children: dict[str, _GroupNode] = field(default_factory=dict)


def _group_path(name: str) -> list[str]:
    parts = [part.strip() for part in name.split(":") if part.strip()]
    return parts or [name.strip() or "Unnamed"]


def _hierarchical_results(
    db: DbSQLite,
    groups: list[GroupConfig],
    today: date,
    paid_off: set[str],
) -> list[GroupResult]:
    """Build generated headings and flattened pre-order rows from group paths."""
    roots: dict[str, _GroupNode] = {}
    for group in groups:
        parts = _group_path(group.name)
        siblings = roots
        path_parts: list[str] = []
        node: _GroupNode | None = None
        for part in parts:
            path_parts.append(part)
            path = ":".join(path_parts)
            node = siblings.setdefault(part, _GroupNode(part, path))
            siblings = node.children
        assert node is not None
        node.groups.append(group)

    flattened: list[GroupResult] = []
    for node in roots.values():
        built = _build_group_node(db, node, today, paid_off, depth=0)
        if built:
            flattened.extend(built)
    return flattened


def _build_group_node(
    db: DbSQLite,
    node: _GroupNode,
    today: date,
    paid_off: set[str],
    *,
    depth: int,
) -> list[GroupResult]:
    child_rows: list[GroupResult] = []
    children: list[GroupResult] = []
    for child in node.children.values():
        rows = _build_group_node(db, child, today, paid_off, depth=depth + 1)
        if rows:
            children.append(rows[0])
            child_rows.extend(rows)

    direct_kind = node.groups[0].kind if node.groups else None
    direct_accounts = [handle for group in node.groups for handle in group.accounts]
    lines, assets, debts, liquid, notes, missing, liquid_missing = _direct_group_totals(
        db, direct_accounts, direct_kind, today, paid_off
    )
    loan_end = _loan_end_date(db, direct_accounts, paid_off)
    member_lines: list[str] = []
    for line in lines:
        member_lines.append(f"{line.name}: {line.note}" if line.note else line.name)
        member_lines.extend(f"  {member}" for member in line.members)
    for child_result in children:
        assets = assets + child_result._assets
        debts = debts + child_result._debts
        liquid = liquid + child_result.liquid
        member_lines.extend(child_result.members)
        missing.extend(child_result.missing_quotes)
        liquid_missing.extend(child_result.liquid_missing_quotes)
        if child_result.loan_end is not None and (
            loan_end is None or child_result.loan_end > loan_end
        ):
            loan_end = child_result.loan_end

    if not lines and not children:
        return []
    kinds = [group.kind for group in node.groups] + [child.kind for child in children]
    kind = _combined_group_kind(kinds)
    if kind == "property" and not debts:
        kind = "asset"
    elif kind == "property" and not assets:
        kind = "liability"
    mixed = bool(assets and debts)
    property_like = kind == "property" or mixed
    total = debts if kind == "liability" and not assets else assets
    if property_like:
        total = assets - debts
    unique_notes = list(dict.fromkeys(note for note in notes if note))
    heading = bool(children)
    result = GroupResult(
        name=node.name,
        path=node.path,
        kind=kind,
        total=total,
        depth=depth,
        heading=heading,
        accounts=lines,
        note="; ".join(unique_notes),
        members=tuple(dict.fromkeys(member_lines)),
        # A generated path row is a summary heading, not a synthetic loan. Its
        # equity belongs in the total column, while value, debt, LTV, and payoff
        # date remain evidence attached to the specific property/loan row.
        value=assets if property_like and not heading else None,
        debt=debts if property_like and not heading else None,
        loan_end=loan_end if property_like and not heading else None,
        liquid=liquid,
        _assets=assets,
        _debts=debts,
        missing_quotes=tuple(dict.fromkeys(missing)),
        liquid_missing_quotes=tuple(dict.fromkeys(liquid_missing)),
        completeness=_withheld(db, tuple(dict.fromkeys(missing)), today),
    )
    return [result, *child_rows]


def _combined_group_kind(kinds: list[str]) -> str:
    unique = set(kinds)
    if not unique:
        return "asset"
    if len(unique) == 1:
        return next(iter(unique))
    if "property" in unique or ("liability" in unique and len(unique) > 1):
        return "property"
    return "asset"


def _direct_group_totals(
    db: DbSQLite,
    handles: list[str],
    kind: str | None,
    today: date,
    paid_off: set[str],
) -> tuple[list[GroupAccountResult], Money, Money, Money, list[str], list[str], list[str]]:
    lines: list[GroupAccountResult] = []
    assets = Money(0)
    debts = Money(0)
    liquid = Money(0)
    notes: list[str] = []
    missing: list[str] = []
    liquid_missing: list[str] = []
    for handle in handles:
        if handle in paid_off:
            continue
        account = db.get_account(handle)
        if account is None or account.hidden:
            continue
        line = _account_group_result(db, account, today)
        lines.append(line)
        if line.note:
            notes.append(line.note)
        missing.extend(line.missing_quotes)
        if kind == "liquid":
            liquid_missing.extend(line.missing_quotes)
        amount = line.total if line.total is not None else Money(0)
        if account.account_class is AccountClass.LIABILITY:
            debts = debts + amount
        else:
            assets = assets + amount
        if kind == "liquid":
            liquid = liquid + amount
    return lines, assets, debts, liquid, notes, missing, liquid_missing


def _withheld(db: DbSQLite, handles: tuple[str, ...], today: date) -> Completeness:
    """A group total is withheld with the evidence for each unvalued account."""
    excluded = []
    for handle in handles:
        account = db.get_account(handle)
        if account is not None:
            valued = valuation.account_value(db, account, as_of=today)
            excluded.append(valuation.excluded_valuation(db, account, valued, today))
    return Completeness.of(excluded, policy=Policy.WITHHOLD, as_of=today)


def _account_group_result(db: DbSQLite, account: Account, today: date) -> GroupAccountResult:
    name = db.full_name(account) or account.name
    if account.atype is not AccountType.FSA:
        subtree = [account, *db.descendants(account.handle)]
        aggregate = valuation.aggregate_value(db, accounts=subtree, as_of=today)
        total = aggregate.amount.value if aggregate.amount is not None else None
        own = valuation.account_value(db, account, as_of=today)
        note = ""
        source = "ledger"
        if (
            own.source == "market"
            and own.commodity is not None
            and own.quantity is not None
            and own.price is not None
        ):
            source = "market"
            currency = own.currency.mnemonic if own.currency is not None else "currency"
            note = (
                f"{own.quantity.format()} {own.commodity.mnemonic} at "
                f"{own.price.format()} {currency} as of {own.price_date}"
            )
            if own.exchange is not None and own.exchange.quote_date is not None:
                reporting = db.get_commodity(own.exchange.target_currency)
                target = reporting.mnemonic if reporting is not None else "reporting currency"
                note += f"; {currency}→{target} rate as of {own.exchange.quote_date}"
        if aggregate.missing_quotes:
            note = "Missing reporting-currency quote"
        elif own.source == "currency" and own.price_date is not None:
            suffix = " · inverse rate" if own.conversion_path == "inverse" else ""
            note = f"{own.price_date} · {own.price_source or 'Unknown source'}{suffix}"
        members = []
        for child in subtree[1:]:
            line = db.full_name(child) or child.name
            valued = valuation.account_value(db, child, as_of=today)
            if valued.missing_quote:
                line += ": missing reporting-currency quote"
            elif valued.price_date is not None and valued.total:
                line += f": {valuation.quote_evidence(db, valued)}"
            members.append(line)
        return GroupAccountResult(
            name=name,
            total=total,
            source=source,
            note=note,
            missing_quotes=aggregate.missing_quotes,
            members=tuple(members),
        )
    if not account.fsa_years:
        return GroupAccountResult(
            name=name,
            total=None,
            source="fsa_availability",
            note=f"{name}: no FSA funding years configured",
        )
    applicable = [
        fsa.year_status(db, account, year, as_of=today)
        for year in account.fsa_years
        if year.start <= today <= (year.runout_through or year.through)
    ]
    if not applicable:
        return GroupAccountResult(
            name=name,
            total=None,
            source="fsa_availability",
            note=f"{name}: no applicable FSA funding year",
        )
    total = sum((status.remaining for status in applicable), Money(0))
    count = len(applicable)
    return GroupAccountResult(
        name=name,
        total=total,
        source="fsa_availability",
        note=f"{count} applicable FSA funding year{'s' if count != 1 else ''}",
    )


def _loan_end_date(db: DbSQLite, handles: list[str], paid_off: set[str]) -> date | None:
    """Latest finite scheduled repayment date for loans selected by a group."""
    loans: set[str] = set()
    for handle in handles:
        account = db.get_account(handle)
        if account is None:
            continue
        for candidate in (account, *db.descendants(account.handle)):
            if candidate.handle in paid_off:
                continue
            if candidate.account_class is not AccountClass.LIABILITY:
                continue
            if candidate.atype is AccountType.LOAN or candidate.linked_asset:
                loans.add(candidate.handle)

    latest: date | None = None
    for scheduled in db.iter_scheduled():
        if not scheduled.usable:
            continue
        if not scheduled.enabled or not any(split.account in loans for split in scheduled.splits):
            continue
        last = scheduled.recurrence.last_occurrence()
        if last is not None and (latest is None or last > latest):
            latest = last
    return latest


def _paid_off_loans(db: DbSQLite, today: date) -> set[str]:
    """Loans with prior ledger activity and no remaining balance as of ``today``."""
    paid_off: set[str] = set()
    for account in db.iter_accounts():
        if account.account_class is not AccountClass.LIABILITY:
            continue
        if account.atype is not AccountType.LOAN and not account.linked_asset:
            continue
        if ledger.balance_recursive(db, account.handle, as_of=today):
            continue
        subtree = [account, *db.descendants(account.handle)]
        has_activity = any(
            Money(row["value_num"], row["value_den"])
            for item in subtree
            for row in db.split_rows(item.handle, end=today)
        )
        if has_activity:
            paid_off.update(item.handle for item in subtree)
    return paid_off
