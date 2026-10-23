"""What a book server offers, whichever process it runs in (#355).

The protocol grows area by area as interfaces move onto it; scheduled transactions
come first. Requests and results are the services' own typed dataclasses, so a
caller written against an in-process server works unchanged against a remote one.
Writes either succeed or raise :class:`~breadsched.api.contract.ApiError` without
changing the book.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from ..gen.lib import ScheduledTransaction
from ..gen.services import (
    AdvanceDays,
    DueResolution,
    DueScheduleReview,
    NextOccurrence,
    ResolveDue,
    ResolveNext,
    SetAdvanceDays,
)

__all__ = ["BookApi", "Change", "Listener"]


@dataclass(frozen=True, slots=True)
class Change:
    """One committed change to the book, as every subscriber sees it.

    ``revision`` is the book's change counter after the change; it rises with each
    recorded change, undo, and redo, whichever process made it. ``kinds`` names what
    changed (``transaction``, ``scheduled``, and so on, as the storage signals call
    them) and ``label`` is the undo step's wording, when there is one. An empty
    ``kinds`` means only that the book moved on, as when a client reconnects after
    missing changes.
    """

    revision: int
    kinds: frozenset[str]
    label: str | None = None


Listener = Callable[[Change], None]


class BookApi(Protocol):
    """The operations every interface reaches a book through."""

    @property
    def revision(self) -> int:
        """The book's change counter: it rises with every recorded change."""
        ...

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        """Call ``listener`` after each change; returns a function that stops it."""
        ...

    def undo(self) -> bool: ...

    def redo(self) -> bool: ...

    # ------------------------------------------------- scheduled transactions

    def find_schedule(self, reference: str) -> ScheduledTransaction:
        """A schedule by handle, unique handle prefix, or exact name."""
        ...

    def due_review(self, as_of: date | None = None) -> list[DueScheduleReview]: ...

    def resolve_due(self, request: ResolveDue) -> DueResolution: ...

    def next_occurrence(
        self, schedule: str, as_of: date | None = None
    ) -> NextOccurrence | None: ...

    def resolve_next(self, request: ResolveNext) -> DueResolution: ...

    def advance_days(self, schedule: str) -> AdvanceDays: ...

    def set_advance_days(self, request: SetAdvanceDays) -> AdvanceDays: ...
