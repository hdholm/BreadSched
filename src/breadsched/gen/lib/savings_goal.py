"""Savings goals: money set aside toward a target amount by a target date.

A goal is treated much like a pending bill. From its start date until its target
date, a prorated share of the target is set aside from each income received, so
by the target date the whole target is set aside. The household can also
allocate extra money to a goal at any time; that reduces what later income must
set aside. What is set aside is an earmark on the goal's account, not the whole
account: one savings account may hold money for several goals and for other
purposes at once.

A goal is a milestone only. Reaching the target date spends nothing; the money
stays set aside until the goal is closed (for example after the purchase it was
saved for), when the earmark is released.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from .base import PrimaryObject
from .money import Money

__all__ = ["GoalAllocation", "SavingsGoal"]


@dataclass(frozen=True)
class GoalAllocation:
    """Extra money set aside for a goal on a date, beyond its prorated share."""

    allocated_on: date
    amount: Money
    memo: str = ""

    def serialize(self) -> dict[str, Any]:
        return {
            "allocated_on": self.allocated_on.isoformat(),
            "amount": [self.amount.numerator, self.amount.denominator],
            "memo": self.memo,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GoalAllocation:
        return cls(
            allocated_on=date.fromisoformat(str(data["allocated_on"])),
            amount=Money(*data["amount"]),
            memo=str(data.get("memo", "")),
        )


class SavingsGoal(PrimaryObject):
    """A named target amount to have set aside in an account by a date."""

    TABLE = "savings_goal"

    def __init__(
        self,
        name: str = "",
        account: str = "",
        target_amount: Money | None = None,
        target_date: date | None = None,
        start_date: date | None = None,
        allocations: list[GoalAllocation] | None = None,
        closed_on: date | None = None,
        description: str = "",
        handle: str | None = None,
    ) -> None:
        super().__init__(handle=handle)
        self.name = name
        #: The asset account that holds (or will hold) the goal's money.
        self.account = account
        self.target_amount = target_amount if target_amount is not None else Money(0)
        self.target_date = target_date or date.min
        #: Income received on or after this date sets money aside for the goal.
        self.start_date = start_date or date.min
        self.allocations = sorted(allocations or [], key=lambda item: item.allocated_on)
        #: Closing releases the earmark, e.g. once the money has been spent.
        self.closed_on = closed_on
        self.description = description

    def allocated(self, as_of: date) -> Money:
        """Extra money allocated on or before ``as_of``."""
        return sum(
            (item.amount for item in self.allocations if item.allocated_on <= as_of), Money(0)
        )

    def is_open(self, as_of: date) -> bool:
        return self.closed_on is None or self.closed_on > as_of

    def _serialize(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "account": self.account,
            "target_amount": [self.target_amount.numerator, self.target_amount.denominator],
            "target_date": self.target_date.isoformat(),
            "start_date": self.start_date.isoformat(),
            "allocations": [item.serialize() for item in self.allocations],
            "closed_on": self.closed_on.isoformat() if self.closed_on is not None else None,
            "description": self.description,
        }

    def _unserialize(self, data: dict[str, Any]) -> None:
        raw_closed = data.get("closed_on")
        self.name = str(data.get("name", ""))
        self.account = str(data.get("account", ""))
        self.target_amount = Money(*data["target_amount"])
        self.target_date = date.fromisoformat(str(data["target_date"]))
        self.start_date = date.fromisoformat(str(data["start_date"]))
        self.allocations = sorted(
            (GoalAllocation.from_dict(item) for item in data.get("allocations", [])),
            key=lambda item: item.allocated_on,
        )
        self.closed_on = date.fromisoformat(str(raw_closed)) if raw_closed else None
        self.description = str(data.get("description", ""))

    def __repr__(self) -> str:
        return f"<SavingsGoal {self.name!r} {self.target_amount} by {self.target_date}>"
