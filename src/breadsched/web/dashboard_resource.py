"""Dashboard response over the shared dashboard engine, and its group settings."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from ..gen.db.sqlite import DbSQLite
from ..gen.engine import dashboard as engine
from .savings_goal_resource import goal_json

if TYPE_CHECKING:
    from .context import Api


def dashboard_report(
    db: DbSQLite, liquidity_days: int | None, emergency_months: int | None
) -> dict:
    """Build and serialize Dashboard state for the web presentation."""
    config = engine.DashboardConfig.load(db)
    if liquidity_days:
        config.liquidity_days = liquidity_days
    if emergency_months:
        config.emergency_months = emergency_months
    board = engine.build(db, config)

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
        "config": {
            "liquidity_days": config.liquidity_days,
            "emergency_months": config.emergency_months,
            "groups": [group.serialize() for group in config.groups],
            "accounts": [
                {"handle": account.handle, "name": db.full_name(account)}
                for account in db.iter_accounts()
                if not account.is_root
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
    """Persist the same group paths and account selections edited by GTK."""
    allowed_kinds = {"liquid", "retirement", "asset", "property", "liability"}
    groups: list[engine.GroupConfig] = []
    raw_groups = payload.get("groups", [])
    if not isinstance(raw_groups, list):
        raise ValueError("dashboard groups must be a list")
    for raw in raw_groups:
        if not isinstance(raw, dict):
            raise ValueError("each dashboard group must be an object")
        name = str(raw.get("name", "")).strip()
        if not name:
            raise ValueError("dashboard group name cannot be empty")
        kind = str(raw.get("kind", "asset"))
        if kind not in allowed_kinds:
            raise ValueError("choose a valid dashboard group kind")
        handles: list[str] = []
        raw_handles = raw.get("accounts", [])
        if not isinstance(raw_handles, list):
            raise ValueError("dashboard group accounts must be a list")
        for raw_handle in raw_handles:
            handle = str(raw_handle)
            account = api.db.get_account(handle)
            if account is None or account.is_root:
                raise ValueError("dashboard group references an unknown account")
            if handle not in handles:
                handles.append(handle)
        groups.append(engine.GroupConfig(name, handles, kind))

    config = engine.DashboardConfig.load(api.db)
    config.groups = groups
    if "liquidity_days" in payload:
        config.liquidity_days = min(365, max(1, int(payload["liquidity_days"])))
    if "emergency_months" in payload:
        config.emergency_months = min(36, max(1, int(payload["emergency_months"])))
    config.save(api.db)
    return {
        "groups": [group.serialize() for group in config.groups],
        "liquidity_days": config.liquidity_days,
        "emergency_months": config.emergency_months,
    }
