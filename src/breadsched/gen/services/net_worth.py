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


@dataclass(frozen=True, slots=True)
class NetWorthPosting:
    """One transaction's effect on net worth within a change's window.

    ``effect`` is the net of its splits in asset and debt accounts, converted to
    the reporting currency with the quote applicable on ``posted``; it is ``None``
    when no such quote exists, and ``currency`` names the unconverted one.
    """

    transaction: str
    posted: date
    description: str
    accounts: tuple[str, ...]
    effect: Money | None
    currency: str


@dataclass(frozen=True, slots=True)
class NetWorthChange:
    """Why net worth moved between the end of ``opening_on`` and ``closing_on``.

    ``posted`` sums the postings; ``revaluation`` is the rest of the change, from
    market prices and exchange rates, so ``posted + revaluation == change``
    exactly. Every total is ``None`` when a valuation or conversion it needs is
    missing, and ``missing`` names what lacked a quote. ``transfers`` counts the
    transactions left out because they only moved money between the household's
    own asset and debt accounts.
    """

    start: date
    end: date
    opening_on: date
    closing_on: date
    partial: bool
    opening: Money | None
    closing: Money | None
    change: Money | None
    posted: Money | None
    revaluation: Money | None
    postings: tuple[NetWorthPosting, ...]
    transfers: int
    missing: tuple[str, ...]


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


def _balance_sheet_accounts(db: DbSQLite) -> list[Account]:
    return [
        account
        for account in db.iter_accounts()
        if not account.is_root
        and account.account_class in {AccountClass.ASSET, AccountClass.LIABILITY}
    ]


def _net_worth_on(
    db: DbSQLite, accounts: list[Account], when: date, reporting: str, ledgers: dict[str, Amount]
) -> tuple[Money | None, list[str]]:
    """Net worth on ``when`` from each account's ledger balance on that date."""
    tops = {account.handle: account.handle for account in accounts}
    sums, missing = _value_on(db, accounts, tops, when, reporting, ledgers)
    if missing:
        return None, missing
    net = Money(0)
    for (_handle, kind), value in sums.items():
        if value is not None:
            net = net + value if kind == "asset" else net - value
    return net, missing


def query_net_worth_change(
    db: DbSQLite, start: date, end: date, today: date | None = None
) -> ServiceResult[NetWorthChange]:
    """The postings behind the net worth change from ``start`` through ``end``.

    Net worth is valued at the end of the day before ``start`` and on ``end``
    (or on ``today`` when ``end`` is later, marking the change partial), exactly
    as each history point is. A split counts from its posting date on, so the
    window is the same one both valuations bracket.
    """
    if end < start:
        return ServiceResult.failure(ServiceError("net_worth.range.invalid", ("start", "end")))
    as_of = today or date.today()
    if start > as_of:
        return ServiceResult.failure(ServiceError("net_worth.range.future", ("start",)))
    closing_on = min(end, as_of)
    opening_on = start - timedelta(days=1)
    reporting = reporting_currency_handle(db)
    accounts = _balance_sheet_accounts(db)
    ledgers = {
        account.handle: ledger.balance_amount(db, account, as_of=opening_on) for account in accounts
    }
    opening, opening_missing = _net_worth_on(db, accounts, opening_on, reporting, ledgers)
    names = {account.handle: db.full_name(account) for account in accounts}
    raw: dict[str, Amount] = {}
    headers: dict[str, tuple[date, str]] = {}
    touched: dict[str, list[str]] = {}
    for account in accounts:
        # The closing balance is the opening one plus the window's splits, which
        # are read once for both the postings and the closing valuation.
        window: Amount | None = None
        for row in db.split_rows(account.handle, start=start, end=closing_on):
            handle = row["txn"]
            currency = row["currency"] if row["currency"] else reporting
            value = Amount(Money(row["value_num"], row["value_den"]), currency)
            raw[handle] = ledger._accumulate(raw.get(handle), value)
            window = ledger._accumulate(window, value)
            headers[handle] = (date.fromisoformat(row["post_date"]), row["description"] or "")
            accounts_touched = touched.setdefault(handle, [])
            if names[account.handle] not in accounts_touched:
                accounts_touched.append(names[account.handle])
        if window is not None:
            ledgers[account.handle] = ledger._accumulate(
                ledgers[account.handle], window * account.sign()
            )
    closing, closing_missing = _net_worth_on(db, accounts, closing_on, reporting, ledgers)
    mnemonics: dict[str, str] = {}
    postings: list[NetWorthPosting] = []
    missing = [*opening_missing, *closing_missing]
    transfers = 0
    for handle, amount in raw.items():
        if not amount:
            transfers += 1
            continue
        posted_on, description = headers[handle]
        if amount.commodity not in mnemonics:
            commodity = db.get_commodity(amount.commodity)
            mnemonics[amount.commodity] = (
                commodity.mnemonic if commodity is not None else amount.commodity
            )
        effect: Money | None = amount.value
        if amount.commodity != reporting:
            converted = valuation.convert_currency(
                db, amount, as_of=posted_on, currency=reporting
            ).amount
            effect = converted.value if converted is not None else None
            if effect is None:
                missing.append(f"{posted_on.isoformat()} {description}")
        postings.append(
            NetWorthPosting(
                handle,
                posted_on,
                description,
                tuple(sorted(touched[handle], key=str.casefold)),
                effect,
                mnemonics[amount.commodity],
            )
        )
    postings.sort(key=lambda item: (item.posted, item.description.casefold(), item.transaction))
    complete = not missing
    change = closing - opening if opening is not None and closing is not None else None
    posted = (
        sum((item.effect or Money(0) for item in postings), Money(0))
        if all(item.effect is not None for item in postings)
        else None
    )
    revaluation = (
        change - posted if complete and change is not None and posted is not None else None
    )
    return ServiceResult.success(
        NetWorthChange(
            start,
            end,
            opening_on,
            closing_on,
            closing_on < end,
            opening,
            closing,
            change,
            posted,
            revaluation,
            tuple(postings),
            transfers,
            tuple(dict.fromkeys(missing)),
        )
    )


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
    accounts = _balance_sheet_accounts(db)
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
