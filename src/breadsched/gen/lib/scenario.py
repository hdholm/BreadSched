"""Saved projection scenarios.

A scenario is the set of *assumptions* a forecast rests on, saved under a name so
two forecasts can be put side by side and the difference attributed to a specific
belief rather than to a forgotten setting.  A scenario stores no results: results
are recomputed from the current ledger, so a saved scenario stays honest as new
actual transactions arrive.

The rates here are annual and nominal.  The engine converts them to a monthly
factor by taking the twelfth root, not by dividing by twelve, so a 6% assumption
compounds to 6% over the year rather than to 6.17%.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any

from .base import PrimaryObject, create_handle
from .money import Money
from .recurrence import Recurrence
from .scheduled import (
    ScheduledAmountChange,
    ScheduledOccurrenceAdjustment,
    ScheduledSplit,
    ScheduledTransaction,
)

__all__ = [
    "ProjectionBasis",
    "OneOff",
    "ScenarioSchedule",
    "Assumptions",
    "AssumptionPeriod",
    "Scenario",
]


class ProjectionBasis(str, Enum):
    """Where the forecast's recurring cash movements come from."""

    BUDGET = "budget"
    SCHEDULED = "scheduled"
    #: Both, with scheduled transactions winning: any account driven by a schedule
    #: is skipped in the budget so the same rent is not counted twice.
    COMBINED = "combined"


class OneOff:
    """A single dated cash movement layered on top of the recurring model."""

    __slots__ = ("when", "account", "amount", "description")

    def __init__(
        self,
        when: date,
        account: str,
        amount: Money | str | int,
        description: str = "",
    ) -> None:
        self.when = when
        self.account = account
        self.amount = amount if isinstance(amount, Money) else Money(amount)
        self.description = description

    def serialize(self) -> dict[str, Any]:
        return {
            "when": self.when.isoformat(),
            "account": self.account,
            "amount": [self.amount.numerator, self.amount.denominator],
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OneOff:
        return cls(
            when=date.fromisoformat(data["when"]),
            account=data["account"],
            amount=Money(*data["amount"]),
            description=data.get("description", ""),
        )


class ScenarioSchedule:
    """A recurring transaction estimate that belongs to one scenario.

    ``source_schedule`` identifies a book-level schedule this entry replaces.
    Replacement is whole-template rather than a partial mutation: this keeps the
    baseline schedule untouched while allowing a scenario to change amount,
    recurrence, accounts, or to suppress the baseline entirely.
    """

    def __init__(
        self,
        *,
        handle: str | None = None,
        name: str = "",
        description: str = "",
        recurrence: Recurrence | None = None,
        splits: list[ScheduledSplit] | None = None,
        source_schedule: str | None = None,
        enabled: bool = True,
        placeholder: bool = True,
        variables: dict[str, str] | None = None,
        amount_changes: list[ScheduledAmountChange] | None = None,
        skipped: list[date] | None = None,
        occurrence_adjustments: list[ScheduledOccurrenceAdjustment] | None = None,
    ) -> None:
        self.handle = handle or create_handle()
        self.name = name
        self.description = description or name
        self.recurrence = recurrence or Recurrence()
        self.splits = list(splits or [])
        self.source_schedule = source_schedule
        self.enabled = enabled
        self.placeholder = placeholder
        self.variables = dict(variables or {})
        self.amount_changes = sorted(list(amount_changes or []), key=lambda item: item.start)
        self.skipped = sorted(set(skipped or []))
        self.occurrence_adjustments = sorted(
            list(occurrence_adjustments or []), key=lambda item: item.when
        )

    @classmethod
    def from_scheduled(
        cls,
        schedule: ScheduledTransaction,
        *,
        enabled: bool = True,
    ) -> ScenarioSchedule:
        """Copy a book schedule into a scenario without linking mutable objects."""
        return cls(
            name=schedule.name,
            description=schedule.description,
            recurrence=Recurrence.from_dict(schedule.recurrence.serialize()),
            splits=[ScheduledSplit.from_dict(split.serialize()) for split in schedule.splits],
            source_schedule=schedule.handle,
            enabled=enabled,
            placeholder=schedule.placeholder,
            variables=dict(schedule.variables),
            amount_changes=[
                ScheduledAmountChange.from_dict(item.serialize())
                for item in schedule.amount_changes
            ],
            skipped=list(schedule.skipped),
            occurrence_adjustments=[
                ScheduledOccurrenceAdjustment.from_dict(item.serialize())
                for item in schedule.occurrence_adjustments
            ],
        )

    def context(self, when: date) -> dict[str, Any]:
        context: dict[str, Any] = dict(self.variables)
        index = self.recurrence.index_of(when)
        context["period"] = index
        context.setdefault("i", index)
        return context

    def effective_amount(self, when: date) -> Money | None:
        for adjustment in self.occurrence_adjustments:
            if adjustment.when == when:
                return adjustment.amount
            if adjustment.when > when:
                break
        effective = None
        for change in self.amount_changes:
            if change.start > when:
                break
            effective = change.amount
        return effective

    def resolved_splits(self, when: date) -> list[tuple[str, Money]]:
        context = self.context(when)
        values = [(split.account, split.resolve(context)) for split in self.splits]
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

    def occurrence_key(self, scenario_handle: str, when: date) -> str:
        return f"scenario:{scenario_handle}:{self.handle}:{when.isoformat()}"

    def serialize(self) -> dict[str, Any]:
        return {
            "handle": self.handle,
            "name": self.name,
            "description": self.description,
            "recurrence": self.recurrence.serialize(),
            "splits": [split.serialize() for split in self.splits],
            "source_schedule": self.source_schedule,
            "enabled": self.enabled,
            "placeholder": self.placeholder,
            "variables": dict(self.variables),
            "amount_changes": [item.serialize() for item in self.amount_changes],
            "skipped": [when.isoformat() for when in self.skipped],
            "occurrence_adjustments": [
                item.serialize() for item in self.occurrence_adjustments
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScenarioSchedule:
        return cls(
            handle=data.get("handle"),
            name=data.get("name", ""),
            description=data.get("description", ""),
            recurrence=Recurrence.from_dict(data["recurrence"]),
            splits=[ScheduledSplit.from_dict(item) for item in data.get("splits", [])],
            source_schedule=data.get("source_schedule"),
            enabled=data.get("enabled", True),
            placeholder=data.get("placeholder", True),
            variables=data.get("variables", {}),
            amount_changes=[
                ScheduledAmountChange.from_dict(item)
                for item in data.get("amount_changes", [])
            ],
            skipped=[date.fromisoformat(item) for item in data.get("skipped", [])],
            occurrence_adjustments=[
                ScheduledOccurrenceAdjustment.from_dict(item)
                for item in data.get("occurrence_adjustments", [])
            ],
        )


class Assumptions:
    """Annual nominal rates, with optional per-account overrides."""

    def __init__(
        self,
        income_growth: Decimal | str = "0.03",
        expense_inflation: Decimal | str = "0.025",
        investment_return: Decimal | str = "0.06",
        cash_interest: Decimal | str = "0.01",
        liability_interest: Decimal | str = "0.0",
        per_account: dict[str, Decimal] | None = None,
    ) -> None:
        self.income_growth = Decimal(str(income_growth))
        self.expense_inflation = Decimal(str(expense_inflation))
        self.investment_return = Decimal(str(investment_return))
        self.cash_interest = Decimal(str(cash_interest))
        self.liability_interest = Decimal(str(liability_interest))
        #: Account handle -> rate, overriding whichever global rate applies.
        self.per_account: dict[str, Decimal] = dict(per_account or {})

    def rate_for(self, account_handle: str, default: Decimal) -> Decimal:
        return self.per_account.get(account_handle, default)

    def serialize(self) -> dict[str, Any]:
        return {
            "income_growth": str(self.income_growth),
            "expense_inflation": str(self.expense_inflation),
            "investment_return": str(self.investment_return),
            "cash_interest": str(self.cash_interest),
            "liability_interest": str(self.liability_interest),
            "per_account": {k: str(v) for k, v in self.per_account.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Assumptions:
        return cls(
            income_growth=data.get("income_growth", "0"),
            expense_inflation=data.get("expense_inflation", "0"),
            investment_return=data.get("investment_return", "0"),
            cash_interest=data.get("cash_interest", "0"),
            liability_interest=data.get("liability_interest", "0"),
            per_account={k: Decimal(v) for k, v in data.get("per_account", {}).items()},
        )

    def __repr__(self) -> str:
        return (
            f"<Assumptions income+{self.income_growth} "
            f"expense+{self.expense_inflation} return {self.investment_return}>"
        )


class AssumptionPeriod:
    """Dated overrides layered on top of a scenario's base assumptions.

    ``start`` is inclusive and ``end`` is inclusive when present. Fields left as
    ``None`` inherit the value already in force. Overlapping periods are applied
    in chronological order, so a later-starting period wins for values it names.
    """

    def __init__(
        self,
        start: date,
        end: date | None = None,
        *,
        income_growth: Decimal | str | None = None,
        expense_inflation: Decimal | str | None = None,
        investment_return: Decimal | str | None = None,
        cash_interest: Decimal | str | None = None,
        liability_interest: Decimal | str | None = None,
        per_account: dict[str, Decimal | str] | None = None,
        description: str = "",
    ) -> None:
        if end is not None and end < start:
            raise ValueError("assumption period end must not precede its start")
        self.start = start
        self.end = end
        self.description = description
        self.income_growth = (
            None if income_growth is None else Decimal(str(income_growth))
        )
        self.expense_inflation = (
            None if expense_inflation is None else Decimal(str(expense_inflation))
        )
        self.investment_return = (
            None if investment_return is None else Decimal(str(investment_return))
        )
        self.cash_interest = (
            None if cash_interest is None else Decimal(str(cash_interest))
        )
        self.liability_interest = (
            None if liability_interest is None else Decimal(str(liability_interest))
        )
        self.per_account: dict[str, Decimal] = {
            handle: Decimal(str(rate)) for handle, rate in (per_account or {}).items()
        }

    def applies(self, when: date) -> bool:
        return self.start <= when and (self.end is None or when <= self.end)

    def apply_to(self, base: Assumptions) -> Assumptions:
        per_account = dict(base.per_account)
        per_account.update(self.per_account)
        return Assumptions(
            income_growth=self.income_growth
            if self.income_growth is not None
            else base.income_growth,
            expense_inflation=self.expense_inflation
            if self.expense_inflation is not None
            else base.expense_inflation,
            investment_return=self.investment_return
            if self.investment_return is not None
            else base.investment_return,
            cash_interest=self.cash_interest
            if self.cash_interest is not None
            else base.cash_interest,
            liability_interest=self.liability_interest
            if self.liability_interest is not None
            else base.liability_interest,
            per_account=per_account,
        )

    def serialize(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat() if self.end else None,
            "description": self.description,
            "income_growth": None
            if self.income_growth is None
            else str(self.income_growth),
            "expense_inflation": None
            if self.expense_inflation is None
            else str(self.expense_inflation),
            "investment_return": None
            if self.investment_return is None
            else str(self.investment_return),
            "cash_interest": None
            if self.cash_interest is None
            else str(self.cash_interest),
            "liability_interest": None
            if self.liability_interest is None
            else str(self.liability_interest),
            "per_account": {k: str(v) for k, v in self.per_account.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AssumptionPeriod:
        return cls(
            start=date.fromisoformat(data["start"]),
            end=date.fromisoformat(data["end"]) if data.get("end") else None,
            income_growth=data.get("income_growth"),
            expense_inflation=data.get("expense_inflation"),
            investment_return=data.get("investment_return"),
            cash_interest=data.get("cash_interest"),
            liability_interest=data.get("liability_interest"),
            per_account=data.get("per_account", {}),
            description=data.get("description", ""),
        )


class Scenario(PrimaryObject):
    """A named, saveable set of forecasting assumptions."""

    TABLE = "scenario"

    def __init__(
        self,
        handle: str | None = None,
        name: str = "",
        description: str = "",
        start: date | None = None,
        years: int = 5,
        basis: ProjectionBasis | str = ProjectionBasis.SCHEDULED,
        budget: str | None = None,
        assumptions: Assumptions | None = None,
        assumption_periods: list[AssumptionPeriod] | None = None,
        schedule_overrides: list[ScenarioSchedule] | None = None,
    ) -> None:
        super().__init__(handle)
        self.name = name
        self.description = description
        self.start = start or date.today().replace(day=1)
        self.years = years
        self.basis = ProjectionBasis(basis) if not isinstance(basis, ProjectionBasis) else basis
        #: Handle of the budget that supplies recurring amounts, if any.
        self.budget = budget
        self.assumptions = assumptions or Assumptions()
        self.assumption_periods = list(assumption_periods or [])
        self.schedule_overrides = list(schedule_overrides or [])
        #: Pretend an account starts at this balance instead of its ledger balance.
        self.opening_overrides: dict[str, Money] = {}
        self.one_offs: list[OneOff] = []
        #: When the budget runs out of periods, repeat its final year with growth.
        self.extend_budget = True

    def assumptions_for(self, when: date) -> Assumptions:
        """Return the assumptions in force on ``when``.

        The base assumptions are copied, then every matching dated period is
        overlaid from oldest to newest. This makes overlaps deterministic while
        keeping scenarios without periods exactly backward compatible.
        """
        current = Assumptions.from_dict(self.assumptions.serialize())
        for period in sorted(self.assumption_periods, key=lambda item: item.start):
            if period.applies(when):
                current = period.apply_to(current)
        return current

    @property
    def months(self) -> int:
        return self.years * 12

    def add_one_off(
        self, when: date, account: str, amount: Money | str | int, description: str = ""
    ) -> OneOff:
        item = OneOff(when, account, amount, description)
        self.one_offs.append(item)
        return item

    def _serialize(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "start": self.start.isoformat(),
            "years": self.years,
            "basis": self.basis.value,
            "budget": self.budget,
            "assumptions": self.assumptions.serialize(),
            "assumption_periods": [p.serialize() for p in self.assumption_periods],
            "schedule_overrides": [item.serialize() for item in self.schedule_overrides],
            "opening_overrides": {
                k: [v.numerator, v.denominator] for k, v in self.opening_overrides.items()
            },
            "one_offs": [o.serialize() for o in self.one_offs],
            "extend_budget": self.extend_budget,
        }

    def _unserialize(self, data: dict[str, Any]) -> None:
        self.name = data["name"]
        self.description = data.get("description", "")
        self.start = date.fromisoformat(data["start"])
        self.years = data.get("years", 5)
        self.basis = ProjectionBasis(data.get("basis", "combined"))
        self.budget = data.get("budget")
        self.assumptions = Assumptions.from_dict(data.get("assumptions", {}))
        self.assumption_periods = [
            AssumptionPeriod.from_dict(item)
            for item in data.get("assumption_periods", [])
        ]
        self.schedule_overrides = [
            ScenarioSchedule.from_dict(item) for item in data.get("schedule_overrides", [])
        ]
        self.opening_overrides = {
            k: Money(*v) for k, v in data.get("opening_overrides", {}).items()
        }
        self.one_offs = [OneOff.from_dict(o) for o in data.get("one_offs", [])]
        self.extend_budget = data.get("extend_budget", True)

    def __repr__(self) -> str:
        return f"<Scenario {self.name!r} {self.years}y basis={self.basis.value}>"
