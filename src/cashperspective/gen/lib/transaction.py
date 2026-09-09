"""Transactions and splits.

A transaction owns its splits, the way GnuCash does, rather than splits pointing at
a transaction from the outside.  That ownership is what makes "a transaction is
atomic and always balances" enforceable: the whole thing is written or none of it is.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime
from enum import Enum
from typing import Any

from .base import PrimaryObject, create_handle
from .money import Money

__all__ = ["ReconcileState", "Split", "Transaction", "UnbalancedError"]


class UnbalancedError(ValueError):
    """Raised when a transaction's split values do not sum to zero."""


class ReconcileState(str, Enum):
    NOT_RECONCILED = "n"
    CLEARED = "c"
    RECONCILED = "y"
    FROZEN = "f"
    VOID = "v"


class Split:
    """One leg of a transaction: an amount posted against one account."""

    __slots__ = ("handle", "account", "value", "quantity", "memo", "action",
                 "reconcile", "reconcile_date")

    def __init__(
        self,
        account: str,
        value: Money | str | int,
        quantity: Money | None = None,
        memo: str = "",
        action: str = "",
        reconcile: ReconcileState = ReconcileState.NOT_RECONCILED,
        handle: str | None = None,
    ) -> None:
        self.handle = handle or create_handle()
        self.account = account
        #: Amount in the *transaction's* currency. Splits must sum to zero on this.
        self.value = value if isinstance(value, Money) else Money(value)
        #: Amount in the *account's* commodity (shares, foreign currency units).
        self.quantity = quantity if quantity is not None else self.value
        self.memo = memo
        self.action = action
        self.reconcile = reconcile
        self.reconcile_date: date | None = None

    @property
    def is_debit(self) -> bool:
        return self.value > 0

    def serialize(self) -> dict[str, Any]:
        return {
            "handle": self.handle,
            "account": self.account,
            "value": [self.value.numerator, self.value.denominator],
            "quantity": [self.quantity.numerator, self.quantity.denominator],
            "memo": self.memo,
            "action": self.action,
            "reconcile": self.reconcile.value,
            "reconcile_date": self.reconcile_date.isoformat() if self.reconcile_date else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Split:
        split = cls(
            account=data["account"],
            value=Money(*data["value"]),
            quantity=Money(*data["quantity"]),
            memo=data.get("memo", ""),
            action=data.get("action", ""),
            reconcile=ReconcileState(data.get("reconcile", "n")),
            handle=data["handle"],
        )
        raw = data.get("reconcile_date")
        split.reconcile_date = date.fromisoformat(raw) if raw else None
        return split

    def __repr__(self) -> str:
        return f"<Split {self.account[:8]} {self.value}>"


class Transaction(PrimaryObject):
    """A balanced set of splits posted on one date."""

    TABLE = "txn"

    def __init__(
        self,
        handle: str | None = None,
        post_date: date | None = None,
        description: str = "",
        currency: str | None = None,
        num: str = "",
        splits: Iterable[Split] | None = None,
    ) -> None:
        super().__init__(handle)
        self.post_date = post_date or date.today()
        self.enter_date = datetime.now()
        self.description = description
        self.currency = currency
        self.num = num
        self.notes = ""
        self.scheduled_from: str | None = None  # handle of the originating schedule
        #: Stable identity of the plan occurrence this transaction resolved.
        self.planned_occurrence: str | None = None
        #: Original expected date, retained when an actual posts on a nearby date.
        self.planned_for: date | None = None
        #: Expected gross amount at resolution time; schedule edits cannot rewrite it.
        self.planned_amount: Money | None = None
        self.splits: list[Split] = list(splits or [])

    # ------------------------------------------------------------------ splits

    def add_split(self, split: Split) -> Split:
        self.splits.append(split)
        return split

    def imbalance(self) -> Money:
        total = Money(0)
        for split in self.splits:
            total = total + split.value
        return total

    def is_balanced(self) -> bool:
        return not self.imbalance()

    def describe(self) -> str:
        """Short identification for error messages and logs.

        An error that says only "a transaction needs at least two splits" cannot be
        acted on: with tens of thousands of transactions in a book, the user needs
        to know *which* one before they can go and look at it.
        """
        label = self.description or "(no description)"
        return f"{self.post_date.isoformat()} {label!r} [{self.handle[:8]}]"

    def validate(self) -> None:
        if len(self.splits) < 2:
            raise UnbalancedError(
                f"{self.describe()}: a transaction needs at least two splits, "
                f"found {len(self.splits)}"
            )
        residual = self.imbalance()
        if residual:
            raise UnbalancedError(
                f"{self.describe()}: splits do not balance; residual {residual} "
                f"across {len(self.splits)} splits"
            )

    def accounts(self) -> list[str]:
        seen: list[str] = []
        for split in self.splits:
            if split.account not in seen:
                seen.append(split.account)
        return seen

    def split_for(self, account: str) -> Split | None:
        for split in self.splits:
            if split.account == account:
                return split
        return None

    def value_for(self, account: str) -> Money:
        total = Money(0)
        for split in self.splits:
            if split.account == account:
                total = total + split.value
        return total

    @classmethod
    def simple(
        cls,
        post_date: date,
        description: str,
        debit_account: str,
        credit_account: str,
        amount: Money | str | int,
        currency: str | None = None,
        memo: str = "",
    ) -> Transaction:
        """Two-split helper: money moves *from* credit_account *to* debit_account."""
        value = amount if isinstance(amount, Money) else Money(amount)
        txn = cls(post_date=post_date, description=description, currency=currency)
        txn.add_split(Split(debit_account, value, memo=memo))
        txn.add_split(Split(credit_account, -value, memo=memo))
        return txn

    # ------------------------------------------------------------ serialisation

    def _serialize(self) -> dict[str, Any]:
        return {
            "post_date": self.post_date.isoformat(),
            "enter_date": self.enter_date.isoformat(),
            "description": self.description,
            "currency": self.currency,
            "num": self.num,
            "notes": self.notes,
            "scheduled_from": self.scheduled_from,
            "planned_occurrence": self.planned_occurrence,
            "planned_for": self.planned_for.isoformat() if self.planned_for else None,
            "planned_amount": None
            if self.planned_amount is None
            else [self.planned_amount.numerator, self.planned_amount.denominator],
            "splits": [s.serialize() for s in self.splits],
        }

    def _unserialize(self, data: dict[str, Any]) -> None:
        self.post_date = date.fromisoformat(data["post_date"])
        self.enter_date = datetime.fromisoformat(data["enter_date"])
        self.description = data.get("description", "")
        self.currency = data.get("currency")
        self.num = data.get("num", "")
        self.notes = data.get("notes", "")
        self.scheduled_from = data.get("scheduled_from")
        self.planned_occurrence = data.get("planned_occurrence")
        raw_planned_for = data.get("planned_for")
        self.planned_for = date.fromisoformat(raw_planned_for) if raw_planned_for else None
        raw_planned_amount = data.get("planned_amount")
        self.planned_amount = (
            Money(*raw_planned_amount) if raw_planned_amount is not None else None
        )
        self.splits = [Split.from_dict(s) for s in data.get("splits", [])]

    def __repr__(self) -> str:
        return f"<Transaction {self.post_date} {self.description!r}>"
