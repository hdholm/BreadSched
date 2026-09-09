"""Pure computation over a database: no storage, no UI."""

from . import (
    activity,
    budgeting,
    cashflow,
    dashboard,
    inference,
    ledger,
    loans,
    planning,
    projection,
    schedule,
)

__all__ = [
    "activity", "budgeting", "cashflow", "dashboard", "inference", "ledger", "loans",
    "planning", "projection", "schedule",
]
