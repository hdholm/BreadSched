"""Read-only net worth history: market-valued assets less debts at each period end."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from ..db.sqlite import DbSQLite
from ..engine import ledger, valuation
from ..engine.activity import ReportingPeriod, reporting_periods
from ..engine.currency import reporting_currency_handle
from ..lib.account import Account, AccountClass
from ..lib.amount import Amount
from ..lib.money import Money
from .contracts import ServiceError, ServiceResult


@dataclass(frozen=True, slots=True)
class NetWorthLine:
    """One top-level account tree's asset or debt value on a point's date.

    ``value`` is ``None`` when any account in the tree lacks a reporting-currency
    valuation on that date; a missing conversion is never presented as performed.
    """

    account: str
    name: str
    kind: str  # "asset" or "debt"
    value: Money | None


@dataclass(frozen=True, slots=True)
class NetWorthPoint:
    """Net worth valued on one date: a period's end, or the as-of date within it.

    ``assets``, ``debts``, ``net_worth``, and ``change`` are ``None`` when any
    account lacks a reporting-currency valuation on ``valued_on``; ``missing``
    names those accounts. ``lines`` break the totals down by top-level account
    and always sum exactly to them when complete.
    """

    start: date
    end: date
    label: str
    valued_on: date
    partial: bool
    assets: Money | None
    debts: Money | None
    net_worth: Money | None
    change: Money | None
    missing: tuple[str, ...]
    lines: tuple[NetWorthLine, ...]


@dataclass(frozen=True, slots=True)
class NetWorthHistory:
    start: date
    end: date
    period: ReportingPeriod
    as_of: date
    points: tuple[NetWorthPoint, ...]


def _top_level(db: DbSQLite, account: Account, cache: dict[str, str]) -> str:
    """Handle of the child of the book root that contains ``account``."""
    if account.handle in cache:
        return cache[account.handle]
    chain = [account]
    current = account
    while current.parent is not None:
        parent = db.get_account(current.parent)
        if parent is None or parent.is_root:
            break
        chain.append(parent)
        current = parent
    top = chain[-1].handle
    for item in chain:
        cache[item.handle] = top
    return top


def _value_on(
    db: DbSQLite,
    accounts: list[Account],
    tops: dict[str, str],
    when: date,
    reporting: str,
    ledgers: dict[str, Amount],
) -> tuple[dict[tuple[str, str], Money | None], list[str]]:
    """Sum complete reporting-currency valuations per (top-level account, kind).

    ``ledgers`` holds each account's ledger balance on ``when``.
    """
    sums: dict[tuple[str, str], Money | None] = {}
    missing: list[str] = []
    for account in accounts:
        kind = "asset" if account.account_class is AccountClass.ASSET else "debt"
        key = (tops[account.handle], kind)
        valued = valuation.account_value(
            db, account, as_of=when, ledger_amount=ledgers[account.handle]
        )
        amount = valued.total_amount
        sums.setdefault(key, Money(0))
        if amount is None or not amount:
            continue
        if valued.missing_quote or amount.commodity != reporting:
            missing.append(db.full_name(account))
            sums[key] = None
        else:
            current = sums[key]
            if current is not None:
                sums[key] = current + amount.value
    return sums, missing


def query_net_worth_history(
    db: DbSQLite,
    start: date,
    end: date,
    period: ReportingPeriod | str = ReportingPeriod.MONTH,
    today: date | None = None,
) -> ServiceResult[NetWorthHistory]:
    """Value the whole book's assets and debts at each period end through today.

    A period containing ``today`` is valued on ``today`` and marked partial;
    later periods are left out, since a ledger has no future balances (Projection
    forecasts those). Debts are liabilities as positive amounts, so net worth is
    assets less debts, exactly as on the Dashboard.
    """
    if end < start:
        return ServiceResult.failure(ServiceError("net_worth.range.invalid", ("start", "end")))
    try:
        grouping = ReportingPeriod(period)
    except ValueError:
        return ServiceResult.failure(ServiceError("net_worth.period.invalid", ("period",)))
    as_of = today or date.today()
    reporting = reporting_currency_handle(db)
    accounts = [
        account
        for account in db.iter_accounts()
        if not account.is_root
        and account.account_class in {AccountClass.ASSET, AccountClass.LIABILITY}
    ]
    cache: dict[str, str] = {}
    tops = {account.handle: _top_level(db, account, cache) for account in accounts}
    names = {handle: db.full_name(handle) for handle in set(tops.values())}
    points: list[NetWorthPoint] = []
    previous: Money | None = None
    # Each account's ledger balance is carried forward from the previous date by
    # adding only the splits since then, so every split is read once.
    ledgers: dict[str, Amount] = {}
    last_valued: date | None = None
    for start_on, end_on, label in reporting_periods(start, end, grouping):
        if start_on > as_of:
            break
        valued_on = min(end_on, as_of)
        since = last_valued + timedelta(days=1) if last_valued is not None else None
        for account in accounts:
            change = ledger.balance_amount(db, account, as_of=valued_on, since=since)
            earlier = ledgers.get(account.handle)
            ledgers[account.handle] = (
                change if earlier is None else ledger._accumulate(earlier, change)
            )
        last_valued = valued_on
        sums, missing = _value_on(db, accounts, tops, valued_on, reporting, ledgers)
        lines = tuple(
            NetWorthLine(handle, names[handle], kind, value)
            for (handle, kind), value in sorted(
                sums.items(), key=lambda item: (item[0][1], names[item[0][0]].casefold())
            )
            if value != Money(0)
        )
        complete = not missing
        assets = (
            sum((line.value or Money(0) for line in lines if line.kind == "asset"), Money(0))
            if complete
            else None
        )
        debts = (
            sum((line.value or Money(0) for line in lines if line.kind == "debt"), Money(0))
            if complete
            else None
        )
        net = assets - debts if assets is not None and debts is not None else None
        points.append(
            NetWorthPoint(
                start_on,
                end_on,
                label,
                valued_on,
                valued_on < end_on,
                assets,
                debts,
                net,
                net - previous if net is not None and previous is not None else None,
                tuple(dict.fromkeys(missing)),
                lines,
            )
        )
        previous = net
    return ServiceResult.success(NetWorthHistory(start, end, grouping, as_of, tuple(points)))
