from decimal import Decimal
from types import SimpleNamespace

from breadsched.gui.planning_context import (
    baseline_scenario,
    persist_baseline_assumptions,
)


class _MetadataDb:
    def __init__(self, path: str) -> None:
        self.path = path
        self.metadata: dict[str, object] = {}

    def get_metadata(self, key: str, default=None):
        return self.metadata.get(key, default)

    def set_metadata(self, key: str, value) -> None:
        self.metadata[key] = value


def test_base_assumptions_round_trip_through_book_metadata() -> None:
    db = _MetadataDb("household.breadsched")
    manager = SimpleNamespace()
    base = baseline_scenario(manager, db)
    base.assumptions.investment_return = Decimal("0.0475")
    base.assumptions.expense_inflation = Decimal("0.031")

    persist_baseline_assumptions(manager, db)

    reopened = baseline_scenario(SimpleNamespace(), db)
    assert reopened.assumptions.investment_return == Decimal("0.0475")
    assert reopened.assumptions.expense_inflation == Decimal("0.031")


def test_base_assumptions_do_not_leak_between_books() -> None:
    first = _MetadataDb("first.breadsched")
    second = _MetadataDb("second.breadsched")
    manager = SimpleNamespace()

    base = baseline_scenario(manager, first)
    base.assumptions.cash_interest = Decimal("0.08")
    persist_baseline_assumptions(manager, first)

    other = baseline_scenario(manager, second)
    assert other.assumptions.cash_interest != Decimal("0.08")
