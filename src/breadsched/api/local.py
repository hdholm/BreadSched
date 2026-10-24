"""The in-process book server (#356).

:class:`LocalBookServer` wraps one open :class:`~breadsched.gen.db.sqlite.DbSQLite`
and implements :class:`~breadsched.api.protocol.BookApi` over the shared services.
It observes the book's own storage signals, so a change made through it, by undo or
redo, or by any other code holding the same book in this process, raises the
revision and reaches every subscriber.

The revision is the book's own change counter (:meth:`DbSQLite.revision`), stored
with the book, so every process that opens it agrees on it (#357).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date

from ..gen.db.sqlite import DbSQLite
from ..gen.lib import ScheduledTransaction
from ..gen.services import (
    AdvanceDays,
    DeleteSchedule,
    DeleteTransaction,
    DueResolution,
    DueScheduleReview,
    DuplicateSchedule,
    FormulaScheduleInput,
    NextOccurrence,
    PostDue,
    PostedDue,
    ResolveDue,
    ResolveNext,
    SavedSchedule,
    SavedTransaction,
    SaveFixedSchedule,
    SaveTransaction,
    ServiceError,
    SetAdvanceDays,
    delete_schedule,
    delete_transaction,
    duplicate_schedule,
    find_schedule,
    next_occurrence,
    pending_due_review,
    post_due,
    resolve_due,
    resolve_next,
    save_fixed_schedule,
    save_formula_schedule,
    save_transaction,
    set_advance_days,
)
from ..gen.services import advance_days as stored_advance_days
from .contract import ApiError, unwrap
from .protocol import Change, Listener

__all__ = ["LocalBookServer", "book_server", "lookup_schedule", "open_book_server"]

#: Storage signal stems whose add, update, and delete signals name a change's kinds.
_STEMS = (
    "account",
    "transaction",
    "scheduled",
    "scenario",
    "commodity",
    "price",
    "fsa-claim",
    "reconciliation",
    "receivable",
    "savings-goal",
)


class LocalBookServer:
    """A book server in this process, over one open book."""

    def __init__(self, db: DbSQLite) -> None:
        self.db = db
        self._pending: set[str] = set()
        self._listeners: list[Listener] = []
        known = db._known_signals()
        self._connections: list[int] = []
        for stem in _STEMS:
            for action in ("add", "update", "delete"):
                signal = f"{stem}-{action}"
                if signal in known:
                    self._connections.append(
                        db.connect(signal, lambda _handles, kind=stem: self._pending.add(kind))
                    )
        self._connections.append(db.connect("database-changed", self._on_changed))

    # ------------------------------------------------------------- changes

    @property
    def revision(self) -> int:
        return self.db.revision()

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        self._listeners.append(listener)

        def stop() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return stop

    def _on_changed(self, _db: DbSQLite) -> None:
        change = Change(self.db.revision(), frozenset(self._pending), self.db.undo_message())
        self._pending.clear()
        for listener in list(self._listeners):
            listener(change)

    def close(self) -> None:
        """Stop observing the book (the book itself stays open)."""
        for handle in self._connections:
            self.db.disconnect(handle)
        self._connections.clear()
        self._listeners.clear()

    def undo(self) -> bool:
        return self.db.undo()

    def redo(self) -> bool:
        return self.db.redo()

    # ------------------------------------------------- scheduled transactions

    def find_schedule(self, reference: str) -> ScheduledTransaction:
        return lookup_schedule(self.db, reference)

    def due_review(self, as_of: date | None = None) -> list[DueScheduleReview]:
        return pending_due_review(self.db, as_of)

    def resolve_due(self, request: ResolveDue) -> DueResolution:
        return unwrap(resolve_due(self.db, request))

    def next_occurrence(self, schedule: str, as_of: date | None = None) -> NextOccurrence | None:
        return next_occurrence(self.db, schedule, as_of=as_of)

    def resolve_next(self, request: ResolveNext) -> DueResolution:
        return unwrap(resolve_next(self.db, request))

    def advance_days(self, schedule: str) -> AdvanceDays:
        found = stored_advance_days(self.db, schedule)
        if found is None:
            raise ApiError.from_service(ServiceError("schedule.not_found", ("schedule",)))
        return found

    def set_advance_days(self, request: SetAdvanceDays) -> AdvanceDays:
        return unwrap(set_advance_days(self.db, request))

    def delete_schedule(self, schedule: str) -> SavedSchedule:
        return unwrap(delete_schedule(self.db, DeleteSchedule(schedule)))

    def duplicate_schedule(self, request: DuplicateSchedule) -> SavedSchedule:
        return unwrap(duplicate_schedule(self.db, request))

    def post_due(self, request: PostDue) -> PostedDue:
        return unwrap(post_due(self.db, request))

    def save_fixed_schedule(self, request: SaveFixedSchedule) -> SavedSchedule:
        return unwrap(save_fixed_schedule(self.db, request))

    def save_formula_schedule(self, request: FormulaScheduleInput) -> SavedSchedule:
        return unwrap(save_formula_schedule(self.db, request))

    # ------------------------------------------------------------ transactions

    def save_transaction(self, request: SaveTransaction) -> SavedTransaction:
        return unwrap(save_transaction(self.db, request))

    def delete_transaction(self, transaction: str) -> SavedTransaction:
        return unwrap(delete_transaction(self.db, DeleteTransaction(transaction)))


def book_server(db: DbSQLite) -> LocalBookServer:
    """The one in-process server over ``db``, for code that holds the book itself.

    GTK holds its book directly; each view and dialog moved onto the book API asks
    for the server here, so one server (and one set of storage-signal connections)
    serves the whole application.
    """
    # Kept on the book itself (a cycle the collector frees with it), not in a cache
    # whose values would keep every book ever opened alive.
    server = getattr(db, "_book_server", None)
    if not isinstance(server, LocalBookServer):
        server = LocalBookServer(db)
        db._book_server = server  # type: ignore[attr-defined]
    return server


def lookup_schedule(db: DbSQLite, reference: str) -> ScheduledTransaction:
    """A schedule by handle, unique handle prefix, or exact name, or an ApiError."""
    found = find_schedule(db, reference)
    if found.value is not None:
        return found.value
    error = found.errors[0]
    if error.code == "schedule.not_found":
        raise ApiError(404, error.code, error.fields, f"no schedule matches {reference!r}")
    raise ApiError(400, error.code, error.fields, f"{reference!r} matches more than one schedule")


@contextmanager
def open_book_server(path: str, mode: str = "w") -> Iterator[LocalBookServer]:
    """Open the book at ``path`` and serve it in process until the block ends."""
    db = DbSQLite()
    db.load(path, mode=mode)
    server = LocalBookServer(db)
    try:
        yield server
    finally:
        server.close()
        db.close()
