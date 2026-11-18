"""Validated changes to the Dashboard's groups, horizons, and sections (#337, #381).

A group's members are stored once, as each member account's ``Account.group``;
the book's dashboard configuration keeps the group names, kinds, and order and the
two horizons. Both editors (desktop and browser) save through here, so an account
is never put in two groups and a rejected change leaves the book as it was.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ..db.sqlite import DbSQLite
from ..engine import dashboard_grid
from ..engine.dashboard import (
    BREADLET_PREFIX,
    GROUP_KINDS,
    SECTIONS,
    Dashboard,
    DashboardConfig,
    GroupConfig,
    build,
    default_config,
)
from ..lib.money import Money
from .contracts import ServiceError, ServiceResult

__all__ = [
    "DashboardBreadlet",
    "breadlet_key",
    "DashboardGroup",
    "DashboardGroupInput",
    "DashboardSettings",
    "DashboardTileInput",
    "EMERGENCY_MONTHS_RANGE",
    "LIQUIDITY_DAYS_RANGE",
    "SaveDashboardConfig",
    "dashboard_board",
    "dashboard_settings",
    "default_layout",
    "default_dashboard_settings",
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
class DashboardTileInput:
    """One Dashboard item's place and size: its key (``DashboardBreadlet.key``), its
    width in columns, and its height in rows."""

    key: str
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class SaveDashboardConfig:
    #: The complete groups, in order; None keeps the stored ones.
    groups: tuple[DashboardGroupInput, ...] | None = None
    liquidity_days: int | None = None
    emergency_months: int | None = None
    #: Sections to show or hide by ``SECTIONS`` name; one not named is unchanged.
    sections: Mapping[str, bool] | None = None
    #: The items' order and sizes; one not listed follows the listed ones at its own
    #: size. An empty layout puts every item back in its own place and size; None
    #: keeps the stored layout.
    layout: tuple[DashboardTileInput, ...] | None = None


def _clamp(value: int, bounds: tuple[int, int]) -> int:
    low, high = bounds
    return min(high, max(low, value))


def save_dashboard_config(
    db: DbSQLite,
    request: SaveDashboardConfig,
    stored: object = None,
    keep: Callable[[dict[str, Any]], None] | None = None,
) -> ServiceResult[DashboardConfig]:
    """Validate and store the Dashboard configuration and its accounts' groups: in the
    book, or with ``keep`` as a person's own arrangement, changing
    their ``stored`` one (else starting from the book's)."""
    config = DashboardConfig.load(db, stored)
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
        if any(not _known_section(name) for name in request.sections):
            return ServiceResult.failure(ServiceError("dashboard.section.invalid", ("sections",)))
        config.sections = {**config.sections, **{k: bool(v) for k, v in request.sections.items()}}
    if request.layout is not None:
        problems: list[ServiceError] = []
        keys: set[str] = set()
        for tile in request.layout:
            if not _known_section(tile.key):
                problems.append(ServiceError("dashboard.layout.key.invalid", ("layout",)))
            elif tile.key in keys:
                problems.append(ServiceError("dashboard.layout.key.duplicate", ("layout",)))
            if dashboard_grid.size_error(tile.width, tile.height):
                problems.append(ServiceError("dashboard.layout.size.invalid", ("layout",)))
            keys.add(tile.key)
        if problems:
            return ServiceResult.failure(*problems)
        config.layout = [
            dashboard_grid.Tile(tile.key, tile.width, tile.height) for tile in request.layout
        ]
    if request.liquidity_days is not None:
        config.liquidity_days = _clamp(request.liquidity_days, LIQUIDITY_DAYS_RANGE)
    if request.emergency_months is not None:
        config.emergency_months = _clamp(request.emergency_months, EMERGENCY_MONTHS_RANGE)
    if keep is None:
        config.save(db)
    else:
        keep(config.serialize_personal())
    return ServiceResult.success(config)


def _dashboard_breadlets(config: DashboardConfig):
    """Every dashboard breadlet with its tile, in the order the Dashboard shows them:
    as the household arranged them, then the rest in their own order (the Dashboard's
    own sections as ``SECTIONS`` lists them, then the others by name)."""
    from ..plug import DASHBOARD, PluginManager

    order = list(SECTIONS)
    plugins = sorted(
        PluginManager.instance().by_category(DASHBOARD),
        key=lambda plugin: (order.index(plugin.id) if plugin.id in SECTIONS else len(order),),
    )
    by_key = {breadlet_key(plugin.id): plugin for plugin in plugins}
    tiles = dashboard_grid.arrange(
        config.layout,
        by_key,
        {key: plugin.size or dashboard_grid.DEFAULT_SIZE for key, plugin in by_key.items()},
    )
    return [(by_key[tile.key], tile) for tile in tiles]


def default_layout() -> tuple[DashboardTileInput, ...]:
    """Every Dashboard item in its own place and size, as a book never arranged
    shows them: what putting the layout back offers before it is saved."""
    return tuple(
        DashboardTileInput(tile.key, tile.width, tile.height)
        for _plugin, tile in _dashboard_breadlets(DashboardConfig())
    )


def breadlet_key(breadlet_id: str) -> str:
    """Where a dashboard breadlet's choice is kept: a section's own name, or the
    breadlet prefix and its id."""
    return breadlet_id if breadlet_id in SECTIONS else f"{BREADLET_PREFIX}{breadlet_id}"


def _known_section(name: str) -> bool:
    if name in SECTIONS:
        return True
    from ..plug import DASHBOARD, PluginManager

    return name.startswith(BREADLET_PREFIX) and (
        PluginManager.instance().get(DASHBOARD, name[len(BREADLET_PREFIX) :]) is not None
    )


@dataclass(frozen=True, slots=True)
class DashboardBreadlet:
    """A dashboard breadlet the Dashboard can show, and whether it does."""

    id: str
    name: str
    description: str
    shown: bool
    #: Its width in columns and height in rows on the Dashboard's grid.
    width: int = dashboard_grid.DEFAULT_SIZE[0]
    height: int = dashboard_grid.DEFAULT_SIZE[1]

    @property
    def key(self) -> str:
        """Its name among the Dashboard's sections, for ``SaveDashboardConfig``."""
        return breadlet_key(self.id)


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
    #: Which of ``SECTIONS`` the Dashboard shows (#381).
    sections: dict[str, bool] = field(default_factory=dict)
    #: Every dashboard breadlet with its size, in the order the Dashboard shows them.
    breadlets: tuple[DashboardBreadlet, ...] = ()
    #: The grid the breadlets are arranged on: its columns, its tallest item in rows,
    #: and a row's height in pixels (``engine.dashboard_grid``).
    columns: int = dashboard_grid.COLUMNS
    max_rows: int = dashboard_grid.MAX_ROWS
    row_height: int = dashboard_grid.ROW_HEIGHT


def settings_of(config: DashboardConfig) -> DashboardSettings:
    return DashboardSettings(
        tuple(
            DashboardGroup(group.name, group.kind, tuple(group.accounts)) for group in config.groups
        ),
        config.liquidity_days,
        config.emergency_months,
        config.reserve,
        config.minimum_bill,
        {name: config.shows(name) for name in SECTIONS},
        tuple(
            DashboardBreadlet(
                plugin.id,
                plugin.name,
                plugin.description,
                config.shows(tile.key, plugin.shown_by_default),
                tile.width,
                tile.height,
            )
            for plugin, tile in _dashboard_breadlets(config)
        ),
    )


def dashboard_settings(db: DbSQLite, stored: object = None) -> DashboardSettings:
    """The Dashboard's settings: a person's own ``stored`` ones, else the book's."""
    return settings_of(DashboardConfig.load(db, stored))


def default_dashboard_settings(db: DbSQLite) -> DashboardSettings:
    """The groups and horizons a new book's Dashboard starts with, saving nothing."""
    return settings_of(default_config(db))


def dashboard_board(
    db: DbSQLite,
    as_of: date | None = None,
    liquidity_days: int | None = None,
    emergency_months: int | None = None,
    stored: object = None,
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
    config = DashboardConfig.load(db, stored)
    if liquidity_days:
        config.liquidity_days = liquidity_days
    if emergency_months:
        config.emergency_months = emergency_months
    return ServiceResult.success(build(db, config, as_of=as_of))
