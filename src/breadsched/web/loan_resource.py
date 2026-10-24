"""HTTP input and output for creating a loan through the shared loan service.

This adapter parses browser terms into ``loans.LoanTerms`` and translates the
preview and save results. Validation and the write stay in ``services.loans``; on
the experimental book-server branch (#354) both go through the book server,
``api.book``.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

from ..gen.engine import loans
from ..gen.services import SaveLoan
from .controls import input_money

if TYPE_CHECKING:
    from .context import Api


def loan_options(api: Api) -> dict:
    """Return the accounts accepted by the shared loan creator."""
    found = api.book.loan_choices()

    def choices(items) -> list[dict[str, str]]:
        return [{"handle": item.handle, "name": item.name} for item in items]

    return {
        "liabilities": choices(found.liabilities),
        "expenses": choices(found.expenses),
        "payment_accounts": choices(found.payment_accounts),
    }


def loan_terms(fraction: int, payload: dict) -> loans.LoanTerms:
    """Parse untrusted web input into the shared typed loan contract."""
    name = str(payload.get("name") or "").strip()
    principal = input_money(payload, payload.get("principal", ""))
    try:
        annual_rate = Decimal(str(payload.get("annual_rate") or "0")) / Decimal(100)
        years = int(payload.get("years") or 0)
        start = date.fromisoformat(str(payload.get("start") or ""))
    except (ValueError, ArithmeticError) as exc:
        raise ValueError("enter a valid rate, term, and first-payment date") from exc
    return loans.LoanTerms(
        name=name,
        principal=principal,
        annual_rate=annual_rate,
        years=years,
        start=start,
        liability=str(payload.get("liability") or ""),
        interest_account=str(payload.get("interest_account") or ""),
        payment_account=str(payload.get("payment_account") or ""),
        fraction=fraction,
    )


def loan_preview(api: Api, payload: dict) -> dict:
    terms = loan_terms(api.book.loan_choices().fraction, payload)
    preview = api.book.preview_loan(terms)
    return {
        "payment": preview.payment,
        "total_interest": preview.total_interest,
        "rows": [
            {
                "period": row.period,
                "payment": row.payment,
                "interest": row.interest,
                "principal": row.principal,
                "balance": row.balance,
            }
            for row in preview.rows
        ],
    }


def loan_save(api: Api, payload: dict) -> dict:
    terms = loan_terms(api.book.loan_choices().fraction, payload)
    opening_balance = payload.get("opening_balance", True)
    if not isinstance(opening_balance, bool):
        raise ValueError("opening_balance must be true or false")
    saved = api.book.save_loan(SaveLoan(terms, opening_balance=opening_balance))
    return {
        "handle": saved.handle,
        "name": saved.name,
        "payment": saved.payment,
    }
