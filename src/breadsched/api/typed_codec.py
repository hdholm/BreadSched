"""One JSON form for the book API's typed requests and results (#357).

**Experimental.** The services' requests and results are frozen dataclasses of a
few kinds of value, so instead of a hand-written codec per area this module writes
any of them as JSON data (:func:`to_data`) and reads it back from its type alone
(:func:`from_data`):

* ``None``, ``bool``, ``int``, ``str``; ``Money`` exactly (``contract.exact_money``),
  ``Decimal`` as text, dates and times in ISO form, an enum as its value;
* a record that has its own stored form (``serialize`` and ``from_dict``, such as
  every ``PrimaryObject``) in that form;
* a dataclass as an object of its fields; tuples, lists, and sets as lists; a
  ``dict`` with text keys as an object; ``X | None`` and other unions.

Reading is strict, because a server reads what a client sends: an unknown field, a
missing required field, or a value of the wrong type is a ``ValueError`` naming
where it is (a 400), never a guess.
"""

from __future__ import annotations

import dataclasses
import types
import typing
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from functools import cache
from typing import Any, TypeVar, Union, get_args, get_origin, get_type_hints

from ..gen.lib.money import Money
from .contract import exact_money

__all__ = ["from_data", "to_data"]

T = TypeVar("T")


def _stored_form(kind: type) -> bool:
    """Whether ``kind`` round-trips through its own ``serialize``/``from_dict``."""
    return callable(getattr(kind, "serialize", None)) and callable(getattr(kind, "from_dict", None))


def to_data(value: Any) -> Any:
    """``value`` as JSON data."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, Money):
        return exact_money(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Enum):
        return to_data(value.value)
    if _stored_form(type(value)):
        return value.serialize()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: to_data(getattr(value, field.name)) for field in dataclasses.fields(value)
        }
    if isinstance(value, Mapping):
        return {str(key): to_data(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_data(item) for item in value]
    raise TypeError(f"cannot write {type(value).__name__} as JSON data")


@cache
def _hints(kind: type) -> dict[str, Any]:
    return get_type_hints(kind)


def _fail(where: str, expected: str) -> ValueError:
    return ValueError(f"{where} must be {expected}")


def from_data(kind: Any, data: Any, where: str = "value") -> Any:
    """A value of type ``kind`` from JSON data, or a ``ValueError`` naming ``where``."""
    if kind is Any:
        return data
    origin = get_origin(kind)
    if origin is Union or origin is types.UnionType:
        options = get_args(kind)
        if data is None:
            if type(None) in options:
                return None
            raise _fail(where, "given")
        failures = []
        for option in options:
            if option is type(None):
                continue
            try:
                return from_data(option, data, where)
            except ValueError as exc:
                failures.append(str(exc))
        raise ValueError(failures[0] if len(failures) == 1 else f"{where} is not valid")
    if kind is type(None):
        if data is not None:
            raise _fail(where, "null")
        return None
    if origin in (tuple, list, set, frozenset):
        if not isinstance(data, list):
            raise _fail(where, "a list")
        args = get_args(kind)
        if origin is tuple and args and args[-1] is not Ellipsis:
            if len(args) != len(data):
                raise _fail(where, f"a list of {len(args)} values")
            return tuple(
                from_data(item_kind, item, f"{where}[{index}]")
                for index, (item_kind, item) in enumerate(zip(args, data, strict=True))
            )
        item_kind = args[0] if args else Any
        items = [from_data(item_kind, item, f"{where}[{index}]") for index, item in enumerate(data)]
        return origin(items)
    if origin is dict:
        if not isinstance(data, dict):
            raise _fail(where, "an object")
        _key, value_kind = get_args(kind) or (str, Any)
        return {
            str(key): from_data(value_kind, value, f"{where}.{key}") for key, value in data.items()
        }
    if origin is typing.Literal:
        if data not in get_args(kind):
            raise _fail(where, "one of " + ", ".join(repr(item) for item in get_args(kind)))
        return data
    if not isinstance(kind, type):
        raise TypeError(f"cannot read {kind!r} from JSON data")
    if kind is bool:
        if not isinstance(data, bool):
            raise _fail(where, "true or false")
        return data
    if kind is int:
        if isinstance(data, bool) or not isinstance(data, int):
            raise _fail(where, "a whole number")
        return data
    if kind is str:
        if not isinstance(data, str):
            raise _fail(where, "text")
        return data
    if kind is Money:
        if not isinstance(data, str):
            raise _fail(where, "an amount in text")
        try:
            return Money(data)
        except (ArithmeticError, TypeError, ValueError):
            raise _fail(where, "an amount") from None
    if kind is Decimal:
        if not isinstance(data, str):
            raise _fail(where, "a decimal number in text")
        try:
            return Decimal(data)
        except InvalidOperation:
            raise _fail(where, "a decimal number") from None
    if kind is datetime:
        if not isinstance(data, str):
            raise _fail(where, "a date and time")
        try:
            return datetime.fromisoformat(data)
        except ValueError:
            raise _fail(where, "a date and time") from None
    if kind is date:
        if not isinstance(data, str):
            raise _fail(where, "a date (YYYY-MM-DD)")
        try:
            return date.fromisoformat(data)
        except ValueError:
            raise _fail(where, "a date (YYYY-MM-DD)") from None
    if issubclass(kind, Enum):
        try:
            return kind(data)
        except ValueError:
            raise _fail(where, "one of " + ", ".join(str(item.value) for item in kind)) from None
    if _stored_form(kind):
        if not isinstance(data, dict):
            raise _fail(where, "an object")
        try:
            return getattr(kind, "from_dict")(data)  # noqa: B009 - mypy cannot see it
        except (KeyError, TypeError, ValueError, AttributeError, ArithmeticError) as exc:
            raise ValueError(f"{where} is not a valid {kind.__name__}: {exc}") from None
    if dataclasses.is_dataclass(kind):
        if not isinstance(data, dict):
            raise _fail(where, "an object")
        hints = _hints(kind)
        fields = {field.name: field for field in dataclasses.fields(kind) if field.init}
        unknown = sorted(set(data) - set(fields))
        if unknown:
            raise ValueError(f"{where} has unknown field: {', '.join(unknown)}")
        values = {}
        for name, field in fields.items():
            if name in data:
                values[name] = from_data(hints[name], data[name], f"{where}.{name}")
            elif (
                field.default is dataclasses.MISSING
                and field.default_factory is dataclasses.MISSING
            ):
                raise ValueError(f"{where}.{name} is required")
        return kind(**values)
    raise TypeError(f"cannot read {kind.__name__} from JSON data")
