"""The database interface every backend must satisfy.

Gramps' ``DbGeneric`` split is followed here: views and engines are written against
this abstract surface, so the SQLite backend can be replaced (with Postgres, with an
in-memory store for tests) without touching a line of UI or engine code.

Two things are non-negotiable in the contract:

* **All writes go through a :class:`DbTxn`.**  A partially written double-entry
  transaction is a corrupt ledger, so the unit of durability is the batch, not the
  individual object.
* **Signals fire after commit, never during.**  A view that repaints in response to
  a half-applied batch would show an unbalanced book.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from datetime import date
from typing import Any, Literal

from ..lib.account import Account
from ..lib.commodity import Commodity
from ..lib.fsa_claim import FsaClaim
from ..lib.scenario import Scenario
from ..lib.scheduled import ScheduledTransaction
from ..lib.transaction import Transaction
from ..utils.callback import Callback

__all__ = ["DbBase", "DbTxn", "DbError", "DbReadonlyError"]


class DbError(Exception):
    """Any database-level failure."""


class DbReadonlyError(DbError):
    """Raised on a write attempt against a read-only database."""


class DbTxn:
    """A unit of work, and the undo record for it.

    Use as a context manager::

        with db.transaction("Add payslip") as txn:
            db.add_transaction(payslip, txn)

    Leaving the block normally commits and emits signals; raising rolls back.
    """

    def __init__(self, message: str, db: DbBase, batch: bool = False) -> None:
        self.message = message
        self.db = db
        #: Batch mode suppresses per-object signals; used by importers.
        self.batch = batch
        #: (table, handle, before, after) with ``None`` meaning absent.
        self.records: list[tuple[str, str, dict | None, dict | None]] = []
        self.timestamp: float = 0.0

    def add(self, table: str, handle: str, before: dict | None, after: dict | None) -> None:
        self.records.append((table, handle, before, after))

    def __len__(self) -> int:
        return len(self.records)

    def __enter__(self) -> DbTxn:
        self.db._txn_begin(self)
        return self

    def __exit__(self, exc_type, exc, tb) -> Literal[False]:
        if exc_type is None:
            self.db._txn_commit(self)
        else:
            self.db._txn_abort(self)
        return False

    def __repr__(self) -> str:
        return f"<DbTxn {self.message!r} {len(self.records)} changes>"


class DbBase(Callback, ABC):
    """Abstract store for the whole object model."""

    __signals__ = {
        "account-add": (list,),
        "account-update": (list,),
        "account-delete": (list,),
        "transaction-add": (list,),
        "transaction-update": (list,),
        "transaction-delete": (list,),
        "commodity-add": (list,),
        "scheduled-add": (list,),
        "scheduled-update": (list,),
        "scheduled-delete": (list,),
        "scenario-add": (list,),
        "scenario-update": (list,),
        "scenario-delete": (list,),
        "fsa-claim-add": (list,),
        "fsa-claim-update": (list,),
        "fsa-claim-delete": (list,),
        "database-changed": (object,),
        "undo-available": (bool,),
        "redo-available": (bool,),
    }

    def __init__(self) -> None:
        Callback.__init__(self)
        self.readonly = False
        self.undo_stack: list[DbTxn] = []
        self.redo_stack: list[DbTxn] = []
        self.undo_limit = 100

    # ------------------------------------------------------------- life cycle

    @abstractmethod
    def load(self, path: str, mode: str = "w") -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @property
    @abstractmethod
    def is_open(self) -> bool: ...

    def transaction(self, message: str, batch: bool = False) -> DbTxn:
        if self.readonly:
            raise DbReadonlyError("database is open read-only")
        return DbTxn(message, self, batch=batch)

    # Backend hooks -----------------------------------------------------------

    @abstractmethod
    def _txn_begin(self, txn: DbTxn) -> None: ...

    @abstractmethod
    def _txn_commit(self, txn: DbTxn) -> None: ...

    @abstractmethod
    def _txn_abort(self, txn: DbTxn) -> None: ...

    @abstractmethod
    def undo(self) -> bool: ...

    @abstractmethod
    def redo(self) -> bool: ...

    # ---------------------------------------------------------------- accounts

    @abstractmethod
    def add_account(self, account: Account, txn: DbTxn) -> str: ...

    @abstractmethod
    def commit_account(self, account: Account, txn: DbTxn) -> None: ...

    @abstractmethod
    def remove_account(self, handle: str, txn: DbTxn) -> None: ...

    @abstractmethod
    def get_account(self, handle: str) -> Account | None: ...

    @abstractmethod
    def iter_accounts(self) -> Iterator[Account]: ...

    @abstractmethod
    def get_account_by_name(self, full_name: str) -> Account | None: ...

    @abstractmethod
    def child_accounts(self, handle: str | None) -> list[Account]: ...

    # ------------------------------------------------------------ transactions

    @abstractmethod
    def add_transaction(self, txn_obj: Transaction, txn: DbTxn) -> str: ...

    @abstractmethod
    def commit_transaction(self, txn_obj: Transaction, txn: DbTxn) -> None: ...

    @abstractmethod
    def remove_transaction(self, handle: str, txn: DbTxn) -> None: ...

    @abstractmethod
    def get_transaction(self, handle: str) -> Transaction | None: ...

    @abstractmethod
    def iter_transactions(
        self,
        account: str | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> Iterator[Transaction]: ...

    # ------------------------------------------------- other primary objects

    @abstractmethod
    def add_commodity(self, commodity: Commodity, txn: DbTxn) -> str: ...

    @abstractmethod
    def get_commodity(self, handle: str) -> Commodity | None: ...

    @abstractmethod
    def get_commodity_by_mnemonic(self, mnemonic: str) -> Commodity | None: ...

    @abstractmethod
    def iter_commodities(self) -> Iterator[Commodity]: ...

    @abstractmethod
    def add_scheduled(self, sched: ScheduledTransaction, txn: DbTxn) -> str: ...

    @abstractmethod
    def commit_scheduled(self, sched: ScheduledTransaction, txn: DbTxn) -> None: ...

    @abstractmethod
    def remove_scheduled(self, handle: str, txn: DbTxn) -> None: ...

    @abstractmethod
    def get_scheduled(self, handle: str) -> ScheduledTransaction | None: ...

    @abstractmethod
    def iter_scheduled(self) -> Iterator[ScheduledTransaction]: ...

    @abstractmethod
    def add_scenario(self, scenario: Scenario, txn: DbTxn) -> str: ...

    @abstractmethod
    def commit_scenario(self, scenario: Scenario, txn: DbTxn) -> None: ...

    @abstractmethod
    def remove_scenario(self, handle: str, txn: DbTxn) -> None: ...

    @abstractmethod
    def get_scenario(self, handle: str) -> Scenario | None: ...

    @abstractmethod
    def iter_scenarios(self) -> Iterator[Scenario]: ...

    # --------------------------------------------------------------- FSA claims

    @abstractmethod
    def add_fsa_claim(self, claim: FsaClaim, txn: DbTxn) -> str: ...

    @abstractmethod
    def commit_fsa_claim(self, claim: FsaClaim, txn: DbTxn) -> None: ...

    @abstractmethod
    def remove_fsa_claim(self, handle: str, txn: DbTxn) -> None: ...

    @abstractmethod
    def get_fsa_claim(self, handle: str) -> FsaClaim | None: ...

    @abstractmethod
    def iter_fsa_claims(self) -> Iterator[FsaClaim]: ...

    # ---------------------------------------------------------------- metadata

    @abstractmethod
    def get_metadata(self, key: str, default: Any = None) -> Any: ...

    @abstractmethod
    def set_metadata(self, key: str, value: Any, txn: DbTxn | None = None) -> None: ...

    # --------------------------------------------------------------- utilities

    def full_name(self, account: Account | str, separator: str = ":") -> str:
        """Colon-delimited path, the same shape GnuCash shows in its account tree."""
        node = self.get_account(account) if isinstance(account, str) else account
        parts: list[str] = []
        seen: set[str] = set()
        while node is not None and node.parent is not None:
            if node.handle in seen:  # cycle guard for a damaged book
                break
            seen.add(node.handle)
            parts.append(node.name)
            node = self.get_account(node.parent)
        return separator.join(reversed(parts))

    def root_account(self) -> Account | None:
        for account in self.iter_accounts():
            if account.parent is None:
                return account
        return None
