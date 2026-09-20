"""Commodities: currencies and securities."""

from __future__ import annotations

from datetime import date
from fractions import Fraction
from typing import Any

from .amount import Amount
from .base import PrimaryObject
from .money import Money

__all__ = ["Commodity", "CommodityPrice", "DEFAULT_CURRENCY"]


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


class CommodityPrice(PrimaryObject):
    """One exact, dated price for a security in a currency."""

    TABLE = "price"

    def __init__(
        self,
        handle: str | None = None,
        commodity: str = "",
        currency: str = "",
        quote_date: date | None = None,
        value: Money | str | int = 1,
        source: str = "breadsched",
        quote_type: str = "last",
    ) -> None:
        super().__init__(handle)
        self.commodity = commodity
        self.currency = currency
        self.quote_date = quote_date or date.today()
        self.value = value if isinstance(value, Money) else Money(value)
        if self.value <= 0:
            raise ValueError("commodity price must be greater than zero")
        self.source = source
        self.quote_type = quote_type

    def _serialize(self) -> dict[str, Any]:
        return {
            "commodity": self.commodity,
            "currency": self.currency,
            "quote_date": self.quote_date.isoformat(),
            "value": [self.value.numerator, self.value.denominator],
            "source": self.source,
            "quote_type": self.quote_type,
        }

    def convert(self, amount: Amount, *, fraction: int | None = None) -> Amount:
        """Convert security units with this exact dated quote.

        The quote date is part of this object, so callers cannot perform an
        implicit timeless conversion or accidentally apply a quote for another
        security. ``fraction`` is the quote currency's smallest subdivision.
        """
        if amount.commodity != self.commodity:
            raise ValueError("price commodity does not match amount commodity")
        scalar = Fraction(amount.value.numerator, amount.value.denominator)
        result = Amount(self.value * scalar, self.currency)
        return result.quantize(fraction) if fraction is not None else result

    def _unserialize(self, data: dict[str, Any]) -> None:
        self.commodity = str(data["commodity"])
        self.currency = str(data["currency"])
        self.quote_date = date.fromisoformat(str(data["quote_date"]))
        self.value = Money(*data["value"])
        if self.value <= 0:
            raise ValueError("commodity price must be greater than zero")
        self.source = str(data.get("source", ""))
        self.quote_type = str(data.get("quote_type", "last"))

    def __repr__(self) -> str:
        return f"<CommodityPrice {self.commodity[:8]} {self.quote_date} {self.value}>"


_SYMBOLS = {"USD": "$", "CAD": "$", "AUD": "$", "GBP": "\u00a3", "EUR": "\u20ac", "JPY": "\u00a5"}

DEFAULT_CURRENCY = Commodity(mnemonic="USD", fullname="US Dollar", fraction=100)
