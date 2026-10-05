"""HTTP input and output for savings goals.

This adapter only parses JSON into the shared savings-goal service's requests and
translates its results. Validation, earmark arithmetic, and every write stay in
``gen/services/savings_goals``, so a rejected request never changes the book.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import TYPE_CHECKING, Any

from ..gen.services.savings_goals import (
    AllocateToGoal,
    SaveSavingsGoal,
    SetGoalOverride,
    allocate_to_goal,
    close_savings_goal,
    delete_savings_goal,
    goal_accounts,
    goal_overrides,
    query_savings_goals,
    reopen_savings_goal,
    save_savings_goal,
    set_goal_override,
)
from ..presentation import goal_override_text, goal_status_text
from .controls import input_money, service_error

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


def _result(api: Api, result) -> Any:
    if result.value is None:
        raise service_error(result.errors[0])
    return result.value


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
    report = _result(api, query_savings_goals(api.db, date.today(), include_closed=include_closed))
    changes = goal_overrides(api.db)
    goals = []
    for item in report.goals:
        entry = goal_json(item)
        entry["overrides"] = [
            {
                "scenario": scenario.handle,
                "scenario_name": scenario.name,
                "target_amount": override.target_amount,
                "target_date": override.target_date,
                "excluded": override.excluded,
                "text": goal_override_text(scenario.name, override),
            }
            for scenario, override in changes.get(item.goal.handle, [])
        ]
        goals.append(entry)
    return {
        "as_of": report.as_of,
        "set_aside": report.set_aside,
        "held": report.held,
        "goals": goals,
        "accounts": [{"handle": handle, "name": name} for handle, name in goal_accounts(api.db)],
        "scenarios": [
            {"handle": scenario.handle, "name": scenario.name}
            for scenario in sorted(api.db.iter_scenarios(), key=lambda item: item.name.casefold())
        ],
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
    goal = _result(api, save_savings_goal(api.db, request))
    return {"handle": goal.handle, "name": goal.name}


def savings_goal_allocate(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    request = AllocateToGoal(
        goal=_text(payload, "handle") or "",
        amount=input_money(dict(payload), payload.get("amount")),
        allocated_on=_date(payload, "date", optional=True) or date.today(),
        memo=_text(payload, "memo", optional=True) or "",
    )
    goal = _result(api, allocate_to_goal(api.db, request))
    return {"handle": goal.handle, "allocated": goal.allocated(date.max)}


def savings_goal_close(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    handle = _text(payload, "handle") or ""
    on = _date(payload, "date", optional=True) or date.today()
    goal = _result(api, close_savings_goal(api.db, handle, on))
    return {"handle": goal.handle, "closed_on": goal.closed_on}


def savings_goal_reopen(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    goal = _result(api, reopen_savings_goal(api.db, _text(payload, "handle") or ""))
    return {"handle": goal.handle}


def savings_goal_delete(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    return {"deleted": _result(api, delete_savings_goal(api.db, _text(payload, "handle") or ""))}


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
    )
    scenario = _result(api, set_goal_override(api.db, request))
    override = scenario.goal_overrides.get(request.goal)
    return {
        "scenario": scenario.handle,
        "text": (
            goal_override_text(scenario.name, override)
            if override is not None
            else f"{scenario.name} follows the goal unchanged"
        ),
    }
