"""Shared planning-scenario state across Plan and Projection views."""

from __future__ import annotations

from datetime import date

from ..api.local import book_server
from ..gen.lib import Scenario
from ..gen.services import SaveBaseAssumptions

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
            scenario.assumptions = book_server(db).base_assumptions()
        manager._planning_baseline_scenario = scenario
        manager._planning_baseline_db = identity
    return scenario


def persist_baseline_assumptions(manager, db) -> None:
    """Persist the current Base assumptions in the open book's metadata."""
    scenario = baseline_scenario(manager, db)
    book_server(db).save_base_assumptions(SaveBaseAssumptions(scenario.assumptions))


def selected_scenario_handle(manager) -> str | None:
    """Return the saved planning scenario selected for the main planning views."""
    return getattr(manager, "_planning_scenario_handle", None)


def select_scenario(manager, handle: str | None, *, source=None) -> None:
    """Select one planning scenario and notify already-created planning views."""
    if selected_scenario_handle(manager) == handle:
        return
    manager._planning_scenario_handle = handle
    _notify(manager, handle, source)


def _planning_views(manager) -> list:
    """Plan, Projection, and every scenario tab's Projection."""
    listing = getattr(manager, "planning_views", None)
    if listing is not None:
        return list(listing())
    views = getattr(manager, "_views", {})
    return [views[name] for name in ("plan", "projection") if name in views]


def _notify(manager, handle: str | None, source) -> None:
    for view in _planning_views(manager):
        if view is source:
            continue
        callback = getattr(view, "planning_scenario_changed", None)
        if callback is not None:
            callback(handle)


def notify_planning_scenario_changed(manager, *, source=None) -> None:
    """Tell planning views that the currently selected scenario changed in place."""
    _notify(manager, selected_scenario_handle(manager), source)
