"""Reimbursable expenses: an expense and what a payer owes back on it.

A receivable never rewrites or erases the expense splits it points at, and a
reimbursement is never counted as new income: like an ordinary refund, the
ledger-side reimbursement is a split crediting the *same* expense account the
money was originally spent from.

What is still owed sits in a receivable account (issue #170): an asset that
counts toward net worth but never toward liquidity, because the money has not
been received and cannot be spent. BreadSched owns the reclassification
transactions that move it there (``postings``) and recomputes them from the
receivable on every change -- see ``engine.receivables.planned_postings``. A
dispute posts nothing.

A cost may instead be recorded straight to the receivable account, as a charge
an employer will repay often is (#378): such a *direct* receivable needs no
reclassification to hold what is owed, and money back credits that account.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from .base import PrimaryObject
from .money import Money
from .split_link import SplitLink

__all__ = ["Receivable", "ReceivableWriteOff"]


@dataclass(frozen=True)
class ReceivableWriteOff:
    """An amount given up as uncollectible; its reclassification returns it to the expense.

    ``account`` is the expense account it becomes: by default the largest linked
    expense's, and required for a charge recorded straight to a receivable
    account, which has no expense of its own (#378).
    """

    written_off_on: date
    amount: Money
    reason: str = ""
    account: str | None = None

    def serialize(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "written_off_on": self.written_off_on.isoformat(),
            "amount": [self.amount.numerator, self.amount.denominator],
            "reason": self.reason,
        }
        if self.account is not None:
            data["account"] = self.account
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReceivableWriteOff:
        return cls(
            written_off_on=date.fromisoformat(str(data["written_off_on"])),
            amount=Money(*data["amount"]),
            reason=str(data.get("reason", "")),
            account=str(data["account"]) if data.get("account") else None,
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
        expenses: list[SplitLink] | None = None,
        reimbursements: list[SplitLink] | None = None,
        disputed_on: date | None = None,
        dispute_note: str = "",
        write_offs: list[ReceivableWriteOff] | None = None,
        handle: str | None = None,
        account: str | None = None,
        postings: list[str] | None = None,
    ) -> None:
        super().__init__(handle=handle)
        self.incurred_date = incurred_date or date.min
        #: The insurer, employer, or other payer expected to reimburse this.
        self.payer = payer
        self.description = description
        self.expected_amount = expected_amount
        self.expected_cash_date = expected_cash_date
        #: Splits recording the original cost: expense-class splits, or (#378)
        #: charges recorded straight to the receivable account, never both.
        self.expenses = list(expenses or [])
        #: Splits crediting money back: an expense-class split, exactly like an
        #: ordinary refund, or a credit to the receivable account itself (such
        #: as a paycheck line); never an income-class split.
        self.reimbursements = list(reimbursements or [])
        self.disputed_on = disputed_on
        self.dispute_note = dispute_note
        self.write_offs = list(write_offs or [])
        #: The receivable-type asset account holding what is still owed; ``None``
        #: for a receivable recorded before issue #170, which posts nothing.
        self.account = account
        #: Handles of the BreadSched-owned reclassification transactions.
        self.postings = list(postings or [])

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
            "account": self.account,
            "postings": list(self.postings),
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
        self.expenses = [SplitLink.from_dict(item) for item in data.get("expenses", [])]
        self.reimbursements = [SplitLink.from_dict(item) for item in data.get("reimbursements", [])]
        self.disputed_on = (
            date.fromisoformat(str(raw_disputed)) if raw_disputed is not None else None
        )
        self.dispute_note = str(data.get("dispute_note", ""))
        self.write_offs = [
            ReceivableWriteOff.from_dict(item) for item in data.get("write_offs", [])
        ]
        raw_account = data.get("account")
        self.account = str(raw_account) if raw_account else None
        self.postings = [str(item) for item in data.get("postings", [])]

    def __repr__(self) -> str:
        return f"<Receivable {self.payer!r} {self.description!r}>"
