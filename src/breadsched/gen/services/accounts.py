"""Typed account lifecycle and settings mutations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..db.base import DbError
from ..db.sqlite import DbSQLite
from ..lib.account import Account, AccountClass, AccountType
from ..lib.imported import baseline
from ..lib.money import Money
from ..lib.transaction import Transaction
from .contracts import ServiceError, ServiceResult

#: How a sale's cost comes from a security's lots, by stored name.
COST_BASIS_METHODS = ("fifo", "average")


@dataclass(frozen=True, slots=True)
class SaveAccount:
    account: Account
    existing_handle: str | None = None
    opening_balance: Money | None = None
    opening_date: date | None = None
    source: Account | None = None


@dataclass(frozen=True, slots=True)
class DeleteAccount:
    handle: str


@dataclass(frozen=True, slots=True)
class SavedAccount:
    handle: str
    name: str


def save_account(db: DbSQLite, request: SaveAccount) -> ServiceResult[SavedAccount]:
    """Validate and atomically add or update an account."""
    account = request.account
    account.name = account.name.strip()
    stored = (
        db.get_account(request.existing_handle) if request.existing_handle is not None else None
    )
    existing = request.source or stored
    if request.source is not None and request.existing_handle != request.source.handle:
        return ServiceResult.failure(ServiceError("account.source.mismatch", ("handle",)))
    errors = list(_account_errors(db, account, existing, request.existing_handle))
    if request.existing_handle is not None and stored is None:
        errors.append(ServiceError("account.not_found", ("handle",)))
    equity = None
    if request.opening_balance is not None:
        equity = db.get_account_by_name("Equity:Opening Balances") or db.get_account_by_name(
            "Equity"
        )
        if equity is None:
            errors.append(ServiceError("account.opening.equity.not_found", ("opening_balance",)))
    if errors:
        return ServiceResult.failure(*errors)

    if request.existing_handle is not None and not account.imported_values:
        # An account imported before its GnuCash fields could be changed holds the
        # values it was imported with: keep them, so a re-import keeps this edit.
        saved = db.stored_account(request.existing_handle)
        if saved is not None and (saved.source_guid or saved.source_type):
            account.imported_values = baseline(saved)
    action = "Edit" if existing is not None else "Add"
    with db.transaction(f"{action} account {account.name}") as txn:
        if existing is None:
            db.add_account(account, txn)
        else:
            db.commit_account(account, txn)
        if request.opening_balance is not None:
            assert equity is not None
            db.add_transaction(
                Transaction.simple(
                    request.opening_date or date.today(),
                    f"{account.name} opening balance",
                    account.handle,
                    equity.handle,
                    request.opening_balance,
                ),
                txn,
            )
    return ServiceResult.success(SavedAccount(account.handle, account.name))


def delete_account(db: DbSQLite, request: DeleteAccount) -> ServiceResult[SavedAccount]:
    """Delete an unused non-root account with a stable failure contract."""
    account = db.get_account(request.handle)
    if account is None:
        return ServiceResult.failure(ServiceError("account.not_found", ("handle",)))
    if account.is_root:
        return ServiceResult.failure(ServiceError("account.root.protected", ("handle",)))
    try:
        with db.transaction(f"Remove account {account.name}") as txn:
            db.remove_account(account.handle, txn)
    except DbError:
        return ServiceResult.failure(ServiceError("account.in_use", ("handle",)))
    return ServiceResult.success(SavedAccount(account.handle, account.name))


def _account_errors(
    db: DbSQLite,
    account: Account,
    existing: Account | None,
    existing_handle: str | None,
) -> tuple[ServiceError, ...]:
    errors: list[ServiceError] = []
    if not account.name:
        errors.append(ServiceError("account.name.required", ("name",)))
    if existing_handle is not None and existing is None:
        errors.append(ServiceError("account.not_found", ("handle",)))
    if existing_handle is not None and account.handle != existing_handle:
        errors.append(ServiceError("account.identity.changed", ("handle",)))
    if existing is None and account.atype in {AccountType.ROOT, AccountType.TECHNICAL}:
        errors.append(ServiceError("account.type.user_required", ("type",)))
    if account.commodity_scu is not None and account.commodity_scu <= 0:
        errors.append(ServiceError("account.commodity_scu.invalid", ("commodity_scu",)))
    if account.commodity is not None and db.get_commodity(account.commodity) is None:
        errors.append(ServiceError("account.commodity.not_found", ("commodity",)))
    errors.extend(_parent_errors(db, account))
    # One check per per-type record (#339).
    errors.extend(_liability_errors(db, account))
    errors.extend(_card_errors(db, account, existing))
    errors.extend(_fsa_errors(account))
    errors.extend(_investment_errors(account))
    if existing is None or (account.name, account.parent) != (existing.name, existing.parent):
        duplicate = next(
            (
                item
                for item in db.iter_accounts()
                if item.handle != account.handle
                and item.parent == account.parent
                and item.name.casefold() == account.name.casefold()
            ),
            None,
        )
        if duplicate is not None:
            errors.append(ServiceError("account.name.duplicate", ("name", "parent")))
    return tuple(errors)


def _parent_errors(db: DbSQLite, account: Account) -> list[ServiceError]:
    if account.parent is None:
        return [] if account.is_root else [ServiceError("account.parent.required", ("parent",))]
    parent = db.get_account(account.parent)
    if parent is None:
        return [ServiceError("account.parent.not_found", ("parent",))]
    seen = {account.handle}
    while parent is not None:
        if parent.handle in seen:
            return [ServiceError("account.parent.cycle", ("parent",))]
        seen.add(parent.handle)
        parent = db.get_account(parent.parent) if parent.parent else None
    return []


def _liability_errors(db: DbSQLite, account: Account) -> list[ServiceError]:
    """``account.liability``: a loan links only to an existing asset."""
    linked_handle = account.liability.linked_asset
    if linked_handle is None:
        return []
    linked = db.get_account(linked_handle)
    if linked is None:
        return [ServiceError("account.linked_asset.not_found", ("linked_asset",))]
    if linked.account_class is not AccountClass.ASSET:
        return [ServiceError("account.linked_asset.invalid", ("linked_asset",))]
    return []


def _investment_errors(account: Account) -> list[ServiceError]:
    """``account.investment``: a known cost-basis method."""
    if account.investment.cost_basis_method not in COST_BASIS_METHODS:
        return [ServiceError("account.cost_basis_method.invalid", ("cost_basis_method",))]
    return []


def _card_errors(db: DbSQLite, account: Account, existing: Account | None) -> list[ServiceError]:
    """``account.card``: terms a credit card can have, and its paying account."""
    errors: list[ServiceError] = []
    if account.atype is not AccountType.CREDIT and account.card.payment_account is not None:
        errors.append(ServiceError("account.card.type_required", ("card_payment_account",)))
    if account.card.payment_day is not None and not 1 <= account.card.payment_day <= 28:
        errors.append(ServiceError("account.card.payment_day.invalid", ("payment_day",)))
    if account.carries_balance and account.card.usual_payment is None:
        errors.append(ServiceError("account.card.usual_payment.required", ("usual_payment",)))
    if account.card.usual_payment is not None and account.card.usual_payment <= 0:
        errors.append(ServiceError("account.card.usual_payment.non_positive", ("usual_payment",)))
    if account.card.payment_account is not None:
        payment = db.get_account(account.card.payment_account)
        if payment is None:
            errors.append(
                ServiceError("account.card.payment_account.not_found", ("payment_account",))
            )
        elif not payment.atype.is_cash_like or payment.placeholder:
            errors.append(
                ServiceError("account.card.payment_account.invalid", ("payment_account",))
            )
    return errors


def _fsa_errors(account: Account) -> list[ServiceError]:
    """``account.fsa``: plan years that do not overlap, and a dependent care plan's rules."""
    years = sorted(account.fsa.years, key=lambda year: year.start)
    if account.fsa.dependent_care:
        for index, year in enumerate(years):
            if year.carryover_limit is not None:
                return [
                    ServiceError(
                        "account.fsa_dependent_care.carryover",
                        (f"fsa_years.{index}.carryover_limit",),
                    )
                ]
    for index, (earlier, later) in enumerate(zip(years, years[1:], strict=False), 1):
        if later.start <= earlier.through:
            return [ServiceError("account.fsa_years.overlap", (f"fsa_years.{index}",))]
    return []
