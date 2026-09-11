"""FSA healthcare-service claims linked to ordinary ledger splits."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .base import create_handle
from .money import Money

__all__ = [
    "FsaClaim",
    "FsaClaimAllocation",
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


@dataclass
class FsaClaimAllocation:
    """One FSA funding source participating in a healthcare claim."""

    account: str
    funding_year_start: date
    target: Money | None = None
    reimbursements: list[FsaClaimSplitLink] = field(default_factory=list)
    rejections: list[FsaClaimRejection] = field(default_factory=list)

    def serialize(self) -> dict[str, Any]:
        return {
            "account": self.account,
            "funding_year_start": self.funding_year_start.isoformat(),
            "target": (
                [self.target.numerator, self.target.denominator]
                if self.target is not None else None
            ),
            "reimbursements": [link.serialize() for link in self.reimbursements],
            "rejections": [item.serialize() for item in self.rejections],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FsaClaimAllocation:
        raw_target = data.get("target")
        return cls(
            account=str(data["account"]),
            funding_year_start=date.fromisoformat(str(data["funding_year_start"])),
            target=Money(*raw_target) if raw_target is not None else None,
            reimbursements=[
                FsaClaimSplitLink.from_dict(item)
                for item in data.get("reimbursements", [])
            ],
            rejections=[
                FsaClaimRejection.from_dict(item)
                for item in data.get("rejections", [])
            ],
        )


@dataclass
class FsaClaim:
    """Financial lifecycle for one healthcare service episode."""

    service_date: date
    provider: str = ""
    description: str = ""
    eob_responsibility: Money | None = None
    payments: list[FsaClaimSplitLink] = field(default_factory=list)
    refunds: list[FsaClaimSplitLink] = field(default_factory=list)
    allocations: list[FsaClaimAllocation] = field(default_factory=list)
    handle: str = field(default_factory=create_handle)

    def serialize(self) -> dict[str, Any]:
        return {
            "handle": self.handle,
            "service_date": self.service_date.isoformat(),
            "provider": self.provider,
            "description": self.description,
            "eob_responsibility": (
                [self.eob_responsibility.numerator, self.eob_responsibility.denominator]
                if self.eob_responsibility is not None else None
            ),
            "payments": [link.serialize() for link in self.payments],
            "refunds": [link.serialize() for link in self.refunds],
            "allocations": [allocation.serialize() for allocation in self.allocations],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FsaClaim:
        raw_eob = data.get("eob_responsibility")
        return cls(
            handle=str(data["handle"]),
            service_date=date.fromisoformat(str(data["service_date"])),
            provider=str(data.get("provider", "")),
            description=str(data.get("description", "")),
            eob_responsibility=Money(*raw_eob) if raw_eob is not None else None,
            payments=[FsaClaimSplitLink.from_dict(item) for item in data.get("payments", [])],
            refunds=[FsaClaimSplitLink.from_dict(item) for item in data.get("refunds", [])],
            allocations=[
                FsaClaimAllocation.from_dict(item) for item in data.get("allocations", [])
            ],
        )
