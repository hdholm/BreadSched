"""The one JSON form of each book-server response (#355).

The command line's ``--json`` and the browser's responses are built here, from
the services' typed results, so both interfaces describe a book identically. Each
function returns plain JSON data; :func:`~breadsched.api.contract.to_json` encodes
the leaves.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from ..gen.lib.money import Money
from ..gen.services import (
    AdvanceDays,
    DueItem,
    DueResolution,
    DueScheduleReview,
    NextOccurrence,
)
from .contract import to_json
from .protocol import Change

__all__ = [
    "advance_days_from_json",
    "advance_days_json",
    "change_from_json",
    "change_json",
    "due_review_from_json",
    "due_review_json",
    "next_occurrence_from_json",
    "next_occurrence_json",
    "resolution_from_json",
    "resolution_json",
]


def due_review_json(reviews: list[DueScheduleReview]) -> dict[str, Any]:
    """Due, missed, and early occurrences, grouped by schedule."""
    return {
        "schedules": [
            {
                "schedule": review.schedule,
                "name": review.name,
                "frequency": review.frequency,
                "total": to_json(review.total),
                "items": [
                    {
                        "date": to_json(item.when),
                        "amount": to_json(item.amount),
                        "overdue": item.overdue,
                        "stage": item.stage,
                    }
                    for item in review.items
                ],
            }
            for review in reviews
        ]
    }


def resolution_json(resolution: DueResolution) -> dict[str, Any]:
    """What a batch of due decisions, or one next-occurrence decision, wrote."""
    return {
        "posted": resolution.posted,
        "skipped": resolution.skipped,
        "deferred": resolution.deferred,
        "transactions": list(resolution.transactions),
    }


def next_occurrence_json(upcoming: NextOccurrence | None) -> dict[str, Any] | None:
    """A schedule's next occurrence still to enter or skip, or None."""
    if upcoming is None:
        return None
    return {
        "schedule": upcoming.schedule,
        "name": upcoming.name,
        "date": to_json(upcoming.when),
        "amount": to_json(upcoming.amount),
    }


def advance_days_json(days: AdvanceDays) -> dict[str, Any]:
    """How far ahead a schedule is entered and reminded."""
    return {
        "schedule": days.schedule,
        "name": days.name,
        "advance_days": days.advance_days,
        "remind_days": days.remind_days,
    }


def change_json(change: Change) -> dict[str, Any]:
    """A change event as a subscriber receives it."""
    return {"revision": change.revision, "kinds": sorted(change.kinds), "label": change.label}


# ------------------------------------------------------------------ decoding
#
# The HTTP client turns each JSON form back into the services' typed results, so a
# caller cannot tell an in-process server from a remote one.


def due_review_from_json(payload: dict[str, Any]) -> list[DueScheduleReview]:
    return [
        DueScheduleReview(
            schedule=review["schedule"],
            name=review["name"],
            frequency=review["frequency"],
            items=tuple(
                DueItem(
                    review["schedule"],
                    date.fromisoformat(item["date"]),
                    Money(item["amount"]),
                    bool(item["overdue"]),
                    item["stage"],
                )
                for item in review["items"]
            ),
        )
        for review in payload["schedules"]
    ]


def resolution_from_json(payload: dict[str, Any]) -> DueResolution:
    return DueResolution(
        int(payload["posted"]),
        int(payload["skipped"]),
        int(payload["deferred"]),
        tuple(payload["transactions"]),
    )


def next_occurrence_from_json(payload: dict[str, Any] | None) -> NextOccurrence | None:
    if payload is None:
        return None
    return NextOccurrence(
        payload["schedule"],
        payload["name"],
        date.fromisoformat(payload["date"]),
        Money(payload["amount"]),
    )


def advance_days_from_json(payload: dict[str, Any]) -> AdvanceDays:
    return AdvanceDays(
        payload["schedule"],
        payload["name"],
        int(payload["advance_days"]),
        int(payload["remind_days"]),
    )


def change_from_json(payload: dict[str, Any]) -> Change:
    return Change(int(payload["revision"]), frozenset(payload["kinds"]), payload.get("label"))
