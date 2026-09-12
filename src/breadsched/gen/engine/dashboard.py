"""The dashboard: what is owned, what is owed, and what has to stay liquid.

Modelled on the household spreadsheet this replaces, but computed from the book
rather than typed in, so the figures cannot drift from the ledger that produced
them.

Four ideas carry the whole view.

**Groups.** Accounts are gathered into named groups — a property and its mortgage,
the retirement accounts, the current accounts — and a group of a property plus its
loan reports equity and loan-to-value, which is the pair of numbers that actually
answers "how is the house doing".

**Normalised bills.** A bill's amount means nothing without its cycle: 619 a
quarter and 200 a month are not comparable until both are monthly. Every scheduled
outflow is expressed per month and per year from its own recurrence, so a
quarterly HOA fee and a fortnightly daycare bill can be added together and sorted
against each other.

**Hold.** A bill due in three weeks is not free money today. For each bill the
dashboard reports what should already be set aside — its accrual so far through
the current cycle — so an annual insurance premium is visibly one twelfth funded a
month after it was last paid, rather than appearing from nowhere in November.

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
from ..lib.account import Account, AccountClass, AccountPlanningRole
from ..lib.money import Money
from ..lib.recurrence import PeriodType
from ..lib.scheduled import ScheduledTransaction
from . import ledger, schedule

__all__ = [
    "DashboardConfig",
    "GroupConfig",
    "Dashboard",
    "GroupResult",
    "BillRow",
    "build",
    "DAYS_PER_MONTH",
]

#: Average length of a month. Used to convert any cycle to a monthly equivalent;
#: 30 would drift by five days a year and make annual bills disagree with monthly
#: ones.
DAYS_PER_MONTH = Decimal("30.436875")
DAYS_PER_YEAR = Decimal("365.2425")

#: Cycle length in days for each recurrence period, before its interval.
_PERIOD_DAYS = {
    PeriodType.DAY: Decimal(1),
    PeriodType.WEEK: Decimal(7),
    PeriodType.SEMI_MONTH: DAYS_PER_MONTH / 2,
    PeriodType.MONTH: DAYS_PER_MONTH,
    PeriodType.YEAR: DAYS_PER_YEAR,
}


def cycle_days(sched: ScheduledTransaction) -> Decimal:
    """How many days one cycle of this schedule lasts.

    A one-off has no cycle; treated as a year so it does not divide by zero and
    does not dominate a monthly average.
    """
    recurrence = sched.recurrence
    if recurrence.period is PeriodType.ONCE:
        return DAYS_PER_YEAR
    base = _PERIOD_DAYS.get(recurrence.period, DAYS_PER_MONTH)
    return base * Decimal(max(1, recurrence.interval))


# --------------------------------------------------------------- configuration


@dataclass
class GroupConfig:
    """A named set of accounts shown as one line on the dashboard."""

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
    """A sensible dashboard for a book that has never configured one.

    Derived from the chart of accounts: cash accounts are the liquid group,
    investment accounts the retirement group, and every liability its own line.
    A first view that shows real numbers is worth more than an empty one with an
    invitation to configure it.
    """
    liquid, retirement, fsa_accounts, investments = [], [], [], []
    liabilities, other_assets = [], []
    for account in db.iter_accounts():
        if account.is_root or account.placeholder or account.exclude_from_projection:
            continue
        if account.planning_role is AccountPlanningRole.RETIREMENT:
            retirement.append(account.handle)
        elif account.planning_role is AccountPlanningRole.FSA:
            fsa_accounts.append(account.handle)
        elif account.planning_role is AccountPlanningRole.INVESTMENT:
            investments.append(account.handle)
        elif account.account_class is AccountClass.LIABILITY:
            liabilities.append(account.handle)
        elif account.atype.is_cash_like:
            liquid.append(account.handle)
        elif account.atype.is_investment:
            retirement.append(account.handle)
        elif account.account_class is AccountClass.ASSET:
            other_assets.append(account.handle)

    # A loan and its asset belong together; leaving them in the generic asset and
    # debt totals hides the equity, which is the number people look for.
    paired: set[str] = set()
    property_groups: list[GroupConfig] = []
    # All the loans on one asset share a line: a house with two mortgages has one
    # equity figure, and a group per loan would count the house once each.
    loans_by_asset: dict[str, list[Account]] = {}
    for loan, asset in linked_pairs(db):
        loans_by_asset.setdefault(asset.handle, []).append(loan)

    for asset_handle, loans in loans_by_asset.items():
        property_asset = db.get_account(asset_handle)
        if property_asset is None:
            continue
        property_groups.append(
            GroupConfig(
                name=property_asset.group or property_asset.name,
                accounts=[asset_handle] + [loan.handle for loan in loans],
                kind="property",
            )
        )
        paired.add(asset_handle)
        paired.update(loan.handle for loan in loans)

    liquid = [h for h in liquid if h not in paired]
    retirement = [h for h in retirement if h not in paired]
    fsa_accounts = [h for h in fsa_accounts if h not in paired]
    investments = [h for h in investments if h not in paired]
    liabilities = [h for h in liabilities if h not in paired]
    other_assets = [h for h in other_assets if h not in paired]

    # An account naming its own group is answering this question directly, so it
    # is honoured here rather than only filling gaps: the default configuration
    # mentions every account, and a field that only applied to unmentioned ones
    # would never do anything.
    named: dict[str, GroupConfig] = {}
    for bucket in (liquid, retirement, fsa_accounts, investments, other_assets, liabilities):
        for handle in list(bucket):
            named_account = db.get_account(handle)
            if named_account is None or not named_account.group:
                continue
            group = named.get(named_account.group)
            if group is None:
                group = GroupConfig(
                    name=named_account.group, accounts=[], kind=_kind_for(named_account)
                )
                named[named_account.group] = group
            group.accounts.append(handle)
            bucket.remove(handle)
            paired.add(handle)

    groups = list(property_groups) + list(named.values())
    if liquid:
        groups.append(GroupConfig("Cash", liquid, "liquid"))
    if retirement:
        groups.append(GroupConfig("Retirement", retirement, "retirement"))
    if fsa_accounts:
        groups.append(GroupConfig("FSA / benefits", fsa_accounts, "asset"))
    if investments:
        groups.append(GroupConfig("Investments", investments, "asset"))
    if other_assets:
        groups.append(GroupConfig("Other assets", other_assets, "asset"))
    if liabilities:
        groups.append(GroupConfig("Debts", liabilities, "liability"))
    return DashboardConfig(groups=groups)


# -------------------------------------------------------------------- results


@dataclass
class GroupResult:
    """One group's totals."""

    name: str
    kind: str
    total: Money
    accounts: list[tuple[str, Money]] = field(default_factory=list)
    #: Set for property groups: the value, what is owed, and the ratio.
    value: Money | None = None
    debt: Money | None = None

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


@dataclass
class BillRow:
    """One recurring outflow, normalised so it can be compared with the others."""

    name: str
    next_due: date
    amount: Money
    cycle_days: Decimal
    schedule: ScheduledTransaction | None = None
    estimate: bool = False

    @property
    def frequency(self) -> str:
        """The schedule's own words for how often it recurs.

        "every 3 months" is what the user set; 3.0000 is an implementation
        detail of the monthly conversion and reads as though it were the truth.
        """
        if self.schedule is not None:
            return self.schedule.recurrence.describe()
        months = self.cycle_months
        return f"every {months:g} month(s)"

    @property
    def cycle_months(self) -> Decimal:
        return (self.cycle_days / DAYS_PER_MONTH).quantize(Decimal("0.0001"))

    @property
    def monthly(self) -> Money:
        return (self.amount * (DAYS_PER_MONTH / self.cycle_days)).quantize(100)

    @property
    def annual(self) -> Money:
        return (self.amount * (DAYS_PER_YEAR / self.cycle_days)).quantize(100)

    def days_until(self, today: date) -> int:
        return (self.next_due - today).days

    def hold(self, today: date) -> Money:
        """What should already be set aside for this bill.

        The accrual so far through the current cycle: a bill last paid two months
        into a twelve-month cycle should be a sixth funded. Reported so an annual
        premium is visibly accumulating rather than arriving as a surprise.

        A bill already overdue is held in full — it is not a future obligation.
        """
        remaining = Decimal(max(0, self.days_until(today)))
        if remaining >= self.cycle_days:
            return Money(0)
        elapsed = self.cycle_days - remaining
        return (self.amount * (elapsed / self.cycle_days)).quantize(100)

    def due_within(self, today: date, days: int) -> bool:
        return self.days_until(today) <= days


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
    annual_outgoings: Money
    income_per_month: Money
    next_income: date | None
    bills: int


@dataclass
class Dashboard:
    """Everything the dashboard shows, computed once."""

    as_of: date
    config: DashboardConfig
    groups: list[GroupResult] = field(default_factory=list)
    bills: list[BillRow] = field(default_factory=list)
    income_per_month: Money = field(default_factory=lambda: Money(0))
    next_income: date | None = None
    liquid: Money = field(default_factory=lambda: Money(0))
    # -------------------------------------------------------------- aggregates

    def group(self, name: str) -> GroupResult | None:
        return next((g for g in self.groups if g.name == name), None)

    def total_of_kind(self, kind: str) -> Money:
        total = Money(0)
        for group in self.groups:
            if group.kind == kind:
                total = total + (group.equity or group.total)
        return total

    @property
    def assets(self) -> Money:
        total = Money(0)
        for group in self.groups:
            if group.kind == "liability":
                continue
            total = total + (group.equity if group.equity is not None else group.total)
        return total

    @property
    def debts(self) -> Money:
        total = Money(0)
        for group in self.groups:
            if group.kind == "liability":
                total = total + group.total
        return total

    @property
    def net_worth(self) -> Money:
        return self.assets - self.debts

    # --------------------------------------------------------------- the bills

    @property
    def monthly_outgoings(self) -> Money:
        total = Money(0)
        for bill in self.bills:
            total = total + bill.monthly
        return total

    @property
    def annual_outgoings(self) -> Money:
        total = Money(0)
        for bill in self.bills:
            total = total + bill.annual
        return total

    @property
    def total_hold(self) -> Money:
        total = Money(0)
        for bill in self.bills:
            total = total + bill.hold(self.as_of)
        return total

    def due_within(self, days: int) -> list[BillRow]:
        return [bill for bill in self.bills if bill.due_within(self.as_of, days)]

    # ------------------------------------------------------------- the verdict

    @property
    def required_liquid(self) -> Money:
        """Bills falling due inside the liquidity window, less income expected."""
        total = Money(0)
        for bill in self.due_within(self.config.liquidity_days):
            total = total + bill.amount
        return total - self.income_within(self.config.liquidity_days)

    def income_within(self, days: int) -> Money:
        """Income expected in the next ``days``, from the scheduled inflows."""
        share = Decimal(days) / DAYS_PER_MONTH
        return (self.income_per_month * share).quantize(100)

    @property
    def emergency_fund(self) -> Money:
        """What the household would need to run with no income at all."""
        return (self.monthly_outgoings * self.config.emergency_months).quantize(100)

    @property
    def available(self) -> Money:
        """Cash that is genuinely free: liquid, less what is spoken for."""
        return self.liquid - self.required_liquid - self.config.reserve

    @property
    def emergency_shortfall(self) -> Money:
        """How far the liquid balance falls short of the emergency fund."""
        return self.emergency_fund - self.liquid

    @property
    def months_covered(self) -> Decimal:
        """How long the liquid balance would last with no income."""
        monthly = self.monthly_outgoings
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
            "annual_outgoings": self.annual_outgoings,
            "income_per_month": self.income_per_month,
            "next_income": self.next_income,
            "bills": len(self.bills),
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
    board = Dashboard(as_of=today, config=config)

    for group in resolve_groups(db, config):
        result = _group_result(db, group, today)
        if result.accounts:
            board.groups.append(result)

    board.liquid = _liquid_total(db, config, today)
    bills, income_per_month, next_income = _bills_and_income(db, today, horizon_days)
    board.bills = bills
    board.income_per_month = income_per_month
    board.next_income = next_income
    return board


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
    """Work out the groups to show, from the configuration *and* the accounts.

    Three sources, in this order:

    1. **A loan naming its asset**, where the configuration does not already put
       the two together. All the loans secured on one asset join a single line —
       a house with two mortgages has one equity figure, not two, and giving each
       loan its own line would count the house twice.
    2. **The dashboard configuration**, which is the explicit answer and beats
       anything inferred.
    3. **An account naming a group**, for accounts the configuration does not
       mention. This is a default placement, not an override: taking an account
       out of a group it was deliberately put in is how a property line loses its
       house and reports a value of zero against its mortgage.

    An account claimed by an earlier rule is dropped from the later ones rather
    than appearing twice, since a house counted in both a property line and an
    asset total inflates net worth by the whole value of the house.
    """
    resolved: list[GroupConfig] = []
    claimed: set[str] = set()

    # 1. Loans gathered onto the asset they are secured on.
    paired_in_config = {
        handle for group in config.groups if _pairs_a_loan(db, group) for handle in group.accounts
    }
    by_asset: dict[str, list[Account]] = {}
    for loan, asset in linked_pairs(db):
        if loan.handle in paired_in_config and asset.handle in paired_in_config:
            continue
        by_asset.setdefault(asset.handle, []).append(loan)

    for asset_handle, loans in by_asset.items():
        resolved_asset = db.get_account(asset_handle)
        if resolved_asset is None:
            continue
        name = resolved_asset.group or resolved_asset.name
        resolved.append(
            GroupConfig(
                name=name,
                accounts=[asset_handle] + [loan.handle for loan in loans],
                kind="property",
            )
        )
        claimed.add(asset_handle)
        claimed.update(loan.handle for loan in loans)

    # 2. The configured groups.
    for group in config.groups:
        remaining = [h for h in group.accounts if h not in claimed]
        if not remaining:
            continue
        resolved.append(GroupConfig(name=group.name, accounts=remaining, kind=group.kind))
        claimed.update(remaining)

    # 3. Accounts naming a group the configuration did not place them in.
    by_name = {group.name: group for group in resolved}
    for account in db.iter_accounts():
        if not account.group or account.handle in claimed or account.is_root:
            continue
        existing_group = by_name.get(account.group)
        if existing_group is None:
            existing_group = GroupConfig(name=account.group, accounts=[], kind=_kind_for(account))
            by_name[account.group] = existing_group
            resolved.append(existing_group)
        existing_group.accounts.append(account.handle)
        claimed.add(account.handle)

    return resolved


def _sides(db: DbSQLite, group: GroupConfig) -> list:
    """The accounts of a group, skipping any that have gone."""
    return [a for a in (db.get_account(h) for h in group.accounts) if a is not None]


def _pairs_a_loan(db: DbSQLite, group: GroupConfig) -> bool:
    """True when a group holds both a liability and something it could secure."""
    accounts = _sides(db, group)
    has_debt = any(a.account_class is AccountClass.LIABILITY for a in accounts)
    has_value = any(a.account_class is AccountClass.ASSET for a in accounts)
    return has_debt and has_value


def _kind_for(account: Account) -> str:
    if account.planning_role is AccountPlanningRole.RETIREMENT:
        return "retirement"
    if account.planning_role in {
        AccountPlanningRole.FSA,
        AccountPlanningRole.INVESTMENT,
    }:
        return "asset"
    if account.account_class is AccountClass.LIABILITY:
        return "liability"
    if account.atype.is_cash_like:
        return "liquid"
    if account.atype.is_investment:
        return "retirement"
    return "asset"


def _group_result(db: DbSQLite, group: GroupConfig, today: date) -> GroupResult:
    lines: list[tuple[str, Money]] = []
    total = Money(0)
    value = Money(0)
    debt = Money(0)

    for handle in group.accounts:
        account = db.get_account(handle)
        if account is None:
            continue
        balance = ledger.balance_recursive(db, handle, as_of=today)
        lines.append((db.full_name(account) or account.name, balance))
        total = total + balance
        if account.account_class is AccountClass.LIABILITY:
            debt = debt + balance
        else:
            value = value + balance

    result = GroupResult(name=group.name, kind=group.kind, total=total, accounts=lines)
    if group.kind == "property" or (value and debt):
        # A property line is only useful as value against loan: either number
        # alone says nothing about whether the house is an asset yet.
        result.value = value
        result.debt = debt
        result.total = value - debt
    return result


def _liquid_total(db: DbSQLite, config: DashboardConfig, today: date) -> Money:
    handles: list[str] = []
    for group in config.groups:
        if group.kind == "liquid":
            handles.extend(group.accounts)
    if not handles:
        return ledger.cash_on_hand(db, as_of=today)
    total = Money(0)
    for handle in handles:
        total = total + ledger.balance_recursive(db, handle, as_of=today)
    return total


def _bills_and_income(
    db: DbSQLite,
    today: date,
    horizon_days: int,
) -> tuple[list[BillRow], Money, date | None]:
    """Split the schedules into outflows and income, normalising both.

    A schedule counts as income when its net effect on income accounts is a
    credit; everything else that leaves money is a bill. Estimates are included:
    "about 600 a month on groceries" is as real a claim on the bank balance as a
    standing order, and leaving it out flatters every figure here.
    """
    horizon = today + timedelta(days=horizon_days)
    bills: list[BillRow] = []
    income_per_month = Money(0)
    next_income: date | None = None

    for sched in db.iter_scheduled():
        if not sched.enabled:
            continue
        upcoming = sched.recurrence.occurrences(horizon, since=today)
        when = upcoming[0] if upcoming else sched.recurrence.next_after(today)
        if when is None:
            continue

        income = Money(0)
        outflow = Money(0)
        for handle, amount in sched.resolved_splits(when=when):
            account = db.get_account(handle)
            if account is None:
                continue
            if account.account_class is AccountClass.INCOME:
                income = income - amount  # income accounts carry credit balances
            elif account.account_class is AccountClass.EXPENSE:
                outflow = outflow + amount
            elif account.account_class is AccountClass.LIABILITY and amount > 0:
                outflow = outflow + amount  # a loan repayment leaves the household

        days = cycle_days(sched)
        if income > 0 and income >= outflow:
            income_per_month = income_per_month + (income * (DAYS_PER_MONTH / days)).quantize(100)
            if next_income is None or when < next_income:
                next_income = when
            continue

        if outflow <= 0:
            continue
        bills.append(
            BillRow(
                name=sched.name,
                next_due=when,
                amount=outflow,
                cycle_days=days,
                schedule=sched,
                estimate=sched.placeholder,
            )
        )

    bills.sort(key=lambda bill: bill.annual.to_decimal(), reverse=True)
    return bills, income_per_month, next_income


def pending_from_ledger(db: DbSQLite, today: date | None = None) -> list:
    """Occurrences already due but not yet posted, for the pending list."""
    return schedule.due_occurrences(db, as_of=today or date.today(), horizon_days=0)
