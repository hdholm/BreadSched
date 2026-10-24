"""The book API's contract: one error envelope and one JSON encoding (#355).

Every failure a book server reports is an :class:`ApiError`: a stable code, the
fields it concerns, an HTTP status, and presentation-owned wording. A service
failure maps to 404 when the code names a missing object (``*.not_found``) and to
400 otherwise, as the browser has always received.

:func:`to_json` is the single leaf encoding of money, rates, decimals, dates, and
enumerations, so the command line's ``--json`` and the browser's responses cannot
drift apart. Money is written exactly (:func:`exact_money`), never rounded.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, TypeVar

from ..gen.lib.money import Money
from ..gen.services import ServiceError, ServiceResult
from ..presentation import service_error_message

__all__ = ["ApiError", "exact_money", "to_json", "unwrap"]

T = TypeVar("T")


# Not frozen: raising through a context manager assigns ``__traceback__``.
@dataclass(eq=False)
class ApiError(Exception):
    """A book-server failure with a stable code and HTTP status."""

    status: int
    code: str
    fields: tuple[str, ...] = ()
    message: str | None = None

    @classmethod
    def from_service(cls, error: ServiceError) -> ApiError:
        status = 404 if error.code.endswith(".not_found") else 400
        return cls(status, error.code, error.fields, service_error_message(error))

    def __str__(self) -> str:
        return self.message or self.code

    def envelope(self) -> dict[str, object]:
        """The error body every transport sends."""
        return {"error": str(self), "code": self.code, "fields": list(self.fields)}


def unwrap(result: ServiceResult[T]) -> T:
    """The result's value, or its first failure raised as an :class:`ApiError`."""
    if result.value is None:
        raise ApiError.from_service(result.errors[0])
    return result.value


#: Beyond this many decimal places an amount is written as a fraction instead.
_MAX_PLACES = 30


def exact_money(value: Money) -> str:
    """``value`` exactly: decimal text with at least two places, or ``"n/d"``.

    ``Money.to_decimal`` rounds to cents, which would lose a share quantity or a
    price; ``Money`` reads both forms back unchanged.
    """
    numerator, denominator = value.numerator, value.denominator
    for places in range(_MAX_PLACES + 1):
        scale = 10**places
        if scale % denominator == 0:
            digits = max(places, 2)
            scaled = abs(numerator) * (scale // denominator) * 10 ** (digits - places)
            whole, fraction = divmod(scaled, 10**digits)
            sign = "-" if numerator < 0 else ""
            return f"{sign}{whole}.{fraction:0{digits}d}"
    return f"{numerator}/{denominator}"


def to_json(value: Any) -> Any:
    """``value`` in its JSON form: money and decimals as strings, dates in ISO."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, Money):
        return exact_money(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Enum):
        return to_json(value.value)
    if isinstance(value, Mapping):
        return {str(key): to_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_json(item) for item in value]
    raise TypeError(f"cannot serialise {type(value).__name__}")
