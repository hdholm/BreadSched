"""Scheduled transactions: a transaction template plus a recurrence rule.

The template's split values may be *formulas* rather than fixed amounts, which is
how GnuCash models a mortgage payment whose interest and principal split shift each
month.  Here a formula is a restricted arithmetic expression over named variables,
evaluated with :mod:`decimal`, never with :func:`eval`.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from ..utils.logs import get_logger
from .base import PrimaryObject
from .formula import FormulaError, evaluate
from .money import Money
from .recurrence import Recurrence
from .transaction import PlanningResolution, Split, Transaction

__all__ = [
    "ScheduledAmountChange",
    "ScheduledMonthAmount",
    "ScheduledOccurrenceAdjustment",
    "ScheduledSplit",
    "scheduled_occurrence_preview",
    "ScheduledTransaction",
]

LOG = get_logger(__name__)


class ScheduledAmountChange:
    """An effective-dated amount for a simple fixed scheduled transaction."""

    __slots__ = ("start", "amount")

    def __init__(self, start: date, amount: Money | str | int) -> None:
        self.start = start
        self.amount = amount if isinstance(amount, Money) else Money(amount)
        if self.amount <= 0:
            raise ValueError("scheduled amount change must be greater than zero")

    def serialize(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat(),
            "amount": [self.amount.numerator, self.amount.denominator],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScheduledAmountChange:
        return cls(date.fromisoformat(data["start"]), Money(*data["amount"]))


class ScheduledMonthAmount:
    """Recurring month-of-year amount for a simple monthly schedule."""

    __slots__ = ("month", "amount")

    def __init__(self, month: int, amount: Money | str | int) -> None:
        if not 1 <= month <= 12:
            raise ValueError("seasonal month must be between 1 and 12")
        self.month = month
        self.amount = amount if isinstance(amount, Money) else Money(amount)
        if self.amount <= 0:
            raise ValueError("seasonal amount must be greater than zero")

    def serialize(self) -> dict[str, Any]:
        return {
            "month": self.month,
            "amount": [self.amount.numerator, self.amount.denominator],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScheduledMonthAmount:
        return cls(int(data["month"]), Money(*data["amount"]))


class ScheduledOccurrenceAdjustment:
    """A one-time amount override for one scheduled occurrence date."""

    __slots__ = ("when", "amount")

    def __init__(self, when: date, amount: Money | str | int) -> None:
        self.when = when
        self.amount = amount if isinstance(amount, Money) else Money(amount)
        if self.amount <= 0:
            raise ValueError("scheduled occurrence amount must be greater than zero")

    def serialize(self) -> dict[str, Any]:
        return {
            "when": self.when.isoformat(),
            "amount": [self.amount.numerator, self.amount.denominator],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScheduledOccurrenceAdjustment:
        return cls(date.fromisoformat(data["when"]), Money(*data["amount"]))


def scheduled_occurrence_preview(
    recurrence: Recurrence,
    base_amount: Money,
    amount_changes: list[ScheduledAmountChange],
    skipped: list[date],
    occurrence_adjustments: list[ScheduledOccurrenceAdjustment],
    *,
    limit: int = 8,
) -> list[tuple[date, Money, str]]:
    """Return upcoming occurrence dates with effective amount and exception state."""
    horizon = date(min(recurrence.start.year + 10, 9999), 12, 31)
    skipped_dates = set(skipped)
    adjustments = {item.when: item.amount for item in occurrence_adjustments}
    changes = sorted(amount_changes, key=lambda item: item.start)
    rows: list[tuple[date, Money, str]] = []
    for when in recurrence.occurrences(horizon):
        amount = base_amount
        status = "Normal"
        for change in changes:
            if change.start > when:
                break
            amount = change.amount
            status = "Future amount"
        if when in adjustments:
            amount = adjustments[when]
            status = "One-time amount"
        if when in skipped_dates:
            status = "Skipped"
        rows.append((when, amount, status))
        if len(rows) >= limit:
            break
    return rows


class ScheduledSplit:
    """One leg of a template, with either a fixed amount or a formula."""

    __slots__ = ("account", "amount", "formula", "memo")

    def __init__(
        self,
        account: str,
        amount: Money | str | int | None = None,
        formula: str = "",
        memo: str = "",
    ) -> None:
        self.account = account
        self.amount = None if amount is None else (
            amount if isinstance(amount, Money) else Money(amount)
        )
        self.formula = formula
        self.memo = memo

    def resolve(self, variables: dict[str, Any] | None = None) -> Money:
        """The amount this leg contributes, evaluating a formula if there is one.

        An imported formula may name variables only GnuCash can supply. Raising
        here would take down whatever asked -- a list view, a projection, a budget
        -- long after the import that accepted it, so an unresolvable formula
        contributes nothing and says so in the log. The text stays on the split, so
        the user can still see and fix it.
        """
        if self.formula:
            try:
                return Money(evaluate(self.formula, variables or {}))
            except (FormulaError, ValueError, ArithmeticError):
                LOG.warning(
                    "scheduled split on account %s has an unusable formula %r; "
                    "treating it as zero",
                    self.account[:8], self.formula,
                )
                return Money(0)
        return self.amount or Money(0)

    def serialize(self) -> dict[str, Any]:
        return {
            "account": self.account,
            "amount": None if self.amount is None
            else [self.amount.numerator, self.amount.denominator],
            "formula": self.formula,
            "memo": self.memo,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScheduledSplit:
        amount = data.get("amount")
        return cls(
            account=data["account"],
            amount=Money(*amount) if amount else None,
            formula=data.get("formula", ""),
            memo=data.get("memo", ""),
        )


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
        amount_changes: list[ScheduledAmountChange] | None = None,
        seasonal_amounts: list[ScheduledMonthAmount] | None = None,
        skipped: list[date] | None = None,
        occurrence_adjustments: list[ScheduledOccurrenceAdjustment] | None = None,
    ) -> None:
        super().__init__(handle)
        self.name = name
        self.description = description or name
        self.recurrence = recurrence or Recurrence()
        self.splits: list[ScheduledSplit] = splits or []
        self.enabled = enabled
        #: Post the real transaction automatically once its date arrives.
        self.auto_create = auto_create
        #: How many days ahead to surface the occurrence in the "due" list.
        self.advance_days = advance_days
        self.currency = currency
        self.amount_changes = sorted(list(amount_changes or []), key=lambda item: item.start)
        self.seasonal_amounts = sorted(
            list(seasonal_amounts or []), key=lambda item: item.month
        )
        self.occurrence_adjustments = sorted(
            list(occurrence_adjustments or []), key=lambda item: item.when
        )
        self.last_posted: date | None = None
        self.variables: dict[str, str] = {}
        #: A budget-only item: a planning figure rather than a commitment. It shapes
        #: budgets and forecasts and is never posted to the ledger, which is how a
        #: household budgets "about 600 a month on groceries" without pretending it
        #: is a standing order that will arrive on the 3rd. Its periodicity is
        #: respected exactly as a real schedule's is.
        self.placeholder: bool = False
        #: Occurrences the user chose not to post and does not want asked about
        #: again. Recorded per date rather than by moving ``last_posted``, because
        #: skipping March must not also dismiss February.
        self.skipped: list[date] = sorted(set(skipped or []))
        #: Budgets this flow is part of, by budget handle.
        self.budgets: list[str] = []
        #: Whether membership has ever been decided for this flow. Until it has,
        #: the flow counts towards every budget, so a schedule created before
        #: budgets existed is not silently missing from the plan.
        #:
        #: A flag rather than "empty means all": removing a flow from the only
        #: budget in the book empties the list, and without this that would read
        #: as putting it back into every budget instead of taking it out.
        self.budgets_decided: bool = False

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

    def effective_amount(self, when: date | None) -> Money | None:
        """One-time override, or latest effective-dated amount in force."""
        if when is None:
            return None
        for adjustment in self.occurrence_adjustments:
            if adjustment.when == when:
                return adjustment.amount
            if adjustment.when > when:
                break
        effective = None
        for item in self.seasonal_amounts:
            if item.month == when.month:
                effective = item.amount
                break
        for change in self.amount_changes:
            if change.start > when:
                break
            effective = change.amount
        return effective

    def context(
        self, when: date | None = None, variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Variables for one occurrence, including its period number.

        ``period`` is what makes a loan work: ipmt and ppmt need to know which
        payment this is, or every month looks like the first one and the interest
        never falls.
        """
        merged: dict[str, Any] = dict(self.variables)
        if when is not None:
            index = self.recurrence.index_of(when)
            merged["period"] = index
            # GnuCash's loan assistant writes the period as `i`. Both names are
            # offered so an imported mortgage evaluates without being rewritten.
            merged.setdefault("i", index)
        merged.update(variables or {})
        return merged

    def resolved_splits(
        self,
        variables: dict[str, Any] | None = None,
        when: date | None = None,
    ) -> list[tuple[str, Money]]:
        """Each leg as ``(account, amount)``, with formulas evaluated."""
        merged = self.context(when, variables)
        values = [(split.account, split.resolve(merged)) for split in self.splits]
        target = self.effective_amount(when)
        if target is None:
            return values
        positive = Money(0)
        for _account, value in values:
            if value > 0:
                positive = positive + value
        if not positive:
            return values
        scale = target / positive
        return [(account, value * scale) for account, value in values]

    def imbalance(self, variables: dict[str, Any] | None = None) -> Money:
        """How far the resolved legs are from summing to zero.

        A template whose two sides are calculated separately can disagree -- one
        formula referencing something we cannot evaluate, or two formulas that
        simply do not net off. A forecast can still use such a schedule; a ledger
        cannot. Exposing the residual lets each caller decide which it is.
        """
        total = Money(0)
        for _account, amount in self.resolved_splits(variables):
            total = total + amount
        return total

    def instantiate(
        self,
        when: date,
        variables: dict[str, Any] | None = None,
        strict: bool = True,
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
        txn.planned_occurrence = self.occurrence_key(when)
        txn.planned_for = when
        txn.planned_amount = self.amount(variables=variables, when=when)
        txn.planning_resolution = PlanningResolution.MATCHED
        # Formula legs are computed to full precision and cannot all land on exact
        # cents; ppmt and ipmt sum to pmt to thirty digits, not to two. Round each
        # leg to the currency's smallest unit and give the last leg the remainder,
        # so the stored transaction balances exactly rather than by a hair.
        resolved = self.resolved_splits(variables=variables, when=when)
        values = [value.quantize(100) for _account, value in resolved]
        if values:
            residual = Money(0)
            for value in values:
                residual = residual + value
            # Only rounding noise is absorbed. A residual of a cent or more means
            # the legs genuinely disagree, and hiding that would turn a broken
            # schedule into a silently wrong one.
            if residual and not residual.quantize(100):
                values[-1] = values[-1] - residual
        for split, value in zip(self.splits, values, strict=False):
            txn.add_split(Split(split.account, value, memo=split.memo))
        if strict:
            txn.validate()
        return txn

    @property
    def postable(self) -> bool:
        """Whether occurrences may become real ledger entries."""
        return self.enabled and not self.placeholder

    def in_budget(self, budget_handle: str | None) -> bool:
        """Whether this flow counts towards ``budget_handle``.

        An empty membership list means "all budgets": the alternative is that
        every schedule created before budgets existed silently vanishes from them.
        """
        if budget_handle is None or not self.budgets_decided:
            return True
        return budget_handle in self.budgets

    def add_to_budget(self, budget_handle: str, all_budgets: list[str]) -> None:
        """Include this flow in one budget.

        Turning an implicit "all" into an explicit list has to enumerate the other
        budgets first, or adding a schedule to one budget would remove it from
        every other without saying so.
        """
        if not self.budgets_decided:
            self.budgets = list(all_budgets)
            self.budgets_decided = True
        if budget_handle not in self.budgets:
            self.budgets.append(budget_handle)

    def remove_from_budget(self, budget_handle: str, all_budgets: list[str]) -> None:
        if not self.budgets_decided:
            self.budgets = list(all_budgets)
            self.budgets_decided = True
        self.budgets = [h for h in self.budgets if h != budget_handle]

    def skip(self, when: date) -> None:
        """Treat ``when`` as dealt with, without posting anything for it."""
        if when not in self.skipped:
            self.skipped.append(when)

    def upcoming(self, until: date, since: date | None = None) -> list[date]:
        if not self.postable:
            return []
        return [
            when for when in self.recurrence.occurrences(until, since)
            if when not in self.skipped
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
            "currency": self.currency,
            "amount_changes": [item.serialize() for item in self.amount_changes],
            "seasonal_amounts": [item.serialize() for item in self.seasonal_amounts],
            "occurrence_adjustments": [
                item.serialize() for item in self.occurrence_adjustments
            ],
            "last_posted": self.last_posted.isoformat() if self.last_posted else None,
            "variables": dict(self.variables),
            "placeholder": self.placeholder,
            "skipped": [when.isoformat() for when in self.skipped],
            "budgets": list(self.budgets),
            "budgets_decided": self.budgets_decided,
        }

    def _unserialize(self, data: dict[str, Any]) -> None:
        self.name = data["name"]
        self.description = data.get("description", "")
        self.recurrence = Recurrence.from_dict(data["recurrence"])
        self.splits = [ScheduledSplit.from_dict(s) for s in data.get("splits", [])]
        self.enabled = data.get("enabled", True)
        self.auto_create = data.get("auto_create", False)
        self.advance_days = data.get("advance_days", 0)
        self.currency = data.get("currency")
        self.amount_changes = sorted(
            [ScheduledAmountChange.from_dict(item) for item in data.get("amount_changes", [])],
            key=lambda item: item.start,
        )
        self.seasonal_amounts = sorted(
            [
                ScheduledMonthAmount.from_dict(item)
                for item in data.get("seasonal_amounts", [])
            ],
            key=lambda item: item.month,
        )
        self.occurrence_adjustments = sorted(
            [
                ScheduledOccurrenceAdjustment.from_dict(item)
                for item in data.get("occurrence_adjustments", [])
            ],
            key=lambda item: item.when,
        )
        raw = data.get("last_posted")
        self.last_posted = date.fromisoformat(raw) if raw else None
        self.variables = dict(data.get("variables", {}))
        self.placeholder = data.get("placeholder", False)
        self.skipped = [date.fromisoformat(d) for d in data.get("skipped", [])]
        self.budgets = list(data.get("budgets", []))
        # Older books recorded membership only as a list; a non-empty one there
        # means the question had been answered.
        self.budgets_decided = data.get("budgets_decided", bool(self.budgets))

    def __repr__(self) -> str:
        return f"<ScheduledTransaction {self.name!r} {self.recurrence.describe()}>"
