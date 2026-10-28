"""One account's register as the browser shows it: rows with everything they name.

``engine.ledger`` builds the rows and running balances; this view carries them with
the full names of every account their splits use, each transaction's linked
documents, and, for an account that holds a security, its shares and valuation, so
an interface lays the register out without reading the book itself.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from ..db.sqlite import DbSQLite
from ..engine import attachments as attachment_engine
from ..engine import ledger, valuation
from ..engine.attachments import AttachmentStatus
from ..engine.ledger import RegisterRow
from ..engine.valuation import AccountValuation
from ..lib.account import Account
from ..lib.commodity import Commodity
from ..lib.money import Money
from .contracts import ServiceError, ServiceResult

__all__ = ["RegisterView", "SecurityHolding", "register_view"]

#: The register shows at most this many of the newest rows unless asked for more.
DEFAULT_ROWS = 250


@dataclass(frozen=True, slots=True)
class SecurityHolding:
    """What a security account holds, and what it is worth with its quote evidence."""

    security: Commodity
    share_fraction: int
    held: Money
    valued: AccountValuation
    evidence: str


@dataclass(frozen=True, slots=True)
class RegisterView:
    account: Account
    full_name: str
    debit_label: str
    credit_label: str
    #: ``None`` for an account kept in a currency.
    holding: SecurityHolding | None
    #: Accounts whose splits take shares.
    security_accounts: tuple[str, ...]
    rows: tuple[RegisterRow, ...]
    #: Full names of every account the rows' splits use, by handle.
    names: Mapping[str, str] = field(default_factory=dict)
    #: Linked documents of each row's transaction that has any, by handle.
    documents: Mapping[str, tuple[AttachmentStatus, ...]] = field(default_factory=dict)

    def name(self, handle: str) -> str:
        return self.names.get(handle, handle)

    def transfer(self, row: RegisterRow) -> str:
        """The 'other side' column: one account name, or ``-- Split --``."""
        others = [split for split in row.transaction.splits if split.handle != row.split.handle]
        return self.name(others[0].account) if len(others) == 1 else "-- Split --"

    def documents_of(self, transaction: str) -> tuple[AttachmentStatus, ...]:
        return self.documents.get(transaction, ())


def register_view(
    db: DbSQLite, account: str, limit: int | None = DEFAULT_ROWS, filter_text: str = ""
) -> ServiceResult[RegisterView]:
    """The newest ``limit`` rows of ``account``'s register, oldest first.

    ``limit=None`` returns every row, as the desktop register shows them.

    ``filter_text`` keeps only the rows whose transaction holds it anywhere, as the
    desktop register's filter does (``ledger.transaction_matches``, #382); the limit
    applies after it.
    """
    found = db.get_account(account)
    if found is None:
        return ServiceResult.failure(ServiceError("account.not_found", ("account",)))
    if limit is not None and limit < 1:
        return ServiceResult.failure(ServiceError("register.rows.invalid", ("limit",)))
    matching = [
        row
        for row in ledger.register(db, account)
        if not filter_text or ledger.transaction_matches(db, row.transaction, filter_text)
    ]
    rows = tuple(matching if limit is None else matching[-limit:])
    debit_label, credit_label = ledger.register_headings(found.atype)
    security = ledger.security_of(db, found)
    holding = None
    if security is not None:
        valued = valuation.account_value(db, found)
        holding = SecurityHolding(
            security,
            ledger.share_fraction(db, found),
            valuation.quantity_balance(db, found),
            valued,
            valuation.quote_evidence(db, valued),
        )
    handles = {split.account for row in rows for split in row.transaction.splits}
    names = {}
    for handle in handles:
        named = db.get_account(handle)
        if named is not None:
            names[handle] = db.full_name(named)
    documents = {
        row.transaction.handle: tuple(attachment_engine.statuses(db, row.transaction))
        for row in rows
        if row.transaction.attachments or row.transaction.source_link
    }
    return ServiceResult.success(
        RegisterView(
            found,
            db.full_name(found),
            debit_label,
            credit_label,
            holding,
            tuple(
                sorted(
                    item.handle
                    for item in db.iter_accounts()
                    if ledger.security_of(db, item) is not None
                )
            ),
            rows,
            names,
            documents,
        )
    )
