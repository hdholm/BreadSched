"""Property-based invariants of exact money arithmetic.

``test_money.py`` names the cases a reader should see; these properties check the
same rules over many generated amounts: arithmetic is exact rational arithmetic,
rounding is half away from zero at every denominator, allocation never loses or
invents a minor unit, and the text and GnuCash forms read back what they wrote.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction

import pytest

from breadsched.gen.lib.money import Money

hypothesis = pytest.importorskip("hypothesis")
st = pytest.importorskip("hypothesis.strategies")
given = hypothesis.given
settings = hypothesis.settings

DENOMINATORS = st.sampled_from([1, 2, 4, 8, 10, 100, 1000, 3, 7, 360, 100_000_000])


@st.composite
def amounts(draw, max_units: int = 10**12):
    """Money of any sign at a commodity-like or awkward denominator."""
    return Money(draw(st.integers(-max_units, max_units)), draw(DENOMINATORS))


def _fraction(value: Money) -> Fraction:
    return Fraction(value.numerator, value.denominator)


@settings(max_examples=300, deadline=None)
@given(amounts(), amounts(), amounts())
def test_addition_is_exact_associative_and_commutative(a, b, c):
    assert _fraction(a + b) == _fraction(a) + _fraction(b)
    assert a + b == b + a
    assert (a + b) + c == a + (b + c)
    assert a - b == a + (-b)
    assert a + Money(0) == a and a - a == Money(0)


@settings(max_examples=300, deadline=None)
@given(amounts(), st.integers(-1000, 1000), st.integers(1, 1000))
def test_scalar_multiplication_and_division_are_exact(a, factor, divisor):
    assert _fraction(a * factor) == _fraction(a) * factor
    assert _fraction(a / divisor) == _fraction(a) / divisor
    assert (a / divisor) * divisor == a
    assert a * Fraction(factor, divisor) == a * factor / divisor


@settings(max_examples=300, deadline=None)
@given(amounts(), amounts())
def test_ordering_and_hash_agree_with_rational_values(a, b):
    assert (a < b) == (_fraction(a) < _fraction(b))
    assert (a == b) == (_fraction(a) == _fraction(b))
    if a == b:
        assert hash(a) == hash(b)
    assert hash(a) == hash(_fraction(a))


@settings(max_examples=300, deadline=None)
@given(amounts(), DENOMINATORS)
def test_quantize_rounds_half_away_from_zero(a, denominator):
    rounded = _fraction(a.quantize(denominator))
    exact = _fraction(a) * denominator
    units = rounded * denominator
    assert units.denominator == 1
    assert abs(units - exact) <= Fraction(1, 2)
    if abs(units - exact) == Fraction(1, 2):
        assert abs(units) > abs(exact)
    # Quantizing to the same denominator twice changes nothing.
    assert a.quantize(denominator).quantize(denominator) == a.quantize(denominator)
    # Rounding is symmetric about zero.
    assert (-a).quantize(denominator) == -a.quantize(denominator)


@settings(max_examples=300, deadline=None)
@given(amounts(), st.integers(0, 6))
def test_to_decimal_rounds_the_same_way_as_quantize(a, places):
    expected = (Decimal(a.numerator) / Decimal(a.denominator)).quantize(
        Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP
    )
    assert a.to_decimal(places) == expected
    assert Money(a.to_decimal(places)) == a.quantize(10**places)


@settings(max_examples=300, deadline=None)
@given(amounts(), st.integers(1, 40), st.sampled_from([1, 100, 1000]))
def test_allocation_keeps_every_minor_unit(a, parts, fraction):
    shares = a.allocate(parts, fraction)
    assert len(shares) == parts
    assert sum(shares, Money(0)) == a.quantize(fraction)
    units = [share * fraction for share in shares]
    assert all(_fraction(unit).denominator == 1 for unit in units)
    assert max(units) - min(units) <= Money(1)
    # Every share has the total's sign (or is zero).
    assert all(share == 0 or (share > 0) == (a.quantize(fraction) > 0) for share in shares)


@settings(max_examples=300, deadline=None)
@given(amounts(), DENOMINATORS)
def test_gnucash_pairs_read_back(a, denominator):
    num, den = a.as_gnc(denominator)
    assert den == denominator
    assert Money.from_gnc(num, den) == a.quantize(denominator)


@settings(max_examples=300, deadline=None)
@given(amounts())
def test_text_forms_read_back(a):
    assert Money(str(a.to_decimal(2))) == a.quantize(100)
    assert Money(a.format()) == a.quantize(100)
    assert Money(f"{a.numerator}/{a.denominator}") == a
    assert repr(a) == f"Money('{a.to_decimal(4)}')"
    assert Money(repr(a)[7:-2]) == a.quantize(10_000)
