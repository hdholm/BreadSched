"""Browser input parsing and service-error translation shared by every web adapter.

Resource adapters turn untrusted JSON into typed service requests and translate the
results. These helpers are the pieces they all need: an amount typed with the
browser's decimal convention, and a stable service failure as an HTTP resource
error with presentation-owned wording.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast

from ..gen.lib import Money
from ..gen.services import ServiceError
from ..gen.utils.amount_input import NumberFormat, parse_user_amount
from ..presentation import service_error_message

__all__ = ["ResourceError", "input_money", "service_error"]


@dataclass(frozen=True, slots=True)
class ResourceError(Exception):
    """A resource-adapter failure with a stable HTTP representation."""

    status: int
    code: str
    fields: tuple[str, ...] = ()
    message: str | None = None


def service_error(error: ServiceError) -> ResourceError:
    """A service failure as a resource error: 404 for a missing object, else 400."""
    status = 404 if error.code.endswith(".not_found") else 400
    return ResourceError(status, error.code, error.fields, service_error_message(error))


def input_money(payload: dict, raw: object) -> Money:
    """Parse a browser-entered amount using the browser decimal convention."""
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        return Money(int(raw[0]), int(raw[1]))
    number_format = str(payload.get("number_format") or "auto")
    if number_format not in {"auto", "dot", "comma"}:
        raise ValueError("invalid number format")
    selected = cast(NumberFormat | Literal["auto"], number_format)
    return Money(parse_user_amount(str(raw).strip(), selected))
