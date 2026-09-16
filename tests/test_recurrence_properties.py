"""Property-based recurrence invariants.

These tests complement the named regression cases in ``test_recurrence.py`` by
exercising many ordinary household recurrence shapes.  They intentionally avoid
business-calendar assumptions beyond BreadSched's documented weekend adjustment.
"""

from datetime import date, timedelta

import pytest

from breadsched.gen.lib.recurrence import PeriodType, Recurrence, WeekendAdjust

hypothesis = pytest.importorskip("hypothesis")
st = pytest.importorskip("hypothesis.strategies")
given = hypothesis.given
settings = hypothesis.settings


@st.composite
def recurrence_cases(draw):
    start = draw(st.dates(min_value=date(2000, 1, 1), max_value=date(2035, 12, 31)))
    period = draw(
        st.sampled_from(
            [
                PeriodType.DAY,
                PeriodType.WEEK,
                PeriodType.MONTH,
                PeriodType.NTH_WEEKDAY,
                PeriodType.LAST_WEEKDAY,
                PeriodType.YEAR,
                PeriodType.SEMI_MONTH,
            ]
        )
    )
    interval = draw(st.integers(min_value=1, max_value=4))
    weekend_adjust = draw(st.sampled_from(list(WeekendAdjust)))

    kwargs: dict[str, object] = {}
    if period in {PeriodType.MONTH, PeriodType.YEAR}:
        kwargs["day_of_month"] = draw(
            st.one_of(st.none(), st.integers(min_value=1, max_value=31), st.just(-1))
        )
    elif period is PeriodType.SEMI_MONTH:
        first, second = draw(
            st.sampled_from(
                [
                    (1, 15),
                    (15, -1),
                    (1, -1),
                    (5, 20),
                ]
            )
        )
        kwargs["day_of_month"] = first
        kwargs["second_day_of_month"] = second

    rule = Recurrence(
        period,
        interval=interval,
        start=start,
        weekend_adjust=weekend_adjust,
        **kwargs,
    )
    minimum_horizon = 45
    if period is PeriodType.SEMI_MONTH:
        # If ``start`` falls after both firing days, the next firing can be
        # ``interval`` months away.  Guarantee that the generated window reaches
        # that first possible occurrence before asserting the result is non-empty.
        minimum_horizon = 32 * interval + 14
    horizon_days = draw(st.integers(min_value=minimum_horizon, max_value=900))
    return rule, start + timedelta(days=horizon_days)


@settings(max_examples=150, deadline=None)
@given(recurrence_cases())
def test_occurrence_details_preserve_order_and_identity(case):
    rule, until = case
    details = rule.occurrence_details(until)

    assert details
    assert [item.number for item in details] == sorted(item.number for item in details)
    assert len({item.number for item in details}) == len(details)
    assert [item.nominal for item in details] == sorted(item.nominal for item in details)

    if rule.weekend_adjust is not WeekendAdjust.NONE:
        assert all(item.adjusted.weekday() < 5 for item in details)

    # Weekend adjustment can make two nominal occurrences land on the same cash
    # date.  Where the adjusted date is unique, index_of() must recover the exact
    # generator-owned occurrence number.
    counts: dict[date, int] = {}
    for item in details:
        counts[item.adjusted] = counts.get(item.adjusted, 0) + 1
    for item in details:
        if counts[item.adjusted] == 1:
            assert rule.index_of(item.adjusted) == item.number


@settings(max_examples=100, deadline=None)
@given(recurrence_cases())
def test_recurrence_serialization_preserves_generated_occurrences(case):
    rule, until = case
    clone = Recurrence.from_dict(rule.serialize())

    assert clone.occurrence_details(until) == rule.occurrence_details(until)
