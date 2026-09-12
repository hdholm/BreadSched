"""Money must be exact.  Every other guarantee in the application rests on this."""

from decimal import Decimal
from fractions import Fraction

import pytest

from breadsched.gen.lib.money import Money, Rate


class TestConstruction:
    def test_parses_decimal_strings_exactly(self):
        assert Money("0.1") + Money("0.2") == Money("0.3")

    def test_rejects_float(self):
        with pytest.raises(TypeError):
            Money(0.1)

    def test_accepts_decimal(self):
        assert Money(Decimal("19.99")).to_decimal() == Decimal("19.99")

    def test_accepts_unambiguous_english_grouping(self):
        assert Money("$1,234.56") == Money("1234.56")

    @pytest.mark.parametrize("text", ["1,80", "1.800,00", "12,50"])
    def test_rejects_ambiguous_locale_formatted_strings(self, text):
        with pytest.raises(ValueError, match="cannot read"):
            Money(text)

    def test_normalises_to_lowest_terms(self):
        value = Money(50, 100)
        assert (value.numerator, value.denominator) == (1, 2)

    def test_negative_denominator_moves_sign_to_numerator(self):
        value = Money(1, -2)
        assert (value.numerator, value.denominator) == (-1, 2)

    def test_zero_denominator_is_an_error(self):
        with pytest.raises(ZeroDivisionError):
            Money(1, 0)


class TestGnuCashInterop:
    def test_round_trips_a_gnucash_pair(self):
        assert Money.from_gnc(31875, 100).to_decimal() == Decimal("318.75")

    def test_exposes_a_pair_at_a_requested_denominator(self):
        assert Money("12.345").as_gnc(100) == (1235, 100)

    def test_handles_non_decimal_denominators(self):
        """GnuCash uses denominators like 1000000 for share quantities."""
        assert Money.from_gnc(1234567, 1000000).to_decimal(6) == Decimal("1.234567")


class TestArithmetic:
    def test_repeated_addition_does_not_drift(self):
        total = Money(0)
        for _ in range(1000):
            total = total + Money("0.01")
        assert total == Money("10.00")

    def test_thirds_stay_exact_until_asked_to_round(self):
        third = Money(100) / 3
        assert third * 3 == Money(100)
        assert third.to_decimal() == Decimal("33.33")

    def test_multiplication_by_a_rate(self):
        assert (Money("1000.00") * Rate("1.05")).to_decimal() == Decimal("1050.00")

    def test_dividing_money_by_money_returns_an_exact_ratio(self):
        assert Money(1) / Money(3) == Fraction(1, 3)

    def test_multiplication_by_two_money_values_is_rejected(self):
        with pytest.raises(TypeError, match="two monetary amounts"):
            Money("10") * Money("2")

    def test_negation_and_absolute(self):
        assert -Money("5") == Money("-5")
        assert abs(Money("-5")) == Money("5")

    def test_zero_is_falsey(self):
        assert not Money(0)
        assert Money("0.01")


class TestRate:
    def test_rate_is_dimensionless_and_decimal_backed(self):
        rate = Rate("0.0625")
        assert rate.decimal == Decimal("0.0625")
        assert rate == Decimal("0.0625")
        assert f"{rate:.2%}" == "6.25%"

    def test_rate_rejects_float(self):
        with pytest.raises(TypeError):
            Rate(0.1)


class TestComparison:
    def test_orders_across_denominators(self):
        assert Money(1, 3) < Money(1, 2)
        assert Money(2, 4) == Money(1, 2)

    def test_compares_against_plain_numeric_values(self):
        assert Money("10.00") > 9
        assert Money("10.00") == Decimal("10")

    def test_non_numeric_comparison_is_false_not_an_error(self):
        assert (Money("10.00") == "abc") is False
        assert (Money("10.00") == "10") is False

    def test_is_hashable_by_value(self):
        assert len({Money(1, 2), Money(2, 4)}) == 1

    def test_hash_matches_equal_python_numbers(self):
        one = Money(1)
        assert one == 1
        assert one == Decimal("1")
        assert hash(one) == hash(1) == hash(Decimal("1"))
        assert 1 in {one}


class TestRoundingAndAllocation:
    def test_quantize_rounds_half_away_from_zero(self):
        assert Money("2.345").quantize(100).to_decimal() == Decimal("2.35")
        assert Money("-2.345").quantize(100).to_decimal() == Decimal("-2.35")

    def test_allocation_conserves_every_cent(self):
        parts = Money("100.00").allocate(3)
        assert sum((p.to_decimal() for p in parts), Decimal(0)) == Decimal("100.00")
        assert [p.to_decimal() for p in parts] == [
            Decimal("33.34"), Decimal("33.33"), Decimal("33.33")
        ]

    def test_allocation_of_a_negative_total(self):
        parts = Money("-10.00").allocate(3)
        assert sum((p.to_decimal() for p in parts), Decimal(0)) == Decimal("-10.00")

    def test_allocation_needs_at_least_one_part(self):
        with pytest.raises(ValueError):
            Money("1.00").allocate(0)


class TestFormatting:
    def test_groups_thousands(self):
        assert Money("1234567.8").format("$") == "$1,234,567.80"

    def test_accounting_parentheses_for_negatives(self):
        assert Money("-42").format("$", parens_negative=True) == "($42.00)"

    def test_plain_negative_by_default(self):
        assert Money("-42").format("$") == "-$42.00"
