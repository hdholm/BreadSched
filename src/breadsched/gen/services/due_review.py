"""Typed review of scheduled occurrences that are due or missed.

Every presentation lists the same due occurrences, grouped by schedule, and
applies one batch of decisions through :func:`resolve_due`:

* **post** — the occurrence happened; write its transaction;
* **skip** — it did not happen or was recorded another way; mark it dealt with;
* **defer** — undecided; leave it due and ask again.

Besides what is due and missed, the review lists each schedule's occurrences
within its *enter in advance* or *remind in advance* days, marked as such (#352);
like any other they stay undecided until chosen. :func:`resolve_next` enters or skips
a schedule's next occurrence however far ahead it is (#351), through the same
validated write.

A decision names a schedule and a date rather than carrying a prepared
transaction. The batch is revalidated against the book immediately before
writing, so an occurrence posted or skipped elsewhere since the list was shown
(another window, the browser, the command line) is refused instead of being
posted twice. Plan-only estimates are never due and so are never posted. The
whole batch commits as one undoable database transaction, or nothing is written.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum

from ..db.sqlite import DbSQLite
from ..engine import schedule as schedule_engine
from ..engine.currency import reporting_currency_handle
from ..engine.plan_allocations import account_classes, allocate_posted
from ..lib.money import Money
from ..lib.scheduled import ScheduledTransaction
from ..lib.transaction import UnbalancedError
from .contracts import ServiceError, ServiceResult
from .quotes import QuoteRequest, quote_requests

__all__ = [
    "DueDecision",
    "DueQuoteNeeds",
    "DueQuotes",
    "LegPrice",
    "LegQuote",
    "due_quotes",
    "DueItem",
    "NextOccurrence",
    "PostDue",
    "PostedDue",
    "PostedOccurrence",
    "post_due",
    "ResolveNext",
    "next_occurrence",
    "resolve_next",
    "DueResolution",
    "DueScheduleReview",
    "ResolveDue",
    "pending_due_review",
    "resolve_due",
]


class DueDecision(str, Enum):
    POST = "post"
    SKIP = "skip"
    DEFER = "defer"


@dataclass(frozen=True, slots=True)
class DueItem:
    """One due or missed occurrence with its resolved amount."""

    schedule: str
    when: date
    amount: Money
    overdue: bool
    #: Why it is listed: ``overdue``, ``due``, ``advance`` (enter in advance), or
    #: ``reminder`` (remind in advance).
    stage: str = "due"


@dataclass(frozen=True, slots=True)
class DueScheduleReview:
    """Every due occurrence of one schedule, oldest first."""

    schedule: str
    name: str
    frequency: str
    items: tuple[DueItem, ...]

    @property
    def total(self) -> Money:
        return sum((item.amount for item in self.items), Money(0))


@dataclass(frozen=True, slots=True)
class LegPrice:
    """The share price, or the number of shares, given for one leg (#417).

    Exactly one of ``price`` (the commodity's value in the transaction's
    currency) and ``shares`` is given. The price, or the one ``shares`` implies, is
    saved as a dated manual quote in the same undoable change that enters it.
    """

    schedule: str
    when: date
    account: str
    price: Money | None = None
    shares: Money | None = None


@dataclass(frozen=True, slots=True)
class ResolveDue:
    decisions: tuple[tuple[str, date, DueDecision], ...]
    as_of: date | None = None
    prices: tuple[LegPrice, ...] = ()


@dataclass(frozen=True, slots=True)
class DueResolution:
    posted: int
    skipped: int
    deferred: int
    transactions: tuple[str, ...]


def _due_now(
    db: DbSQLite, as_of: date
) -> dict[tuple[str, date], tuple[schedule_engine.Occurrence, schedule_engine.ReviewStage]]:
    return {
        (occurrence.schedule.handle, occurrence.when): (occurrence, stage)
        for occurrence, stage in schedule_engine.review_occurrences(db, as_of=as_of)
    }


def pending_due_review(db: DbSQLite, as_of: date | None = None) -> list[DueScheduleReview]:
    """Due and missed occurrences through ``as_of``, and those its schedules
    enter or remind in advance, grouped by schedule."""
    today = as_of or date.today()
    grouped: dict[str, list[tuple[schedule_engine.Occurrence, str]]] = {}
    for occurrence, stage in _due_now(db, today).values():
        grouped.setdefault(occurrence.schedule.handle, []).append((occurrence, stage.value))
    reviews = []
    for handle, occurrences in grouped.items():
        occurrences.sort(key=lambda item: item[0].when)
        definition = occurrences[0][0].schedule
        reviews.append(
            DueScheduleReview(
                schedule=handle,
                name=definition.name,
                frequency=definition.recurrence.describe(),
                items=tuple(
                    DueItem(handle, item.when, item.amount, item.when < today, stage)
                    for item, stage in occurrences
                ),
            )
        )
    reviews.sort(key=lambda review: (review.items[0].when, review.name.casefold()))
    return reviews


def resolve_due(db: DbSQLite, request: ResolveDue) -> ServiceResult[DueResolution]:
    """Apply one reviewed batch of post, skip, and defer decisions atomically."""
    today = request.as_of or date.today()
    due = _due_now(db, today)
    errors: list[ServiceError] = []
    seen: set[tuple[str, date]] = set()
    for handle, when, _decision in request.decisions:
        key = (handle, when)
        field = f"{handle}@{when.isoformat()}"
        if key in seen:
            errors.append(ServiceError("schedule.due.duplicate", ("decisions", field)))
            continue
        seen.add(key)
        if key not in due:
            code = (
                "schedule.due.not_found"
                if db.get_scheduled(handle) is None
                else "schedule.due.not_pending"
            )
            errors.append(ServiceError(code, ("decisions", field)))
    if errors:
        return ServiceResult.failure(*errors)
    occurrences = {key: occurrence for key, (occurrence, _stage) in due.items()}
    return _apply(
        db, occurrences, request.decisions, "Review due scheduled transactions", request.prices
    )


def _apply(
    db: DbSQLite,
    due: dict[tuple[str, date], schedule_engine.Occurrence],
    decisions: tuple[tuple[str, date, DueDecision], ...],
    message: str,
    prices: tuple[LegPrice, ...] = (),
) -> ServiceResult[DueResolution]:
    """Write validated decisions as one undoable change."""
    given, errors = _given_prices(db, due, decisions, prices)
    if errors:
        return ServiceResult.failure(*errors)
    posted: list[str] = []
    skipped = deferred = 0
    touched: dict[str, ScheduledTransaction] = {}
    class_of = account_classes(db)
    try:
        with db.transaction(message) as txn:
            for handle, when, decision in decisions:
                if decision is DueDecision.DEFER:
                    deferred += 1
                    continue
                occurrence = due[(handle, when)]
                definition = touched.setdefault(handle, occurrence.schedule)
                if decision is DueDecision.SKIP:
                    definition.skip(when)
                    skipped += 1
                    continue
                legs = given.get((handle, when), {})
                for typed in legs.values():
                    schedule_engine.record_leg_price(
                        db, txn, typed.commodity, typed.currency, when, typed.price
                    )
                real = schedule_engine.entered_transaction(db, definition, when)
                for split in real.splits:
                    shares = legs[split.account].shares if split.account in legs else None
                    if shares is not None:
                        # The shares typed are what was bought, exactly.
                        split.quantity = shares if split.value >= 0 else -shares
                allocate_posted(real, class_of)
                db.add_transaction(real, txn)
                posted.append(real.handle)
                if definition.last_posted is None or when > definition.last_posted:
                    definition.last_posted = when
            for definition in touched.values():
                db.commit_scheduled(definition, txn)
    except UnbalancedError:
        return ServiceResult.failure(ServiceError("schedule.due.unbalanced", ("decisions",)))
    except schedule_engine.UnpricedLegError as unpriced:
        # The fields name the occurrence, then the commodity that has no quote.
        commodity = db.get_commodity(unpriced.commodity)
        return ServiceResult.failure(
            ServiceError(
                "schedule.due.no_quote",
                (
                    "decisions",
                    f"{unpriced.schedule}@{unpriced.when.isoformat()}",
                    commodity.mnemonic if commodity is not None else unpriced.commodity,
                ),
            )
        )
    return ServiceResult.success(DueResolution(len(posted), skipped, deferred, tuple(posted)))


@dataclass(frozen=True, slots=True)
class _Given:
    commodity: str
    currency: str
    price: Money
    shares: Money | None


def _given_prices(
    db: DbSQLite,
    due: dict[tuple[str, date], schedule_engine.Occurrence],
    decisions: tuple[tuple[str, date, DueDecision], ...],
    prices: tuple[LegPrice, ...],
) -> tuple[dict[tuple[str, date], dict[str, _Given]], list[ServiceError]]:
    """Check each given price names a priced leg of an occurrence being entered."""
    entering = {
        (handle, when) for handle, when, decision in decisions if decision is DueDecision.POST
    }
    given: dict[tuple[str, date], dict[str, _Given]] = {}
    errors: list[ServiceError] = []
    for item in prices:
        key = (item.schedule, item.when)
        field = f"{item.schedule}@{item.when.isoformat()}"
        amount = item.price if item.price is not None else item.shares
        if (item.price is None) == (item.shares is None) or amount is None or amount <= 0:
            errors.append(ServiceError("schedule.due.price.invalid", ("prices", field)))
            continue
        occurrence = due.get(key)
        if key not in entering or occurrence is None:
            errors.append(ServiceError("schedule.due.price.not_entered", ("prices", field)))
            continue
        draft = occurrence.schedule.instantiate(item.when, strict=False)
        leg = next(
            (
                found
                for found in schedule_engine.leg_pricings(db, draft)
                if found.split.account == item.account
            ),
            None,
        )
        if leg is None or not leg.split.value or item.account in given.get(key, {}):
            errors.append(ServiceError("schedule.due.price.leg", ("prices", field)))
            continue
        price = (
            item.price
            if item.price is not None
            else Money(
                abs(leg.split.value.numerator) * item.shares.denominator,  # type: ignore[union-attr]
                leg.split.value.denominator * item.shares.numerator,  # type: ignore[union-attr]
            )
        )
        given.setdefault(key, {})[item.account] = _Given(
            leg.commodity, leg.currency, price, item.shares
        )
    return given, errors


@dataclass(frozen=True, slots=True)
class LegQuote:
    """One leg of an occurrence to enter that buys or sells shares (#417).

    ``price`` is the latest stored quote on or before ``when`` (``None`` when
    there is none), and ``shares`` what the leg's value buys at it.
    """

    schedule: str
    when: date
    name: str
    account: str
    account_name: str
    commodity: str
    mnemonic: str
    #: The transaction currency's mnemonic, which ``price`` is in.
    currency: str
    value: Money
    price: Money | None
    price_date: date | None
    price_source: str
    shares: Money | None
    #: The commodity's online quote source, or empty.
    quote_source: str


@dataclass(frozen=True, slots=True)
class DueQuotes:
    """The occurrences about to be entered, as (schedule, date) pairs."""

    occurrences: tuple[tuple[str, date], ...]
    as_of: date | None = None


@dataclass(frozen=True, slots=True)
class DueQuoteNeeds:
    """Each leg of those occurrences that buys or sells shares, and the online
    quotes worth fetching first: those whose stored quote is older than the
    occurrence (or than today, for one ahead of today)."""

    legs: tuple[LegQuote, ...]
    requests: tuple[QuoteRequest, ...]
    #: The reporting currency's mnemonic, which a fetch asks quotes in.
    currency: str | None

    @property
    def unpriced(self) -> tuple[LegQuote, ...]:
        """The legs with no quote at all: a price or shares must be given."""
        return tuple(item for item in self.legs if item.price is None)


def due_quotes(db: DbSQLite, request: DueQuotes) -> DueQuoteNeeds:
    """What entering these occurrences needs to price their shares (#417)."""
    today = request.as_of or date.today()
    online = {item.commodity: item for item in quote_requests(db)}
    legs: list[LegQuote] = []
    wanted: dict[str, QuoteRequest] = {}
    for handle, when in request.occurrences:
        definition = db.get_scheduled(handle)
        if definition is None:
            continue
        draft = definition.instantiate(when, strict=False)
        for leg in schedule_engine.leg_pricings(db, draft):
            if not leg.split.value:
                continue
            commodity = db.get_commodity(leg.commodity)
            currency = db.get_commodity(leg.currency)
            quote = leg.quote
            fetch = online.get(leg.commodity)
            if fetch is not None and (quote is None or quote.quote_date < min(when, today)):
                wanted[leg.commodity] = fetch
            legs.append(
                LegQuote(
                    handle,
                    when,
                    definition.name,
                    leg.split.account,
                    db.full_name(leg.split.account),
                    leg.commodity,
                    commodity.mnemonic if commodity is not None else leg.commodity,
                    currency.mnemonic if currency is not None else leg.currency,
                    leg.split.value,
                    leg.price,
                    quote.quote_date if quote is not None else None,
                    quote.source if quote is not None else "",
                    abs(leg.units) if leg.units is not None else None,
                    commodity.quote_source if commodity is not None else "",
                )
            )
    plan_currency = db.get_commodity(reporting_currency_handle(db))
    return DueQuoteNeeds(
        tuple(legs),
        tuple(sorted(wanted.values(), key=lambda item: (item.source, item.symbol))),
        plan_currency.mnemonic if plan_currency is not None else None,
    )


@dataclass(frozen=True, slots=True)
class NextOccurrence:
    """A schedule's next occurrence still to enter or skip (#351)."""

    schedule: str
    name: str
    when: date
    amount: Money


@dataclass(frozen=True, slots=True)
class ResolveNext:
    """Enter or skip ``schedule``'s next occurrence, which must still be ``when``."""

    schedule: str
    decision: DueDecision
    #: The date the user was shown; refused if the next occurrence has changed.
    when: date | None = None
    as_of: date | None = None
    prices: tuple[LegPrice, ...] = ()


def next_occurrence(
    db: DbSQLite, schedule: str, as_of: date | None = None
) -> NextOccurrence | None:
    """The schedule's next occurrence still to enter or skip, however far ahead."""
    definition = db.get_scheduled(schedule)
    if definition is None:
        return None
    occurrence = schedule_engine.next_pending(db, definition, as_of=as_of)
    if occurrence is None:
        return None
    return NextOccurrence(schedule, definition.name, occurrence.when, occurrence.amount)


def resolve_next(db: DbSQLite, request: ResolveNext) -> ServiceResult[DueResolution]:
    """Enter or skip a schedule's next occurrence before it is due (#351)."""
    definition = db.get_scheduled(request.schedule)
    if definition is None:
        return ServiceResult.failure(ServiceError("schedule.due.not_found", ("schedule",)))
    if request.decision is DueDecision.DEFER:
        return ServiceResult.failure(ServiceError("schedule.next.decision", ("decision",)))
    occurrence = schedule_engine.next_pending(db, definition, as_of=request.as_of)
    if occurrence is None:
        return ServiceResult.failure(ServiceError("schedule.next.none", ("schedule",)))
    if request.when is not None and occurrence.when != request.when:
        return ServiceResult.failure(ServiceError("schedule.due.not_pending", ("when",)))
    verb = "Enter" if request.decision is DueDecision.POST else "Skip"
    return _apply(
        db,
        {(definition.handle, occurrence.when): occurrence},
        ((definition.handle, occurrence.when, request.decision),),
        f"{verb} next occurrence of {definition.name}",
        request.prices,
    )


@dataclass(frozen=True, slots=True)
class PostDue:
    """Post what is due without a review: automatic schedules only, or every one.

    Plan-only estimates are never posted. ``as_of`` defaults to today.
    """

    as_of: date | None = None
    only_automatic: bool = True


@dataclass(frozen=True, slots=True)
class PostedOccurrence:
    transaction: str
    schedule: str
    when: date
    description: str


@dataclass(frozen=True, slots=True)
class PostedDue:
    posted: tuple[PostedOccurrence, ...]


def post_due(db: DbSQLite, request: PostDue) -> ServiceResult[PostedDue]:
    """Post every due occurrence the request allows as one undoable change."""
    try:
        written = schedule_engine.post_due(
            db, as_of=request.as_of, only_auto=request.only_automatic
        )
    except UnbalancedError:
        return ServiceResult.failure(ServiceError("schedule.due.unbalanced", ("schedule",)))
    return ServiceResult.success(
        PostedDue(
            tuple(
                PostedOccurrence(
                    txn.handle, txn.scheduled_from or "", txn.post_date, txn.description
                )
                for txn in written
            )
        )
    )
