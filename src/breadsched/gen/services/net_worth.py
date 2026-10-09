"""Read-only net worth history: market-valued assets less debts at each period end."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from ..db.sqlite import DbSQLite
from ..engine import ledger, valuation
from ..engine.activity import ReportingPeriod, reporting_periods
from ..engine.chart_model import LINE, STACKED, ChartMarker, ChartModel, ChartSeries
from ..engine.completeness import Completeness, Excluded, Policy
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
    #: Withheld (unavailable) with the excluded balances when ``missing`` (#236).
    completeness: Completeness = field(default_factory=Completeness)
    groups: tuple[NetWorthLine, ...] = ()


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
    #: Withheld (unavailable) with the excluded balances and postings (#236).
    completeness: Completeness = field(default_factory=Completeness)


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


def _groups(db: DbSQLite, accounts: list[Account], tops: dict[str, str]) -> dict[str, str]:
    """Each account's group: its top-level tree, or a child of a tree holding a whole kind.

    A book usually keeps every asset under one "Assets" account and every debt under
    one "Liabilities" account, so a top-level breakdown says nothing; there the
    group is the top-level account's child that holds the account (or the top-level
    account itself, for anything posted to it directly).
    """
    by_kind: dict[bool, set[str]] = {}
    for account in accounts:
        by_kind.setdefault(account.account_class is AccountClass.ASSET, set()).add(
            tops[account.handle]
        )
    sole = {next(iter(found)) for found in by_kind.values() if len(found) == 1}
    groups: dict[str, str] = {}
    for account in accounts:
        top = tops[account.handle]
        current = account
        if top in sole:
            # Climb to the top-level account's child that holds this account.
            while current.handle != top and current.parent is not None and current.parent != top:
                parent = db.get_account(current.parent)
                if parent is None:
                    break
                current = parent
        groups[account.handle] = current.handle if top in sole else top
    return groups


def _value_on(
    db: DbSQLite,
    accounts: list[Account],
    tops: dict[str, str],
    when: date,
    reporting: str,
    ledgers: dict[str, Amount],
) -> tuple[dict[tuple[str, str], Money | None], list[Excluded]]:
    """Sum complete reporting-currency valuations per (top-level account, kind).

    ``ledgers`` holds each account's ledger balance on ``when``. The second value
    is the evidence for each balance that has no reporting-currency value.
    """
    sums: dict[tuple[str, str], Money | None] = {}
    missing: list[Excluded] = []
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
            missing.append(valuation.excluded_valuation(db, account, valued, when))
            sums[key] = None
        else:
            current = sums[key]
            if current is not None:
                sums[key] = current + amount.value
    return sums, missing


def _missing_names(items: list[Excluded]) -> list[str]:
    """The legacy ``missing`` names: account names, or "date description" postings."""
    return [
        f"{item.when.isoformat()} {item.label}"
        if item.kind == "posting" and item.when is not None
        else item.label
        for item in items
    ]


def _balance_sheet_accounts(db: DbSQLite) -> list[Account]:
    return [
        account
        for account in db.iter_accounts()
        if not account.is_root
        and account.account_class in {AccountClass.ASSET, AccountClass.LIABILITY}
    ]


def _net_worth_on(
    db: DbSQLite, accounts: list[Account], when: date, reporting: str, ledgers: dict[str, Amount]
) -> tuple[Money | None, list[Excluded]]:
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
                missing.append(
                    Excluded(
                        "posting",
                        description or "Untitled",
                        mnemonics[amount.commodity],
                        amount.value,
                        posted_on,
                    )
                )
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
            tuple(dict.fromkeys(_missing_names(missing))),
            Completeness.of(dict.fromkeys(missing), policy=Policy.WITHHOLD, as_of=closing_on),
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
    assets less debts, exactly as on the Dashboard. Each point breaks its totals
    down by top-level account (``lines``) and by group (``groups``, see ``_groups``).
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
    groups = _groups(db, accounts, tops)
    names = {handle: db.full_name(handle) for handle in {*tops.values(), *groups.values()}}
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
        by_group, missing = _value_on(db, accounts, groups, valued_on, reporting, ledgers)
        # Each top-level line is the exact sum of its groups (None if any is).
        sums: dict[tuple[str, str], Money | None] = {}
        group_top = {groups[handle]: tops[handle] for handle in groups}
        for (group, kind), value in by_group.items():
            key = (group_top[group], kind)
            current = sums.get(key, Money(0))
            sums[key] = None if current is None or value is None else current + value

        def breakdown(values: dict[tuple[str, str], Money | None]) -> tuple[NetWorthLine, ...]:
            return tuple(
                NetWorthLine(handle, names[handle], kind, value)
                for (handle, kind), value in sorted(
                    values.items(), key=lambda item: (item[0][1], names[item[0][0]].casefold())
                )
                if value != Money(0)
            )

        lines = breakdown(sums)
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
                tuple(dict.fromkeys(_missing_names(missing))),
                lines,
                Completeness.of(dict.fromkeys(missing), policy=Policy.WITHHOLD, as_of=valued_on),
                breakdown(by_group),
            )
        )
        previous = net
    return ServiceResult.success(NetWorthHistory(start, end, grouping, as_of, tuple(points)))


#: The most groups a composition chart names; the rest share one "Other".
_NAMED_GROUPS = 7


def net_worth_charts(history: NetWorthHistory, currency: str = "") -> tuple[ChartModel, ...]:
    """Net worth over the history and its composition, from the points' own values.

    The first chart draws assets, debts, and net worth as lines (slots 1 to 3); a
    point with a missing quote breaks each line rather than being drawn. The second
    stacks each group's value for every point, assets up from zero and debts down,
    so a column's segments sum to that point's net worth (its ``totals``). Groups
    are ranked by their largest value; past the seventh, the rest are combined as
    "Other". A partial point (valued to date) is marked with an as-of rule.
    """
    points = history.points
    if not points:
        return ()
    labels = tuple(point.label for point in points)
    markers = tuple(
        ChartMarker(index, f"As of {point.valued_on.isoformat()}")
        for index, point in enumerate(points)
        if point.partial
    )
    withheld = [point.label for point in points if point.net_worth is None]
    note = (
        f"Missing quote, so left out: {', '.join(withheld)}. The table names the accounts."
        if withheld
        else ""
    )
    lines = ChartModel(
        "net_worth",
        "Assets, debts, and net worth",
        LINE,
        labels,
        (
            ChartSeries("assets", "Assets", tuple(point.assets for point in points), 1),
            ChartSeries("debts", "Debts", tuple(point.debts for point in points), 2),
            ChartSeries("net_worth", "Net worth", tuple(point.net_worth for point in points), 3),
        ),
        currency,
        markers,
        partial_note=note,
    )
    # A group's signed value per point: assets up, debts down; None where withheld.
    keys: dict[tuple[str, str], str] = {}
    signed: dict[tuple[str, str], list[Money | None]] = {}
    for index, point in enumerate(points):
        for line in point.groups:
            key = (line.account, line.kind)
            keys[key] = line.name if line.kind == "asset" else f"{line.name} (debt)"
            values = signed.setdefault(key, [Money(0)] * len(points))
            values[index] = (
                None if line.value is None else line.value if line.kind == "asset" else -line.value
            )
    for values in signed.values():
        for index, point in enumerate(points):
            if point.net_worth is None:
                values[index] = None
    largest = {
        key: max((abs(value) for value in values if value is not None), default=Money(0))
        for key, values in signed.items()
    }
    ranked = sorted(signed, key=lambda key: (key[1] != "asset", -largest[key], keys[key]))
    ordered = sorted(ranked, key=lambda key: -largest[key])
    named = set(ordered if len(ordered) <= _NAMED_GROUPS + 1 else ordered[:_NAMED_GROUPS])
    series = [
        ChartSeries(f"{key[1]}:{key[0]}", keys[key], tuple(signed[key]), slot)
        for slot, key in enumerate((key for key in ranked if key in named), start=1)
    ]
    rest = [key for key in ranked if key not in named]
    if rest:
        series.append(
            ChartSeries(
                "other",
                "Other",
                tuple(
                    None
                    if point.net_worth is None
                    else sum((signed[key][index] or Money(0) for key in rest), Money(0))
                    for index, point in enumerate(points)
                ),
                _NAMED_GROUPS + 1,
            )
        )
    for index, point in enumerate(points):
        if point.net_worth is not None and point.net_worth != sum(
            (item.values[index] or Money(0) for item in series), Money(0)
        ):
            raise AssertionError("net worth groups do not reconcile to net worth")
    composition = ChartModel(
        "net_worth_composition",
        "Net worth by group",
        STACKED,
        labels,
        tuple(series),
        currency,
        markers=(),
        partial_note=note,
        totals=tuple(point.net_worth for point in points),
    )
    return lines, composition
