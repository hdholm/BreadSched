"""Relationships an imported book does not state, proposed and applied (#356).

``engine.inference`` finds them: the asset each loan was borrowed against, and each
credit card's payment account, payment day, whether it is paid in full, and its
usual payment. This lists them with their evidence for any interface and applies
those at or above a confidence, as one undoable step, without sending the
proposed values back and forth.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..db.sqlite import DbSQLite
from ..engine import inference
from .contracts import ServiceError, ServiceResult

__all__ = [
    "AccountSuggestion",
    "AppliedSuggestions",
    "ApplyAccountSuggestions",
    "account_suggestions",
    "apply_account_suggestions",
]


@dataclass(frozen=True, slots=True)
class AccountSuggestion:
    """One proposed account setting, as it is shown."""

    account: str
    account_name: str
    field: str
    value: str
    #: From 0 to 1, to two places.
    confidence: Decimal
    reason: str


@dataclass(frozen=True, slots=True)
class ApplyAccountSuggestions:
    #: Apply the suggestions at least this confident (0 to 1).
    min_confidence: Decimal = Decimal("0.6")
    message: str = "Apply inferred account relationships"


@dataclass(frozen=True, slots=True)
class AppliedSuggestions:
    applied: int
    considered: int


def _confidence(suggestion: inference.Suggestion) -> Decimal:
    return Decimal(str(round(suggestion.confidence, 2)))


def _shown(suggestion: inference.Suggestion) -> AccountSuggestion:
    return AccountSuggestion(
        suggestion.account,
        suggestion.account_name,
        suggestion.field,
        suggestion.value_label or str(suggestion.value),
        _confidence(suggestion),
        suggestion.reason,
    )


def account_suggestions(db: DbSQLite) -> tuple[AccountSuggestion, ...]:
    """Every relationship the book suggests, most confident first; writes nothing."""
    return tuple(_shown(item) for item in inference.infer_all(db).suggestions)


def apply_account_suggestions(
    db: DbSQLite, request: ApplyAccountSuggestions
) -> ServiceResult[AppliedSuggestions]:
    if not 0 <= request.min_confidence <= 1:
        return ServiceResult.failure(
            ServiceError("inference.confidence.invalid", ("min_confidence",))
        )
    found = inference.infer_all(db).suggestions
    chosen = [item for item in found if _confidence(item) >= request.min_confidence]
    applied = inference.apply_suggestions(db, chosen, message=request.message)
    return ServiceResult.success(AppliedSuggestions(applied, len(found)))
