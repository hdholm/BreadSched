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
from ..lib.recurrence import PeriodType, Recurrence
from ..lib.scheduled import ScheduledTransaction
from . import fsa, ledger, schedule, valuation
from .escrow import recognition as escrow_recognition

__all__ = [
    "DashboardConfig",
    "GroupConfig",
    "Dashboard",
    "GroupResult",
    "GroupAccountResult",
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
    note: str = ""


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
    note: str = ""
    #: Set for property groups: the value, what is owed, and the ratio.
    value: Money | None = None
    debt: Money | None = None
    #: Latest bounded repayment date among active loans in this group.
    loan_end: date | None = None
    #: Aggregate amount from liquid groups at or beneath this node.
    liquid: Money = field(default_factory=lambda: Money(0))
    _assets: Money = field(default_factory=lambda: Money(0), repr=False)
    _debts: Money = field(default_factory=lambda: Money(0), repr=False)

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
    """One pending dated cash flow, normalised for long-range comparisons."""

    name: str
    next_due: date
    amount: Money
    cycle_days: Decimal
    schedule: ScheduledTransaction | None = None
    estimate: bool = False
    income: bool = False
    held: Money = field(default_factory=lambda: Money(0))
    reserve_for: date | None = None
    account: str | None = None
    recurrence: Recurrence | None = None
    generated: bool = False
    emergency_amount: Money = field(default_factory=lambda: Money(0))

    @property
    def frequency(self) -> str:
        """The schedule's own words for how often it recurs.

        "every 3 months" is what the user set; 3.0000 is an implementation
        detail of the monthly conversion and reads as though it were the truth.
        """
        if self.recurrence is not None:
            return self.recurrence.describe()
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

    @property
    def emergency_monthly(self) -> Money:
        return (self.emergency_amount * (DAYS_PER_MONTH / self.cycle_days)).quantize(100)

    def days_until(self, today: date) -> int:
        return (self.next_due - today).days

    def hold(self, today: date) -> Money | None:
        """Funds reserved by income already received; income rows have no hold."""
        del today  # The builder calculates the dated reserve once for its as-of date.
        return None if self.income else self.held

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
    emergency_monthly_outgoings: Money
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
    pending: list[BillRow] = field(default_factory=list)
    income_per_month: Money = field(default_factory=lambda: Money(0))
    next_income: date | None = None
    _income_events: list[tuple[date, Money]] = field(default_factory=list, repr=False)
    liquid: Money = field(default_factory=lambda: Money(0))
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
        total = Money(0)
        for group in self.groups:
            if group.depth:
                continue
            if group.kind == "liability":
                continue
            total = total + (group.equity if group.equity is not None else group.total)
        return total

    @property
    def debts(self) -> Money:
        total = Money(0)
        for group in self.groups:
            if group.depth:
                continue
            if group.kind == "liability":
                total = total + group.total
        return total

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
    def monthly_outgoings(self) -> Money:
        total = Money(0)
        for bill in self._normalised_bills():
            total = total + bill.monthly
        return total

    @property
    def annual_outgoings(self) -> Money:
        total = Money(0)
        for bill in self._normalised_bills():
            total = total + bill.annual
        return total

    @property
    def emergency_monthly_outgoings(self) -> Money:
        """Recurring costs explicitly retained when household income stops."""
        return sum((bill.emergency_monthly for bill in self._normalised_bills()), Money(0))

    def _normalised_bills(self) -> list[BillRow]:
        """One representative row per recurring obligation.

        Every missed occurrence remains a pending liquidity obligation, but three
        overdue monthly payments do not turn one monthly bill into three separate
        recurring expenses for emergency-fund sizing.
        """
        recurring: dict[tuple[str, str], BillRow] = {}
        standalone: list[BillRow] = []
        for bill in self.bills:
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
                if bill.next_due > horizon or bill.next_due < self.as_of
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
        return (self.emergency_monthly_outgoings * self.config.emergency_months).quantize(100)

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
            "emergency_monthly_outgoings": self.emergency_monthly_outgoings,
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
    paid_off = _paid_off_loans(db, today)
    resolved = resolve_groups(db, config)
    board.groups = _hierarchical_results(db, resolved, today, paid_off)

    if any(group.kind == "liquid" for group in resolved):
        board.liquid = sum((group.liquid for group in board.groups if group.depth == 0), Money(0))
    else:
        board.liquid = ledger.cash_on_hand(db, as_of=today)
    pending, income_per_month, next_income, income_events = _pending_cash_flow(
        db, today, horizon_days, paid_off
    )
    board.pending = pending
    board.income_per_month = income_per_month
    board.next_income = next_income
    board._income_events = income_events
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
    lines, assets, debts, liquid, notes = _direct_group_totals(
        db, direct_accounts, direct_kind, today, paid_off
    )
    loan_end = _loan_end_date(db, direct_accounts, paid_off)
    for child_result in children:
        assets = assets + child_result._assets
        debts = debts + child_result._debts
        liquid = liquid + child_result.liquid
        if child_result.note:
            notes.append(child_result.note)
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
        # A generated path row is a summary heading, not a synthetic loan. Its
        # equity belongs in the total column, while value, debt, LTV, and payoff
        # date remain evidence attached to the specific property/loan row.
        value=assets if property_like and not heading else None,
        debt=debts if property_like and not heading else None,
        loan_end=loan_end if property_like and not heading else None,
        liquid=liquid,
        _assets=assets,
        _debts=debts,
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
) -> tuple[list[GroupAccountResult], Money, Money, Money, list[str]]:
    lines: list[GroupAccountResult] = []
    assets = Money(0)
    debts = Money(0)
    liquid = Money(0)
    notes: list[str] = []
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
        amount = line.total if line.total is not None else Money(0)
        if account.account_class is AccountClass.LIABILITY:
            debts = debts + amount
        else:
            assets = assets + amount
        if kind == "liquid":
            liquid = liquid + amount
    return lines, assets, debts, liquid, notes


def _account_group_result(db: DbSQLite, account: Account, today: date) -> GroupAccountResult:
    name = db.full_name(account) or account.name
    if account.atype is not AccountType.FSA:
        total = valuation.value_recursive(db, account.handle, as_of=today)
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
        return GroupAccountResult(
            name=name,
            total=total,
            source=source,
            note=note,
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


def _flow_amounts(db: DbSQLite, sched: ScheduledTransaction, when: date) -> tuple[Money, Money]:
    """Return positive income and household outflow for one occurrence."""
    income = Money(0)
    outflow = Money(0)
    legs = list(sched.resolved_splits(when=when))
    accounts = {
        handle: account
        for handle, _amount in legs
        if (account := db.get_account(handle)) is not None
    }
    escrow = escrow_recognition(legs, accounts)
    for handle, amount in legs:
        account = accounts.get(handle)
        if account is None:
            continue
        if account.account_class is AccountClass.INCOME:
            income = income - amount  # income accounts carry credit balances
        elif account.account_class is AccountClass.EXPENSE:
            outflow = outflow + amount
        elif account.account_class is AccountClass.LIABILITY and amount > 0:
            outflow = outflow + amount
    return income, outflow + escrow.planning_expense_adjustment


def _emergency_outflow(db: DbSQLite, sched: ScheduledTransaction, when: date) -> Money:
    """Return the non-duplicated portion retained when income stops.

    The choice belongs to the economically meaningful positive leg. Cash/bank
    funding legs are credits and never counted. Escrow draws reduce a previously
    funded restricted asset, so their covered expense legs are suppressed.
    """
    legs = list(sched.resolved_splits(when=when))
    accounts = {
        handle: account
        for handle, _amount in legs
        if (account := db.get_account(handle)) is not None
    }
    escrow = escrow_recognition(legs, accounts)
    positive: dict[str, Money] = {}
    for handle, amount in legs:
        if amount > 0:
            positive[handle] = positive.get(handle, Money(0)) + amount

    total = Money(0)
    for handle, amount in positive.items():
        account = accounts.get(handle)
        if account is None or not account.emergency_fund_included:
            continue
        if account.account_class is AccountClass.EXPENSE:
            amount = amount - escrow.covered_expenses.get(handle, Money(0))
        if amount > 0:
            total = total + amount
    return total


def _next_unskipped(sched: ScheduledTransaction, after: date) -> date | None:
    """First occurrence after a date, respecting per-date skips."""
    candidate = sched.recurrence.next_after(after)
    while candidate is not None and candidate in sched.skipped:
        candidate = sched.recurrence.next_after(candidate)
    return candidate


def _cycle_start(recurrence: Recurrence, due: date, cycle_length: Decimal) -> date:
    """Previous firing for a bill cycle, or an inferred first-cycle boundary."""
    lookback = timedelta(days=int(cycle_length) + 15)
    previous = recurrence.occurrences(due - timedelta(days=1), since=due - lookback)
    return previous[-1] if previous else due - timedelta(days=max(1, int(cycle_length)))


def _income_events_for_cycle(
    db: DbSQLite,
    schedules: list[ScheduledTransaction],
    start: date,
    through: date,
    today: date,
) -> tuple[Money, Money]:
    """Return total cycle income and the share already received by ``today``.

    Future scheduled income belongs in the denominator. Past income participates
    only when its schedule says it was handled or a corresponding ledger
    transaction exists; a missed pay event cannot reserve cash that never arrived.
    """
    total = Money(0)
    received = Money(0)
    for sched in schedules:
        for when in sched.recurrence.occurrences(through, since=start + timedelta(days=1)):
            if when in sched.skipped:
                continue
            income, outflow = _flow_amounts(db, sched, when)
            if income <= 0 or income < outflow:
                continue
            happened = when > today
            if not happened and sched.last_posted is not None and when <= sched.last_posted:
                happened = True
            if not happened:
                happened = schedule.already_posted(db, sched.handle, when)
            if not happened:
                continue
            total = total + income
            if when <= today:
                received = received + income
    return total, received


def _reserve_bill(
    db: DbSQLite,
    bill: BillRow,
    income_schedules: list[ScheduledTransaction],
    today: date,
) -> None:
    """Attach the exact income-triggered reserve for one bill row."""
    recurrence = bill.recurrence
    if recurrence is None:
        bill.held = bill.amount
        bill.reserve_for = bill.next_due
        return

    target_due = bill.next_due
    target_amount = bill.amount
    if target_due < today:
        if bill.schedule is None:
            return
        next_due = _next_unskipped(bill.schedule, today)
        if next_due is None:
            return
        _income, next_amount = _flow_amounts(db, bill.schedule, next_due)
        if next_amount <= 0:
            return
        target_due = next_due
        target_amount = next_amount

    start = _cycle_start(recurrence, target_due, bill.cycle_days)
    total_income, received_income = _income_events_for_cycle(
        db, income_schedules, start, target_due, today
    )
    bill.reserve_for = target_due
    if total_income <= 0:
        # With no identified income before the due date, existing cash is the
        # only known funding source; protect the complete obligation.
        bill.held = target_amount
        return
    bill.held = (target_amount * (received_income / total_income)).quantize(100)


def _credit_card_rows(
    db: DbSQLite,
    today: date,
    schedules: list[ScheduledTransaction],
) -> list[BillRow]:
    """Account-tied card payments shared with Scheduled and Upcoming."""
    rows: list[BillRow] = []
    for definition in schedule.account_payment_definitions(db, today, schedules):
        if definition.amount_due <= 0:
            continue
        account = db.get_account(definition.account)
        rows.append(
            BillRow(
                name=definition.name,
                next_due=definition.next_due,
                amount=definition.amount_due,
                cycle_days=DAYS_PER_MONTH,
                account=definition.account,
                recurrence=definition.recurrence,
                generated=True,
                emergency_amount=(
                    definition.amount_due
                    if account is not None and account.emergency_fund_included
                    else Money(0)
                ),
            )
        )
    return rows


def _pending_cash_flow(
    db: DbSQLite,
    today: date,
    horizon_days: int,
    paid_off: set[str],
) -> tuple[list[BillRow], Money, date | None, list[tuple[date, Money]]]:
    """Build dated pending income and bills, plus income used by liquidity."""
    horizon = today + timedelta(days=horizon_days)
    schedules = [sched for sched in db.iter_scheduled() if sched.enabled and sched.usable]
    due_by_schedule: dict[str, list[date]] = {}
    for occurrence in schedule.due_occurrences(db, as_of=today, horizon_days=0):
        due_by_schedule.setdefault(occurrence.schedule.handle, []).append(occurrence.when)

    pending: list[BillRow] = []
    bills: list[BillRow] = []
    income_schedules: list[ScheduledTransaction] = []
    income_events: list[tuple[date, Money]] = []
    income_per_month = Money(0)

    for sched in schedules:
        unresolved = due_by_schedule.get(sched.handle, [])
        next_due = _next_unskipped(sched, today)
        dates = unresolved or ([next_due] if next_due is not None else [])
        representative = (dates[-1] if dates else None) or sched.recurrence.last_occurrence()
        if representative is None:
            continue
        income, outflow = _flow_amounts(db, sched, representative)
        if income > 0 and income >= outflow:
            income_schedules.append(sched)
        if not dates:
            continue

        days = cycle_days(sched)
        if income > 0 and income >= outflow:
            income_per_month = income_per_month + (income * (DAYS_PER_MONTH / days)).quantize(100)
            for when in dates:
                event_income, event_outflow = _flow_amounts(db, sched, when)
                if event_income <= 0 or event_income < event_outflow:
                    continue
                pending.append(
                    BillRow(
                        name=sched.name,
                        next_due=when,
                        amount=event_income,
                        cycle_days=days,
                        schedule=sched,
                        estimate=sched.placeholder,
                        income=True,
                        recurrence=sched.recurrence,
                    )
                )
            for occurrence_date in sched.recurrence.occurrences(horizon, since=today):
                if occurrence_date in sched.skipped:
                    continue
                if occurrence_date <= today and schedule.already_posted(
                    db, sched.handle, occurrence_date
                ):
                    continue
                event_income, event_outflow = _flow_amounts(db, sched, occurrence_date)
                if event_income > 0 and event_income >= event_outflow:
                    income_events.append((occurrence_date, event_income))
            continue

        for when in dates:
            _income, event_outflow = _flow_amounts(db, sched, when)
            if event_outflow <= 0:
                continue
            legs = list(sched.resolved_splits(when=when))
            if any(
                handle in paid_off
                and amount > 0
                and (account := db.get_account(handle)) is not None
                and account.account_class is AccountClass.LIABILITY
                for handle, amount in legs
            ):
                continue
            bill = BillRow(
                name=sched.name,
                next_due=when,
                amount=event_outflow,
                cycle_days=days,
                schedule=sched,
                estimate=sched.placeholder,
                recurrence=sched.recurrence,
                emergency_amount=_emergency_outflow(db, sched, when),
            )
            bills.append(bill)
            pending.append(bill)

    card_rows = _credit_card_rows(db, today, schedules)
    bills.extend(card_rows)
    pending.extend(card_rows)
    latest_overdue = {
        bill.schedule.handle: bill
        for bill in bills
        if bill.next_due < today and bill.schedule is not None
    }
    for bill in bills:
        if (
            bill.next_due < today
            and bill.schedule is not None
            and latest_overdue[bill.schedule.handle] is not bill
        ):
            continue
        _reserve_bill(db, bill, income_schedules, today)

    pending.sort(key=lambda row: (row.next_due, not row.income, row.name.casefold()))
    income_events.sort(key=lambda item: item[0])
    next_income = next((when for when, _amount in income_events if when >= today), None)
    return pending, income_per_month, next_income, income_events


def pending_from_ledger(db: DbSQLite, today: date | None = None) -> list:
    """Occurrences already due but not yet posted, for the pending list."""
    return schedule.due_occurrences(db, as_of=today or date.today(), horizon_days=0)
