"""Payees: stable, user-reviewed identities for who a transaction was with.

A payee never replaces a transaction's description. Imported descriptions stay
exactly as the source wrote them; the payee is a separate reference the user
accepts, so renaming a payee changes every accepted transaction's payee at once
without rewriting any source text.
"""

from __future__ import annotations

from typing import Any

from .base import PrimaryObject

__all__ = ["Payee"]


class Payee(PrimaryObject):
    """A named payee and the normalized description keys that identify it."""

    TABLE = "payee"

    def __init__(
        self,
        name: str = "",
        match_keys: list[str] | None = None,
        handle: str | None = None,
    ) -> None:
        super().__init__(handle=handle)
        self.name = name
        #: Normalized description keys (see ``engine.payees.match_key``). A key
        #: belongs to at most one payee, so a proposal is never ambiguous.
        self.match_keys = list(match_keys or [])

    def _serialize(self) -> dict[str, Any]:
        return {"name": self.name, "match_keys": list(self.match_keys)}

    def _unserialize(self, data: dict[str, Any]) -> None:
        self.name = str(data.get("name", ""))
        self.match_keys = [str(key) for key in data.get("match_keys", [])]

    def __repr__(self) -> str:
        return f"<Payee {self.name!r}>"
