"""Read-only Dashboard response over the shared dashboard engine."""

from __future__ import annotations

from collections.abc import Mapping

from ..gen.db.sqlite import DbSQLite
from ..gen.engine import dashboard as engine


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
        "summary": _plain(board.summary()),
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
                "kind": group.kind,
                "total": str(group.total.to_decimal()),
                "value": (str(group.value.to_decimal()) if group.value is not None else None),
                "debt": str(group.debt.to_decimal()) if group.debt is not None else None,
                "equity": (str(group.equity.to_decimal()) if group.equity is not None else None),
                "loan_to_value": (
                    float(group.loan_to_value) if group.loan_to_value is not None else None
                ),
                "loan_end": group.loan_end.isoformat() if group.loan_end is not None else None,
                "accounts": [
                    {
                        "name": account.name,
                        "balance": (
                            str(account.total.to_decimal()) if account.total is not None else None
                        ),
                        "source": account.source,
                        "note": account.note,
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
                "cycle_months": float(item.cycle_months),
                "amount": str(item.amount.to_decimal()),
                "monthly": str(item.monthly.to_decimal()),
                "annual": str(item.annual.to_decimal()),
                "schedule": item.schedule.handle if item.schedule is not None else None,
            }
            for item in board.incomes
        ],
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
