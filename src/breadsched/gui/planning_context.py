"""Shared planning-scenario state across Plan and Projection views."""

from __future__ import annotations

from datetime import date

from ..gen.lib import ProjectionBasis, Scenario

__all__ = ["baseline_scenario", "selected_scenario_handle", "select_scenario"]


def baseline_scenario(manager) -> Scenario:
    """Return the session's shared Baseline assumptions."""
    scenario = getattr(manager, "_planning_baseline_scenario", None)
    if scenario is None:
        scenario = Scenario(
            name="Baseline",
            start=date.today().replace(month=1, day=1),
            years=10,
            basis=ProjectionBasis.SCHEDULED,
        )
        manager._planning_baseline_scenario = scenario
    return scenario


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
