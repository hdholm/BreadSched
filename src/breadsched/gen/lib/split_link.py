"""A reference from a BreadSched record to one split of an ordinary ledger transaction.

FSA claims (payments, refunds, reimbursements, repayments) and receivables
(expenses) point at the ledger the same way, and store the same form:
``{"transaction": ..., "split": ...}`` (#338).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["SplitLink"]


@dataclass(frozen=True)
class SplitLink:
    """Reference to one split in an ordinary ledger transaction."""

    transaction: str
    split: str

    def serialize(self) -> dict[str, str]:
        return {"transaction": self.transaction, "split": self.split}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SplitLink:
        return cls(transaction=str(data["transaction"]), split=str(data["split"]))
