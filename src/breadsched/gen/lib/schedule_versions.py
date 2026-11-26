"""Versions of a scheduled transaction: the whole transaction as it is from a date on,
on one date, or in a calendar month every year.

A schedule's own legs are its *template*. A **version** is the whole transaction
again, every leg with its account, a fixed amount or a formula, and its memo:

* ``from`` a date: from that occurrence on, until a later ``from`` version;
* ``once`` on a date: that one occurrence only;
* ``month``: every year in that calendar month.

Versions are **layered**. The template is the bottom layer; the ``from`` version in
force (the latest starting on or before the occurrence) replaces it; the version for
the occurrence's month (if any) replaces that; and a ``once`` version for the date
replaces that. Each version's formulas may use ``base``, the same leg's amount in the
layer below (matched by account, in order; a leg new in the version has base 0), and
``base_total``, the layer below's total (its positive legs, the amount the
transaction moves). When nothing below moves money (``base_total`` is 0), a formula
dividing by it keeps the leg's ``base``, so a scaled version never invents an
amount. So December's version can be ``base * 1.2`` and keep applying
after a raise, and ``base * 650 / base_total`` scales the whole transaction to 650.

A version may add a leg (an account the layer below lacks), leave one out (removing
it for those occurrences), or change any leg's amount or formula; each version must
balance, as the template must.

Books written before schema 16 kept four narrower kinds of change, which
:func:`versions_from_legacy` turns into versions with the same amounts.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import TYPE_CHECKING, Any

from .formula import FormulaError, evaluate
from .money import Money

if TYPE_CHECKING:
    from .scheduled import ScheduledSplit

__all__ = [
    "BASE",
    "BASE_TOTAL",
    "MONTH_NAMES",
    "ResolvedLeg",
    "ScheduledVersion",
    "VersionKind",
    "first_in_month",
    "layered_legs",
    "starting_version",
    "versions_from_legacy",
]

#: The formula variables a version's legs may use.
BASE = "base"
BASE_TOTAL = "base_total"

MONTH_NAMES = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


class VersionKind(str, Enum):
    FROM = "from"
    ONCE = "once"
    MONTH = "month"


class ScheduledVersion:
    """The whole transaction for some occurrences: from a date on, on one date, or in
    one calendar month every year."""

    __slots__ = ("kind", "start", "month", "splits")

    def __init__(
        self,
        kind: VersionKind | str,
        splits: Sequence[ScheduledSplit],
        *,
        start: date | None = None,
        month: int | None = None,
    ) -> None:
        self.kind = VersionKind(kind)
        if self.kind is VersionKind.MONTH:
            if month is None or not 1 <= month <= 12:
                raise ValueError("a month version needs a month from 1 to 12")
            start = None
        else:
            if start is None:
                raise ValueError("a dated version needs its date")
            month = None
        self.start = start
        self.month = month
        self.splits = list(splits)

    @classmethod
    def moving(
        cls,
        kind: VersionKind | str,
        legs: Sequence[ScheduledSplit],
        amount: Money,
        *,
        start: date | None = None,
        month: int | None = None,
    ) -> ScheduledVersion:
        """The transaction below scaled so it moves ``amount``: each of ``legs``'
        accounts as ``base * amount / base_total``, in its own column."""
        from .scheduled import ScheduledSplit

        return cls(
            kind,
            [
                ScheduledSplit(
                    split.account,
                    formula=_scaled_formula(amount),
                    memo=split.memo,
                    planning_flow=split.planning_flow,
                    investment_activity=split.investment_activity,
                    direction=-1 if split.amount is not None and split.amount < 0 else 1,
                )
                for split in legs
            ],
            start=start,
            month=month,
        )

    def applies(self, when: date) -> bool:
        if self.kind is VersionKind.MONTH:
            return when.month == self.month
        assert self.start is not None
        if self.kind is VersionKind.ONCE:
            return when == self.start
        return self.start <= when

    def label(self) -> str:
        """How the version is named in explanations: "from 2026-06-01" and so on."""
        if self.kind is VersionKind.MONTH:
            assert self.month is not None
            return f"every {MONTH_NAMES[self.month - 1]}"
        assert self.start is not None
        if self.kind is VersionKind.ONCE:
            return f"on {self.start.isoformat()} only"
        return f"from {self.start.isoformat()}"

    def sort_key(self) -> tuple:
        order = {VersionKind.FROM: 0, VersionKind.MONTH: 1, VersionKind.ONCE: 2}[self.kind]
        return (order, self.start or date.min, self.month or 0)

    def copy(self) -> ScheduledVersion:
        from .scheduled import ScheduledSplit

        return ScheduledVersion(
            self.kind,
            [ScheduledSplit.from_dict(split.serialize()) for split in self.splits],
            start=self.start,
            month=self.month,
        )

    def serialize(self) -> dict[str, Any]:
        data: dict[str, Any] = {"kind": self.kind.value}
        if self.kind is VersionKind.MONTH:
            data["month"] = self.month
        else:
            assert self.start is not None
            data["start"] = self.start.isoformat()
        data["splits"] = [split.serialize() for split in self.splits]
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ScheduledVersion:
        from .scheduled import ScheduledSplit

        start = data.get("start")
        return cls(
            data["kind"],
            [ScheduledSplit.from_dict(item) for item in data.get("splits", [])],
            start=date.fromisoformat(start) if start else None,
            month=int(data["month"]) if data.get("month") is not None else None,
        )

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ScheduledVersion) and self.serialize() == other.serialize()

    def __repr__(self) -> str:
        return f"<ScheduledVersion {self.label()} ({len(self.splits)} legs)>"


def sorted_versions(versions: Iterable[ScheduledVersion]) -> list[ScheduledVersion]:
    return sorted(versions, key=ScheduledVersion.sort_key)


@dataclass(frozen=True)
class ResolvedLeg:
    """One leg of an occurrence: its definition, its amount, and where that came from."""

    split: ScheduledSplit
    value: Money
    source: str

    @property
    def account(self) -> str:
        return self.split.account


def _positive_total(legs: Sequence[ResolvedLeg]) -> Money:
    total = Money(0)
    for leg in legs:
        if leg.value > 0:
            total = total + leg.value
    return total


def _apply(
    version: ScheduledVersion, below: Sequence[ResolvedLeg], context: Mapping[str, Any]
) -> list[ResolvedLeg]:
    """``version``'s legs, each with ``base`` taken from the matching leg below."""
    unused = list(below)
    total = _positive_total(below)
    legs = []
    for split in version.splits:
        match = next((leg for leg in unused if leg.account == split.account), None)
        if match is not None:
            unused.remove(match)
        base = match.value if match is not None else Money(0)
        variables = dict(context)
        variables[BASE] = base
        variables[BASE_TOTAL] = total
        source = f"{'formula' if split.formula else 'amount'} {version.label()}"
        if split.formula and not total and BASE_TOTAL in split.formula:
            # Nothing below moves money, so there is nothing to scale: as the
            # schedules written before schema 16 did, the leg keeps its base.
            try:
                value = Money(evaluate(split.formula, variables))
            except FormulaError:
                legs.append(ResolvedLeg(split, base, f"{source} (nothing below to scale)"))
                continue
            legs.append(ResolvedLeg(split, value, source))
            continue
        legs.append(ResolvedLeg(split, split.resolve(variables), source))
    return legs


def layered_legs(
    splits: Sequence[ScheduledSplit],
    versions: Sequence[ScheduledVersion],
    context: Mapping[str, Any],
    when: date | None,
) -> list[ResolvedLeg]:
    """The legs of the occurrence on ``when``: the template, then the ``from``
    version in force, the month's version, and the date's ``once`` version, each
    layered on the one below (see the module's description). Without ``when``, the
    template alone."""
    template_context = dict(context)
    template_context.setdefault(BASE, Money(0))
    template_context.setdefault(BASE_TOTAL, Money(0))
    layer = [
        ResolvedLeg(
            split,
            split.resolve(template_context),
            "formula expression" if split.formula else "fixed template amount",
        )
        for split in splits
    ]
    if when is None or not versions:
        return layer
    dated = [item for item in versions if item.kind is VersionKind.FROM and item.applies(when)]
    if dated:
        layer = _apply(max(dated, key=lambda item: item.start or date.min), layer, context)
    for kind in (VersionKind.MONTH, VersionKind.ONCE):
        found = next((item for item in versions if item.kind is kind and item.applies(when)), None)
        if found is not None:
            layer = _apply(found, layer, context)
    return layer


# --------------------------------------------------------------- new versions


def first_in_month(recurrence, month: int) -> date | None:
    """The schedule's first occurrence in ``month`` within ten years of its start."""
    start = recurrence.start
    until = date(min(start.year + 10, 9999), 12, 31)
    return next((when for when in recurrence.occurrences(until) if when.month == month), None)


def starting_version(
    schedule, kind: VersionKind, start: date | None = None, month: int | None = None
) -> ScheduledVersion:
    """A new version of ``kind`` filled in from what ``schedule`` then is.

    ``schedule`` is a ``ScheduledTransaction`` or ``ScenarioSchedule`` holding the
    versions already chosen. A version **from** a date starts as every leg in force
    the day before, as it is defined there (a formula stays a formula); a **month**
    version starts as every leg ``base * 1.0`` in its own direction, so the month is
    the usual transaction until a factor is typed; a version **once** starts as that
    occurrence's legs as amounts. Every editor (desktop, browser, command line)
    starts a version this way.
    """
    from .scheduled import ScheduledSplit

    if kind is VersionKind.MONTH:
        if month is None:
            raise ValueError("a month version needs its month")
        when = first_in_month(schedule.recurrence, month)
    else:
        if start is None:
            raise ValueError("a dated version needs its date")
        when = start
    if kind is VersionKind.ONCE:
        legs = [
            ScheduledSplit(
                leg.account,
                leg.value.quantize(100),
                memo=leg.split.memo,
                planning_flow=leg.split.planning_flow,
                investment_activity=leg.split.investment_activity,
            )
            for leg in schedule.legs_at(when)
        ]
        return ScheduledVersion(kind, legs, start=start)
    dated = [item for item in schedule.versions if item.kind is VersionKind.FROM]
    try:
        context = schedule.context(when) if when is not None else dict(schedule.variables)
    except ValueError:
        context = dict(schedule.variables)
    as_of = when
    if kind is VersionKind.FROM and when is not None:
        as_of = date.fromordinal(when.toordinal() - 1)
    below = layered_legs(schedule.splits, dated, context, as_of)
    if kind is VersionKind.FROM:
        legs = [ScheduledSplit.from_dict(leg.split.serialize()) for leg in below]
        return ScheduledVersion(kind, legs, start=start)
    legs = [
        ScheduledSplit(
            leg.account,
            formula=f"{BASE} * 1.0",
            memo=leg.split.memo,
            planning_flow=leg.split.planning_flow,
            investment_activity=leg.split.investment_activity,
            direction=-1 if leg.value < 0 else 1,
        )
        for leg in below
    ]
    return ScheduledVersion(kind, legs, month=month)


# ------------------------------------------------------------------ legacy changes


def _money(data: Any) -> Money:
    return Money(*data) if isinstance(data, list | tuple) else Money(data)


def _scaled_formula(amount: Money) -> str:
    """The formula that scales the layer below so the transaction moves ``amount``."""
    return f"{BASE} * {format(amount.rate(), 'f')} / {BASE_TOTAL}"


def versions_from_legacy(
    split_data: Sequence[Mapping[str, Any]],
    amount_changes: Sequence[Mapping[str, Any]] = (),
    seasonal_amounts: Sequence[Mapping[str, Any]] = (),
    occurrence_adjustments: Sequence[Mapping[str, Any]] = (),
) -> list[ScheduledVersion]:
    """Versions with the amounts a schedule stored before schema 16 produced.

    Before schema 16 a schedule kept a whole-transaction amount from a date
    (``amount_changes``), each fixed leg's amount from a date (the legs'
    ``amount_changes``), a whole-transaction amount for a calendar month
    (``seasonal_amounts``), and a whole-transaction amount for one date
    (``occurrence_adjustments``). Each whole-transaction amount scaled every leg so
    the transaction moved that amount; a month's applied only while no
    whole-transaction amount was in force, and a date's always.

    * Each date where a leg's or the transaction's amount changed becomes a ``from``
      version: each leg as the template had it, with its own amount then in force
      (a formula leg kept its formula, which per-leg amounts never changed), scaled
      to the whole-transaction amount then in force. With fixed legs the amounts are
      exact; a formula leg under a whole-transaction amount takes its amount on
      that date.
    * Each month becomes a ``month`` version whose legs are ``base * A /
      base_total``: the layer below scaled to move ``A``, as before. Unlike before,
      it also applies after a whole-transaction ``from`` amount takes effect.
    * Each date's amount becomes a ``once`` version, ``base * A / base_total``.
    """
    from .scheduled import ScheduledSplit

    template = [ScheduledSplit.from_dict(item) for item in split_data]
    leg_changes = [
        sorted(
            (
                (date.fromisoformat(item["start"]), _money(item["amount"]))
                for item in (data.get("amount_changes") or [])
            ),
            key=lambda item: item[0],
        )
        for data in split_data
    ]
    totals = sorted(
        ((date.fromisoformat(item["start"]), _money(item["amount"])) for item in amount_changes),
        key=lambda item: item[0],
    )
    versions: list[ScheduledVersion] = []
    starts = sorted(
        {start for changes in leg_changes for start, _ in changes} | {start for start, _ in totals}
    )
    for start in starts:
        legs = []
        for split, changes in zip(template, leg_changes, strict=True):
            leg = ScheduledSplit.from_dict(split.serialize())
            if not split.formula:
                for change_start, amount in changes:
                    if change_start <= start:
                        leg.amount = amount
            legs.append(leg)
        total = next((amount for when, amount in reversed(totals) if when <= start), None)
        if total is not None:
            if any(leg.formula for leg in legs):
                # The scale depended on every formula's value on each date; the
                # amount on the change's own date is kept.
                values = [leg.resolve({BASE: Money(0), BASE_TOTAL: Money(0)}) for leg in legs]
                for leg, value in zip(legs, values, strict=True):
                    leg.formula = ""
                    leg.amount = value
            positive = Money(0)
            for leg in legs:
                if leg.amount is not None and leg.amount > 0:
                    positive = positive + leg.amount
            if positive:
                for leg in legs:
                    leg.amount = (leg.amount or Money(0)) * (total / positive)
        versions.append(ScheduledVersion(VersionKind.FROM, legs, start=start))

    # Each leg in the column its template amount is in, so the editor shows it
    # there; the value is the same either way.
    for item in seasonal_amounts:
        versions.append(
            ScheduledVersion.moving(
                VersionKind.MONTH, template, _money(item["amount"]), month=int(item["month"])
            )
        )
    for item in occurrence_adjustments:
        versions.append(
            ScheduledVersion.moving(
                VersionKind.ONCE,
                template,
                _money(item["amount"]),
                start=date.fromisoformat(item["when"]),
            )
        )
    return sorted_versions(versions)
