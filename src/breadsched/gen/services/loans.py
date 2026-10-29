"""Typed loan-creation contract shared by presentation adapters."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import cast

from ..db.sqlite import DbSQLite
from ..engine import loans
from ..engine.currency import reporting_fraction
from ..lib.account import Account, AccountClass, AccountType
from ..lib.money import Money
from .contracts import ServiceError, ServiceResult
from .savings_goals import NamedChoice


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


@dataclass(frozen=True, slots=True)
class LoanRow:
    period: int
    payment: Money
    interest: Money
    principal: Money
    balance: Money


@dataclass(frozen=True, slots=True)
class LoanPreview:
    """A loan's payment, its total interest, and its first payments; nothing is saved."""

    payment: Money
    total_interest: Money
    rows: tuple[LoanRow, ...]


@dataclass(frozen=True, slots=True)
class LoanChoices:
    """The accounts a new loan may use, and the book's amount precision."""

    liabilities: tuple[NamedChoice, ...]
    expenses: tuple[NamedChoice, ...]
    payment_accounts: tuple[NamedChoice, ...]
    fraction: int


def loan_choices(db: DbSQLite, include_hidden: bool = False) -> LoanChoices:
    """The accounts a new loan may use; hidden ones too when ``include_hidden`` (the
    desktop's Show Hidden Accounts)."""
    accounts = sorted(
        (
            account
            for account in db.iter_accounts()
            if not account.is_root
            and not account.placeholder
            and (include_hidden or not account.hidden)
        ),
        key=db.full_name,
    )

    def choices(predicate: Callable[[Account], bool]) -> tuple[NamedChoice, ...]:
        return tuple(
            NamedChoice(account.handle, db.full_name(account))
            for account in accounts
            if predicate(account)
        )

    return LoanChoices(
        choices(lambda account: account.account_class is AccountClass.LIABILITY),
        choices(lambda account: account.account_class is AccountClass.EXPENSE),
        choices(lambda account: account.atype.is_cash_like),
        reporting_fraction(db),
    )


def preview_loan(
    db: DbSQLite, terms: loans.LoanTerms, rows: int = 12
) -> ServiceResult[LoanPreview]:
    errors = validate_loan(db, terms)
    if errors:
        return ServiceResult.failure(*errors)
    return ServiceResult.success(
        LoanPreview(
            terms.payment(),
            terms.total_interest(),
            tuple(
                LoanRow(
                    cast(int, row["period"]),
                    cast(Money, row["payment"]),
                    cast(Money, row["interest"]),
                    cast(Money, row["principal"]),
                    cast(Money, row["balance"]),
                )
                for row in loans.schedule_preview(terms, rows=rows)
            ),
        )
    )
