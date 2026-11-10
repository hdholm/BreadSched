"""Budget jars: every scheduled outflow and savings goal, filled and drawn by date.

A jar is money spoken for. Every scheduled outflow (a bill, or an estimate such as
"about 600 a month on groceries") is a jar for each account it spends from, and
every savings goal is a jar for its account.

* **Filling.** A scheduled occurrence fills its jar from planned income exactly as
  the Dashboard reserves a bill: each income occurrence between the previous
  occurrence and this one (its cycle) sets aside the occurrence's amount times that
  income's share of the cycle's income. Income that was scheduled but never
  arrived fills nothing; future income is expected. With no income in the cycle the
  whole amount is set aside at the start of the cycle. Shares are rounded to the
  currency's fraction with the last taking the remainder, so an occurrence's fills
  add up to its amount. A goal fills as ``savings_goals`` sets money aside (its
  income share and its allocations), measured on real dates.
* **Drawing.** An occurrence is drawn by the actual transaction the Plan matched to
  it, on that transaction's date, for that transaction's amount in the jar's
  account; an unmatched occurrence draws nothing, so its money stays in the jar.
  Every other posting to a jar's account since its first cycle began is spending
  too, as the Plan's category row counts it (#312): it draws the account's jar
  when that is unambiguous (the only jar, or the only estimate, in the account and
  currency), and otherwise an "Unmatched spending" line in the account, so the
  account's actual always equals the Plan's. A refund draws a negative amount. A
  goal is drawn when it is closed (its earmark is released, usually because the
  money was spent).
* **Lapsing.** An estimate is an allowance for its period, from its date to the
  day before its next occurrence. The day after a period ends,
  whatever that period left is released (``lapsed``), and an overspent period is
  brought back to zero the same way, so nothing carries into the next period. What
  income has already set aside for later occurrences stays. A period that ends
  after today lapses, in the report, with what it would hold if nothing more were
  spent. A bill's money stays until it is drawn.
* **Reporting.** Every fill, planned draw, and actual draw is dated. Periods
  (month, quarter, year) only gather those dated events; nothing is spread into
  monthly cells. Jars are bundled by account, and a jar's level at a period's end
  is what it holds then: every fill less every actual draw and lapse since the
  schedule began. What a bill's earlier occurrences left over or overspent is
  carried in, and a bill never matched to an actual keeps its money in the jar until
  it is matched or skipped; an estimate's earlier periods have lapsed. A goal's
  level is its whole earmark.

Amounts are in each jar's currency (the schedule's currency or the reporting
currency, and the goal account's commodity); totals never add currencies together.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta

from ..db.sqlite import DbSQLite
from ..lib.account import Account, AccountClass
from ..lib.money import Money
from ..lib.savings_goal import SavingsGoal
from ..lib.scheduled import ScheduledTransaction
from . import savings_goals
from .activity import ReportingPeriod, reporting_periods
from .cash_flow import income_occurrences, income_schedules
from .chart_model import BARS, ChartModel, ChartSeries
from .currency import reporting_currency_handle, reporting_fraction
from .dashboard_bills import cycle_days
from .plan_allocations import OccurrenceActual
from .planning import PlannedEvent, PlannedSplit, period_end, scheduled_events

__all__ = [
    "Jar",
    "JarAccount",
    "JarEvent",
    "JarKind",
    "JarPeriod",
    "JarsReport",
    "PeriodTotal",
    "budget_jars",
    "currency_labels",
    "jar_charts",
    "jar_kind_label",
]


#: How far past the range to look for occurrences whose cycle begins inside it.
_HORIZON_DAYS = 400


class JarKind:
    BILL = "bill"
    ESTIMATE = "estimate"
    GOAL = "goal"
    #: Spending in an account with several jars that no Review match assigns.
    UNMATCHED = "unmatched"


@dataclass(frozen=True, slots=True)
class JarEvent:
    """One dated change: ``fill``, a ``planned`` draw, an ``actual`` draw, or what an
    estimate's ended period left, ``lapsed`` (negative when it was overspent)."""

    when: date
    kind: str
    amount: Money
    #: The occurrence (schedule key) or goal it belongs to, and a transaction if any.
    occurrence: str = ""
    transaction: str | None = None
    #: An actual that counts toward its occurrence as a whole draws the whole of
    #: its legs; one that allocates part draws only that part.
    whole: bool = True


@dataclass(frozen=True, slots=True)
class JarPeriod:
    start: date
    end: date
    label: str
    filled: Money
    planned: Money
    actual: Money
    level: Money
    #: What estimates' ended periods released (negative: overspending written off).
    lapsed: Money = field(default_factory=lambda: Money(0))

    @property
    def variance(self) -> Money:
        """Actual less planned draws: positive when more was spent than planned."""
        return self.actual - self.planned


@dataclass(frozen=True, slots=True)
class Jar:
    key: str
    kind: str
    name: str
    account: str
    account_name: str
    currency: str | None
    #: Level before the report's first day (fills made earlier for its occurrences).
    opening: Money
    events: tuple[JarEvent, ...]
    periods: tuple[JarPeriod, ...]
    #: The schedule or goal handle.
    source: str = ""
    #: What the jar is filling toward: the next planned draw after the range (the
    #: last one in it when none follows), or a goal's target amount.
    target: Money = field(default_factory=lambda: Money(0))


@dataclass(frozen=True, slots=True)
class JarAccount:
    """The jars of one account and one currency, with their totals by period."""

    account: str
    account_name: str
    currency: str | None
    jars: tuple[Jar, ...]
    periods: tuple[JarPeriod, ...]


@dataclass(frozen=True, slots=True)
class PeriodTotal:
    currency: str | None
    periods: tuple[JarPeriod, ...]


@dataclass(frozen=True, slots=True)
class JarsReport:
    start: date
    end: date
    period: ReportingPeriod
    today: date
    labels: tuple[tuple[date, date, str], ...]
    accounts: tuple[JarAccount, ...]
    totals: tuple[PeriodTotal, ...]
    problems: tuple[str, ...]

    @property
    def jars(self) -> tuple[Jar, ...]:
        return tuple(jar for account in self.accounts for jar in account.jars)


# ------------------------------------------------------------------ scheduled


def _jar_legs(splits: Iterable[PlannedSplit], accounts: dict[str, Account]) -> dict[str, Money]:
    """The legs a scheduled outflow spends into: expense accounts, else non-cash ones."""
    expense: dict[str, Money] = defaultdict(lambda: Money(0))
    other: dict[str, Money] = defaultdict(lambda: Money(0))
    for split in splits:
        account = accounts.get(split.account)
        if account is None or split.amount <= 0:
            continue
        if account.account_class is AccountClass.EXPENSE:
            expense[split.account] += split.amount
        elif not account.is_spendable_cash and account.account_class in (
            AccountClass.LIABILITY,
            AccountClass.ASSET,
        ):
            other[split.account] += split.amount
    return dict(expense) if expense else dict(other)


def _actual_legs(item: OccurrenceActual, accounts: dict[str, Account]) -> dict[str, Money]:
    """What one actual draws from an occurrence's jars: its legs when it counts as a
    whole, else only what it allocates."""
    if item.share == 1:
        legs = [PlannedSplit(split.account, split.value) for split in item.transaction.splits]
    else:
        legs = [PlannedSplit(account, amount) for account, amount in item.allocated]
    return _jar_legs(legs, accounts)


def _cycle_start(schedule: ScheduledTransaction, due: date) -> date:
    days = cycle_days(schedule)
    lookback = timedelta(days=int(days) + 15)
    previous = [
        when
        for when in schedule.recurrence.occurrences(due - timedelta(days=1), since=due - lookback)
        if when not in schedule.skipped
    ]
    return previous[-1] if previous else due - timedelta(days=max(1, int(days)))


def _shares(amount: Money, weights: list[Money], fraction: int) -> list[Money]:
    total = sum(weights, Money(0))
    parts: list[Money] = []
    given = Money(0)
    for index, weight in enumerate(weights):
        if index == len(weights) - 1:
            part = amount - given
        else:
            part = (amount * (weight / total)).quantize(fraction)
        parts.append(part)
        given = given + part
    return parts


def _fills(
    db: DbSQLite,
    schedule: ScheduledTransaction,
    due: date,
    amount: Money,
    incomes: list[ScheduledTransaction],
    today: date,
    fraction: int,
) -> list[tuple[date, Money]]:
    start = _cycle_start(schedule, due)
    received = list(income_occurrences(db, incomes, start, due, today))
    if not received or sum((income for _when, income in received), Money(0)) <= 0:
        return [(start, amount)]
    parts = _shares(amount, [income for _when, income in received], fraction)
    return [(when, part) for (when, _income), part in zip(received, parts, strict=True)]


def _scheduled_jars(
    db: DbSQLite,
    start: date,
    end: date,
    today: date,
    accounts: dict[str, Account],
) -> dict[tuple[str, str], tuple[ScheduledTransaction, str | None, list[JarEvent]]]:
    """Each schedule's jars by (schedule, account), with fills and matched draws."""
    reporting = reporting_currency_handle(db)
    fraction = reporting_fraction(db)
    incomes = income_schedules(db, today)
    schedules = {schedule.handle: schedule for schedule in db.iter_scheduled()}
    jars: dict[tuple[str, str], tuple[ScheduledTransaction, str | None, list[JarEvent]]] = {}
    # Every earlier occurrence counts toward the level the range opens with, so read
    # from the first schedule's start; an occurrence due after the range still fills
    # its jar inside it (a quarterly bill fills from every paycheck of its quarter),
    # so look one year further.
    first = min((schedule.recurrence.start for schedule in schedules.values()), default=start)
    events: list[PlannedEvent] = scheduled_events(
        db, min(first, start), end + timedelta(days=_HORIZON_DAYS)
    )
    for event in events:
        schedule = schedules.get(event.source_handle or "")
        if schedule is None:
            continue
        if event.planned_date > end and _cycle_start(schedule, event.planned_date) > end:
            continue
        legs = _jar_legs(event.expected_splits, accounts)
        if not legs:
            continue
        draws = [(item, _actual_legs(item, accounts)) for item in event.actuals]
        for account, amount in legs.items():
            key = (schedule.handle, account)
            if key not in jars:
                jars[key] = (schedule, schedule.currency or reporting, [])
            found = jars[key][2]
            for when, part in _fills(
                db, schedule, event.planned_date, amount, incomes, today, fraction
            ):
                found.append(JarEvent(when, "fill", part, event.key))
            found.append(JarEvent(event.planned_date, "planned", amount, event.key))
            for item, actual_legs in draws:
                found.append(
                    JarEvent(
                        item.transaction.post_date,
                        "actual",
                        actual_legs.get(account, Money(0)),
                        event.key,
                        item.transaction.handle,
                        item.share == 1,
                    )
                )
    return jars


def _unmatched_draws(
    db: DbSQLite,
    jars: dict[tuple[str, str], tuple[ScheduledTransaction, str | None, list[JarEvent]]],
    end: date,
) -> dict[tuple[str, str | None], list[JarEvent]]:
    """Postings to jar accounts that no matched occurrence drew, by account and currency.

    Only postings since the account's first jar cycle began count, and only in the
    jar's currency; a matched transaction's leg in an account is drawn by its
    occurrence instead.
    """
    reporting = reporting_currency_handle(db)
    begins: dict[tuple[str, str | None], date] = {}
    drawn: dict[tuple[str, str], Money] = defaultdict(lambda: Money(0))
    whole: set[tuple[str, str]] = set()
    for (_handle, account), (_schedule, currency, events) in jars.items():
        fills = [event.when for event in events if event.kind == "fill"]
        if fills:
            key = (account, currency)
            begins[key] = min(min(fills), begins.get(key, fills[0]))
        for event in events:
            if event.kind == "actual" and event.transaction:
                if event.whole:
                    whole.add((event.transaction, account))
                drawn[(event.transaction, account)] += event.amount
    if not begins:
        return {}
    found: dict[tuple[str, str | None], list[JarEvent]] = defaultdict(list)
    first = min(begins.values())
    for transaction in db.iter_transactions(start=first, end=end):
        currency = transaction.currency or reporting
        amounts: dict[str, Money] = defaultdict(lambda: Money(0))
        for split in transaction.splits:
            amounts[split.account] += split.value
        for account, total in amounts.items():
            begin = begins.get((account, currency))
            # A leg an occurrence drew in part leaves the rest unmatched.
            amount = total - drawn.get((transaction.handle, account), Money(0))
            if (
                begin is None
                or transaction.post_date < begin
                or not amount
                or (transaction.handle, account) in whole
            ):
                continue
            found[(account, currency)].append(
                JarEvent(transaction.post_date, "actual", amount, "", transaction.handle)
            )
    return found


def _lapses(
    schedule: ScheduledTransaction, events: list[JarEvent], through: date
) -> list[JarEvent]:
    """What each period of an estimate's jar left, released the day after it ends.

    The release brings the jar to what income has already set aside for the
    occurrences still to come, so a leftover (or an overspend) from the ended
    period, matched or not, never carries forward. A period that ends after today
    lapses with what it holds by then if nothing more is spent, as every later level
    assumes; the report needs none after ``through``.
    """
    planned = sorted((event for event in events if event.kind == "planned"), key=lambda e: e.when)
    filled_for: dict[str, Money] = defaultdict(lambda: Money(0))
    for event in events:
        if event.kind == "fill":
            filled_for[event.occurrence] += event.amount
    actuals = sorted((e for e in events if e.kind == "actual"), key=lambda e: e.when)
    found: list[JarEvent] = []
    # Every fill of an occurrence comes before its date, so on the day a period
    # lapses the level less what later occurrences already hold is the ended
    # occurrences' fills less every draw so far and every earlier release.
    ended = Money(0)
    drawn = Money(0)
    released = Money(0)
    position = 0
    for index, occurrence in enumerate(planned):
        if index + 1 < len(planned):
            lapses_on = planned[index + 1].when
        else:
            lapses_on = period_end(schedule, occurrence.when) + timedelta(days=1)
        if lapses_on > through:
            break
        ended = ended + filled_for.get(occurrence.occurrence, Money(0))
        while position < len(actuals) and actuals[position].when < lapses_on:
            drawn = drawn + actuals[position].amount
            position += 1
        amount = ended - drawn - released
        if amount:
            found.append(JarEvent(lapses_on, "lapsed", amount, occurrence.occurrence))
            released = released + amount
    return found


# ----------------------------------------------------------------------- goals


def _goal_events(
    db: DbSQLite,
    goal: SavingsGoal,
    start: date,
    end: date,
    today: date,
    labels: list[tuple[date, date, str]],
) -> tuple[Money, list[JarEvent]]:
    """A goal's set-aside changes on the dates they happen, and its release on closing.

    The earmark changes on income dates and allocation dates (and day by day when
    no income is expected, so each period's last day is measured too).
    """
    schedules = income_schedules(db, today)

    def held(on: date) -> Money:
        return savings_goals.goal_progress(db, goal, on, schedules=schedules).set_aside

    opening = held(start - timedelta(days=1))
    days = {
        when
        for when, _income in income_occurrences(
            db, schedules, start - timedelta(days=1), end, today
        )
    }
    days.update(item.allocated_on for item in goal.allocations if start <= item.allocated_on <= end)
    days.update(last for _first, last, _label in labels)
    if start <= goal.target_date <= end:
        days.add(goal.target_date)
    closing = (
        goal.closed_on if goal.closed_on is not None and start <= goal.closed_on <= end else None
    )
    found: list[JarEvent] = []
    previous = opening
    for when in sorted(day for day in days if closing is None or day < closing):
        now = held(when)
        if now != previous:
            found.append(JarEvent(when, "fill", now - previous, goal.handle))
            previous = now
    if closing is not None:
        before = held(closing - timedelta(days=1))
        if before != previous:
            found.append(
                JarEvent(closing - timedelta(days=1), "fill", before - previous, goal.handle)
            )
        if before:
            found.append(JarEvent(closing, "actual", before, goal.handle))
    if start <= goal.target_date <= end:
        found.append(JarEvent(goal.target_date, "planned", goal.target_amount, goal.handle))
    return opening, found


# ---------------------------------------------------------------------- report


def _periods(
    opening: Money, events: list[JarEvent], labels: list[tuple[date, date, str]]
) -> tuple[JarPeriod, ...]:
    level = opening
    found: list[JarPeriod] = []
    for first, last, label in labels:
        inside = [event for event in events if first <= event.when <= last]
        filled = sum((e.amount for e in inside if e.kind == "fill"), Money(0))
        planned = sum((e.amount for e in inside if e.kind == "planned"), Money(0))
        actual = sum((e.amount for e in inside if e.kind == "actual"), Money(0))
        lapsed = sum((e.amount for e in inside if e.kind == "lapsed"), Money(0))
        level = level + filled - actual - lapsed
        found.append(JarPeriod(first, last, label, filled, planned, actual, level, lapsed))
    return tuple(found)


def _jar_order(jar: Jar) -> tuple[int, str]:
    """Goals first, then scheduled jars by name, and unmatched spending last."""
    rank = {JarKind.GOAL: 0, JarKind.UNMATCHED: 2}.get(jar.kind, 1)
    return rank, jar.name.casefold()


def _add(periods: Iterable[tuple[JarPeriod, ...]], labels) -> tuple[JarPeriod, ...]:
    rows = list(periods)
    found: list[JarPeriod] = []
    for index, (first, last, label) in enumerate(labels):
        found.append(
            JarPeriod(
                first,
                last,
                label,
                sum((row[index].filled for row in rows), Money(0)),
                sum((row[index].planned for row in rows), Money(0)),
                sum((row[index].actual for row in rows), Money(0)),
                sum((row[index].level for row in rows), Money(0)),
                sum((row[index].lapsed for row in rows), Money(0)),
            )
        )
    return tuple(found)


def budget_jars(
    db: DbSQLite,
    start: date,
    end: date,
    *,
    period: ReportingPeriod = ReportingPeriod.MONTH,
    today: date | None = None,
) -> JarsReport:
    """Every jar's fills and draws from ``start`` through ``end``, by period and account."""
    today = today or date.today()
    labels = reporting_periods(start, end, period)
    accounts = {account.handle: account for account in db.iter_accounts()}
    jars: list[Jar] = []
    problems: list[str] = []

    scheduled = _scheduled_jars(db, start, end, today, accounts)
    unmatched = _unmatched_draws(db, scheduled, end)
    for (account, currency), draws in unmatched.items():
        members = [
            (key, item)
            for key, item in scheduled.items()
            if key[1] == account and item[1] == currency
        ]
        estimates = [(key, item) for key, item in members if item[0].placeholder]
        owner = members if len(members) == 1 else estimates if len(estimates) == 1 else []
        if owner:
            owner[0][1][2].extend(draws)
            continue
        opening = -sum((event.amount for event in draws if event.when < start), Money(0))
        draws.sort(key=lambda item: item.when)
        jars.append(
            Jar(
                f"unmatched:{account}:{currency}",
                JarKind.UNMATCHED,
                "Unmatched spending",
                account,
                db.full_name(account),
                currency,
                opening,
                tuple(draws),
                _periods(opening, draws, labels),
            )
        )

    for (handle, account), (schedule, currency, events) in scheduled.items():
        if schedule.placeholder:
            events.extend(_lapses(schedule, events, end))
        opening = sum(
            (event.amount for event in events if event.when < start and event.kind == "fill"),
            Money(0),
        ) - sum(
            (
                event.amount
                for event in events
                if event.when < start and event.kind in ("actual", "lapsed")
            ),
            Money(0),
        )
        events.sort(key=lambda item: (item.when, item.kind))
        planned = [event for event in events if event.kind == "planned"]
        after = [event for event in planned if event.when > end]
        inside = [event for event in planned if event.when <= end]
        target = after[0].amount if after else (inside[-1].amount if inside else Money(0))
        jars.append(
            Jar(
                f"schedule:{handle}:{account}",
                JarKind.ESTIMATE if schedule.placeholder else JarKind.BILL,
                schedule.description or "Scheduled",
                account,
                db.full_name(account),
                currency,
                opening,
                tuple(events),
                _periods(opening, events, labels),
                handle,
                target,
            )
        )

    for goal in db.iter_savings_goals():
        if goal.closed_on is not None and goal.closed_on < start:
            continue
        goal_account = accounts.get(goal.account)
        if goal_account is None:
            problems.append(f"Goal {goal.name}: its account no longer exists")
            continue
        opening, events = _goal_events(db, goal, start, end, today, labels)
        if not events and not opening:
            continue
        events.sort(key=lambda item: (item.when, item.kind))
        jars.append(
            Jar(
                f"goal:{goal.handle}",
                JarKind.GOAL,
                goal.name,
                goal.account,
                db.full_name(goal_account),
                goal_account.commodity or reporting_currency_handle(db),
                opening,
                tuple(events),
                _periods(opening, events, labels),
                goal.handle,
                goal.target_amount,
            )
        )

    grouped: dict[tuple[str, str | None], list[Jar]] = defaultdict(list)
    for jar in jars:
        grouped[(jar.account, jar.currency)].append(jar)
    bundles = [
        JarAccount(
            account,
            members[0].account_name,
            currency,
            tuple(sorted(members, key=_jar_order)),
            _add((jar.periods for jar in members), labels),
        )
        for (account, currency), members in grouped.items()
    ]
    bundles.sort(key=lambda item: (item.account_name.casefold(), str(item.currency)))
    by_currency: dict[str | None, list[JarAccount]] = defaultdict(list)
    for bundle in bundles:
        by_currency[bundle.currency].append(bundle)
    totals = tuple(
        PeriodTotal(currency, _add((bundle.periods for bundle in members), labels))
        for currency, members in sorted(by_currency.items(), key=lambda item: str(item[0]))
    )
    return JarsReport(
        start, end, period, today, tuple(labels), tuple(bundles), totals, tuple(problems)
    )


_KIND_LABELS = {
    JarKind.BILL: "Bill",
    JarKind.ESTIMATE: "Estimate",
    JarKind.GOAL: "Goal",
    JarKind.UNMATCHED: "Unmatched",
}


def jar_kind_label(kind: str) -> str:
    return _KIND_LABELS.get(kind, kind)


def currency_labels(report: JarsReport, mnemonics: Mapping[str, str]) -> dict[str | None, str]:
    """The mnemonic of each currency the report uses (its handle, or "", when unnamed)."""
    return {
        handle: mnemonics.get(handle, handle) if handle else ""
        for handle in {bundle.currency for bundle in report.accounts}
    }


def jar_charts(report: JarsReport, labels: dict[str | None, str]) -> tuple[ChartModel, ...]:
    """For each account: planned against actual draws by period, and jar levels.

    Planned and actual are paired columns per period (slots 1 and 2); the levels
    chart puts each jar's level at the range's end beside its target (slots 3 and
    4). The values are the report's own, so the tables beside the charts agree.
    """
    charts: list[ChartModel] = []
    period_labels = tuple(label for _first, _last, label in report.labels)
    for bundle in report.accounts:
        currency = labels.get(bundle.currency, "")
        charts.append(
            ChartModel(
                f"jars:{bundle.account}:{bundle.currency}:draws",
                f"{bundle.account_name}: planned and actual",
                BARS,
                period_labels,
                (
                    ChartSeries(
                        "planned", "Planned", tuple(item.planned for item in bundle.periods), 1
                    ),
                    ChartSeries(
                        "actual", "Actual", tuple(item.actual for item in bundle.periods), 2
                    ),
                ),
                currency,
            )
        )
        charts.append(
            ChartModel(
                f"jars:{bundle.account}:{bundle.currency}:levels",
                f"{bundle.account_name}: jar levels",
                BARS,
                tuple(jar.name for jar in bundle.jars),
                (
                    ChartSeries(
                        "level",
                        "Level",
                        tuple(
                            jar.periods[-1].level if jar.periods else Money(0)
                            for jar in bundle.jars
                        ),
                        3,
                    ),
                    ChartSeries("target", "Target", tuple(jar.target for jar in bundle.jars), 4),
                ),
                currency,
            )
        )
    return tuple(charts)
