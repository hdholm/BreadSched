"""Dashboard response over the shared dashboard engine, and its group settings."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from ..gen.engine import dashboard as engine
from ..gen.services import DashboardGroupInput, SaveDashboardConfig
from ..gen.services.dashboard_config import DashboardGroup
from .savings_goal_resource import goal_json

if TYPE_CHECKING:
    from ..api.protocol import BookApi
    from .context import Api


def dashboard_report(
    book: BookApi, liquidity_days: int | None, emergency_months: int | None
) -> dict:
    """Serialize the book server's Dashboard for the web presentation."""
    board = book.dashboard(None, liquidity_days, emergency_months)
    config = board.config

    return {
        "summary": _plain(board.report_summary()),
        "unavailable_reasons": {
            key: board.unavailable_reason(key)
            for key, value in board.report_summary().items()
            if value is None
        },
        "missing_quotes": list(board.missing_quotes),
        "liquid_missing_quotes": list(board.liquid_missing_quotes),
        # Withheld totals with each excluded balance and how to include it (#236).
        "completeness": board.completeness.as_dict(),
        "liquid_completeness": board.liquid_completeness.as_dict(),
        "coverage_notes": list(board.coverage_notes),
        # Allocation detail lives on the goals page; the Dashboard shows progress.
        "goals": [
            _plain({key: value for key, value in goal_json(item).items() if key != "allocations"})
            for item in board.goals
        ],
        # Each reimbursable expense still owed, soonest expected first (#381).
        "reimbursables": [_reimbursable_row(item, board) for item in board.reimbursables],
        "config": {
            "liquidity_days": config.liquidity_days,
            "emergency_months": config.emergency_months,
            "sections": {name: config.shows(name) for name in engine.SECTIONS},
            "section_titles": dict(engine.SECTIONS),
            "groups": [group_json(group) for group in config.groups],
            "accounts": [
                {"handle": record.account.handle, "name": record.full_name}
                for record in book.accounts()
                if not record.account.is_root
            ],
        },
        "groups": [
            {
                "name": group.name,
                "path": group.path,
                "depth": group.depth,
                "heading": group.heading,
                "note": group.note,
                "members": list(group.members),
                "kind": group.kind,
                "total": (
                    str(group.report_total.to_decimal()) if group.report_total is not None else None
                ),
                "value": (
                    str(group.report_value.to_decimal()) if group.report_value is not None else None
                ),
                "debt": (
                    str(group.report_debt.to_decimal()) if group.report_debt is not None else None
                ),
                "equity": (
                    str(group.report_equity.to_decimal())
                    if group.report_equity is not None
                    else None
                ),
                "loan_to_value": (
                    float(group.report_loan_to_value)
                    if group.report_loan_to_value is not None
                    else None
                ),
                "missing_quotes": list(group.missing_quotes),
                "completeness": group.completeness.as_dict(),
                "loan_end": group.loan_end.isoformat() if group.loan_end is not None else None,
                "accounts": [
                    {
                        "name": account.name,
                        "balance": (
                            str(account.total.to_decimal()) if account.total is not None else None
                        ),
                        "source": account.source,
                        "note": account.note,
                        "members": list(account.members),
                    }
                    for account in group.accounts
                ],
            }
            for group in board.groups
        ],
        "bills": [
            {
                "name": item.name,
                "next_due": item.next_due.isoformat(),
                "days_until": item.days_until(board.as_of),
                "frequency": item.frequency,
                "cycle_months": float(item.cycle_months),
                "amount": str(item.amount.to_decimal()),
                "monthly": None if item.generated else str(item.monthly.to_decimal()),
                "annual": None if item.generated else str(item.annual.to_decimal()),
                "hold": None if item.income else str(item.held.to_decimal()),
                "reserve_for": item.reserve_for.isoformat() if item.reserve_for else None,
                "estimate": item.estimate,
                "generated": item.generated,
                "schedule": item.schedule.handle if item.schedule is not None else None,
                "account": item.account,
            }
            for item in board.bills
        ],
        "income": [
            {
                "name": item.name,
                "next_due": item.next_due.isoformat(),
                "days_until": item.days_until(board.as_of),
                "frequency": item.frequency,
                "cycle_months": float(item.cycle_months),
                "amount": str(item.amount.to_decimal()),
                "monthly": str(item.monthly.to_decimal()),
                "annual": str(item.annual.to_decimal()),
                "schedule": item.schedule.handle if item.schedule is not None else None,
            }
            for item in board.incomes
        ],
        "display_bills": [_display_row(item, board) for item in board.display_bills],
        "display_income": [_display_row(item, board) for item in board.display_incomes],
    }


def _display_row(item: engine.BillRow | engine.MissedGroup, board: engine.Dashboard) -> dict:
    """One presented row: a pending occurrence or a schedule's grouped missed dates."""
    if isinstance(item, engine.MissedGroup):
        last_due, missed, occurrences, account = item.last_due, item.count, item.occurrences, None
    else:
        last_due, missed, account = item.next_due, 0, item.account
        occurrences = ((item.next_due, item.amount),)
    return {
        "name": item.name,
        "next_due": item.next_due.isoformat(),
        "last_due": last_due.isoformat(),
        "days_until": item.days_until(board.as_of),
        "missed": missed,
        "occurrences": [
            {"date": when.isoformat(), "amount": str(amount.to_decimal())}
            for when, amount in occurrences
        ],
        "frequency": item.frequency,
        "amount": str(item.amount.to_decimal()),
        "monthly": None if item.generated else str(item.monthly.to_decimal()),
        "annual": None if item.generated else str(item.annual.to_decimal()),
        "hold": None if item.income else str(item.held.to_decimal()),
        "estimate": item.estimate,
        "generated": item.generated,
        "schedule": item.schedule.handle if item.schedule is not None else None,
        "account": account,
    }


def _reimbursable_row(item, board: engine.Dashboard) -> dict:
    receivable = item.receivable
    expected = receivable.expected_cash_date
    return {
        "handle": receivable.handle,
        "description": receivable.description,
        "payer": receivable.payer,
        "incurred": receivable.incurred_date.isoformat(),
        "expected": expected.isoformat() if expected is not None else None,
        "overdue": expected is not None and expected < board.as_of,
        "attention": engine.reimbursable_overdue(item, board.as_of),
        "remaining": str(item.remaining.to_decimal()),
        "status": item.status.value,
        "status_label": item.status.label,
    }


def _plain(values: Mapping[str, object]) -> dict[str, str | None]:
    """Convert Money and date values to strings the browser can read."""
    out: dict[str, str | None] = {}
    for key, value in values.items():
        if hasattr(value, "to_decimal"):
            out[key] = str(value.to_decimal())
        elif hasattr(value, "isoformat"):
            out[key] = value.isoformat()
        else:
            out[key] = str(value) if value is not None else None
    return out


def dashboard_config_save(api: Api, payload: dict) -> dict:
    """Save the same settings Configure Dashboard edits in GTK (#381).

    Parsing stays here; validation and the write (each account's group plus the
    stored names, kinds, horizons, and sections) are ``save_dashboard_config``'s
    (#337). A key left out keeps what is stored.
    """
    sections = payload.get("sections")
    if sections is not None and (
        not isinstance(sections, dict) or not all(isinstance(v, bool) for v in sections.values())
    ):
        raise ValueError("dashboard sections must map each section to true or false")
    groups: list[DashboardGroupInput] | None = None if "groups" not in payload else []
    raw_groups = payload.get("groups", [])
    if not isinstance(raw_groups, list):
        raise ValueError("dashboard groups must be a list")
    for raw in raw_groups:
        if not isinstance(raw, dict):
            raise ValueError("each dashboard group must be an object")
        raw_handles = raw.get("accounts", [])
        if not isinstance(raw_handles, list):
            raise ValueError("dashboard group accounts must be a list")
        assert groups is not None
        groups.append(
            DashboardGroupInput(
                name=str(raw.get("name", "")),
                kind=str(raw.get("kind", "asset")),
                accounts=tuple(str(handle) for handle in raw_handles),
            )
        )
    config = api.book.save_dashboard_settings(
        SaveDashboardConfig(
            groups=tuple(groups) if groups is not None else None,
            liquidity_days=int(payload["liquidity_days"]) if "liquidity_days" in payload else None,
            emergency_months=(
                int(payload["emergency_months"]) if "emergency_months" in payload else None
            ),
            sections={str(k): v for k, v in sections.items()} if sections is not None else None,
        ),
    )
    return {
        "groups": [group_json(group) for group in config.groups],
        "liquidity_days": config.liquidity_days,
        "emergency_months": config.emergency_months,
        "sections": dict(config.sections),
    }


def group_json(group: engine.GroupConfig | DashboardGroup) -> dict:
    """A configured group with its member accounts, as both editors show it."""
    return {"name": group.name, "kind": group.kind, "accounts": list(group.accounts)}
