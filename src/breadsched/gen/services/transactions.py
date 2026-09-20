"""Typed transaction construction and atomic persistence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..db.sqlite import DbSQLite
from ..engine import fsa_claims, investment
from ..lib.amount import Amount
from ..lib.commodity import DEFAULT_CURRENCY, DEFAULT_CURRENCY_HANDLE, Commodity
from ..lib.money import Money
from ..lib.transaction import (
    InvestmentActivityKind,
    PlanningFlowKind,
    Split,
    Transaction,
)
from .contracts import ServiceError, ServiceResult


@dataclass(frozen=True, slots=True)
class TransactionSplitInput:
    """One ledger leg with distinct value and account-commodity quantity."""

    account: str
    value: Amount
    quantity: Amount | None = None
    handle: str | None = None
    memo: str = ""
    planning_flow: PlanningFlowKind | None = None
    investment_activity: InvestmentActivityKind | None = None


@dataclass(frozen=True, slots=True)
class TransactionInput:
    """Presentation-neutral editable fields for one ledger transaction."""

    post_date: date
    description: str
    splits: tuple[TransactionSplitInput, ...]
    num: str = ""
    notes: str = ""
    currency: str | None = None
    investment_activity: InvestmentActivityKind | None = None


@dataclass(frozen=True, slots=True)
class ClaimAttachment:
    """Optional claim link committed with a newly posted transaction."""

    claim: str
    role: str
    funding_year_start: date | None = None


@dataclass(frozen=True, slots=True)
class SaveTransaction:
    definition: TransactionInput
    existing_handle: str | None = None
    claim_attachment: ClaimAttachment | None = None
    source: Transaction | None = None


@dataclass(frozen=True, slots=True)
class DeleteTransaction:
    handle: str


@dataclass(frozen=True, slots=True)
class SavedTransaction:
    handle: str
    post_date: date
    description: str


def transaction_currency(db: DbSQLite, preferred: str | None = None) -> str:
    """Resolve a transaction-currency handle, including legacy empty books."""
    if preferred is not None:
        return preferred
    configured = db.get_metadata("default_currency")
    if isinstance(configured, str):
        commodity = db.get_commodity(configured)
        if commodity is not None and commodity.is_currency:
            return commodity.handle
    usd = db.get_commodity_by_mnemonic("USD")
    if usd is not None and usd.is_currency:
        return usd.handle
    first = next((item for item in db.iter_commodities() if item.is_currency), None)
    return first.handle if first is not None else DEFAULT_CURRENCY_HANDLE


def build_transaction(
    db: DbSQLite,
    request: SaveTransaction,
) -> ServiceResult[Transaction]:
    """Build and validate a candidate without changing the book."""
    if request.source is not None and request.existing_handle != request.source.handle:
        return ServiceResult.failure(ServiceError("transaction.source.mismatch", ("handle",)))
    existing = request.source
    if existing is None and request.existing_handle is not None:
        existing = db.get_transaction(request.existing_handle)
    if request.existing_handle is not None and existing is None:
        return ServiceResult.failure(ServiceError("transaction.not_found", ("handle",)))

    definition = request.definition
    errors: list[ServiceError] = []
    if not definition.description.strip():
        errors.append(ServiceError("transaction.description.required", ("description",)))
    if len(definition.splits) < 2:
        errors.append(ServiceError("transaction.splits.too_few", ("splits",)))
    if len({split.account for split in definition.splits}) < 2:
        errors.append(ServiceError("transaction.accounts.same", ("splits",)))

    preferred_currency = definition.currency or (
        existing.currency if existing is not None else None
    )
    currency = transaction_currency(db, preferred_currency)
    currency_commodity = db.get_commodity(currency)
    if currency_commodity is None and currency != DEFAULT_CURRENCY_HANDLE:
        errors.append(ServiceError("transaction.currency.not_found", ("currency",)))
    elif currency_commodity is not None and not currency_commodity.is_currency:
        errors.append(ServiceError("transaction.currency.invalid", ("currency",)))

    source_splits = {split.handle: split for split in existing.splits} if existing else {}
    seen_handles: set[str] = set()
    candidate_splits: list[Split] = []
    for index, item in enumerate(definition.splits):
        path = f"splits.{index}"
        account = db.get_account(item.account)
        if account is None:
            errors.append(ServiceError("transaction.account.not_found", (f"{path}.account",)))
        if item.value.commodity != currency:
            errors.append(ServiceError("transaction.value.commodity", (f"{path}.value",)))
        account_commodity = account.commodity if account is not None else None
        quantity_commodity = account_commodity or currency
        if item.quantity is not None and item.quantity.commodity != quantity_commodity:
            errors.append(ServiceError("transaction.quantity.commodity", (f"{path}.quantity",)))
        source = source_splits.get(item.handle or "")
        if item.handle is not None:
            if item.handle in seen_handles:
                errors.append(ServiceError("transaction.split.duplicate", (f"{path}.handle",)))
            seen_handles.add(item.handle)
            if existing is not None and source is None:
                errors.append(ServiceError("transaction.split.not_found", (f"{path}.handle",)))
        if (
            account_commodity is not None
            and account_commodity != currency
            and item.quantity is None
            and (source is None or source.account != item.account)
        ):
            errors.append(ServiceError("transaction.quantity.required", (f"{path}.quantity",)))
        if (
            account is not None
            and account.hidden
            and (source is None or source.account != item.account)
        ):
            errors.append(ServiceError("transaction.account.hidden", (f"{path}.account",)))

        if source is not None:
            split = Split.from_dict(source.serialize())
            if split.account != item.account:
                split.quantity = (
                    item.quantity.value if item.quantity is not None else item.value.value
                )
        else:
            quantity = item.quantity.value if item.quantity is not None else item.value.value
            split = Split(item.account, item.value.value, quantity=quantity, handle=item.handle)
        split.account = item.account
        split.value = item.value.value
        if item.quantity is not None:
            split.quantity = item.quantity.value
        split.memo = item.memo.strip()
        split.planning_flow = item.planning_flow
        split.investment_activity = item.investment_activity
        default_activity = definition.investment_activity
        if split.investment_activity is None and default_activity is not None:
            direction = default_activity.direction
            if (direction >= 0 and item.value.value > 0) or (
                direction <= 0 and item.value.value < 0
            ):
                split.investment_activity = default_activity
        candidate_splits.append(split)

    values_share_currency = all(item.value.commodity == currency for item in definition.splits)
    if values_share_currency and sum(
        (item.value for item in definition.splits), Amount(Money(0), currency)
    ):
        errors.append(ServiceError("transaction.unbalanced", ("splits",)))
    if errors:
        return ServiceResult.failure(*errors)

    if existing is not None:
        candidate = Transaction.from_dict(existing.serialize())
    else:
        candidate = Transaction()
    candidate.post_date = definition.post_date
    candidate.description = definition.description.strip()
    candidate.num = definition.num.strip()
    candidate.notes = definition.notes.strip()
    candidate.currency = currency
    candidate.splits = candidate_splits

    if investment.activity_problems(db, candidate.splits):
        return ServiceResult.failure(ServiceError("transaction.investment.invalid", ("splits",)))
    return ServiceResult.success(candidate)


def save_transaction(
    db: DbSQLite,
    request: SaveTransaction,
) -> ServiceResult[SavedTransaction]:
    """Validate and persist a transaction, plus an optional claim link, atomically."""
    if request.existing_handle is not None and db.get_transaction(request.existing_handle) is None:
        return ServiceResult.failure(ServiceError("transaction.not_found", ("handle",)))
    built = build_transaction(db, request)
    if built.value is None:
        return ServiceResult.failure(*built.errors)
    candidate = built.value
    action = "Edit" if request.existing_handle is not None else "Add"

    class ClaimNotFound(Exception):
        pass

    class ClaimInvalid(Exception):
        pass

    try:
        with db.transaction(f"{action} {candidate.description}") as txn:
            if (
                candidate.currency == DEFAULT_CURRENCY_HANDLE
                and db.get_commodity(DEFAULT_CURRENCY_HANDLE) is None
            ):
                db.add_commodity(Commodity.from_dict(DEFAULT_CURRENCY.serialize()), txn)
                db.set_metadata("default_currency", DEFAULT_CURRENCY_HANDLE, txn)
            if request.existing_handle is None:
                db.add_transaction(candidate, txn)
            else:
                db.commit_transaction(candidate, txn)
            attachment = request.claim_attachment
            if attachment is not None:
                try:
                    fsa_claims.attach_transaction_to_claim(
                        db,
                        attachment.claim,
                        candidate.handle,
                        role=attachment.role,
                        funding_year_start=attachment.funding_year_start,
                        txn=txn,
                    )
                except KeyError as exc:
                    raise ClaimNotFound from exc
                except ValueError as exc:
                    raise ClaimInvalid from exc
    except ClaimNotFound:
        return ServiceResult.failure(ServiceError("transaction.claim.not_found", ("claim",)))
    except ClaimInvalid:
        return ServiceResult.failure(ServiceError("transaction.claim.invalid", ("claim",)))
    return ServiceResult.success(
        SavedTransaction(candidate.handle, candidate.post_date, candidate.description)
    )


def delete_transaction(db: DbSQLite, request: DeleteTransaction) -> ServiceResult[SavedTransaction]:
    """Delete one persisted transaction through the shared mutation boundary."""
    existing = db.get_transaction(request.handle)
    if existing is None:
        return ServiceResult.failure(ServiceError("transaction.not_found", ("handle",)))
    with db.transaction(f"Delete {existing.description}") as txn:
        db.remove_transaction(existing.handle, txn)
    return ServiceResult.success(
        SavedTransaction(existing.handle, existing.post_date, existing.description)
    )
