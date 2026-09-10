"""Recurrence rules decide which month a cash movement lands in."""

from datetime import date
from decimal import Decimal

import pytest

from cashperspective.gen.lib.formula import FormulaError, evaluate
from cashperspective.gen.lib.recurrence import PeriodType, Recurrence, WeekendAdjust, add_months


class TestAddMonths:
    def test_clamps_to_a_short_month(self):
        assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)

    def test_handles_a_leap_february(self):
        assert add_months(date(2028, 1, 31), 1) == date(2028, 2, 29)

    def test_last_day_marker(self):
        assert add_months(date(2026, 3, 15), 0, day=-1) == date(2026, 3, 31)

    def test_crosses_a_year_boundary(self):
        assert add_months(date(2026, 11, 15), 3) == date(2027, 2, 15)

    def test_goes_backwards(self):
        assert add_months(date(2026, 2, 15), -3) == date(2025, 11, 15)


class TestMonthly:
    def test_month_end_anchor_does_not_drift(self):
        """The classic bug: 31 Jan -> 28 Feb -> 28 Mar instead of 31 Mar."""
        rule = Recurrence(PeriodType.MONTH, start=date(2026, 1, 31))
        assert rule.occurrences(date(2026, 4, 30)) == [
            date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31), date(2026, 4, 30),
        ]

    def test_quarterly_is_a_monthly_multiple(self):
        rule = Recurrence(PeriodType.MONTH, interval=3, start=date(2026, 1, 15))
        assert rule.occurrences(date(2026, 12, 31)) == [
            date(2026, 1, 15), date(2026, 4, 15), date(2026, 7, 15), date(2026, 10, 15),
        ]

    def test_explicit_last_day_of_month(self):
        rule = Recurrence(PeriodType.MONTH, start=date(2026, 1, 1), day_of_month=-1)
        assert rule.occurrences(date(2026, 3, 31)) == [
            date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31),
        ]


class TestOtherPeriods:
    def test_fortnightly(self):
        rule = Recurrence(PeriodType.WEEK, interval=2, start=date(2026, 1, 2))
        occurrences = rule.occurrences(date(2026, 2, 28))
        assert occurrences[:3] == [date(2026, 1, 2), date(2026, 1, 16), date(2026, 1, 30)]

    def test_semi_monthly_fires_twice_a_month(self):
        rule = Recurrence(
            PeriodType.SEMI_MONTH, start=date(2026, 1, 1),
            day_of_month=1, second_day_of_month=15,
        )
        assert rule.occurrences(date(2026, 2, 28)) == [
            date(2026, 1, 1), date(2026, 1, 15), date(2026, 2, 1), date(2026, 2, 15),
        ]

    def test_annual(self):
        rule = Recurrence(PeriodType.YEAR, start=date(2026, 6, 1))
        assert rule.occurrences(date(2029, 1, 1)) == [
            date(2026, 6, 1), date(2027, 6, 1), date(2028, 6, 1),
        ]

    def test_once_fires_exactly_once(self):
        rule = Recurrence(PeriodType.ONCE, start=date(2026, 5, 5))
        assert rule.occurrences(date(2030, 1, 1)) == [date(2026, 5, 5)]


class TestLimits:
    def test_count_limit(self):
        rule = Recurrence(PeriodType.MONTH, start=date(2026, 1, 1), count=3)
        assert len(rule.occurrences(date(2030, 1, 1))) == 3

    def test_end_date_limit(self):
        rule = Recurrence(PeriodType.MONTH, start=date(2026, 1, 1), end=date(2026, 3, 15))
        assert rule.occurrences(date(2030, 1, 1)) == [date(2026, 1, 1), date(2026, 2, 1),
                                                      date(2026, 3, 1)]

    def test_window_start_filters_without_changing_the_count(self):
        rule = Recurrence(PeriodType.MONTH, start=date(2026, 1, 1), count=3)
        assert rule.occurrences(date(2030, 1, 1), since=date(2026, 2, 1)) == [
            date(2026, 2, 1), date(2026, 3, 1),
        ]

    def test_next_after_returns_none_once_spent(self):
        rule = Recurrence(PeriodType.MONTH, start=date(2026, 1, 1), count=2)
        assert rule.next_after(date(2026, 1, 15)) == date(2026, 2, 1)
        assert rule.next_after(date(2026, 3, 1)) is None

    def test_interval_must_be_positive(self):
        with pytest.raises(ValueError):
            Recurrence(PeriodType.MONTH, interval=0)


class TestWeekendAdjustment:
    def test_pushes_back_off_a_saturday(self):
        # 1 August 2026 is a Saturday.
        rule = Recurrence(
            PeriodType.MONTH, start=date(2026, 8, 1),
            weekend_adjust=WeekendAdjust.PREVIOUS, count=1,
        )
        assert rule.occurrences(date(2026, 8, 31)) == [date(2026, 7, 31)]

    def test_pushes_forward_off_a_sunday(self):
        # 2 August 2026 is a Sunday.
        rule = Recurrence(
            PeriodType.MONTH, start=date(2026, 8, 2),
            weekend_adjust=WeekendAdjust.NEXT, count=1,
        )
        assert rule.occurrences(date(2026, 8, 31)) == [date(2026, 8, 3)]

    def test_leaves_weekdays_alone(self):
        rule = Recurrence(
            PeriodType.MONTH, start=date(2026, 8, 5),
            weekend_adjust=WeekendAdjust.NEXT, count=1,
        )
        assert rule.occurrences(date(2026, 8, 31)) == [date(2026, 8, 5)]


class TestSerialisation:
    def test_round_trip(self):
        rule = Recurrence(
            PeriodType.WEEK, interval=2, start=date(2026, 1, 2),
            end=date(2027, 1, 1), weekend_adjust=WeekendAdjust.PREVIOUS,
        )
        clone = Recurrence.from_dict(rule.serialize())
        assert clone.occurrences(date(2026, 3, 1)) == rule.occurrences(date(2026, 3, 1))

    def test_describe_is_human_readable(self):
        rule = Recurrence(PeriodType.WEEK, interval=2, start=date(2026, 1, 2))
        assert rule.describe() == "every 2 weeks"


class TestFormula:
    def test_evaluates_arithmetic(self):
        assert round(evaluate("1000 * 0.05 / 12", {}), 4) == Decimal("4.1667")

    def test_resolves_variables(self):
        assert evaluate("balance * rate", {"balance": 200, "rate": "0.5"}) == 100

    def test_rejects_function_calls(self):
        with pytest.raises(FormulaError):
            evaluate("__import__('os').system('rm -rf /')", {})

    def test_rejects_attribute_access(self):
        with pytest.raises(FormulaError):
            evaluate("balance.__class__", {"balance": 1})

    def test_rejects_unknown_variables(self):
        with pytest.raises(FormulaError):
            evaluate("mystery + 1", {})

    def test_rejects_division_by_zero(self):
        with pytest.raises(FormulaError):
            evaluate("1 / 0", {})

    def test_empty_expression_is_zero(self):
        assert evaluate("", {}) == 0


def test_occurrences_jump_over_old_daily_history(monkeypatch):
    rule = Recurrence(PeriodType.DAY, start=date(2000, 1, 1))
    adjustments = 0
    original = rule._adjust

    def counted(when):
        nonlocal adjustments
        adjustments += 1
        return original(when)

    monkeypatch.setattr(rule, "_adjust", counted)
    dates = rule.occurrences(date(2026, 1, 31), since=date(2026, 1, 1))

    assert dates[0] == date(2026, 1, 1)
    assert dates[-1] == date(2026, 1, 31)
    assert len(dates) == 31
    # A historical replay would perform roughly 9,500 adjustments here.
    assert adjustments < 50
