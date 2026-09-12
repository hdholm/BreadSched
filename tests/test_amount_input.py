from decimal import Decimal

import pytest

from breadsched.gen.utils.amount_input import parse_user_amount


def test_user_amount_accepts_period_decimal_and_grouping():
    assert parse_user_amount("$1,234.56") == Decimal("1234.56")


def test_user_amount_accepts_comma_decimal_and_grouping():
    assert parse_user_amount("1.234,56") == Decimal("1234.56")
    assert parse_user_amount("45,67") == Decimal("45.67")


def test_user_amount_explicit_format_resolves_ambiguous_single_separator():
    assert parse_user_amount("1,234", "dot") == Decimal("1234")
    assert parse_user_amount("1,234", "comma") == Decimal("1.234")


def test_user_amount_rejects_malformed_grouping():
    with pytest.raises(ValueError):
        parse_user_amount("12,34,567", "dot")
