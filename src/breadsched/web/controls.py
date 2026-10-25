"""Browser input parsing and service-error translation shared by every web adapter.

Resource adapters turn untrusted JSON into typed service requests and translate the
results. These helpers are the pieces they all need: an amount typed with the
browser's decimal convention, and a stable service failure as an HTTP resource
error with presentation-owned wording.
"""

from __future__ import annotations

from typing import Literal, cast

from ..api.contract import ApiError
from ..gen.engine.entry_input import parse_amount_text
from ..gen.lib import Money
from ..gen.services import ServiceError
from ..gen.utils.amount_input import NumberFormat, parse_user_amount

__all__ = ["ResourceError", "input_amount", "input_money", "service_error"]


#: A resource-adapter failure is the book API's own error envelope (#355).
ResourceError = ApiError


def service_error(error: ServiceError) -> ResourceError:
    """A service failure as a resource error: 404 for a missing object, else 400."""
    return ApiError.from_service(error)


def input_money(payload: dict, raw: object) -> Money:
    """Parse a browser-entered amount using the browser decimal convention."""
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        return Money(int(raw[0]), int(raw[1]))
    number_format = str(payload.get("number_format") or "auto")
    if number_format not in {"auto", "dot", "comma"}:
        raise ValueError("invalid number format")
    selected = cast(NumberFormat | Literal["auto"], number_format)
    return Money(parse_user_amount(str(raw).strip(), selected))


def input_amount(payload: dict, raw: object) -> Money:
    """A browser-entered amount that may be arithmetic (``76.84*1.06``), as the
    schedule editors accept (#370); otherwise read like :func:`input_money`."""
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        return Money(int(raw[0]), int(raw[1]))
    number_format = str(payload.get("number_format") or "auto")
    if number_format not in {"auto", "dot", "comma"}:
        raise ValueError("invalid number format")
    selected = cast(NumberFormat | Literal["auto"], number_format)
    return Money(parse_amount_text(str(raw).strip(), selected))
