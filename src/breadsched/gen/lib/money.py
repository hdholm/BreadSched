"""Exact monetary arithmetic.

Money is a rational number held as ``numerator / denominator`` of integers, which
is exactly the representation GnuCash uses for its ``gnc_numeric`` values.  Keeping
the same shape means a GnuCash split's ``value_num`` / ``value_denom`` pair survives
a round trip through this application without a single rounding artefact, and it
means arithmetic here is exact rather than binary-floating-point approximate.

Never use ``float`` for money.  ``Money`` accepts ``str``, ``int`` and ``Decimal``
and refuses ``float`` deliberately.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext
from fractions import Fraction
from math import gcd
from numbers import Integral
from typing import overload

__all__ = ["Money", "Rate", "ZERO"]

_US_GROUPED = re.compile(r"^[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?$")


class Rate(Decimal):
    """Dimensionless decimal rate used for growth, interest, and returns.

    ``Rate`` subclasses :class:`Decimal` deliberately: rates retain Decimal's
    exact arithmetic, formatting, JSON boundary handling, and compatibility with
    existing scenario data while remaining distinguishable from monetary amounts.
    """

    def __new__(cls, value: Rate | Decimal | str | int = 0) -> Rate:
        if isinstance(value, float):
            raise TypeError("refusing to build Rate from float; use str or Decimal")
        result = super().__new__(cls, str(value))
        if not result.is_finite():
            raise ValueError("rate must be finite")
        return result

    @property
    def decimal(self) -> Decimal:
        return Decimal(self)


class Money:
    """An exact rational amount of a single commodity."""

    __slots__ = ("_num", "_den")

    def __init__(self, numerator: int | str | Decimal = 0, denominator: int = 1) -> None:
        if isinstance(numerator, Money):
            num, den = numerator._num, numerator._den * denominator
        elif isinstance(numerator, str):
            num, den = self._parse(numerator)
            den *= denominator
        elif isinstance(numerator, Decimal):
            num, den = self._from_decimal(numerator)
            den *= denominator
        elif isinstance(numerator, Integral):
            num, den = int(numerator), int(denominator)
        elif isinstance(numerator, float):
            raise TypeError("refusing to build Money from float; use str or Decimal")
        else:
            raise TypeError(f"cannot build Money from {type(numerator).__name__}")

        if den == 0:
            raise ZeroDivisionError("Money denominator must not be zero")
        if den < 0:
            num, den = -num, -den
        common = gcd(abs(num), den) or 1
        self._num = num // common
        self._den = den // common

    # ---------------------------------------------------------------- builders

    @staticmethod
    def _parse(text: str) -> tuple[int, int]:
        raw = text
        text = text.strip().replace("$", "")
        if not text:
            return 0, 1

        # Money itself deliberately uses one unambiguous numeric syntax. Locale-
        # aware user/import parsing belongs at the input boundary. Accept strict
        # English thousands grouping for backward compatibility, but reject comma
        # forms that could also mean a decimal separator rather than silently
        # scaling them by 100 or 1000.
        if "," in text:
            if not _US_GROUPED.fullmatch(text):
                raise ValueError(f"cannot read {raw!r} as an amount")
            text = text.replace(",", "")

        try:
            if "/" in text:
                num, _, den = text.partition("/")
                return int(num), int(den)
            return Money._from_decimal(Decimal(text))
        except (InvalidOperation, ValueError) as exc:
            # decimal raises InvalidOperation, which tells a caller nothing about
            # what it was handed. Anything reading a foreign file needs the text.
            raise ValueError(f"cannot read {raw!r} as an amount") from exc

    @staticmethod
    def _from_decimal(value: Decimal) -> tuple[int, int]:
        sign, digits, exponent = value.as_tuple()
        if not isinstance(exponent, int):  # NaN / Infinity
            raise ValueError(f"cannot represent {value} as Money")
        mantissa = int("".join(str(d) for d in digits) or "0")
        if sign:
            mantissa = -mantissa
        if exponent >= 0:
            return mantissa * 10**exponent, 1
        return mantissa, 10 ** (-exponent)

    @classmethod
    def from_gnc(cls, numerator: int, denominator: int) -> Money:
        """Build from a GnuCash ``*_num`` / ``*_denom`` column pair."""
        return cls(int(numerator), int(denominator))

    @classmethod
    def from_minor(cls, units: int, fraction: int = 100) -> Money:
        """Build from minor units, e.g. ``from_minor(1250)`` is 12.50."""
        return cls(int(units), int(fraction))

    # -------------------------------------------------------------- properties

    @property
    def numerator(self) -> int:
        return self._num

    @property
    def denominator(self) -> int:
        return self._den

    def as_gnc(self, denominator: int = 100) -> tuple[int, int]:
        """Return a ``(num, denom)`` pair at the requested denominator."""
        return self.quantize(denominator)._num_at(denominator), denominator

    def _num_at(self, denominator: int) -> int:
        scaled = self._num * denominator
        quotient, remainder = divmod(abs(scaled), self._den)
        if remainder * 2 >= self._den:
            quotient += 1
        return quotient if scaled >= 0 else -quotient

    # -------------------------------------------------------------- arithmetic

    @staticmethod
    def _coerce(other: object) -> Money | None:
        if isinstance(other, Money):
            return other
        if isinstance(other, (str, Decimal)) or isinstance(other, Integral):
            return Money(other)  # type: ignore[arg-type]
        return None

    @staticmethod
    def _coerce_comparison(other: object) -> Money | None:
        if isinstance(other, Money):
            return other
        if isinstance(other, Decimal):
            return Money(other)
        if isinstance(other, Integral):
            return Money(int(other))
        return None

    def __add__(self, other: object) -> Money:
        rhs = self._coerce(other)
        if rhs is None:
            return NotImplemented
        return Money(self._num * rhs._den + rhs._num * self._den, self._den * rhs._den)

    __radd__ = __add__

    def __sub__(self, other: object) -> Money:
        rhs = self._coerce(other)
        if rhs is None:
            return NotImplemented
        return Money(self._num * rhs._den - rhs._num * self._den, self._den * rhs._den)

    def __rsub__(self, other: object) -> Money:
        rhs = self._coerce(other)
        if rhs is None:
            return NotImplemented
        return rhs - self

    def __mul__(self, other: object) -> Money:
        if isinstance(other, Money):
            raise TypeError("cannot multiply two monetary amounts; use a Rate or scalar")
        if isinstance(other, Fraction):
            return Money(self._num * other.numerator, self._den * other.denominator)
        if isinstance(other, Rate):
            factor = other.decimal
        elif isinstance(other, Decimal):
            factor = other
        elif isinstance(other, Integral):
            factor = Decimal(int(other))
        else:
            return NotImplemented
        num, den = self._from_decimal(factor)
        return Money(self._num * num, self._den * den)

    __rmul__ = __mul__

    @overload
    def __truediv__(self, other: Money) -> Fraction: ...

    @overload
    def __truediv__(self, other: int | Decimal) -> Money: ...

    def __truediv__(self, other: object) -> Money | Fraction:
        if isinstance(other, Money):
            if other._num == 0:
                raise ZeroDivisionError("division by zero Money")
            return Fraction(self._num * other._den, self._den * other._num)
        if isinstance(other, Decimal):
            num, den = self._from_decimal(other)
        elif isinstance(other, Integral):
            num, den = int(other), 1
        else:
            return NotImplemented
        if num == 0:
            raise ZeroDivisionError("division by zero scalar")
        return Money(self._num * den, self._den * num)

    def __neg__(self) -> Money:
        return Money(-self._num, self._den)

    def __abs__(self) -> Money:
        return Money(abs(self._num), self._den)

    def __bool__(self) -> bool:
        return self._num != 0

    # -------------------------------------------------------------- comparison

    def _cmp_key(self, other: Money) -> tuple[int, int]:
        return self._num * other._den, other._num * self._den

    def __eq__(self, other: object) -> bool:
        rhs = self._coerce_comparison(other)
        if rhs is None:
            return NotImplemented
        return self._num == rhs._num and self._den == rhs._den

    def __hash__(self) -> int:
        # Python requires equal numeric objects to share a hash. Fraction already
        # implements the cross-numeric hash contract for ints and Decimals.
        return hash(Fraction(self._num, self._den))

    def __lt__(self, other: object) -> bool:
        rhs = self._coerce_comparison(other)
        if rhs is None:
            return NotImplemented
        left, right = self._cmp_key(rhs)
        return left < right

    def __le__(self, other: object) -> bool:
        rhs = self._coerce_comparison(other)
        if rhs is None:
            return NotImplemented
        left, right = self._cmp_key(rhs)
        return left <= right

    def __gt__(self, other: object) -> bool:
        result = self.__le__(other)
        return result if result is NotImplemented else not result

    def __ge__(self, other: object) -> bool:
        result = self.__lt__(other)
        return result if result is NotImplemented else not result

    # ------------------------------------------------------------- conversions

    def quantize(self, denominator: int = 100) -> Money:
        """Round to a whole multiple of ``1/denominator`` (half away from zero)."""
        return Money(self._num_at(denominator), denominator)

    def to_decimal(self, places: int = 2) -> Decimal:
        with localcontext() as ctx:
            ctx.prec = 40
            value = Decimal(self._num) / Decimal(self._den)
        return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)

    def rate(self) -> Decimal:
        """Full-precision Decimal, for use as a growth or return factor."""
        with localcontext() as ctx:
            ctx.prec = 40
            return Decimal(self._num) / Decimal(self._den)

    def allocate(self, parts: int) -> list[Money]:
        """Split into ``parts`` cent-exact amounts that sum back to ``self``.

        Used when a yearly budget figure has to be spread across months without
        losing or inventing a cent.
        """
        if parts < 1:
            raise ValueError("parts must be >= 1")
        total_cents = self._num_at(100)
        base, remainder = divmod(abs(total_cents), parts)
        sign = 1 if total_cents >= 0 else -1
        return [
            Money(sign * (base + (1 if i < remainder else 0)), 100) for i in range(parts)
        ]

    # ------------------------------------------------------------ presentation

    def format(self, symbol: str = "", places: int = 2, parens_negative: bool = False) -> str:
        value = self.to_decimal(places)
        negative = value < 0
        body = f"{symbol}{abs(value):,.{places}f}"
        if not negative:
            return body
        return f"({body})" if parens_negative else f"-{body}"

    def __str__(self) -> str:
        return self.format()

    def __repr__(self) -> str:
        return f"Money('{self.to_decimal(4)}')"


ZERO = Money(0)
