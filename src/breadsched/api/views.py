"""The one JSON form of each book-server response (#355).

The command line's ``--json`` and the browser's responses are built here, from
the services' typed results, so both interfaces describe a book identically. Each
function returns plain JSON data; :func:`~breadsched.api.contract.to_json` encodes
the leaves.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from ..gen.engine.categorization import CategoryConflict
from ..gen.engine.reconciliation import ReconciliationCandidate, ReconciliationSummary
from ..gen.engine.savings_goals import GoalProgress
from ..gen.lib import Account
from ..gen.lib.money import Money
from ..gen.lib.reconciliation import Reconciliation
from ..gen.lib.savings_goal import SavingsGoal
from ..gen.lib.scenario import GoalOverride
from ..gen.lib.transaction import ReconcileState
from ..gen.services import (
    AccountBalance,
    AccountRecord,
    AccountRef,
    AdvanceDays,
    BalanceSummary,
    ChartLine,
    DueItem,
    DueResolution,
    DueScheduleReview,
    NextOccurrence,
    PostedDue,
    PostedOccurrence,
    RegisterLine,
    RegisterPage,
    SavedAccount,
    SavedSchedule,
    SavedTransaction,
    TagCount,
)
from ..gen.services.categorization import (
    AppliedCategories,
    CategorizationRules,
    CategoryChoice,
    CategoryProposal,
    CategoryRule,
    ProposalLine,
    RuleLine,
)
from ..gen.services.reconciliations import ReconciliationView
from ..gen.services.savings_goals import (
    GoalOverrideLine,
    NamedChoice,
    SavingsGoalReport,
    SavingsGoalsView,
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
    "account_record_json",
    "account_record_from_json",
    "chart_json",
    "chart_from_json",
    "saved_account_json",
    "saved_account_from_json",
    "rule_json",
    "rule_from_json",
    "categorization_rules_json",
    "categorization_rules_from_json",
    "applied_categories_json",
    "applied_categories_from_json",
    "tag_counts_json",
    "tag_counts_from_json",
    "savings_goals_json",
    "savings_goals_from_json",
    "reconciliation_summary_json",
    "reconciliation_summary_from_json",
    "reconciliation_view_json",
    "reconciliation_view_from_json",
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
    return {
        "revision": change.revision,
        "kinds": sorted(change.kinds),
        "label": change.label,
        "transactions": sorted(change.transactions),
        "can_undo": change.can_undo,
        "can_redo": change.can_redo,
        "by": change.by,
        "undo_by": change.undo_by,
    }


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
    return Change(
        int(payload["revision"]),
        frozenset(payload["kinds"]),
        payload.get("label"),
        frozenset(payload.get("transactions", ())),
        bool(payload.get("can_undo", False)),
        bool(payload.get("can_redo", False)),
        payload.get("by"),
        payload.get("undo_by"),
    )


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
        "assets": to_json(summary.assets),
        "liabilities": to_json(summary.liabilities),
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
        money(payload.get("assets")),
        money(payload.get("liabilities")),
    )


def account_record_json(record: AccountRecord) -> dict[str, Any]:
    """An account in its stored form, with its full name."""
    return {"account": record.account.serialize(), "full_name": record.full_name}


def account_record_from_json(payload: dict[str, Any]) -> AccountRecord:
    return AccountRecord(Account.from_dict(payload["account"]), payload["full_name"])


def chart_json(lines: list[ChartLine]) -> dict[str, Any]:
    """The account tree, depth first, with each line's value and quote evidence."""
    return {
        "lines": [
            {
                **account_record_json(line.record),
                "depth": line.depth,
                "balance": to_json(line.balance),
                "missing_quotes": list(line.missing_quotes),
                "quote_date": to_json(line.quote_date),
                "quote_source": line.quote_source,
                "quote_age_days": line.quote_age_days,
                "conversion_path": line.conversion_path,
                "quote_evidence": line.quote_evidence,
                "book_balance": to_json(line.book_balance),
                "own_balance": to_json(line.own_balance),
                "valuation_source": line.valuation_source,
                "quantity": to_json(line.quantity),
                "price": to_json(line.price),
                "missing_quote": line.missing_quote,
                "commodity": line.commodity,
                "currency": line.currency,
            }
            for line in lines
        ]
    }


def chart_from_json(payload: dict[str, Any]) -> list[ChartLine]:
    def money(raw):
        return None if raw is None else Money(raw)

    return [
        ChartLine(
            account_record_from_json(row),
            int(row["depth"]),
            money(row["balance"]),
            tuple(row["missing_quotes"]),
            None if row["quote_date"] is None else date.fromisoformat(row["quote_date"]),
            row["quote_source"],
            row["quote_age_days"],
            row["conversion_path"],
            row["quote_evidence"],
            money(row["book_balance"]),
            Money(row["own_balance"]),
            row["valuation_source"],
            money(row["quantity"]),
            money(row["price"]),
            bool(row["missing_quote"]),
            row["commodity"],
            row["currency"],
        )
        for row in payload["lines"]
    ]


def saved_account_json(saved: SavedAccount) -> dict[str, Any]:
    return {"account": saved.handle, "name": saved.name}


def saved_account_from_json(payload: dict[str, Any]) -> SavedAccount:
    return SavedAccount(payload["account"], payload["name"])


def rule_json(rule: CategoryRule) -> dict[str, Any]:
    return rule.serialize()


def rule_from_json(payload: dict[str, Any]) -> CategoryRule:
    return CategoryRule.from_dict(payload)


def categorization_rules_json(view: CategorizationRules) -> dict[str, Any]:
    """The rules, their proposals, and the category choices, with account names."""
    return {
        "rules": [
            {
                "position": line.position,
                **rule_json(line.rule),
                "category_name": line.category_name,
            }
            for line in view.rules
        ],
        "proposals": [
            {
                "transaction": line.proposal.transaction,
                "date": to_json(line.proposal.when),
                "description": line.proposal.description,
                "amount": to_json(line.proposal.amount),
                "placeholder": line.proposal.placeholder,
                "category": line.proposal.category,
                "category_name": line.category_name,
                "rule_position": line.proposal.rule_position,
                "rule": rule_json(line.proposal.rule),
                "conflicts": [
                    {
                        "rule_position": conflict.rule_position,
                        "category": conflict.category,
                        "category_name": name,
                    }
                    for conflict, name in zip(
                        line.proposal.conflicts, line.conflict_names, strict=True
                    )
                ],
            }
            for line in view.proposals
        ],
        "categories": [{"handle": item.handle, "name": item.name} for item in view.categories],
    }


def categorization_rules_from_json(payload: dict[str, Any]) -> CategorizationRules:
    return CategorizationRules(
        tuple(
            RuleLine(int(row["position"]), rule_from_json(row), row["category_name"])
            for row in payload["rules"]
        ),
        tuple(
            ProposalLine(
                CategoryProposal(
                    row["transaction"],
                    date.fromisoformat(row["date"]),
                    row["description"],
                    Money(row["amount"]),
                    row["placeholder"],
                    row["category"],
                    int(row["rule_position"]),
                    rule_from_json(row["rule"]),
                    tuple(
                        CategoryConflict(int(item["rule_position"]), item["category"])
                        for item in row["conflicts"]
                    ),
                ),
                row["category_name"],
                tuple(item["category_name"] for item in row["conflicts"]),
            )
            for row in payload["proposals"]
        ),
        tuple(CategoryChoice(item["handle"], item["name"]) for item in payload["categories"]),
    )


def applied_categories_json(applied: AppliedCategories) -> dict[str, Any]:
    return {"assigned": applied.assigned, "unchanged": applied.unchanged}


def applied_categories_from_json(payload: dict[str, Any]) -> AppliedCategories:
    return AppliedCategories(int(payload["assigned"]), int(payload["unchanged"]))


def tag_counts_json(counts: list[TagCount]) -> dict[str, Any]:
    return {"tags": [{"tag": item.tag, "transactions": item.transactions} for item in counts]}


def tag_counts_from_json(payload: dict[str, Any]) -> list[TagCount]:
    return [TagCount(row["tag"], int(row["transactions"])) for row in payload["tags"]]


def _goal_progress_json(item: GoalProgress) -> dict[str, Any]:
    return {
        "goal": item.goal.serialize(),
        "as_of": to_json(item.as_of),
        "account_name": item.account_name,
        "target": to_json(item.target),
        "allocated": to_json(item.allocated),
        "from_income": to_json(item.from_income),
        "set_aside": to_json(item.set_aside),
        "remaining": to_json(item.remaining),
        "status": item.status,
        "basis": item.basis,
        "liquid_account": item.liquid_account,
        "account_balance": to_json(item.account_balance),
    }


def _goal_progress_from_json(row: dict[str, Any]) -> GoalProgress:
    return GoalProgress(
        SavingsGoal.from_dict(row["goal"]),
        date.fromisoformat(row["as_of"]),
        row["account_name"],
        Money(row["target"]),
        Money(row["allocated"]),
        Money(row["from_income"]),
        Money(row["set_aside"]),
        Money(row["remaining"]),
        row["status"],
        row["basis"],
        bool(row["liquid_account"]),
        Money(row["account_balance"]),
    )


def _choices_json(items: tuple[NamedChoice, ...]) -> list[dict[str, str]]:
    return [{"handle": item.handle, "name": item.name} for item in items]


def _choices_from_json(rows: list[dict[str, str]]) -> tuple[NamedChoice, ...]:
    return tuple(NamedChoice(row["handle"], row["name"]) for row in rows)


def savings_goals_json(view: SavingsGoalsView) -> dict[str, Any]:
    """Every goal's earmark with its scenario changes, and the forms' choices."""
    report = view.report
    return {
        "as_of": to_json(report.as_of),
        "set_aside": to_json(report.set_aside),
        "held": to_json(report.held),
        "goals": [_goal_progress_json(item) for item in report.goals],
        "overrides": {
            goal: [
                {
                    "scenario": line.scenario,
                    "scenario_name": line.scenario_name,
                    "override": line.override.serialize(),
                }
                for line in lines
            ]
            for goal, lines in view.overrides.items()
        },
        "accounts": _choices_json(view.accounts),
        "purchase_accounts": _choices_json(view.purchase_accounts),
        "scenarios": _choices_json(view.scenarios),
        "currency": view.currency,
    }


def savings_goals_from_json(payload: dict[str, Any]) -> SavingsGoalsView:
    return SavingsGoalsView(
        SavingsGoalReport(
            date.fromisoformat(payload["as_of"]),
            tuple(_goal_progress_from_json(row) for row in payload["goals"]),
            Money(payload["set_aside"]),
            Money(payload["held"]),
        ),
        {
            goal: tuple(
                GoalOverrideLine(
                    line["scenario"],
                    line["scenario_name"],
                    GoalOverride.from_dict(line["override"]),
                )
                for line in lines
            )
            for goal, lines in payload["overrides"].items()
        },
        _choices_from_json(payload["accounts"]),
        _choices_from_json(payload["purchase_accounts"]),
        _choices_from_json(payload["scenarios"]),
        payload["currency"],
    )


def reconciliation_summary_json(state: ReconciliationSummary) -> dict[str, Any]:
    """A statement session as stored, with its balances and every candidate entry."""
    return {
        "reconciliation": state.reconciliation.serialize(),
        "opening_balance": to_json(state.opening_balance),
        "selected_balance": to_json(state.selected_balance),
        "difference": to_json(state.difference),
        "balanced": state.balanced,
        "selected_in": to_json(state.selected_in),
        "selected_out": to_json(state.selected_out),
        "candidates": [
            {
                "transaction": item.transaction,
                "split": item.split,
                "date": to_json(item.post_date),
                "description": item.description,
                "amount": to_json(item.amount),
                "state": item.state.value,
                "selected": item.selected,
                "after_statement": item.after_statement,
                "funds_in": item.funds_in,
            }
            for item in state.candidates
        ],
    }


def reconciliation_summary_from_json(payload: dict[str, Any]) -> ReconciliationSummary:
    return ReconciliationSummary(
        Reconciliation.from_dict(payload["reconciliation"]),
        Money(payload["opening_balance"]),
        Money(payload["selected_balance"]),
        Money(payload["difference"]),
        tuple(
            ReconciliationCandidate(
                row["transaction"],
                row["split"],
                date.fromisoformat(row["date"]),
                row["description"],
                Money(row["amount"]),
                ReconcileState(row["state"]),
                bool(row["selected"]),
                after_statement=bool(row["after_statement"]),
                funds_in=bool(row["funds_in"]),
            )
            for row in payload["candidates"]
        ),
        selected_in=Money(payload["selected_in"]),
        selected_out=Money(payload["selected_out"]),
    )


def reconciliation_view_json(view: ReconciliationView) -> dict[str, Any]:
    return {
        "account": view.account,
        "account_name": view.account_name,
        "current": (
            reconciliation_summary_json(view.current) if view.current is not None else None
        ),
        "history": [item.serialize() for item in view.history],
        "reimbursement_proposals": view.reimbursement_proposals,
        "claim_link_proposals": view.claim_link_proposals,
    }


def reconciliation_view_from_json(payload: dict[str, Any]) -> ReconciliationView:
    current = payload["current"]
    return ReconciliationView(
        payload["account"],
        payload["account_name"],
        reconciliation_summary_from_json(current) if current is not None else None,
        tuple(Reconciliation.from_dict(item) for item in payload["history"]),
        int(payload["reimbursement_proposals"]),
        int(payload["claim_link_proposals"]),
    )
