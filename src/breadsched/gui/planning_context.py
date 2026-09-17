"""Shared planning-scenario state across Plan and Projection views."""

from __future__ import annotations

from datetime import date

from ..gen.lib import Assumptions, Scenario
from ..gen.services import BASE_ASSUMPTIONS_KEY, SaveBaseAssumptions, save_base_assumptions

__all__ = [
    "baseline_scenario",
    "notify_planning_scenario_changed",
    "persist_baseline_assumptions",
    "selected_scenario_handle",
    "select_scenario",
]


def _db_identity(db) -> object | None:
    if db is None:
        return None
    return getattr(db, "path", None) or id(db)


def baseline_scenario(manager, db=None) -> Scenario:
    """Return the shared Base scenario, loading its assumptions from this book."""
    identity = _db_identity(db)
    scenario = getattr(manager, "_planning_baseline_scenario", None)
    loaded_for = getattr(manager, "_planning_baseline_db", object())
    if scenario is None or (db is not None and loaded_for != identity):
        scenario = Scenario(
            name="Base scenario",
            start=date.today().replace(month=1, day=1),
            years=10,
        )
        if db is not None:
            stored = db.get_metadata(BASE_ASSUMPTIONS_KEY, None)
            if isinstance(stored, dict):
                scenario.assumptions = Assumptions.from_dict(stored)
        manager._planning_baseline_scenario = scenario
        manager._planning_baseline_db = identity
    return scenario


def persist_baseline_assumptions(manager, db) -> None:
    """Persist the current Base assumptions in the open book's metadata."""
    scenario = baseline_scenario(manager, db)
    result = save_base_assumptions(db, SaveBaseAssumptions(scenario.assumptions))
    if not result.ok:  # pragma: no cover - GTK controls constrain these values
        raise ValueError(result.errors[0].code)


def selected_scenario_handle(manager) -> str | None:
    """Return the saved planning scenario selected for the main planning views."""
    return getattr(manager, "_planning_scenario_handle", None)


def select_scenario(manager, handle: str | None, *, source=None) -> None:
    """Select one planning scenario and notify already-created planning views."""
    if selected_scenario_handle(manager) == handle:
        return
    manager._planning_scenario_handle = handle
    for name in ("plan", "projection"):
        view = getattr(manager, "_views", {}).get(name)
        if view is None or view is source:
            continue
        callback = getattr(view, "planning_scenario_changed", None)
        if callback is not None:
            callback(handle)


def notify_planning_scenario_changed(manager, *, source=None) -> None:
    """Tell planning views that the currently selected scenario changed in place."""
    handle = selected_scenario_handle(manager)
    for name in ("plan", "projection"):
        view = getattr(manager, "_views", {}).get(name)
        if view is None or view is source:
            continue
        callback = getattr(view, "planning_scenario_changed", None)
        if callback is not None:
            callback(handle)
