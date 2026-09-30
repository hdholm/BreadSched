"""FSA healthcare-service claims linked to ordinary ledger splits."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .base import PrimaryObject
from .money import Money

__all__ = [
    "FsaClaim",
    "FsaClaimAllocation",
    "FsaClaimEvent",
    "FsaClaimRejection",
    "FsaClaimSplitLink",
]


@dataclass(frozen=True)
class FsaClaimSplitLink:
    """Reference to one split in an ordinary ledger transaction."""

    transaction: str
    split: str

    def serialize(self) -> dict[str, str]:
        return {"transaction": self.transaction, "split": self.split}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FsaClaimSplitLink:
        return cls(transaction=str(data["transaction"]), split=str(data["split"]))


@dataclass(frozen=True)
class FsaClaimRejection:
    """A rejected or failed reimbursement attempt with no ledger posting."""

    attempted_on: date
    amount: Money
    reason: str = ""

    def serialize(self) -> dict[str, Any]:
        return {
            "attempted_on": self.attempted_on.isoformat(),
            "amount": [self.amount.numerator, self.amount.denominator],
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FsaClaimRejection:
        return cls(
            attempted_on=date.fromisoformat(str(data["attempted_on"])),
            amount=Money(*data["amount"]),
            reason=str(data.get("reason", "")),
        )


def _money(value: Money | None) -> list[int] | None:
    return [value.numerator, value.denominator] if value is not None else None


def _unmoney(raw: Any) -> Money | None:
    return Money(*raw) if raw is not None else None


@dataclass(frozen=True)
class FsaClaimEvent:
    """One correction in a claim's life: a changed EOB, or the claim closed or reopened.

    ``previous`` and ``current`` are the EOB responsibility before and after an
    ``eob_changed`` event and are ``None`` for the others.
    """

    EOB_CHANGED = "eob_changed"
    CLOSED = "closed"
    REOPENED = "reopened"

    kind: str
    on: date
    previous: Money | None = None
    current: Money | None = None
    note: str = ""

    def serialize(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "on": self.on.isoformat(),
            "previous": _money(self.previous),
            "current": _money(self.current),
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FsaClaimEvent:
        return cls(
            kind=str(data["kind"]),
            on=date.fromisoformat(str(data["on"])),
            previous=_unmoney(data.get("previous")),
            current=_unmoney(data.get("current")),
            note=str(data.get("note", "")),
        )


@dataclass
class FsaClaimAllocation:
    """One FSA funding source participating in a healthcare claim."""

    account: str
    funding_year_start: date
    target: Money | None = None
    reimbursements: list[FsaClaimSplitLink] = field(default_factory=list)
    rejections: list[FsaClaimRejection] = field(default_factory=list)
    #: Money paid back into the FSA for this funding year, such as an
    #: over-reimbursement returned after a lower EOB.
    repayments: list[FsaClaimSplitLink] = field(default_factory=list)

    def serialize(self) -> dict[str, Any]:
        return {
            "account": self.account,
            "funding_year_start": self.funding_year_start.isoformat(),
            "target": (
                [self.target.numerator, self.target.denominator]
                if self.target is not None
                else None
            ),
            "reimbursements": [link.serialize() for link in self.reimbursements],
            "rejections": [item.serialize() for item in self.rejections],
            "repayments": [link.serialize() for link in self.repayments],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FsaClaimAllocation:
        raw_target = data.get("target")
        return cls(
            account=str(data["account"]),
            funding_year_start=date.fromisoformat(str(data["funding_year_start"])),
            target=Money(*raw_target) if raw_target is not None else None,
            reimbursements=[
                FsaClaimSplitLink.from_dict(item) for item in data.get("reimbursements", [])
            ],
            rejections=[FsaClaimRejection.from_dict(item) for item in data.get("rejections", [])],
            repayments=[FsaClaimSplitLink.from_dict(item) for item in data.get("repayments", [])],
        )


class FsaClaim(PrimaryObject):
    """Financial lifecycle for one healthcare service episode."""

    TABLE = "fsa_claim"

    def __init__(
        self,
        service_date: date | None = None,
        provider: str = "",
        description: str = "",
        eob_responsibility: Money | None = None,
        payments: list[FsaClaimSplitLink] | None = None,
        refunds: list[FsaClaimSplitLink] | None = None,
        allocations: list[FsaClaimAllocation] | None = None,
        handle: str | None = None,
        receivable: str | None = None,
        closed_on: date | None = None,
        close_reason: str = "",
        events: list[FsaClaimEvent] | None = None,
    ) -> None:
        super().__init__(handle=handle)
        self.service_date = service_date or date.min
        self.provider = provider
        self.description = description
        self.eob_responsibility = eob_responsibility
        self.payments = list(payments or [])
        self.refunds = list(refunds or [])
        self.allocations = list(allocations or [])
        #: A receivable whose payer covers part of this same expense; the FSA
        #: then claims only the remainder (issue #192). Linked from the claim.
        self.receivable = receivable
        #: Set when the household stops pursuing what is left to reimburse.
        self.closed_on = closed_on
        self.close_reason = close_reason
        #: EOB changes and closings/reopenings, oldest first.
        self.events = list(events or [])

    def links(self) -> list[FsaClaimSplitLink]:
        """Every ledger split the claim points at: payments, refunds, and FSA money."""
        found = [*self.payments, *self.refunds]
        for allocation in self.allocations:
            found.extend(allocation.reimbursements)
            found.extend(allocation.repayments)
        return found

    def _serialize(self) -> dict[str, Any]:
        return {
            "service_date": self.service_date.isoformat(),
            "provider": self.provider,
            "description": self.description,
            "eob_responsibility": (
                [self.eob_responsibility.numerator, self.eob_responsibility.denominator]
                if self.eob_responsibility is not None
                else None
            ),
            "payments": [link.serialize() for link in self.payments],
            "refunds": [link.serialize() for link in self.refunds],
            "allocations": [allocation.serialize() for allocation in self.allocations],
            "receivable": self.receivable,
            "closed_on": self.closed_on.isoformat() if self.closed_on is not None else None,
            "close_reason": self.close_reason,
            "events": [event.serialize() for event in self.events],
        }

    def _unserialize(self, data: dict[str, Any]) -> None:
        raw_eob = data.get("eob_responsibility")
        self.service_date = date.fromisoformat(str(data["service_date"]))
        self.provider = str(data.get("provider", ""))
        self.description = str(data.get("description", ""))
        self.eob_responsibility = Money(*raw_eob) if raw_eob is not None else None
        self.payments = [FsaClaimSplitLink.from_dict(item) for item in data.get("payments", [])]
        self.refunds = [FsaClaimSplitLink.from_dict(item) for item in data.get("refunds", [])]
        self.allocations = [
            FsaClaimAllocation.from_dict(item) for item in data.get("allocations", [])
        ]
        raw_receivable = data.get("receivable")
        self.receivable = str(raw_receivable) if raw_receivable else None
        raw_closed = data.get("closed_on")
        self.closed_on = date.fromisoformat(str(raw_closed)) if raw_closed else None
        self.close_reason = str(data.get("close_reason", ""))
        self.events = [FsaClaimEvent.from_dict(item) for item in data.get("events", [])]
