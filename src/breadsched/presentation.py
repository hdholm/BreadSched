"""Shared adapter-owned wording for stable service errors."""

from __future__ import annotations

from .gen.services import ServiceError

_SERVICE_MESSAGES = {
    "transaction.not_found": "The transaction no longer exists",
    "transaction.source.mismatch": "The transaction changed while it was being edited",
    "transaction.description.required": "Give the transaction a description",
    "transaction.splits.too_few": "A transaction needs at least two splits",
    "transaction.accounts.same": "Use at least two different accounts",
    "transaction.account.not_found": "Choose an existing account for every split",
    "transaction.account.hidden": "Hidden accounts cannot be used for new transactions",
    "transaction.split.duplicate": "A transaction split was submitted more than once",
    "transaction.split.not_found": "A transaction split no longer exists",
    "transaction.unbalanced": "The transaction is out of balance",
    "transaction.investment.invalid": "Check the investment activity classification",
    "transaction.claim.not_found": "The selected FSA claim no longer exists",
    "transaction.claim.invalid": "The transaction cannot be attached to that FSA claim",
}


def service_error_message(error: ServiceError) -> str:
    """Translate a machine-readable service failure at the presentation boundary."""
    return _SERVICE_MESSAGES.get(error.code, error.code)
