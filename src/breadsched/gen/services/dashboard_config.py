"""Validated changes to the Dashboard's groups and horizons (#337).

A group's members are stored once, as each member account's ``Account.group``;
the book's dashboard configuration keeps the group names, kinds, and order and the
two horizons. Both editors (desktop and browser) save through here, so an account
is never put in two groups and a rejected change leaves the book as it was.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..db.sqlite import DbSQLite
from ..engine.dashboard import GROUP_KINDS, Dashboard, DashboardConfig, GroupConfig, build
from ..lib.money import Money
from .contracts import ServiceError, ServiceResult

__all__ = [
    "DashboardGroup",
    "DashboardGroupInput",
    "DashboardSettings",
    "EMERGENCY_MONTHS_RANGE",
    "LIQUIDITY_DAYS_RANGE",
    "SaveDashboardConfig",
    "dashboard_board",
    "dashboard_settings",
    "save_dashboard_config",
    "settings_of",
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
    if request.liquidity_days is not None:
        config.liquidity_days = _clamp(request.liquidity_days, LIQUIDITY_DAYS_RANGE)
    if request.emergency_months is not None:
        config.emergency_months = _clamp(request.emergency_months, EMERGENCY_MONTHS_RANGE)
    config.save(db)
    return ServiceResult.success(config)


@dataclass(frozen=True, slots=True)
class DashboardGroup:
    """One group with its member accounts, which the stored form keeps on the accounts."""

    name: str
    kind: str
    accounts: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DashboardSettings:
    """The Dashboard's groups, with their members, and its horizons."""

    groups: tuple[DashboardGroup, ...]
    liquidity_days: int
    emergency_months: int
    reserve: Money
    minimum_bill: Money


def settings_of(config: DashboardConfig) -> DashboardSettings:
    return DashboardSettings(
        tuple(
            DashboardGroup(group.name, group.kind, tuple(group.accounts)) for group in config.groups
        ),
        config.liquidity_days,
        config.emergency_months,
        config.reserve,
        config.minimum_bill,
    )


def dashboard_settings(db: DbSQLite) -> DashboardSettings:
    return settings_of(DashboardConfig.load(db))


def dashboard_board(
    db: DbSQLite,
    as_of: date | None = None,
    liquidity_days: int | None = None,
    emergency_months: int | None = None,
) -> ServiceResult[Dashboard]:
    """The Dashboard on ``as_of`` (today when none), computed once.

    ``liquidity_days`` and ``emergency_months`` look at other horizons without
    saving them; ``None`` or 0 keeps the book's own, and a negative one is refused.
    """
    errors = []
    if liquidity_days is not None and liquidity_days < 0:
        errors.append(ServiceError("dashboard.liquidity_days.invalid", ("liquidity_days",)))
    if emergency_months is not None and emergency_months < 0:
        errors.append(ServiceError("dashboard.emergency_months.invalid", ("emergency_months",)))
    if errors:
        return ServiceResult.failure(*errors)
    config = DashboardConfig.load(db)
    if liquidity_days:
        config.liquidity_days = liquidity_days
    if emergency_months:
        config.emergency_months = emergency_months
    return ServiceResult.success(build(db, config, as_of=as_of))
