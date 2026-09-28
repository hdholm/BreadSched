"""Reimbursable expenses: an expense and what a payer owes back on it.

A receivable never rewrites or erases the expense splits it points at, and a
reimbursement is never counted as new income: like an ordinary refund, the
ledger-side reimbursement is a split crediting the *same* expense account the
money was originally spent from, so it offsets net spending in reports
without touching the original expense split. Disputes and write-offs are
metadata layered over those splits; recording either never posts anything to
the ledger on its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from .base import PrimaryObject
from .money import Money

__all__ = ["Receivable", "ReceivableSplitLink", "ReceivableWriteOff"]


@dataclass(frozen=True)
class ReceivableSplitLink:
    """Reference to one split in an ordinary ledger transaction."""

    transaction: str
    split: str

    def serialize(self) -> dict[str, str]:
        return {"transaction": self.transaction, "split": self.split}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReceivableSplitLink:
        return cls(transaction=str(data["transaction"]), split=str(data["split"]))


@dataclass(frozen=True)
class ReceivableWriteOff:
    """An amount given up as uncollectible, with no ledger posting of its own."""

    written_off_on: date
    amount: Money
    reason: str = ""

    def serialize(self) -> dict[str, Any]:
        return {
            "written_off_on": self.written_off_on.isoformat(),
            "amount": [self.amount.numerator, self.amount.denominator],
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReceivableWriteOff:
        return cls(
            written_off_on=date.fromisoformat(str(data["written_off_on"])),
            amount=Money(*data["amount"]),
            reason=str(data.get("reason", "")),
        )


class Receivable(PrimaryObject):
    """Financial lifecycle for one expense a payer is expected to reimburse."""

    TABLE = "receivable"

    def __init__(
        self,
        incurred_date: date | None = None,
        payer: str = "",
        description: str = "",
        expected_amount: Money | None = None,
        expected_cash_date: date | None = None,
        expenses: list[ReceivableSplitLink] | None = None,
        reimbursements: list[ReceivableSplitLink] | None = None,
        disputed_on: date | None = None,
        dispute_note: str = "",
        write_offs: list[ReceivableWriteOff] | None = None,
        handle: str | None = None,
    ) -> None:
        super().__init__(handle=handle)
        self.incurred_date = incurred_date or date.min
        #: The insurer, employer, or other payer expected to reimburse this.
        self.payer = payer
        self.description = description
        self.expected_amount = expected_amount
        self.expected_cash_date = expected_cash_date
        #: Expense-class splits recording the original out-of-pocket cost.
        self.expenses = list(expenses or [])
        #: Expense-class splits crediting money back, exactly like an ordinary
        #: refund; never an income-class split.
        self.reimbursements = list(reimbursements or [])
        self.disputed_on = disputed_on
        self.dispute_note = dispute_note
        self.write_offs = list(write_offs or [])

    def _serialize(self) -> dict[str, Any]:
        return {
            "incurred_date": self.incurred_date.isoformat(),
            "payer": self.payer,
            "description": self.description,
            "expected_amount": (
                [self.expected_amount.numerator, self.expected_amount.denominator]
                if self.expected_amount is not None
                else None
            ),
            "expected_cash_date": (
                self.expected_cash_date.isoformat() if self.expected_cash_date is not None else None
            ),
            "expenses": [link.serialize() for link in self.expenses],
            "reimbursements": [link.serialize() for link in self.reimbursements],
            "disputed_on": self.disputed_on.isoformat() if self.disputed_on is not None else None,
            "dispute_note": self.dispute_note,
            "write_offs": [item.serialize() for item in self.write_offs],
        }

    def _unserialize(self, data: dict[str, Any]) -> None:
        raw_expected = data.get("expected_amount")
        raw_cash_date = data.get("expected_cash_date")
        raw_disputed = data.get("disputed_on")
        self.incurred_date = date.fromisoformat(str(data["incurred_date"]))
        self.payer = str(data.get("payer", ""))
        self.description = str(data.get("description", ""))
        self.expected_amount = Money(*raw_expected) if raw_expected is not None else None
        self.expected_cash_date = (
            date.fromisoformat(str(raw_cash_date)) if raw_cash_date is not None else None
        )
        self.expenses = [ReceivableSplitLink.from_dict(item) for item in data.get("expenses", [])]
        self.reimbursements = [
            ReceivableSplitLink.from_dict(item) for item in data.get("reimbursements", [])
        ]
        self.disputed_on = (
            date.fromisoformat(str(raw_disputed)) if raw_disputed is not None else None
        )
        self.dispute_note = str(data.get("dispute_note", ""))
        self.write_offs = [
            ReceivableWriteOff.from_dict(item) for item in data.get("write_offs", [])
        ]

    def __repr__(self) -> str:
        return f"<Receivable {self.payer!r} {self.description!r}>"
