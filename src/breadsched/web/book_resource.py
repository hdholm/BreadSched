"""HTTP output for the open book as a whole: its summary and verification report.

Both come from the book server (``book_summary``, ``verify_book``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..versioning import version_details

if TYPE_CHECKING:
    from .context import Api


def summary(api: Api) -> dict:
    found = api.book.book_summary()
    cash, net_worth = found.cash, found.net_worth
    return {
        "book": found.path,
        "accounts": found.accounts,
        "transactions": found.transactions,
        "cash": cash.amount.value if cash.amount is not None else None,
        "cash_missing_quotes": list(cash.missing_quotes),
        "net_worth": net_worth.amount.value if net_worth.amount is not None else None,
        "net_worth_missing_quotes": list(net_worth.missing_quotes),
        "net_worth_incompatible_accounts": list(net_worth.incompatible_accounts),
        "scenarios": list(found.scenarios),
    }


def verify(api: Api) -> dict[str, object]:
    """Run physical and logical checks without modifying the open book."""
    report = api.book.verify_book()
    payload = report.as_dict()
    payload.update(version_details(native_schema_version=report.native_schema_version))
    return payload
