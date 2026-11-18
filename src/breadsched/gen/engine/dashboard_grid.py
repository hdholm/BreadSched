"""Where each Dashboard item sits, and how big it is.

The Dashboard is a grid :data:`COLUMNS` wide whose rows are all the same height.
Each item (a dashboard breadlet) is a :class:`Tile`: its key, a width in columns,
and a height in rows. The household arranges them by putting them in order and
sizing them, the way dashboard widgets are arranged on a router's status page; an
item larger than its tile scrolls inside it.

:func:`pack` places the tiles the way a CSS grid with ``grid-auto-flow: row dense``
does, so the desktop (which packs them itself) and the browser (whose CSS packs
them) draw one arrangement the same way: each tile, in order, takes the first
place from the top left where it fits, so a small tile fills a gap a larger one
left above it. A grid narrower than :data:`COLUMNS` (a narrow window) packs the
same tiles, each no wider than the grid.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = [
    "COLUMNS",
    "DEFAULT_SIZE",
    "MAX_ROWS",
    "ROW_HEIGHT",
    "Placed",
    "Tile",
    "arrange",
    "pack",
    "size_error",
]

#: Columns across the Dashboard at full width.
COLUMNS = 4
#: The most rows one item may be tall.
MAX_ROWS = 8
#: Pixels in one row of the grid, on the desktop and in the browser.
ROW_HEIGHT = 120
#: An item's size until the household changes it, when its breadlet names none.
DEFAULT_SIZE = (2, 2)


@dataclass(frozen=True, slots=True)
class Tile:
    """One Dashboard item's place in the order and its size."""

    key: str
    width: int
    height: int

    def serialize(self) -> dict[str, Any]:
        return {"key": self.key, "width": self.width, "height": self.height}

    @classmethod
    def from_dict(cls, data: object) -> Tile | None:
        """A stored tile, brought within the grid; None for one that is not a tile."""
        if not isinstance(data, Mapping) or not isinstance(data.get("key"), str):
            return None
        try:
            width, height = int(data.get("width", 0)), int(data.get("height", 0))
        except (TypeError, ValueError):
            return None
        return cls(data["key"], min(COLUMNS, max(1, width)), min(MAX_ROWS, max(1, height)))


@dataclass(frozen=True, slots=True)
class Placed:
    """A tile at its column and row (both from 0) in a packed grid."""

    tile: Tile
    column: int
    row: int
    #: Its width in this grid: never wider than the grid.
    width: int


def size_error(width: int, height: int) -> bool:
    """Whether a width and height fall outside the grid."""
    return not (1 <= width <= COLUMNS and 1 <= height <= MAX_ROWS)


def arrange(
    stored: Sequence[Tile], keys: Iterable[str], sizes: Mapping[str, tuple[int, int]]
) -> list[Tile]:
    """Every item of ``keys`` as a tile: those ``stored`` in their stored order and
    size, then the rest in the order given at their default ``sizes``."""
    keys = list(keys)
    known = set(keys)
    placed = [tile for tile in stored if tile.key in known]
    seen = {tile.key for tile in placed}
    for key in keys:
        if key not in seen:
            width, height = sizes.get(key, DEFAULT_SIZE)
            placed.append(Tile(key, width, height))
            seen.add(key)
    return placed


def pack(tiles: Iterable[Tile], columns: int = COLUMNS) -> list[Placed]:
    """Each tile at the first place, from the top left, where it fits (``dense``)."""
    columns = max(1, columns)
    taken: list[list[bool]] = []

    def free(row: int, column: int, width: int, height: int) -> bool:
        for r in range(row, row + height):
            if r < len(taken) and any(taken[r][column : column + width]):
                return False
        return True

    found = []
    for tile in tiles:
        width = min(tile.width, columns)
        row = 0
        while True:
            column = next(
                (c for c in range(columns - width + 1) if free(row, c, width, tile.height)),
                None,
            )
            if column is not None:
                break
            row += 1
        while len(taken) < row + tile.height:
            taken.append([False] * columns)
        for r in range(row, row + tile.height):
            for c in range(column, column + width):
                taken[r][c] = True
        found.append(Placed(tile, column, row, width))
    return found
