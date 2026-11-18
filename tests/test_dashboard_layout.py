"""Arranging the Dashboard: each item's place and size on its grid."""

from __future__ import annotations

import pytest

from breadsched.gen.engine import dashboard_grid
from breadsched.gen.engine.dashboard import DashboardConfig
from breadsched.gen.engine.dashboard_grid import Tile, arrange, pack
from breadsched.gen.services import (
    DashboardTileInput,
    SaveDashboardConfig,
    save_dashboard_config,
)
from breadsched.gen.services.dashboard_config import dashboard_settings


def _places(tiles, columns=dashboard_grid.COLUMNS):
    return [(item.tile.key, item.column, item.row, item.width) for item in pack(tiles, columns)]


class TestPacking:
    """The desktop packs tiles as the browser's CSS grid (``row dense``) does."""

    def test_each_tile_takes_the_first_place_where_it_fits(self):
        tiles = [
            Tile("summary", 4, 1),
            Tile("bills", 2, 3),
            Tile("small", 1, 1),
            Tile("wide", 3, 2),
            Tile("tall", 1, 2),
        ]
        assert _places(tiles) == [
            ("summary", 0, 0, 4),
            ("bills", 0, 1, 2),
            ("small", 2, 1, 1),
            # Three columns fit only below Pending bills...
            ("wide", 0, 4, 3),
            # ...and a later small tile fills the gap beside it (dense).
            ("tall", 3, 1, 1),
        ]

    def test_a_narrow_grid_stacks_each_tile_no_wider_than_itself(self):
        tiles = [Tile("summary", 4, 1), Tile("bills", 2, 3), Tile("goals", 2, 2)]
        assert _places(tiles, 1) == [
            ("summary", 0, 0, 1),
            ("bills", 0, 1, 1),
            ("goals", 0, 4, 1),
        ]
        assert _places(tiles, 2) == [
            ("summary", 0, 0, 2),
            ("bills", 0, 1, 2),
            ("goals", 0, 4, 2),
        ]

    def test_arranged_items_come_first_then_the_rest_at_their_own_size(self):
        stored = [Tile("goals", 1, 4), Tile("removed-breadlet", 2, 2)]
        tiles = arrange(stored, ["summary", "goals", "bills"], {"summary": (4, 1)})
        assert tiles == [Tile("goals", 1, 4), Tile("summary", 4, 1), Tile("bills", 2, 2)]

    @pytest.mark.parametrize(
        ("stored", "expected"),
        [
            ({"key": "bills", "width": 9, "height": 0}, Tile("bills", 4, 1)),
            ({"key": "bills", "width": "2", "height": 3}, Tile("bills", 2, 3)),
            ({"key": "bills", "width": "wide"}, None),
            ({"width": 2, "height": 2}, None),
            ("bills", None),
        ],
    )
    def test_a_stored_tile_is_brought_within_the_grid(self, stored, expected):
        assert Tile.from_dict(stored) == expected


class TestStoredLayout:
    def test_a_book_saved_before_layouts_keeps_every_item_in_its_own_place(self, db, book):
        settings = dashboard_settings(db)
        order = [item.key for item in settings.breadlets]
        assert order[:8] == [
            "summary",
            "cash_flow",
            "groups",
            "bills",
            "income",
            "reimbursables",
            "claims",
            "goals",
        ]
        sizes = {item.key: (item.width, item.height) for item in settings.breadlets}
        assert sizes["summary"] == (4, 2)
        assert sizes["cash_flow"] == (4, 2)
        assert sizes["bills"] == (2, 3)
        assert sizes["breadlet:spending-this-month"] == (2, 3)
        assert (settings.columns, settings.max_rows, settings.row_height) == (4, 8, 120)
        assert DashboardConfig.from_dict({"groups": []}).layout == []

    def test_a_layout_orders_and_sizes_the_items_and_survives_reloading(self, db, book):
        result = save_dashboard_config(
            db,
            SaveDashboardConfig(
                layout=(
                    DashboardTileInput("goals", 1, 4),
                    DashboardTileInput("breadlet:spending-this-month", 3, 2),
                    DashboardTileInput("summary", 4, 1),
                )
            ),
        )
        assert result.ok, result.errors
        stored = db.get_metadata("dashboard")
        assert stored["layout"][0] == {"key": "goals", "width": 1, "height": 4}
        settings = dashboard_settings(db)
        assert [(item.key, item.width, item.height) for item in settings.breadlets[:4]] == [
            ("goals", 1, 4),
            ("breadlet:spending-this-month", 3, 2),
            ("summary", 4, 1),
            # The rest follow in their own order and size.
            ("cash_flow", 4, 2),
        ]
        # Showing and hiding leaves the layout alone.
        assert save_dashboard_config(db, SaveDashboardConfig(sections={"goals": False})).ok
        assert dashboard_settings(db).breadlets[0].key == "goals"
        # An empty layout puts every item back.
        assert save_dashboard_config(db, SaveDashboardConfig(layout=())).ok
        assert dashboard_settings(db).breadlets[0].key == "summary"

    @pytest.mark.parametrize(
        ("layout", "codes"),
        [
            ((DashboardTileInput("cards", 2, 2),), ["dashboard.layout.key.invalid"]),
            ((DashboardTileInput("breadlet:missing", 2, 2),), ["dashboard.layout.key.invalid"]),
            (
                (DashboardTileInput("bills", 2, 2), DashboardTileInput("bills", 1, 1)),
                ["dashboard.layout.key.duplicate"],
            ),
            ((DashboardTileInput("bills", 5, 2),), ["dashboard.layout.size.invalid"]),
            ((DashboardTileInput("bills", 2, 0),), ["dashboard.layout.size.invalid"]),
            ((DashboardTileInput("bills", 2, 9),), ["dashboard.layout.size.invalid"]),
        ],
    )
    def test_a_rejected_layout_stores_nothing(self, db, book, layout, codes):
        assert save_dashboard_config(
            db, SaveDashboardConfig(layout=(DashboardTileInput("goals", 1, 1),))
        ).ok
        before = db.get_metadata("dashboard")
        result = save_dashboard_config(
            db, SaveDashboardConfig(layout=layout, liquidity_days=60, sections={"bills": False})
        )
        assert [error.code for error in result.errors] == codes
        assert db.get_metadata("dashboard") == before

    def test_a_person_arranges_their_own_dashboard_apart_from_the_book(self, db, book):
        kept: dict = {}
        result = save_dashboard_config(
            db,
            SaveDashboardConfig(layout=(DashboardTileInput("bills", 4, 2),)),
            None,
            kept.update,
        )
        assert result.ok
        assert kept["layout"] == [{"key": "bills", "width": 4, "height": 2}]
        assert dashboard_settings(db, kept).breadlets[0].key == "bills"
        # The book's own arrangement is unchanged.
        assert dashboard_settings(db).breadlets[0].key == "summary"
