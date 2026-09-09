"""Base classes for stored objects.

Follows the Gramps object model: every stored thing is a *primary object* with an
opaque, immutable ``handle`` that never changes for the life of the record, plus a
human-facing ``gid`` (Gramps calls it a Gramps ID) that a user may edit.  References
between objects are always by handle, never by Python reference, so an object can be
serialised, cached, and re-read independently of the rest of the database.

Serialisation is to plain JSON-compatible structures.  Gramps pickles tuples; JSON is
chosen here instead because it stays readable in the SQLite file and survives version
skew far more gracefully.
"""

from __future__ import annotations

import uuid
from typing import Any, ClassVar

__all__ = ["PrimaryObject", "create_handle"]


def create_handle() -> str:
    """Return a fresh 32-character hex handle.

    The same shape as a GnuCash GUID, so a GUID imported from a GnuCash book can be
    reused verbatim as a handle and the link back to the source book is preserved.
    """
    return uuid.uuid4().hex


class PrimaryObject:
    """Something the database stores in its own table."""

    #: SQLite table name; subclasses must set this.
    TABLE: ClassVar[str] = ""
    #: Bumped when the serialised layout of the subclass changes.
    SCHEMA: ClassVar[int] = 1

    def __init__(self, handle: str | None = None, gid: str = "") -> None:
        self.handle: str = handle or create_handle()
        self.gid: str = gid
        self.change: int = 0
        self.private: bool = False
        self.tags: list[str] = []

    # ------------------------------------------------------------ serialisation

    def serialize(self) -> dict[str, Any]:
        data = {
            "_schema": self.SCHEMA,
            "handle": self.handle,
            "gid": self.gid,
            "change": self.change,
            "private": self.private,
            "tags": list(self.tags),
        }
        data.update(self._serialize())
        return data

    def unserialize(self, data: dict[str, Any]) -> PrimaryObject:
        self.handle = data["handle"]
        self.gid = data.get("gid", "")
        self.change = data.get("change", 0)
        self.private = data.get("private", False)
        self.tags = list(data.get("tags", []))
        self._unserialize(data)
        return self

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Any:
        return cls().unserialize(data)

    # Subclass hooks -----------------------------------------------------------

    def _serialize(self) -> dict[str, Any]:
        raise NotImplementedError

    def _unserialize(self, data: dict[str, Any]) -> None:
        raise NotImplementedError

    # ------------------------------------------------------------------ dunders

    def __eq__(self, other: object) -> bool:
        return isinstance(other, PrimaryObject) and self.handle == other.handle

    def __hash__(self) -> int:
        return hash(self.handle)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.handle[:8]}>"
