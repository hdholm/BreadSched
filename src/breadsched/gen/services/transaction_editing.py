"""What a transaction editor shows besides the transaction, read once (#358).

The desktop's transaction editor names every account a split may use, knows which
hold a security (so their splits carry shares), explains how a stored transaction
is recognised for escrow, lists its linked documents, and offers the FSA claims
still open. This view carries all of that, so the editor reads the book through
the book server rather than piece by piece.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..db.sqlite import DbSQLite
from ..engine import attachments as attachment_engine
from ..engine import escrow, fsa_claims, ledger
from ..engine.attachments import AttachmentStatus
from ..lib.fsa_claim import FsaClaim
from .contracts import ServiceError, ServiceResult
from .ledger_reads import AccountRecord

__all__ = ["TransactionEditorView", "transaction_editor"]


@dataclass(frozen=True, slots=True)
class TransactionEditorView:
    #: Every account, with its full name, the root included.
    accounts: tuple[AccountRecord, ...]
    #: Accounts that hold a security, so their splits carry shares.
    security_accounts: tuple[str, ...]
    #: How a stored transaction is recognised for escrow; empty for a new one.
    escrow_explanations: tuple[str, ...] = ()
    #: A stored transaction's linked documents, checked against the files.
    documents: tuple[AttachmentStatus, ...] = ()
    #: Where relative document locations resolve, if anywhere.
    folder: Path | None = None
    #: FSA claims not yet settled, which a new entry may attach to.
    open_claims: tuple[FsaClaim, ...] = ()


def transaction_editor(
    db: DbSQLite, transaction: str | None = None
) -> ServiceResult[TransactionEditorView]:
    stored = db.get_transaction(transaction) if transaction else None
    if transaction and stored is None:
        return ServiceResult.failure(ServiceError("transaction.not_found", ("transaction",)))
    accounts = list(db.iter_accounts())
    explanations: tuple[str, ...] = ()
    documents: tuple[AttachmentStatus, ...] = ()
    if stored is not None:
        by_handle = {account.handle: account for account in accounts}
        explanations = tuple(
            escrow.recognition(
                ((split.account, split.value) for split in stored.splits), by_handle
            ).explanations(by_handle)
        )
        documents = tuple(attachment_engine.statuses(db, stored))
    return ServiceResult.success(
        TransactionEditorView(
            accounts=tuple(AccountRecord(account, db.full_name(account)) for account in accounts),
            security_accounts=tuple(
                account.handle
                for account in accounts
                if ledger.security_of(db, account) is not None
            ),
            escrow_explanations=explanations,
            documents=documents,
            folder=attachment_engine.attachment_folder(db),
            open_claims=tuple(
                claim
                for claim in fsa_claims.iter_claims(db)
                if not fsa_claims.claim_summary(db, claim).status.settled
            ),
        )
    )
