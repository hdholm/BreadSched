"""Typed application-service contracts shared by every presentation adapter."""

from .contracts import ServiceError, ServiceResult
from .plan import (
    BASE_SCENARIO,
    PlanComparison,
    PlanQuery,
    PlanQueryResult,
    ScenarioChoice,
    query_plan,
)
from .schedules import (
    SavedScenarioSchedule,
    SavedSchedule,
    SaveScenarioSchedule,
    SaveSchedule,
    save_scenario_schedule,
    save_schedule,
)

__all__ = [
    "BASE_SCENARIO",
    "PlanComparison",
    "PlanQuery",
    "PlanQueryResult",
    "ScenarioChoice",
    "SaveSchedule",
    "SavedSchedule",
    "SaveScenarioSchedule",
    "SavedScenarioSchedule",
    "ServiceError",
    "ServiceResult",
    "query_plan",
    "save_schedule",
    "save_scenario_schedule",
]
