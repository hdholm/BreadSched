"""HTTP input and output for creating a loan through the shared loan service.

This adapter parses browser terms into ``loans.LoanTerms`` and translates the
preview and save results. Validation and the write stay in ``services.loans``.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

from ..gen.db.sqlite import DbSQLite
from ..gen.engine import loans
from ..gen.engine.currency import reporting_fraction
from ..gen.lib import AccountClass
from ..gen.services import SaveLoan, save_loan, validate_loan
from .controls import input_money, service_error

if TYPE_CHECKING:
    from .context import Api


def loan_options(api: Api) -> dict:
    """Return the accounts accepted by the shared loan creator."""

    def choices(predicate) -> list[dict[str, str]]:
        return [
            {"handle": account.handle, "name": api.db.full_name(account)}
            for account in sorted(api.db.iter_accounts(), key=api.db.full_name)
            if predicate(account)
            and not account.is_root
            and not account.placeholder
            and not account.hidden
        ]

    return {
        "liabilities": choices(lambda account: account.account_class is AccountClass.LIABILITY),
        "expenses": choices(lambda account: account.account_class is AccountClass.EXPENSE),
        "payment_accounts": choices(lambda account: account.atype.is_cash_like),
    }


def loan_terms(db: DbSQLite, payload: dict) -> loans.LoanTerms:
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
        fraction=reporting_fraction(db),
    )


def loan_preview(api: Api, payload: dict) -> dict:
    terms = loan_terms(api.db, payload)
    errors = validate_loan(api.db, terms)
    if errors:
        raise service_error(errors[0])
    return {
        "payment": terms.payment(),
        "total_interest": terms.total_interest(),
        "rows": loans.schedule_preview(terms, rows=12),
    }


def loan_save(api: Api, payload: dict) -> dict:
    terms = loan_terms(api.db, payload)
    opening_balance = payload.get("opening_balance", True)
    if not isinstance(opening_balance, bool):
        raise ValueError("opening_balance must be true or false")
    result = save_loan(api.db, SaveLoan(terms, opening_balance=opening_balance))
    if not result.ok:
        raise service_error(result.errors[0])
    saved = result.value
    assert saved is not None
    return {
        "handle": saved.handle,
        "name": saved.name,
        "payment": saved.payment,
    }
