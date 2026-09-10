"""Cash-flow budgets.

This is a *cash-flow* budget, not an accrual budget: a line is an amount of money
expected to move in or out during a period, so a line may target any account, not
only income and expense ones.  Budgeting a transfer into a brokerage account is
therefore a first-class thing, which is what makes the budget usable as the driver
of a savings projection.

Amounts are stored per period index rather than as one annual figure, because real
household cash flow is lumpy: insurance in March, tuition in August, heating in
January.  Flattening that into a twelfth of the year hides exactly the months where
the current account runs dry.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from enum import Enum
from typing import Any

from .base import PrimaryObject
from .money import Money
from .recurrence import add_months

__all__ = ["PeriodKind", "BudgetLine", "Budget"]


class PeriodKind(str, Enum):
    MONTH = "month"
    QUARTER = "quarter"
    YEAR = "year"

    @property
    def months(self) -> int:
        return {"month": 1, "quarter": 3, "year": 12}[self.value]


class BudgetLine:
    """Planned cash movement for one account, period by period."""

    __slots__ = ("account", "amounts", "note")

    def __init__(
        self,
        account: str,
        amounts: dict[int, Money] | None = None,
        note: str = "",
    ) -> None:
        self.account = account
        self.amounts: dict[int, Money] = dict(amounts or {})
        self.note = note

    def amount(self, period: int) -> Money:
        return self.amounts.get(period, Money(0))

    def set_amount(self, period: int, value: Money | str | int) -> None:
        self.amounts[period] = value if isinstance(value, Money) else Money(value)

    def spread(self, total: Money | str | int, periods: int, start: int = 0) -> None:
        """Spread a total evenly across ``periods``, allocating stray cents."""
        value = total if isinstance(total, Money) else Money(total)
        for offset, part in enumerate(value.allocate(periods)):
            self.amounts[start + offset] = part

    def total(self) -> Money:
        result = Money(0)
        for value in self.amounts.values():
            result = result + value
        return result

    def serialize(self) -> dict[str, Any]:
        return {
            "account": self.account,
            "note": self.note,
            "amounts": {
                str(period): [value.numerator, value.denominator]
                for period, value in sorted(self.amounts.items())
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BudgetLine:
        return cls(
            account=data["account"],
            amounts={int(k): Money(*v) for k, v in data.get("amounts", {}).items()},
            note=data.get("note", ""),
        )

    def __repr__(self) -> str:
        return f"<BudgetLine {self.account[:8]} total={self.total()}>"


class Budget(PrimaryObject):
    """A named set of budget lines covering a contiguous run of periods."""

    TABLE = "budget"

    def __init__(
        self,
        handle: str | None = None,
        name: str = "",
        description: str = "",
        start: date | None = None,
        periods: int = 12,
        kind: PeriodKind | str = PeriodKind.MONTH,
    ) -> None:
        super().__init__(handle)
        self.name = name
        self.description = description
        self.start = start or date(date.today().year, 1, 1)
        self.periods = periods
        self.kind = PeriodKind(kind) if not isinstance(kind, PeriodKind) else kind
        self.lines: dict[str, BudgetLine] = {}
        #: The scenario projected from this budget. A projection that is not tied
        #: to the plan it came from cannot be compared with another plan.
        self.scenario: str | None = None

    # ---------------------------------------------------------------- calendar

    def period_start(self, index: int) -> date:
        return add_months(self.start, index * self.kind.months, day=self.start.day)

    def period_end(self, index: int) -> date:
        nxt = add_months(self.start, (index + 1) * self.kind.months, day=self.start.day)
        return date.fromordinal(nxt.toordinal() - 1)

    def period_label(self, index: int) -> str:
        begin = self.period_start(index)
        if self.kind is PeriodKind.MONTH:
            return f"{begin:%b %Y}"
        if self.kind is PeriodKind.QUARTER:
            return f"Q{(begin.month - 1) // 3 + 1} {begin.year}"
        return str(begin.year)

    def period_for(self, when: date) -> int | None:
        """Index of the period containing ``when``, or ``None`` if out of range."""
        months = (when.year - self.start.year) * 12 + (when.month - self.start.month)
        if when.day < self.start.day:
            months -= 1
        index = months // self.kind.months
        return index if 0 <= index < self.periods else None

    def iter_periods(self) -> Iterator[int]:
        return iter(range(self.periods))

    # ------------------------------------------------------------------- lines

    def line(self, account: str, create: bool = True) -> BudgetLine | None:
        existing = self.lines.get(account)
        if existing is None and create:
            existing = BudgetLine(account)
            self.lines[account] = existing
        return existing

    def set_amount(self, account: str, period: int, value: Money | str | int) -> None:
        line = self.line(account)
        assert line is not None
        line.set_amount(period, value)

    def set_monthly(self, account: str, value: Money | str | int) -> None:
        """Set the same amount in every period of the budget."""
        line = self.line(account)
        assert line is not None
        for period in range(self.periods):
            line.set_amount(period, value)

    def amount(self, account: str, period: int) -> Money:
        line = self.lines.get(account)
        return line.amount(period) if line else Money(0)

    # ------------------------------------------------------------ serialisation

    def _serialize(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "start": self.start.isoformat(),
            "periods": self.periods,
            "kind": self.kind.value,
            "lines": [line.serialize() for line in self.lines.values()],
            "scenario": self.scenario,
        }

    def _unserialize(self, data: dict[str, Any]) -> None:
        self.name = data["name"]
        self.description = data.get("description", "")
        self.start = date.fromisoformat(data["start"])
        self.periods = data.get("periods", 12)
        self.kind = PeriodKind(data.get("kind", "month"))
        self.scenario = data.get("scenario")
        self.lines = {}
        for raw in data.get("lines", []):
            line = BudgetLine.from_dict(raw)
            self.lines[line.account] = line

    def clone(self, name: str) -> Budget:
        """A copy under a new name, with its own identity.

        Used to try an alternative plan without disturbing the one in force: the
        copy carries the figures but not the original's handle or scenario, so
        editing it cannot reach back into the budget it came from.
        """
        copy = Budget(
            name=name,
            description=self.description,
            start=self.start,
            periods=self.periods,
            kind=self.kind,
        )
        for handle, line in self.lines.items():
            copy.lines[handle] = BudgetLine(
                account=handle, amounts=dict(line.amounts), note=line.note
            )
        return copy

    def __repr__(self) -> str:
        return f"<Budget {self.name!r} {self.periods}x{self.kind.value}>"
