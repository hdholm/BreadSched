"""Persisted bank-statement reconciliation sessions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any

from .base import PrimaryObject
from .money import Money

__all__ = ["Reconciliation", "ReconciliationEvent", "ReconciliationStatus"]


class ReconciliationStatus(str, Enum):
    """Lifecycle state retained for an auditable reconciliation history."""

    OPEN = "open"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ReconciliationEvent:
    """One immutable lifecycle entry retained with a statement session."""

    action: str
    occurred_at: datetime

    def serialize(self) -> dict[str, str]:
        return {"action": self.action, "occurred_at": self.occurred_at.isoformat()}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReconciliationEvent:
        return cls(
            action=str(data["action"]),
            occurred_at=datetime.fromisoformat(str(data["occurred_at"])),
        )


class Reconciliation(PrimaryObject):
    """One statement balance and the account splits checked against it."""

    TABLE = "reconciliation"

    def __init__(
        self,
        account: str = "",
        statement_date: date | None = None,
        ending_balance: Money | str | int = 0,
        selected_splits: list[str] | None = None,
        status: ReconciliationStatus | str = ReconciliationStatus.OPEN,
        handle: str | None = None,
    ) -> None:
        super().__init__(handle=handle)
        self.account = account
        self.statement_date = statement_date or date.today()
        self.ending_balance = (
            ending_balance if isinstance(ending_balance, Money) else Money(ending_balance)
        )
        self.selected_splits = list(selected_splits or [])
        self.status = (
            status if isinstance(status, ReconciliationStatus) else ReconciliationStatus(status)
        )
        self.opened_at = datetime.now(timezone.utc)
        self.completed_at: datetime | None = None
        self.cancelled_at: datetime | None = None
        self.audit_events = [ReconciliationEvent("opened", self.opened_at)]

    def _serialize(self) -> dict[str, Any]:
        return {
            "account": self.account,
            "statement_date": self.statement_date.isoformat(),
            "ending_balance": [self.ending_balance.numerator, self.ending_balance.denominator],
            "selected_splits": list(self.selected_splits),
            "status": self.status.value,
            "opened_at": self.opened_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "cancelled_at": self.cancelled_at.isoformat() if self.cancelled_at else None,
            "audit_events": [event.serialize() for event in self.audit_events],
        }

    def _unserialize(self, data: dict[str, Any]) -> None:
        self.account = str(data["account"])
        self.statement_date = date.fromisoformat(str(data["statement_date"]))
        self.ending_balance = Money(*data["ending_balance"])
        self.selected_splits = [str(handle) for handle in data.get("selected_splits", [])]
        self.status = ReconciliationStatus(str(data.get("status", "open")))
        self.opened_at = datetime.fromisoformat(str(data["opened_at"]))
        completed = data.get("completed_at")
        cancelled = data.get("cancelled_at")
        self.completed_at = datetime.fromisoformat(str(completed)) if completed else None
        self.cancelled_at = datetime.fromisoformat(str(cancelled)) if cancelled else None
        self.audit_events = [
            ReconciliationEvent.from_dict(item) for item in data.get("audit_events", [])
        ]
        if not self.audit_events:
            self.audit_events = [ReconciliationEvent("opened", self.opened_at)]
