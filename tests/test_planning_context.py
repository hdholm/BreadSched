from decimal import Decimal
from types import SimpleNamespace

import pytest

from breadsched.gen.db.sqlite import DbSQLite
from breadsched.gui.planning_context import (
    baseline_scenario,
    persist_baseline_assumptions,
)


@pytest.fixture
def books(tmp_path):
    """Open books by name, closed after the test."""
    opened: list[DbSQLite] = []

    def open_book(name: str) -> DbSQLite:
        db = DbSQLite()
        db.load(str(tmp_path / name))
        opened.append(db)
        return db

    yield open_book
    for db in opened:
        db.close()


def test_base_assumptions_round_trip_through_book_metadata(books) -> None:
    db = books("household.breadsched")
    manager = SimpleNamespace()
    base = baseline_scenario(manager, db)
    base.assumptions.investment_return = Decimal("0.0475")
    base.assumptions.expense_inflation = Decimal("0.031")

    persist_baseline_assumptions(manager, db)

    reopened = baseline_scenario(SimpleNamespace(), db)
    assert reopened.assumptions.investment_return == Decimal("0.0475")
    assert reopened.assumptions.expense_inflation == Decimal("0.031")


def test_base_assumptions_do_not_leak_between_books(books) -> None:
    first = books("first.breadsched")
    second = books("second.breadsched")
    manager = SimpleNamespace()

    base = baseline_scenario(manager, first)
    base.assumptions.cash_interest = Decimal("0.08")
    persist_baseline_assumptions(manager, first)

    other = baseline_scenario(manager, second)
    assert other.assumptions.cash_interest != Decimal("0.08")
