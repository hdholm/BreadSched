"""Why Review suggests what it does, and why it suggests nothing.

``planning.match_candidates`` ranks the scheduled occurrences near an actual
transaction. This module says, for every interface alike, what made each
candidate a candidate (the accounts it shares, how close its amount and date are,
whether its name matches) and how close a match it is. When there is no
candidate it says why: the nearest occurrence sharing an account is outside the
matching window or in another currency, every candidate was rejected, or no
schedule uses the transaction's accounts at all. For a transaction that moves
FSA money it names the flow (:mod:`fsa_flows`) and what to do with it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from ..db.sqlite import DbSQLite
from ..lib.account import AccountType
from ..lib.money import Money
from ..lib.transaction import Transaction
from . import fsa_flows, planning
from .currency import reporting_currency_handle
from .fsa_flows import FsaFlowKind

__all__ = [
    "CLOSE",
    "POSSIBLE",
    "CandidateExplanation",
    "explain_candidate",
    "explain_candidates",
    "fsa_hint",
    "no_candidate_reason",
]

#: A candidate on nearly the same day for nearly the same amount.
CLOSE = "close"
#: Any other candidate within the matching window.
POSSIBLE = "possible"

#: How far (days) to look for an occurrence to explain an empty candidate list.
_EXPLAIN_WINDOW_DAYS = 60
_WORD = re.compile(r"[a-z0-9]+")
_IGNORED_WORDS = {"the", "and", "for", "of", "to", "a", "an", "payment", "pay"}


@dataclass(frozen=True, slots=True)
class CandidateExplanation:
    candidate: planning.MatchCandidate
    confidence: str
    reasons: tuple[str, ...]

    @property
    def label(self) -> str:
        return "Close match" if self.confidence == CLOSE else "Possible match"

    def as_dict(self) -> dict[str, object]:
        return {"confidence": self.confidence, "label": self.label, "reasons": list(self.reasons)}


def _words(text: str) -> set[str]:
    return {
        word for word in _WORD.findall(text.lower()) if len(word) > 2 and word not in _IGNORED_WORDS
    }


def _days(count: int) -> str:
    return "1 day" if count == 1 else f"{count} days"


def _account_names(db: DbSQLite, handles: set[str]) -> str:
    names = sorted(
        account.name for handle in handles if (account := db.get_account(handle)) is not None
    )
    return ", ".join(names)


def _gross(transaction: Transaction) -> Money:
    total = Money(0)
    for split in transaction.splits:
        if split.value > 0:
            total = total + split.value
    return total


def explain_candidate(
    db: DbSQLite, transaction: Transaction, candidate: planning.MatchCandidate
) -> CandidateExplanation:
    """The reasons ``candidate`` is offered for ``transaction`` and how close it is."""
    event = candidate.event
    reasons: list[str] = []
    shared = {split.account for split in transaction.splits} & {
        split.account for split in event.expected_splits
    }
    if shared:
        reasons.append(f"Uses the same account: {_account_names(db, shared)}")
    actual = _gross(transaction)
    difference = candidate.amount_difference
    if not difference:
        reasons.append("Same amount")
        amount_close = True
    else:
        direction = "more" if actual > event.expected_amount else "less"
        share = (
            (difference.to_decimal() / event.expected_amount.to_decimal() * 100)
            if event.expected_amount
            else Decimal(100)
        )
        reasons.append(
            f"{difference.format()} {direction} than expected ({share.quantize(Decimal(1))}%)"
        )
        amount_close = share <= 5
    offset = (transaction.post_date - event.planned_date).days
    if offset == 0:
        reasons.append("Same day as planned")
    else:
        when = "after" if offset > 0 else "before"
        reasons.append(f"{_days(abs(offset))} {when} the planned date")
    common = _words(transaction.description) & _words(event.description)
    if common:
        reasons.append(f"Description shares “{', '.join(sorted(common))}”")
    confidence = CLOSE if amount_close and abs(offset) <= 2 else POSSIBLE
    return CandidateExplanation(candidate, confidence, tuple(reasons))


def explain_candidates(db: DbSQLite, transaction: Transaction) -> list[CandidateExplanation]:
    """Every current candidate for ``transaction``, in ranking order, with its reasons."""
    return [
        explain_candidate(db, transaction, candidate)
        for candidate in planning.match_candidates(db, transaction)
    ]


def no_candidate_reason(db: DbSQLite, transaction: Transaction, *, window_days: int = 7) -> str:
    """Why Review offers no scheduled occurrence for ``transaction``."""
    accounts = {split.account for split in transaction.splits}
    start = transaction.post_date - timedelta(days=_EXPLAIN_WINDOW_DAYS)
    end = transaction.post_date + timedelta(days=_EXPLAIN_WINDOW_DAYS)
    currency = transaction.currency or reporting_currency_handle(db)
    open_events = [
        event
        for event in planning.scheduled_events(db, start, end, include_actualized=True)
        if event.status is not planning.EventStatus.ACTUALIZED
        and accounts & {split.account for split in event.expected_splits}
    ]
    rejected = [
        event for event in open_events if event.key in transaction.rejected_plan_occurrences
    ]
    usable = [
        event for event in open_events if event.key not in transaction.rejected_plan_occurrences
    ]
    nearby = [
        event
        for event in usable
        if abs((transaction.post_date - event.planned_date).days) <= window_days
    ]
    other_currency = [event for event in nearby if event.expected_currency != currency]
    if other_currency:
        event = other_currency[0]
        commodity = db.get_commodity(event.expected_currency) if event.expected_currency else None
        name = commodity.mnemonic if commodity is not None else "another currency"
        return (
            f"“{event.description}” on {event.planned_date.isoformat()} is planned in "
            f"{name}, so it is not offered for a transaction in another currency."
        )
    if rejected and not nearby:
        count = len(rejected)
        noun = "candidate" if count == 1 else "candidates"
        return (
            f"You rejected {count} {noun} for this transaction. Mark it Unexpected if "
            "it was not planned."
        )
    if usable:
        nearest = min(
            usable, key=lambda event: abs((transaction.post_date - event.planned_date).days)
        )
        distance = abs((transaction.post_date - nearest.planned_date).days)
        return (
            f"The nearest scheduled item using these accounts, “{nearest.description}” on "
            f"{nearest.planned_date.isoformat()}, is {_days(distance)} away; Review offers "
            f"items within {window_days} days. Mark this Unexpected if it was not planned."
        )
    return (
        "No schedule uses this transaction's accounts. Mark it Unexpected if it was not "
        "planned, or create a schedule for it in Scheduled."
    )


_FSA_HINTS = {
    FsaFlowKind.FUNDING: (
        "Money into the FSA from payroll or a contribution: it funds the account but "
        "does not use the election, so it needs no claim."
    ),
    FsaFlowKind.DIRECT_PAYMENT: (
        "Paid from the FSA card: attach it to the claim for this service as “Paid from "
        "the FSA card”, which records the payment and its reimbursement at once."
    ),
    FsaFlowKind.REIMBURSEMENT: (
        "The FSA paid money out to you: attach it to the claim it reimburses as "
        "“FSA reimbursement”."
    ),
    FsaFlowKind.PROVIDER_REFUND: (
        "A provider refunded money to the FSA card: attach it to the claim as "
        "“Refunded to the FSA card”, which gives that much of the election back."
    ),
    FsaFlowKind.REPAYMENT: (
        "Money paid back into the FSA for an over-reimbursed claim; it is already linked."
    ),
    FsaFlowKind.TRANSFER: "A move between FSA accounts; it needs no claim.",
}


def fsa_hint(db: DbSQLite, transaction: Transaction) -> str | None:
    """What an FSA movement in ``transaction`` is and what to do with it, or ``None``."""
    accounts = {account.handle: account for account in db.iter_accounts()}
    for split in transaction.splits:
        account = accounts.get(split.account)
        if account is not None and account.atype is AccountType.FSA:
            kind = fsa_flows.classify(transaction, split, accounts)
            return _FSA_HINTS[kind]
    return None
