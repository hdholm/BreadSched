"""HTTP input and output for savings goals.

This adapter only parses JSON into the book server's savings-goal requests and
translates its results. Validation, earmark arithmetic, and every write stay in
``gen/services/savings_goals``, behind the book server (``Api.book``), so a rejected
request never changes the book.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import TYPE_CHECKING, Any

from ..gen.engine.savings_goals import goals_chart
from ..gen.services.savings_goals import (
    AllocateToGoal,
    SaveSavingsGoal,
    SetGoalOverride,
)
from ..presentation import goal_override_text, goal_status_text
from .controls import input_money

if TYPE_CHECKING:
    from ..gen.engine.savings_goals import GoalProgress
    from .context import Api
    from .resources import QueryParams


def _text(payload: Mapping[str, Any], key: str, *, optional: bool = False) -> str | None:
    value = payload.get(key)
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be text")
    return value


def _date(payload: Mapping[str, Any], key: str, *, optional: bool = False) -> date | None:
    text = (_text(payload, key, optional=optional) or "").strip()
    if not text and optional:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{key} must be YYYY-MM-DD") from None


def goal_json(item: GoalProgress) -> dict[str, object]:
    """One goal's stored fields and its earmark on the report date."""
    goal = item.goal
    return {
        "handle": goal.handle,
        "name": goal.name,
        "description": goal.description,
        "account": goal.account,
        "account_name": item.account_name,
        "start_date": goal.start_date,
        "target_date": goal.target_date,
        "closed_on": goal.closed_on,
        "target": item.target,
        "allocated": item.allocated,
        "from_income": item.from_income,
        "set_aside": item.set_aside,
        "remaining": item.remaining,
        "status": item.status,
        "status_text": goal_status_text(item),
        "allocations": [
            {"date": entry.allocated_on, "amount": entry.amount, "memo": entry.memo}
            for entry in goal.allocations
        ],
    }


def savings_goals(api: Api, query: QueryParams) -> dict[str, object]:
    """Every goal's progress today, with the accounts a goal can be held in."""
    include_closed = query.text("closed") == "1"
    query.finish()
    view = api.book.savings_goals(date.today(), include_closed)
    report = view.report
    goals = []
    for item in report.goals:
        entry = goal_json(item)
        entry["overrides"] = [
            {
                "scenario": line.scenario,
                "scenario_name": line.scenario_name,
                "target_amount": line.override.target_amount,
                "target_date": line.override.target_date,
                "excluded": line.override.excluded,
                "purchase_on": line.override.purchase_on,
                "purchase_account": line.override.purchase_account,
                "text": goal_override_text(line.scenario_name, line.override),
            }
            for line in view.overrides.get(item.goal.handle, ())
        ]
        goals.append(entry)
    return {
        "chart": goals_chart(report.goals, view.currency).as_dict(),
        "as_of": report.as_of,
        "set_aside": report.set_aside,
        "held": report.held,
        "goals": goals,
        "accounts": [{"handle": item.handle, "name": item.name} for item in view.accounts],
        "purchase_accounts": [
            {"handle": item.handle, "name": item.name} for item in view.purchase_accounts
        ],
        "scenarios": [{"handle": item.handle, "name": item.name} for item in view.scenarios],
    }


def savings_goal_save(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    request = SaveSavingsGoal(
        name=_text(payload, "name") or "",
        account=_text(payload, "account") or "",
        target_amount=input_money(dict(payload), payload.get("target_amount")),
        target_date=_date(payload, "target_date") or date.today(),
        start_date=_date(payload, "start_date", optional=True) or date.today(),
        description=_text(payload, "description", optional=True) or "",
        handle=_text(payload, "handle", optional=True) or None,
    )
    goal = api.book.save_savings_goal(request)
    return {"handle": goal.handle, "name": goal.name}


def savings_goal_allocate(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    request = AllocateToGoal(
        goal=_text(payload, "handle") or "",
        amount=input_money(dict(payload), payload.get("amount")),
        allocated_on=_date(payload, "date", optional=True) or date.today(),
        memo=_text(payload, "memo", optional=True) or "",
    )
    goal = api.book.allocate_to_goal(request)
    return {"handle": goal.handle, "allocated": goal.allocated(date.max)}


def savings_goal_close(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    handle = _text(payload, "handle") or ""
    on = _date(payload, "date", optional=True) or date.today()
    goal = api.book.close_savings_goal(handle, on)
    return {"handle": goal.handle, "closed_on": goal.closed_on}


def savings_goal_reopen(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    goal = api.book.reopen_savings_goal(_text(payload, "handle") or "")
    return {"handle": goal.handle}


def savings_goal_delete(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    return {"deleted": api.book.delete_savings_goal(_text(payload, "handle") or "")}


def savings_goal_override(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    """Change a goal in one scenario; with no change given, the scenario follows it."""
    raw_target = payload.get("target_amount")
    excluded = payload.get("excluded", False)
    if not isinstance(excluded, bool):
        raise ValueError("excluded must be true or false")
    request = SetGoalOverride(
        scenario=_text(payload, "scenario") or "",
        goal=_text(payload, "handle") or "",
        target_amount=(
            input_money(dict(payload), raw_target) if raw_target not in (None, "") else None
        ),
        target_date=_date(payload, "target_date", optional=True),
        excluded=excluded,
        purchase_on=_date(payload, "purchase_on", optional=True),
        purchase_account=_text(payload, "purchase_account", optional=True) or None,
    )
    scenario = api.book.set_goal_override(request)
    override = scenario.goal_overrides.get(request.goal)
    return {
        "scenario": scenario.handle,
        "text": (
            goal_override_text(scenario.name, override)
            if override is not None
            else f"{scenario.name} follows the goal unchanged"
        ),
    }
