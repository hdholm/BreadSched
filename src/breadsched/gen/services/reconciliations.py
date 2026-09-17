"""Typed statement-reconciliation mutation contracts."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import TypeVar

from ..db.sqlite import DbSQLite
from ..engine import reconciliation
from ..lib.money import Money
from ..lib.reconciliation import Reconciliation
from .contracts import ServiceError, ServiceResult


@dataclass(frozen=True, slots=True)
class StartReconciliation:
    account: str
    statement_date: date
    ending_balance: Money


@dataclass(frozen=True, slots=True)
class UpdateReconciliation:
    handle: str
    selected_splits: tuple[str, ...] | None = None
    ending_balance: Money | None = None


@dataclass(frozen=True, slots=True)
class ReconciliationAction:
    handle: str


T = TypeVar("T")


def _result(call: Callable[[], T]) -> ServiceResult[T]:
    try:
        return ServiceResult.success(call())
    except reconciliation.ReconciliationError as exc:
        return ServiceResult.failure(ServiceError(exc.code, exc.fields))
    except KeyError:
        return ServiceResult.failure(ServiceError("reconciliation.not_found", ("handle",)))


def start_reconciliation(
    db: DbSQLite,
    request: StartReconciliation,
) -> ServiceResult[reconciliation.ReconciliationSummary]:
    """Start a statement session and return its calculated initial state."""
    if db.get_account(request.account) is None:
        return ServiceResult.failure(ServiceError("reconciliation.account.not_found", ("account",)))

    def start() -> reconciliation.ReconciliationSummary:
        session = reconciliation.start(
            db,
            request.account,
            request.statement_date,
            request.ending_balance,
        )
        return reconciliation.summary(db, session)

    return _result(start)


def update_reconciliation(
    db: DbSQLite,
    request: UpdateReconciliation,
) -> ServiceResult[reconciliation.ReconciliationSummary]:
    """Replace selected splits and/or the exact ending balance atomically."""
    return _result(
        lambda: reconciliation.update(
            db,
            request.handle,
            split_handles=(
                list(request.selected_splits) if request.selected_splits is not None else None
            ),
            ending_balance=request.ending_balance,
        )
    )


def complete_reconciliation(
    db: DbSQLite,
    request: ReconciliationAction,
) -> ServiceResult[Reconciliation]:
    return _result(lambda: reconciliation.complete(db, request.handle))


def cancel_reconciliation(
    db: DbSQLite,
    request: ReconciliationAction,
) -> ServiceResult[Reconciliation]:
    return _result(lambda: reconciliation.cancel(db, request.handle))


def reopen_reconciliation(
    db: DbSQLite,
    request: ReconciliationAction,
) -> ServiceResult[Reconciliation]:
    return _result(lambda: reconciliation.reopen(db, request.handle))
