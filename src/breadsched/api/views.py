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
    AccountBalance,
    AccountRef,
    AdvanceDays,
    BalanceSummary,
    DueItem,
    DueResolution,
    DueScheduleReview,
    NextOccurrence,
    PostedDue,
    PostedOccurrence,
    RegisterLine,
    RegisterPage,
    SavedSchedule,
    SavedTransaction,
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
    "posted_due_from_json",
    "posted_due_json",
    "resolution_from_json",
    "resolution_json",
    "saved_schedule_from_json",
    "saved_schedule_json",
    "saved_transaction_from_json",
    "saved_transaction_json",
    "account_ref_json",
    "account_ref_from_json",
    "register_json",
    "register_from_json",
    "account_balance_json",
    "account_balance_from_json",
    "balance_summary_json",
    "balance_summary_from_json",
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


def saved_schedule_json(saved: SavedSchedule) -> dict[str, Any]:
    """The schedule a delete or duplicate acted on."""
    return {"schedule": saved.handle, "name": saved.name}


def posted_due_json(posted: PostedDue) -> dict[str, Any]:
    """What posting without a review wrote, one transaction per occurrence."""
    return {
        "posted": len(posted.posted),
        "transactions": [
            {
                "transaction": item.transaction,
                "schedule": item.schedule,
                "date": to_json(item.when),
                "description": item.description,
            }
            for item in posted.posted
        ],
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


def saved_schedule_from_json(payload: dict[str, Any]) -> SavedSchedule:
    return SavedSchedule(payload["schedule"], payload["name"])


def posted_due_from_json(payload: dict[str, Any]) -> PostedDue:
    return PostedDue(
        tuple(
            PostedOccurrence(
                item["transaction"],
                item["schedule"],
                date.fromisoformat(item["date"]),
                item["description"],
            )
            for item in payload["transactions"]
        )
    )


def saved_transaction_json(saved: SavedTransaction) -> dict[str, Any]:
    """The transaction a save or delete acted on."""
    return {
        "transaction": saved.handle,
        "date": to_json(saved.post_date),
        "description": saved.description,
    }


def saved_transaction_from_json(payload: dict[str, Any]) -> SavedTransaction:
    return SavedTransaction(
        payload["transaction"], date.fromisoformat(payload["date"]), payload["description"]
    )


def account_ref_json(account: AccountRef) -> dict[str, Any]:
    return {
        "handle": account.handle,
        "name": account.name,
        "full_name": account.full_name,
        "type": account.type,
        "commodity": account.commodity,
    }


def account_ref_from_json(payload: dict[str, Any]) -> AccountRef:
    return AccountRef(
        payload["handle"],
        payload["name"],
        payload["full_name"],
        payload["type"],
        payload.get("commodity"),
    )


def register_json(page: RegisterPage) -> dict[str, Any]:
    """An account's register lines, oldest first, with running balances."""
    return {
        "account": account_ref_json(page.account),
        "rows": [
            {
                "handle": line.transaction,
                "split": line.split,
                "date": to_json(line.when),
                "description": line.description,
                "transfer": line.transfer,
                "amount": to_json(line.amount),
                "balance": to_json(line.balance),
                "tags": list(line.tags),
                "attachments": line.attachments,
            }
            for line in page.lines
        ],
    }


def register_from_json(payload: dict[str, Any]) -> RegisterPage:
    return RegisterPage(
        account_ref_from_json(payload["account"]),
        tuple(
            RegisterLine(
                row["handle"],
                row["split"],
                date.fromisoformat(row["date"]),
                row["description"],
                row["transfer"],
                Money(row["amount"]),
                Money(row["balance"]),
                tuple(row["tags"]),
                int(row["attachments"]),
            )
            for row in payload["rows"]
        ),
    )


def account_balance_json(balance: AccountBalance) -> dict[str, Any]:
    return {
        "account": balance.account.full_name,
        "handle": balance.account.handle,
        "balance": to_json(balance.amount),
        "recursive": balance.recursive,
        "account_ref": account_ref_json(balance.account),
    }


def account_balance_from_json(payload: dict[str, Any]) -> AccountBalance:
    return AccountBalance(
        account_ref_from_json(payload["account_ref"]),
        Money(payload["balance"]),
        bool(payload["recursive"]),
    )


def balance_summary_json(summary: BalanceSummary) -> dict[str, Any]:
    return {
        "cash": to_json(summary.cash),
        "net_worth": to_json(summary.net_worth),
        "cash_missing_quotes": list(summary.cash_missing_quotes),
        "net_worth_missing_quotes": list(summary.net_worth_missing_quotes),
        "net_worth_incompatible_accounts": list(summary.net_worth_incompatible_accounts),
    }


def balance_summary_from_json(payload: dict[str, Any]) -> BalanceSummary:
    def money(raw):
        return None if raw is None else Money(raw)

    return BalanceSummary(
        money(payload["cash"]),
        money(payload["net_worth"]),
        tuple(payload["cash_missing_quotes"]),
        tuple(payload["net_worth_missing_quotes"]),
        tuple(payload["net_worth_incompatible_accounts"]),
    )
