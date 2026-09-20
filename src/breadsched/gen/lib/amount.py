"""Commodity-tagged exact amounts."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from typing import overload

from .money import Money, Rate

__all__ = ["Amount"]


@dataclass(frozen=True, slots=True, eq=False)
class Amount:
    """An exact scalar paired with the commodity that gives it meaning.

    Arithmetic which combines two amounts is deliberately closed over one
    commodity. A dated price conversion must produce a new amount before values
    in different commodities can be compared or netted.
    """

    value: Money
    commodity: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, Money):
            raise TypeError("amount value must be Money")
        if not isinstance(self.commodity, str) or not self.commodity:
            raise ValueError("amount commodity must be a non-empty identifier")

    def _same_commodity(self, other: object) -> Amount:
        if not isinstance(other, Amount):
            raise TypeError(f"cannot combine Amount with {type(other).__name__}")
        if self.commodity != other.commodity:
            raise TypeError(
                f"cannot combine unlike commodities {self.commodity!r} and {other.commodity!r}"
            )
        return other

    def __add__(self, other: object) -> Amount:
        rhs = self._same_commodity(other)
        return Amount(self.value + rhs.value, self.commodity)

    def __sub__(self, other: object) -> Amount:
        rhs = self._same_commodity(other)
        return Amount(self.value - rhs.value, self.commodity)

    def __mul__(self, scalar: Fraction | Rate | Decimal | int) -> Amount:
        return Amount(self.value * scalar, self.commodity)

    __rmul__ = __mul__

    @overload
    def __truediv__(self, other: Amount) -> Fraction: ...

    @overload
    def __truediv__(self, other: Rate | Decimal | int) -> Amount: ...

    def __truediv__(self, other: object) -> Amount | Fraction:
        if isinstance(other, Amount):
            rhs = self._same_commodity(other)
            return self.value / rhs.value
        if isinstance(other, (Rate, Decimal, int)):
            return Amount(self.value / other, self.commodity)
        return NotImplemented

    def __neg__(self) -> Amount:
        return Amount(-self.value, self.commodity)

    def __abs__(self) -> Amount:
        return Amount(abs(self.value), self.commodity)

    def __bool__(self) -> bool:
        return bool(self.value)

    def __eq__(self, other: object) -> bool:
        rhs = self._same_commodity(other)
        return self.value == rhs.value

    def __lt__(self, other: object) -> bool:
        rhs = self._same_commodity(other)
        return self.value < rhs.value

    def __le__(self, other: object) -> bool:
        rhs = self._same_commodity(other)
        return self.value <= rhs.value

    def __gt__(self, other: object) -> bool:
        rhs = self._same_commodity(other)
        return self.value > rhs.value

    def __ge__(self, other: object) -> bool:
        rhs = self._same_commodity(other)
        return self.value >= rhs.value

    def __hash__(self) -> int:
        return hash((self.value, self.commodity))

    def quantize(self, fraction: int) -> Amount:
        return Amount(self.value.quantize(fraction), self.commodity)

    def format(self, symbol: str = "", places: int = 2, parens_negative: bool = False) -> str:
        return self.value.format(symbol, places, parens_negative)
