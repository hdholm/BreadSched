"""Typed loan-creation contract shared by presentation adapters."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace

from ..db.sqlite import DbSQLite
from ..engine import loans
from ..lib.account import Account, AccountClass, AccountType
from ..lib.money import Money
from .contracts import ServiceError, ServiceResult


@dataclass(frozen=True, slots=True)
class SaveLoan:
    terms: loans.LoanTerms
    opening_balance: bool = True


@dataclass(frozen=True, slots=True)
class SavedLoan:
    handle: str
    name: str
    payment: Money


def save_loan(db: DbSQLite, request: SaveLoan) -> ServiceResult[SavedLoan]:
    """Validate and persist a loan schedule through one mutation boundary."""
    terms = replace(request.terms, name=request.terms.name.strip())
    errors = validate_loan(db, terms)
    if errors:
        return ServiceResult.failure(*errors)

    saved = loans.create_loan(db, terms, opening_balance=request.opening_balance)
    return ServiceResult.success(SavedLoan(saved.handle, saved.name, terms.payment()))


def validate_loan(db: DbSQLite, terms: loans.LoanTerms) -> tuple[ServiceError, ...]:
    """Return stable validation failures without mutating the book."""
    errors: list[ServiceError] = []
    if not terms.name.strip():
        errors.append(ServiceError("loan.name.required", ("name",)))
    if terms.principal <= 0:
        errors.append(ServiceError("loan.principal.non_positive", ("principal",)))
    if terms.annual_rate < 0:
        errors.append(ServiceError("loan.rate.negative", ("annual_rate",)))
    if not 1 <= terms.years <= 100:
        errors.append(ServiceError("loan.years.out_of_range", ("years",)))

    errors.extend(
        _account_errors(
            db,
            terms.liability,
            "liability",
            lambda account: account.account_class is AccountClass.LIABILITY,
            "loan.account.liability_required",
        )
    )
    errors.extend(
        _account_errors(
            db,
            terms.interest_account,
            "interest_account",
            lambda account: account.account_class is AccountClass.EXPENSE,
            "loan.account.expense_required",
        )
    )
    errors.extend(
        _account_errors(
            db,
            terms.payment_account,
            "payment_account",
            lambda account: account.atype.is_cash_like,
            "loan.account.cash_required",
        )
    )

    if (terms.escrow is None) != (terms.escrow_account is None):
        errors.append(ServiceError("loan.escrow.incomplete", ("escrow", "escrow_account")))
    elif terms.escrow is not None and terms.escrow_account is not None:
        if terms.escrow <= 0:
            errors.append(ServiceError("loan.escrow.non_positive", ("escrow",)))
        errors.extend(
            _account_errors(
                db,
                terms.escrow_account,
                "escrow_account",
                lambda account: account.atype is AccountType.ESCROW,
                "loan.account.escrow_required",
            )
        )

    return tuple(errors)


def _account_errors(
    db: DbSQLite,
    handle: str,
    field: str,
    accepts: Callable[[Account], bool],
    role_code: str,
) -> list[ServiceError]:
    account = db.get_account(handle)
    if account is None:
        return [ServiceError("loan.account.not_found", (field,))]
    if account.is_root or account.placeholder or account.hidden:
        return [ServiceError("loan.account.unavailable", (field,))]
    if not accepts(account):
        return [ServiceError(role_code, (field,))]
    return []
