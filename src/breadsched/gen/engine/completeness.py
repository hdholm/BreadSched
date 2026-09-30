"""Whether a reported value covers everything it should (#236).

A reporting-currency figure is **complete** when every input converted,
**partial** when it is a labelled subtotal of the inputs that converted, and
**unavailable** when the report withholds it rather than show a subtotal. A
genuine zero is complete. Each report chooses its policy — Plan, Expense
Explorer and Projection show partial subtotals, while balances, net worth and
the Dashboard withhold — but every result carries the same structured status
and the evidence of what was left out, so adapters never infer completeness
from note text.

Completeness concerns valuation only. A value that does not apply yet, such as
the variance of a future period, is *not applicable*, which is a separate,
time-based state.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from enum import Enum

from ..lib.money import Money

__all__ = [
    "Completeness",
    "CompletenessStatus",
    "Excluded",
    "Policy",
    "combine",
]


class CompletenessStatus(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


class Policy(str, Enum):
    """What a report does with a value some inputs of which lack a conversion."""

    #: Show the subtotal of the inputs that converted, labelled partial.
    SUBTOTAL = "subtotal"
    #: Show nothing; a partial balance or net worth would mislead.
    WITHHOLD = "withhold"


@dataclass(frozen=True, slots=True)
class Excluded:
    """One input left out for lack of a reporting-currency valuation.

    ``amount`` is in ``currency`` (a commodity mnemonic), never in reporting
    units. ``reason`` says which quote is missing: an exchange rate for a
    foreign currency, or a price for a security.
    """

    kind: str
    label: str
    currency: str
    amount: Money | None
    when: date | None
    reason: str = "exchange rate"
    #: Handles of the accounts involved, so a view can tell which rows it affects.
    accounts: tuple[str, ...] = ()

    @property
    def action(self) -> str:
        if self.reason == "price":
            return f"Add a {self.currency} price in Accounts to include it."
        return f"Add a {self.currency} exchange rate in Accounts to include it."

    def text(self) -> str:
        amount = f" {self.amount.format()} {self.currency}" if self.amount is not None else ""
        dated = f" ({self.kind} {self.when.isoformat()})" if self.when is not None else ""
        return f"{self.label}{amount}{dated}: no {self.reason}"

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "label": self.label,
            "currency": self.currency,
            "amount": self.amount,
            "date": self.when,
            "reason": self.reason,
            "action": self.action,
            "accounts": list(self.accounts),
        }


@dataclass(frozen=True, slots=True)
class Completeness:
    """Structured coverage of one value or series, with the excluded evidence."""

    status: CompletenessStatus = CompletenessStatus.COMPLETE
    policy: Policy = Policy.SUBTOTAL
    excluded: tuple[Excluded, ...] = field(default=())
    as_of: date | None = None

    @classmethod
    def of(
        cls,
        excluded: Iterable[Excluded],
        *,
        policy: Policy,
        as_of: date | None = None,
    ) -> Completeness:
        """Complete when nothing was excluded, else partial or unavailable by policy."""
        items = tuple(excluded)
        if not items:
            return cls(CompletenessStatus.COMPLETE, policy, (), as_of)
        status = (
            CompletenessStatus.UNAVAILABLE
            if policy is Policy.WITHHOLD
            else CompletenessStatus.PARTIAL
        )
        return cls(status, policy, items, as_of)

    @property
    def complete(self) -> bool:
        return self.status is CompletenessStatus.COMPLETE

    @property
    def label(self) -> str:
        """Short text shown beside the value; empty when complete."""
        count = len(self.excluded)
        noun = "amount" if count == 1 else "amounts"
        if self.status is CompletenessStatus.PARTIAL:
            return f"Partial: excludes {count} unconverted {noun}"
        if self.status is CompletenessStatus.UNAVAILABLE:
            verb = "lacks" if count == 1 else "lack"
            return f"Unavailable: {count} {noun} {verb} a quote"
        return ""

    @property
    def currencies(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(item.currency for item in self.excluded))

    def touching(self, accounts: set[str] | frozenset[str]) -> Completeness:
        """Only the exclusions that involve one of ``accounts``."""
        return Completeness.of(
            (item for item in self.excluded if accounts.intersection(item.accounts)),
            policy=self.policy,
            as_of=self.as_of,
        )

    def detail(self) -> tuple[str, ...]:
        """One line per excluded input, then the corrective action per currency."""
        if self.complete:
            return ()
        lines = [item.text() for item in self.excluded]
        lines.extend(
            dict.fromkeys(item.action for item in self.excluded),
        )
        return tuple(lines)

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "policy": self.policy.value,
            "label": self.label,
            "detail": list(self.detail()),
            "as_of": self.as_of,
            "excluded": [item.as_dict() for item in self.excluded],
        }


def combine(*parts: Completeness) -> Completeness:
    """Coverage of a value computed from several inputs, such as a delta.

    The worst status wins and all evidence is kept once. A comparison of two
    partial values is itself partial: equal labels do not mean equal coverage.
    """
    order = (
        CompletenessStatus.COMPLETE,
        CompletenessStatus.PARTIAL,
        CompletenessStatus.UNAVAILABLE,
    )
    status = max((part.status for part in parts), key=order.index, default=order[0])
    excluded = tuple(dict.fromkeys(item for part in parts for item in part.excluded))
    policy = next((part.policy for part in parts if part.status is status), Policy.SUBTOTAL)
    as_of = next((part.as_of for part in parts if part.as_of is not None), None)
    return Completeness(status, policy, excluded, as_of)
