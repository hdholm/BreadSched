"""Versioned book-server resources: ``/api/v1`` (#357).

**Experimental.** Each handler serves one operation of the book API
(:class:`~breadsched.api.protocol.BookApi`) as a resource, through the request's
book server (``Api.book``), and returns its JSON form from
:mod:`breadsched.api.views`. ``web.resources.V1_ROUTES`` maps methods and paths to
them; ``web.transport`` adds each response's revision (``ETag``), refuses a write
whose ``If-Match`` names an older revision, and streams change events.

Resources so far, all under ``/api/v1``:

* ``GET /book`` – the book's revision.
* ``GET /due-review`` (``as_of``) and ``POST /due-review`` (``decisions``, ``as_of``).
* ``GET /schedules/lookup`` (``reference``) – one schedule, as it is stored.
* ``GET`` and ``POST /schedules/{handle}/next`` (``decision``, ``date``, ``as_of``).
* ``GET`` and ``PATCH /schedules/{handle}/advance-days``.
* ``DELETE /schedules/{handle}`` and ``POST /schedules/{handle}/duplicate`` (``name``).
* ``POST /post-due`` (``as_of``, ``all``) – post what is due without a review.
* ``POST /schedules/fixed`` and ``POST /schedules/formula`` – save from an editor;
  the body is ``api.schedule_codec``'s form of the typed request.
* ``POST /undo`` and ``POST /redo``.
* ``GET /events`` (``since``) – server-sent change events, served by the transport.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any

from ..api.schedule_codec import fixed_request_from_json, formula_request_from_json
from ..api.views import (
    advance_days_json,
    due_review_json,
    next_occurrence_json,
    posted_due_json,
    resolution_json,
    saved_schedule_json,
)
from ..gen.services import (
    DueDecision,
    DuplicateSchedule,
    PostDue,
    ResolveDue,
    ResolveNext,
    SetAdvanceDays,
)
from .context import Api

if TYPE_CHECKING:
    from .resources import QueryParams

__all__ = [
    "advance_days_get",
    "advance_days_patch",
    "book_get",
    "due_review_get",
    "due_review_post",
    "next_get",
    "next_post",
    "post_due_post",
    "schedule_delete",
    "schedule_duplicate",
    "schedule_save_fixed",
    "schedule_save_formula",
    "redo_post",
    "schedule_lookup",
    "undo_post",
]

Path = dict[str, str]


def _optional_date(body: dict[str, Any], key: str) -> date | None:
    raw = body.get(key)
    if raw in (None, ""):
        return None
    if not isinstance(raw, str):
        raise ValueError(f"{key} must be a date (YYYY-MM-DD)")
    return date.fromisoformat(raw)


def _optional_days(body: dict[str, Any], key: str) -> int | None:
    raw = body.get(key)
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError(f"{key} must be a whole number of days")
    return raw


def _only(body: dict[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise ValueError(f"unknown field: {', '.join(unknown)}")


def book_get(api: Api, _path: Path, query: QueryParams) -> dict:
    query.finish()
    return {"revision": api.book.revision}


def due_review_get(api: Api, _path: Path, query: QueryParams) -> dict:
    as_of = query.iso_date("as_of")
    query.finish()
    return due_review_json(api.book.due_review(as_of))


def due_review_post(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    _only(body, {"decisions", "as_of"})
    raw = body.get("decisions")
    if not isinstance(raw, list):
        raise ValueError("decisions must be a list")
    decisions: list[tuple[str, date, DueDecision]] = []
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"schedule", "date", "decision"}:
            raise ValueError("each decision needs exactly schedule, date, and decision")
        decisions.append(
            (
                str(item["schedule"]),
                date.fromisoformat(str(item["date"])),
                DueDecision(item["decision"]),
            )
        )
    request = ResolveDue(tuple(decisions), as_of=_optional_date(body, "as_of"))
    return resolution_json(api.book.resolve_due(request))


def schedule_lookup(api: Api, _path: Path, query: QueryParams) -> dict:
    reference = query.text("reference", required=True) or ""
    query.finish()
    return {"schedule": api.book.find_schedule(reference).serialize()}


def next_get(api: Api, path: Path, query: QueryParams) -> dict:
    as_of = query.iso_date("as_of")
    query.finish()
    return {"next": next_occurrence_json(api.book.next_occurrence(path["handle"], as_of))}


def next_post(api: Api, path: Path, body: dict[str, Any]) -> dict:
    _only(body, {"decision", "date", "as_of"})
    decision = body.get("decision")
    if decision not in ("post", "skip"):
        raise ValueError("decision must be post or skip")
    request = ResolveNext(
        path["handle"],
        DueDecision(decision),
        when=_optional_date(body, "date"),
        as_of=_optional_date(body, "as_of"),
    )
    return resolution_json(api.book.resolve_next(request))


def advance_days_get(api: Api, path: Path, query: QueryParams) -> dict:
    query.finish()
    return advance_days_json(api.book.advance_days(path["handle"]))


def advance_days_patch(api: Api, path: Path, body: dict[str, Any]) -> dict:
    _only(body, {"advance_days", "remind_days"})
    request = SetAdvanceDays(
        path["handle"],
        advance_days=_optional_days(body, "advance_days"),
        remind_days=_optional_days(body, "remind_days"),
    )
    return advance_days_json(api.book.set_advance_days(request))


def undo_post(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    _only(body, set())
    return {"done": api.book.undo(), "revision": api.book.revision}


def redo_post(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    _only(body, set())
    return {"done": api.book.redo(), "revision": api.book.revision}


def schedule_delete(api: Api, path: Path, body: dict[str, Any]) -> dict:
    _only(body, set())
    return saved_schedule_json(api.book.delete_schedule(path["handle"]))


def schedule_duplicate(api: Api, path: Path, body: dict[str, Any]) -> dict:
    _only(body, {"name"})
    name = body.get("name")
    if name is not None and not isinstance(name, str):
        raise ValueError("name must be text")
    request = DuplicateSchedule(path["handle"], name=name or None)
    return saved_schedule_json(api.book.duplicate_schedule(request))


def post_due_post(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    _only(body, {"as_of", "all"})
    everything = body.get("all", False)
    if not isinstance(everything, bool):
        raise ValueError("all must be true or false")
    request = PostDue(as_of=_optional_date(body, "as_of"), only_automatic=not everything)
    return posted_due_json(api.book.post_due(request))


def schedule_save_fixed(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    return saved_schedule_json(api.book.save_fixed_schedule(fixed_request_from_json(body)))


def schedule_save_formula(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    return saved_schedule_json(api.book.save_formula_schedule(formula_request_from_json(body)))
