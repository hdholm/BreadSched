"""Commodities: currencies and securities."""

from __future__ import annotations

from typing import Any

from .base import PrimaryObject

__all__ = ["Commodity", "DEFAULT_CURRENCY"]


class Commodity(PrimaryObject):
    """A unit of value: a currency such as USD, or a security such as VTSAX.

    ``fraction`` is the smallest tradable subdivision (100 for a decimal currency,
    1 for a whole-share security), matching GnuCash's ``commodities.fraction``.
    """

    TABLE = "commodity"

    def __init__(
        self,
        handle: str | None = None,
        namespace: str = "CURRENCY",
        mnemonic: str = "USD",
        fullname: str = "",
        fraction: int = 100,
        symbol: str = "",
    ) -> None:
        super().__init__(handle)
        self.namespace = namespace
        self.mnemonic = mnemonic
        self.fullname = fullname or mnemonic
        self.fraction = fraction
        self.symbol = symbol or _SYMBOLS.get(mnemonic, "")

    @property
    def is_currency(self) -> bool:
        return self.namespace.upper() in ("CURRENCY", "ISO4217")

    def _serialize(self) -> dict[str, Any]:
        return {
            "namespace": self.namespace,
            "mnemonic": self.mnemonic,
            "fullname": self.fullname,
            "fraction": self.fraction,
            "symbol": self.symbol,
        }

    def _unserialize(self, data: dict[str, Any]) -> None:
        self.namespace = data["namespace"]
        self.mnemonic = data["mnemonic"]
        self.fullname = data.get("fullname", "")
        self.fraction = data.get("fraction", 100)
        self.symbol = data.get("symbol", "")

    def __repr__(self) -> str:
        return f"<Commodity {self.namespace}:{self.mnemonic}>"


_SYMBOLS = {"USD": "$", "CAD": "$", "AUD": "$", "GBP": "\u00a3", "EUR": "\u20ac", "JPY": "\u00a5"}

DEFAULT_CURRENCY = Commodity(mnemonic="USD", fullname="US Dollar", fraction=100)
