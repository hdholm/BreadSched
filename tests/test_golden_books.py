"""Independent, hand-calculated acceptance books for Plan and Projection."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from golden_books import (
    build_and_reopen_book,
    plan_snapshot,
    projection_snapshot,
    read_book,
    read_expected,
)

from breadsched.gen.engine import projection
from breadsched.gen.engine.activity import ReportingPeriod, build_category_report
from breadsched.gen.lib import Scenario
from breadsched.gen.services.plan import PlanQuery, query_plan

BOOKS = (
    "plan-cash-timing",
    "plan-classified-flows",
    "mortgage-escrow",
    "projection-accrual",
    "scenario-overlay",
)


def assert_subset(actual: Any, expected: Any, path: str = "snapshot") -> None:
    """Compare an authored expectation while allowing diagnostic snapshot fields."""
    if isinstance(expected, dict):
        assert isinstance(actual, dict), path
        for key, value in expected.items():
            assert key in actual, f"{path}.{key} is missing"
            assert_subset(actual[key], value, f"{path}.{key}")
        return
    if isinstance(expected, list):
        assert isinstance(actual, list), path
        assert len(actual) == len(expected), path
        for index, (actual_item, expected_item) in enumerate(zip(actual, expected, strict=True)):
            assert_subset(actual_item, expected_item, f"{path}[{index}]")
        return
    assert actual == expected, path


@pytest.mark.parametrize("name", BOOKS)
def test_plan_matches_hand_calculated_golden(name, tmp_path):
    declaration = read_book(name)
    db = build_and_reopen_book(name, tmp_path / f"{name}.breadsched")
    try:
        actual = plan_snapshot(db, declaration["plan"])
    finally:
        db.close()
    assert_subset(actual, read_expected(name, "plan"))


@pytest.mark.parametrize("name", BOOKS)
def test_projection_matches_hand_calculated_golden(name, tmp_path):
    declaration = read_book(name)
    db = build_and_reopen_book(name, tmp_path / f"{name}.breadsched")
    try:
        scenario = db.get_scenario(declaration["scenarios"][0]["id"])
        assert scenario is not None
        actual = projection_snapshot(db, scenario)
    finally:
        db.close()
    assert_subset(actual, read_expected(name, "projection"))


def test_cash_timing_rolls_up_without_changing_exact_dated_math(tmp_path):
    name = "plan-cash-timing"
    db = build_and_reopen_book(name, tmp_path / "cash-timing.breadsched")
    try:
        result = query_plan(
            db,
            PlanQuery(
                start=date(2026, 1, 1),
                end=date(2026, 2, 28),
                period=ReportingPeriod.QUARTER,
                today=date(2026, 1, 31),
            ),
        )
        assert result.ok and result.value is not None
        report = result.value.report
        assert [period.label for period in report.activity.periods] == ["Q1 2026"]
        assert report.activity.planned_cash_change.to_decimal() == 1300
        assert report.cash_position.minimum.to_decimal() == 500
        assert report.cash_position.minimum_date == date(2026, 1, 5)
    finally:
        db.close()


def test_cash_timing_preserves_event_order_and_future_na(tmp_path):
    name = "plan-cash-timing"
    db = build_and_reopen_book(name, tmp_path / "cash-order.breadsched")
    try:
        report = build_category_report(
            db,
            date(2026, 1, 1),
            date(2026, 2, 28),
            scenario=Scenario(name="Flat", start=date(2026, 1, 1), years=1),
            as_of=date(2026, 1, 31),
        )
        assert [
            event.planned_date
            for period in report.activity.periods
            for event in period.planned_events
        ] == [
            date(2026, 1, 5),
            date(2026, 1, 10),
            date(2026, 1, 20),
            date(2026, 2, 2),
            date(2026, 2, 5),
            date(2026, 2, 10),
            date(2026, 2, 20),
        ]
        assert all(row.variance[1] is None for row in report.categories)
        assert all(row.variance[1] is None for row in report.cash_bridge)
    finally:
        db.close()


def test_scenario_overlay_leaves_base_unchanged_and_explains_rates(tmp_path):
    name = "scenario-overlay"
    db = build_and_reopen_book(name, tmp_path / "scenario-overlay.breadsched")
    try:
        alternative = db.get_scenario("alternative")
        assert alternative is not None
        base_plan = query_plan(
            db,
            PlanQuery(
                start=date(2026, 1, 1),
                end=date(2026, 2, 28),
                compare="alternative",
                today=date(2026, 2, 28),
            ),
        )
        assert base_plan.ok and base_plan.value is not None
        assert [
            period.planned_cash_change.to_decimal()
            for period in base_plan.value.report.activity.periods
        ] == [1200, 1200]

        result = projection.project(db, alternative)
        january = projection.explain_month(db, result, 0)
        february = projection.explain_month(db, result, 1)
        assert january.assumption_sources["cash_interest"] == "Alternative"
        assert february.assumption_sources["cash_interest"] == "Alternative"
        assert january.holdings[0].annual_rate_source == "Alternative"
        assert february.holdings[0].annual_rate_source == "Alternative"
        assert january.holdings[0].annual_rate == Decimal("0.04")
        assert february.holdings[0].annual_rate == Decimal("0.05")
    finally:
        db.close()
