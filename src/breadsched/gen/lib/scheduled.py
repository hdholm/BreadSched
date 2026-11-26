"""Scheduled transactions: a transaction template plus a recurrence rule.

The template's split values may be *formulas* rather than fixed amounts, which is
how GnuCash models a mortgage payment whose interest and principal split shift each
month.  Here a formula is a restricted arithmetic expression over named variables,
evaluated with :mod:`decimal`, never with :func:`eval`.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, timedelta
from enum import Enum
from typing import TYPE_CHECKING, Any

from ..utils.logs import get_logger
from .base import PrimaryObject
from .formula import FormulaError, evaluate
from .money import Money
from .recurrence import Recurrence
from .schedule_versions import (
    BASE,
    BASE_TOTAL,
    ResolvedLeg,
    ScheduledVersion,
    VersionKind,
    layered_legs,
    sorted_versions,
    versions_from_legacy,
)
from .transaction import (
    InvestmentActivityKind,
    PlanningFlowKind,
    PlanningResolution,
    Split,
    Transaction,
)

if TYPE_CHECKING:
    from .scenario import ScenarioSchedule

__all__ = [
    "ScheduleGrowthPolicy",
    "ScheduledSplit",
    "ScheduledVersion",
    "VersionKind",
    "occurrence_preview",
    "ScheduledTransaction",
]

LOG = get_logger(__name__)


class ScheduleGrowthPolicy(str, Enum):
    """How projection assumptions escalate a scheduled transaction."""

    AUTO = "auto"
    NONE = "none"
    INCOME = "income"
    INFLATION = "inflation"


def occurrence_preview(
    definition: ScheduledTransaction | ScenarioSchedule,
    today: date,
    *,
    limit: int = 8,
) -> list[tuple[date, Money, str]]:
    """The next occurrences an editor lists as upcoming, each at the amount it will have.

    The list starts at the first occurrence not yet entered: the day after the last
    one posted, or today when none was (an estimate is never posted). Each amount
    is what the definition resolves to on that date, so every version counts: from a
    date on, a month's, and one date's.
    """
    last_posted = getattr(definition, "last_posted", None)
    if definition.placeholder or last_posted is None:
        since = today
    else:
        since = last_posted + timedelta(days=1)
    horizon = date(min(since.year + 10, 9999), 12, 31)
    rows: list[tuple[date, Money, str]] = []
    for when in definition.recurrence.occurrences(horizon, since=since):
        try:
            values = definition.resolved_splits(when=when)
        except (FormulaError, ArithmeticError):
            break
        amount = Money(0)
        for _account, value in values:
            if value > 0:
                amount = amount + value
        status = _preview_status(definition, when)
        if when < today:
            status = "Overdue" if status == "Normal" else f"Overdue, {status.lower()}"
        rows.append((when, amount, status))
        if len(rows) >= limit:
            break
    return rows


def _preview_status(definition: ScheduledTransaction | ScenarioSchedule, when: date) -> str:
    if when in definition.skipped:
        return "Skipped"
    applying = {item.kind for item in definition.versions if item.applies(when)}
    if VersionKind.ONCE in applying:
        return "One-time version"
    if VersionKind.MONTH in applying:
        return "Month version"
    if VersionKind.FROM in applying:
        return "Changed version"
    return "Normal"


class ScheduledSplit:
    """One leg of a template, with either a fixed amount or a formula."""

    __slots__ = (
        "account",
        "amount",
        "formula",
        "memo",
        "planning_flow",
        "investment_activity",
        "direction",
    )

    def __init__(
        self,
        account: str,
        amount: Money | str | int | None = None,
        formula: str = "",
        memo: str = "",
        planning_flow: PlanningFlowKind | str | None = None,
        investment_activity: InvestmentActivityKind | str | None = None,
        direction: int = 1,
    ) -> None:
        self.account = account
        #: For a formula, the way it moves money, as a register's two amount columns
        #: do: 1 (the default) is the formula's own sign, the Increase column; -1 is
        #: the Decrease column, where the formula gives the amount taken out and its
        #: ``base`` is the amount below as that column shows it (``schedule_versions``).
        self.direction = -1 if direction == -1 else 1
        self.amount = (
            None if amount is None else (amount if isinstance(amount, Money) else Money(amount))
        )
        self.formula = formula
        self.memo = memo
        inferred_flow = (
            PlanningFlowKind.DEBT_PRINCIPAL
            if planning_flow is None and "ppmt(" in formula.lower().replace(" ", "")
            else None
        )
        self.planning_flow = (
            inferred_flow
            if planning_flow is None
            else planning_flow
            if isinstance(planning_flow, PlanningFlowKind)
            else PlanningFlowKind(planning_flow)
        )
        self.investment_activity = (
            None
            if investment_activity is None
            else investment_activity
            if isinstance(investment_activity, InvestmentActivityKind)
            else InvestmentActivityKind(investment_activity)
        )

    def resolve(self, variables: Mapping[str, Any] | None = None) -> Money:
        """The amount this leg contributes, evaluating a formula if there is one.

        An imported formula may name variables only GnuCash can supply. Raising
        here would take down whatever asked -- a list view, a projection, or Plan
        -- long after the import that accepted it, so an unresolvable formula
        contributes nothing and says so in the log. The text stays on the split, so
        the user can still see and fix it.
        """
        if self.formula:
            given = dict(variables or {})
            if self.direction == -1 and "base" in given:
                given["base"] = -given["base"]
            try:
                return Money(evaluate(self.formula, given)) * self.direction
            except (FormulaError, ValueError, ArithmeticError):
                LOG.warning(
                    "scheduled split on account %s has an unusable formula %r; treating it as zero",
                    self.account[:8],
                    self.formula,
                )
                return Money(0)
        return self.amount or Money(0)

    def serialize(self) -> dict[str, Any]:
        return {
            "account": self.account,
            "amount": None
            if self.amount is None
            else [self.amount.numerator, self.amount.denominator],
            "formula": self.formula,
            "memo": self.memo,
            "planning_flow": self.planning_flow.value if self.planning_flow else None,
            "investment_activity": (
                self.investment_activity.value if self.investment_activity else None
            ),
            # Written only for a formula taking money out, so other legs are unchanged.
            **({"direction": -1} if self.direction == -1 and self.formula else {}),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ScheduledSplit:
        amount = data.get("amount")
        return cls(
            account=data["account"],
            amount=Money(*amount) if amount else None,
            formula=data.get("formula", ""),
            memo=data.get("memo", ""),
            planning_flow=data.get("planning_flow"),
            investment_activity=data.get("investment_activity"),
            direction=int(data.get("direction", 1) or 1),
        )


def versions_of(data: Mapping[str, Any]) -> list[ScheduledVersion]:
    """A stored schedule's versions, converting the changes written before schema 16."""
    if "versions" in data:
        return sorted_versions(ScheduledVersion.from_dict(item) for item in data["versions"] or [])
    return versions_from_legacy(
        data.get("splits", []),
        data.get("amount_changes", []) or [],
        data.get("seasonal_amounts", []) or [],
        data.get("occurrence_adjustments", []) or [],
    )


def formula_problem(
    splits: list[ScheduledSplit], versions: list[ScheduledVersion], context: Mapping[str, Any]
) -> str | None:
    """The first formula, of the template or a version, the evaluator cannot read."""
    variables = {**context, BASE: Money(0), BASE_TOTAL: Money(1)}
    labelled = [(split, "") for split in splits] + [
        (split, f" ({version.label()})") for version in versions for split in version.splits
    ]
    for split, where in labelled:
        if not split.formula:
            continue
        try:
            evaluate(split.formula, variables)
        except (FormulaError, ValueError, ArithmeticError) as exc:
            return f"formula {split.formula!r}{where}: {exc}"
    return None


class ScheduledTransaction(PrimaryObject):
    """A recurring transaction that has not happened yet."""

    TABLE = "scheduled"

    def __init__(
        self,
        handle: str | None = None,
        name: str = "",
        description: str = "",
        recurrence: Recurrence | None = None,
        splits: list[ScheduledSplit] | None = None,
        enabled: bool = True,
        auto_create: bool = False,
        advance_days: int = 0,
        currency: str | None = None,
        remind_days: int = 0,
        versions: list[ScheduledVersion] | None = None,
        skipped: list[date] | None = None,
        growth_policy: ScheduleGrowthPolicy | str = ScheduleGrowthPolicy.AUTO,
    ) -> None:
        super().__init__(handle)
        self.name = name
        self.description = description or name
        self.recurrence = recurrence or Recurrence()
        self.splits: list[ScheduledSplit] = splits or []
        self.enabled = enabled
        #: Post the real transaction automatically once its date arrives.
        self.auto_create = auto_create
        #: Enter in advance (GnuCash's "create in advance"): an occurrence this many
        #: days ahead joins the due review proposed for posting, and an automatic
        #: schedule posts it then (#352).
        self.advance_days = advance_days
        #: Remind in advance: an occurrence this many days ahead joins the due
        #: review as a reminder, proposed as Later until decided (#352).
        self.remind_days = remind_days
        self.currency = currency
        self.growth_policy = ScheduleGrowthPolicy(growth_policy)
        #: The whole transaction from a date on, on one date, or in a month every
        #: year (``schedule_versions``), layered on the template legs.
        self.versions: list[ScheduledVersion] = sorted_versions(versions or [])
        self.last_posted: date | None = None
        self.variables: dict[str, str] = {}
        #: An estimated item: a planning figure rather than a commitment. It shapes
        #: Plan and forecasts and is never posted to the ledger, which is how a
        #: household plans "about 600 a month on groceries" without pretending it
        #: is a standing order that will arrive on the 3rd. Its periodicity is
        #: respected exactly as a real schedule's is.
        self.placeholder: bool = False
        #: Structured analyzer evidence retained when this estimate began as a
        #: historical proposal.  The saved schedule itself records the user's
        #: accepted adjustments; this records why the original draft existed.
        self.estimate_evidence: dict[str, Any] | None = None
        #: Occurrences the user chose not to post and does not want asked about
        #: again. Recorded per date rather than by moving ``last_posted``, because
        #: skipping March must not also dismiss February.
        self.skipped: list[date] = sorted(set(skipped or []))
        #: Occurrences closed early: what is left of them is no
        #: longer expected, though what was matched to them still counts.
        self.closed_occurrences: list[date] = []
        #: Exact importer-owned recurrence representation when the source cannot be
        #: mapped safely to BreadSched's recurrence model.
        self.source_recurrence: dict[str, Any] | list[dict[str, Any] | str] | str | None = None
        self.unsupported_reason: str = ""
        #: The number, notes, and linked documents every transaction it posts carries
        #: (schema 15), as it carries the tags every stored object has: a rent
        #: schedule's lease, its cheque number, its tag.
        self.num: str = ""
        self.notes: str = ""
        self.attachments: list[str] = []
        #: Schedules have no GnuCash-linked document; documents are statused as a
        #: transaction's are (``engine.attachments.statuses``).
        self.source_link: str | None = None

    # ------------------------------------------------------------- realisation

    @staticmethod
    def occurrence_key_for(schedule_handle: str, when: date) -> str:
        """Stable identity for one firing of a schedule."""
        return f"scheduled:{schedule_handle}:{when.isoformat()}"

    def occurrence_key(self, when: date) -> str:
        return self.occurrence_key_for(self.handle, when)

    def amount(
        self,
        variables: dict[str, Any] | None = None,
        when: date | None = None,
    ) -> Money:
        """Absolute size of the movement, including effective-dated changes."""
        total = Money(0)
        for _account, value in self.resolved_splits(variables=variables, when=when):
            if value > 0:
                total = total + value
        return total

    def context(
        self, when: date | None = None, variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Variables for one occurrence, including its period number.

        ``period`` is what makes a loan work: ipmt and ppmt need to know which
        payment this is, or every month looks like the first one and the interest
        never falls.
        """
        merged: dict[str, Any] = dict(self.variables)
        index: int | None = None
        if when is not None and self.has_formulas():
            # Import validation and editor previews begin with the recurrence's
            # nominal anchor.  A weekend rule can move that first cash date, so
            # the anchor itself is not necessarily returned by ``occurrences``.
            # It still unambiguously identifies period one.
            try:
                index = 1 if when == self.recurrence.start else self.recurrence.index_of(when)
            except ValueError:
                # Only the template's formulas (a loan's) need the period, and only
                # on occurrences; a version's formula on any other date does not.
                if any(split.formula for split in self.splits):
                    raise
                index = None
        if when is not None and self.has_formulas() and index is not None:
            merged["period"] = index
            # GnuCash's loan assistant writes the period as `i`. Both names are
            # offered so an imported mortgage evaluates without being rewritten.
            merged.setdefault("i", index)
        merged.update(variables or {})
        return merged

    def has_formulas(self) -> bool:
        """Whether any leg, of the template or of a version, is a formula."""
        return any(split.formula for split in self.splits) or any(
            split.formula for version in self.versions for split in version.splits
        )

    def legs_at(
        self, when: date | None = None, variables: dict[str, Any] | None = None
    ) -> list[ResolvedLeg]:
        """The occurrence's legs: each leg's definition, amount, and its source
        (the template, or the version it comes from), with formulas evaluated."""
        return layered_legs(self.splits, self.versions, self.context(when, variables), when)

    def resolved_splits(
        self,
        variables: dict[str, Any] | None = None,
        when: date | None = None,
    ) -> list[tuple[str, Money]]:
        """Each leg as ``(account, amount)``, with versions and formulas applied."""
        return [(leg.account, leg.value) for leg in self.legs_at(when, variables)]

    def resolved_split_sources(self, when: date | None = None) -> list[str]:
        """Where each resolved leg's amount comes from, in the same order."""
        return [leg.source for leg in self.legs_at(when)]

    def imbalance(
        self,
        variables: dict[str, Any] | None = None,
        when: date | None = None,
    ) -> Money:
        """How far the resolved legs are from summing to zero.

        A template whose two sides are calculated separately can disagree -- one
        formula referencing something we cannot evaluate, or two formulas that
        simply do not net off. A forecast can still use such a schedule; a ledger
        cannot. Exposing the residual lets each caller decide which it is.
        """
        total = Money(0)
        for _account, amount in self.resolved_splits(variables, when=when):
            total = total + amount
        return total

    def instantiate(
        self,
        when: date,
        variables: dict[str, Any] | None = None,
        strict: bool = True,
        fraction: int = 100,
    ) -> Transaction:
        """Build a concrete :class:`Transaction` for one occurrence.

        ``strict`` validates that the legs balance, which is right when the result
        is going into the ledger. Forecasting passes ``strict=False``: a projection
        that refuses to run because one schedule's formulas disagree is less useful
        than one that runs and says which schedule is wrong.
        """
        txn = Transaction(
            post_date=when,
            description=self.description,
            currency=self.currency,
        )
        txn.scheduled_from = self.handle
        # What every posting carries: its number, notes, tags, and documents.
        txn.num = self.num
        txn.notes = self.notes
        txn.tags = list(self.tags)
        txn.attachments = list(self.attachments)
        txn.planned_occurrence = self.occurrence_key(when)
        txn.planned_for = when
        txn.planned_amount = self.amount(variables=variables, when=when)
        txn.planning_resolution = PlanningResolution.MATCHED
        # Formula legs are computed to full precision and cannot all land on exact
        # cents; ppmt and ipmt sum to pmt to thirty digits, not to two. Round each
        # leg to the currency's smallest unit and give the last leg the remainder,
        # so the stored transaction balances exactly rather than by a hair.
        legs = self.legs_at(when, variables)
        values = [leg.value.quantize(fraction) for leg in legs]
        if values:
            residual = Money(0)
            for value in values:
                residual = residual + value
            # Only rounding noise is absorbed. A residual of one minor unit or more means
            # the legs genuinely disagree, and hiding that would turn a broken
            # schedule into a silently wrong one.
            if residual and not residual.quantize(fraction):
                values[-1] = values[-1] - residual
        for split, value in zip((leg.split for leg in legs), values, strict=True):
            txn.add_split(
                Split(
                    split.account,
                    value,
                    memo=split.memo,
                    planning_flow=split.planning_flow,
                    investment_activity=split.investment_activity,
                )
            )
        if strict:
            txn.validate()
        return txn

    def formula_problem(self, when: date | None = None) -> str | None:
        """Explain the first formula that the safe evaluator cannot resolve."""
        context = self.context(when or self.recurrence.start)
        return formula_problem(self.splits, self.versions, context)

    @property
    def usable(self) -> bool:
        """Whether the definition can safely participate in calculations/posting."""
        return not self.unsupported_reason and self.formula_problem() is None

    @property
    def postable(self) -> bool:
        """Whether occurrences may become real ledger entries."""
        return self.enabled and not self.placeholder and self.usable

    def skip(self, when: date) -> None:
        """Treat ``when`` as dealt with, without posting anything for it."""
        if when not in self.skipped:
            self.skipped.append(when)

    def current_occurrence(self, today: date) -> date | None:
        """The occurrence a Match window opens on: the latest on or
        before ``today`` that was not skipped, else the next one."""
        start = self.recurrence.start or today
        since = max(start, today - timedelta(days=400))
        past = [
            when
            for when in self.recurrence.occurrences(today, since=since)
            if when not in self.skipped
        ]
        if past:
            return past[-1]
        following = self.recurrence.next_after(today)
        while following is not None and following in self.skipped:
            following = self.recurrence.next_after(following)
        return following

    def upcoming(self, until: date, since: date | None = None) -> list[date]:
        if not self.postable:
            return []
        return [
            when for when in self.recurrence.occurrences(until, since) if when not in self.skipped
        ]

    # ------------------------------------------------------------ serialisation

    def _serialize(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "recurrence": self.recurrence.serialize(),
            "splits": [s.serialize() for s in self.splits],
            "enabled": self.enabled,
            "auto_create": self.auto_create,
            "advance_days": self.advance_days,
            # Written only when set, so every other schedule's stored form is unchanged.
            **({"remind_days": self.remind_days} if self.remind_days else {}),
            "currency": self.currency,
            "growth_policy": self.growth_policy.value,
            "versions": [item.serialize() for item in self.versions],
            "last_posted": self.last_posted.isoformat() if self.last_posted else None,
            "variables": dict(self.variables),
            "placeholder": self.placeholder,
            "estimate_evidence": self.estimate_evidence,
            "skipped": [when.isoformat() for when in self.skipped],
            **(
                {"closed_occurrences": [when.isoformat() for when in self.closed_occurrences]}
                if self.closed_occurrences
                else {}
            ),
            "source_recurrence": self.source_recurrence,
            "unsupported_reason": self.unsupported_reason,
            # Written only when set, so every other schedule's stored form is unchanged.
            **({"num": self.num} if self.num else {}),
            **({"notes": self.notes} if self.notes else {}),
            **({"attachments": list(self.attachments)} if self.attachments else {}),
        }

    def _unserialize(self, data: dict[str, Any]) -> None:
        self.name = data["name"]
        self.description = data.get("description", "")
        self.recurrence = Recurrence.from_dict(data["recurrence"])
        self.splits = [ScheduledSplit.from_dict(s) for s in data.get("splits", [])]
        self.versions = versions_of(data)
        self.enabled = data.get("enabled", True)
        self.auto_create = data.get("auto_create", False)
        self.advance_days = int(data.get("advance_days", 0) or 0)
        self.remind_days = int(data.get("remind_days", 0) or 0)
        self.currency = data.get("currency")
        self.growth_policy = ScheduleGrowthPolicy(data.get("growth_policy", "auto"))
        raw = data.get("last_posted")
        self.last_posted = date.fromisoformat(raw) if raw else None
        self.variables = dict(data.get("variables", {}))
        self.placeholder = data.get("placeholder", False)
        self.estimate_evidence = data.get("estimate_evidence")
        self.skipped = [date.fromisoformat(d) for d in data.get("skipped", [])]
        self.closed_occurrences = sorted(
            date.fromisoformat(d) for d in data.get("closed_occurrences", [])
        )
        self.source_recurrence = data.get("source_recurrence")
        self.unsupported_reason = str(data.get("unsupported_reason", ""))
        self.num = str(data.get("num", "") or "")
        self.notes = str(data.get("notes", "") or "")
        self.attachments = [str(item) for item in data.get("attachments", []) or []]

    def __repr__(self) -> str:
        return f"<ScheduledTransaction {self.name!r} {self.recurrence.describe()}>"
