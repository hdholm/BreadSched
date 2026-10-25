"""Validated changes to the Dashboard's groups, horizons, and sections (#337, #381).

A group's members are stored once, as each member account's ``Account.group``;
the book's dashboard configuration keeps the group names, kinds, and order and the
two horizons. Both editors (desktop and browser) save through here, so an account
is never put in two groups and a rejected change leaves the book as it was.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ..db.sqlite import DbSQLite
from ..engine.dashboard import GROUP_KINDS, SECTIONS, DashboardConfig, GroupConfig
from .contracts import ServiceError, ServiceResult

__all__ = [
    "DashboardGroupInput",
    "EMERGENCY_MONTHS_RANGE",
    "LIQUIDITY_DAYS_RANGE",
    "SaveDashboardConfig",
    "save_dashboard_config",
]

#: The horizons' bounds; a value outside is brought within them.
LIQUIDITY_DAYS_RANGE = (1, 365)
EMERGENCY_MONTHS_RANGE = (1, 36)


@dataclass(frozen=True, slots=True)
class DashboardGroupInput:
    name: str
    kind: str = "asset"
    accounts: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SaveDashboardConfig:
    #: The complete groups, in order; None keeps the stored ones.
    groups: tuple[DashboardGroupInput, ...] | None = None
    liquidity_days: int | None = None
    emergency_months: int | None = None
    #: Sections to show or hide by ``SECTIONS`` name; one not named is unchanged.
    sections: Mapping[str, bool] | None = None


def _clamp(value: int, bounds: tuple[int, int]) -> int:
    low, high = bounds
    return min(high, max(low, value))


def save_dashboard_config(
    db: DbSQLite, request: SaveDashboardConfig
) -> ServiceResult[DashboardConfig]:
    """Validate and store the Dashboard configuration and its accounts' groups."""
    config = DashboardConfig.load(db)
    if request.groups is not None:
        errors: list[ServiceError] = []
        groups: list[GroupConfig] = []
        names: set[str] = set()
        owner: dict[str, str] = {}
        for item in request.groups:
            name = item.name.strip()
            if not name:
                errors.append(ServiceError("dashboard.group.name.empty", ("groups",)))
                continue
            if name in names:
                errors.append(ServiceError("dashboard.group.name.duplicate", ("groups",)))
            names.add(name)
            if item.kind not in GROUP_KINDS:
                errors.append(ServiceError("dashboard.group.kind.invalid", ("groups",)))
            members: list[str] = []
            for handle in item.accounts:
                account = db.get_account(handle)
                if account is None or account.is_root:
                    errors.append(ServiceError("dashboard.group.account.not_found", ("groups",)))
                    continue
                if handle in members:
                    continue  # listed twice in one group: once is meant
                if owner.setdefault(handle, name) != name:
                    errors.append(ServiceError("dashboard.group.account.duplicate", ("groups",)))
                    continue
                members.append(handle)
            groups.append(GroupConfig(name, members, item.kind))
        if errors:
            return ServiceResult.failure(*errors)
        config.groups = groups
    if request.sections is not None:
        if any(name not in SECTIONS for name in request.sections):
            return ServiceResult.failure(ServiceError("dashboard.section.invalid", ("sections",)))
        config.sections = {**config.sections, **{k: bool(v) for k, v in request.sections.items()}}
    if request.liquidity_days is not None:
        config.liquidity_days = _clamp(request.liquidity_days, LIQUIDITY_DAYS_RANGE)
    if request.emergency_months is not None:
        config.emergency_months = _clamp(request.emergency_months, EMERGENCY_MONTHS_RANGE)
    config.save(db)
    return ServiceResult.success(config)
