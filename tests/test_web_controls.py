"""Shared browser control parsing: amounts, service errors, and schedule controls."""

from __future__ import annotations

from datetime import date

import pytest

from breadsched.gen.lib import (
    Money,
    PeriodType,
    Recurrence,
    ScheduledSplit,
    ScheduledTransaction,
    WeekendAdjust,
)
from breadsched.gen.services import ServiceError
from breadsched.web.controls import input_money, service_error
from breadsched.web.schedule_controls import (
    frequency_key,
    parse_amount_changes,
    parse_occurrence_adjustments,
    parse_skipped,
    schedule_recurrence,
    weekend_key,
)


def test_amounts_follow_the_browser_decimal_convention():
    assert input_money({}, "1,234.50") == Money("1234.50")
    assert input_money({"number_format": "comma"}, "1.234,50") == Money("1234.50")
    assert input_money({}, [12345, 100]) == Money("123.45")
    with pytest.raises(ValueError):
        input_money({"number_format": "roman"}, "1")


def test_service_errors_become_resource_errors():
    missing = service_error(ServiceError("schedule.not_found", ("handle",)))
    refused = service_error(ServiceError("schedule.amount.non_positive", ("amount",)))
    assert (missing.status, missing.code, missing.fields) == (
        404,
        "schedule.not_found",
        ("handle",),
    )
    assert refused.status == 400 and refused.message != refused.code


def test_one_recurrence_parser_serves_every_schedule_editor():
    recurrence = schedule_recurrence(
        {"frequency": "biweekly", "start": "2026-01-02", "count": "5", "weekend": "next"}
    )
    assert (recurrence.period, recurrence.interval, recurrence.count) == (PeriodType.WEEK, 2, 5)
    assert recurrence.weekend_adjust is WeekendAdjust.NEXT
    assert frequency_key(recurrence) == "biweekly"
    assert weekend_key(recurrence.weekend_adjust) == "next"

    # A one-time schedule ignores end and count fields its editor may leave filled.
    once = schedule_recurrence({"frequency": "once", "start": "2026-03-01", "end": "x"})
    assert (once.period, once.end, once.count) == (PeriodType.ONCE, None, None)

    for payload, message in (
        ({"frequency": "fortnightly", "start": "2026-01-02"}, "unsupported schedule frequency"),
        ({"start": "soon"}, "first due date is invalid"),
        ({"start": "2026-01-02", "end": "2025-01-01"}, "cannot precede"),
        ({"start": "2026-01-02", "end": "2026-12-31", "count": "3"}, "not both"),
        ({"start": "2026-01-02", "count": "0"}, "must be positive"),
        ({"start": "2026-01-02", "weekend": "sometimes"}, "weekend"),
    ):
        with pytest.raises(ValueError, match=message):
            schedule_recurrence(payload)


def test_recurrence_keeps_hidden_day_details_of_the_same_shape():
    existing = ScheduledTransaction(
        name="Rent",
        recurrence=Recurrence(
            PeriodType.SEMI_MONTH, start=date(2026, 1, 1), day_of_month=1, second_day_of_month=15
        ),
        splits=[ScheduledSplit("a", Money(1)), ScheduledSplit("b", Money(-1))],
    )
    same = schedule_recurrence({"frequency": "semimonthly", "start": "2026-02-01"}, existing)
    other = schedule_recurrence({"frequency": "monthly", "start": "2026-02-01"}, existing)
    assert (same.day_of_month, same.second_day_of_month) == (1, 15)
    assert (other.day_of_month, other.second_day_of_month) == (None, None)


def test_exceptions_must_fall_on_occurrences_and_be_unique():
    recurrence = schedule_recurrence({"frequency": "monthly", "start": "2026-01-05"})
    assert parse_skipped({"skipped": ["2026-03-05"]}, recurrence) == [date(2026, 3, 5)]
    with pytest.raises(ValueError, match="not an occurrence"):
        parse_skipped({"skipped": ["2026-03-06"]}, recurrence)
    adjustments = parse_occurrence_adjustments(
        {"occurrence_adjustments": [{"when": "2026-02-05", "amount": "12.50"}]}, recurrence
    )
    assert [(item.when, item.amount) for item in adjustments] == [
        (date(2026, 2, 5), Money("12.50"))
    ]
    with pytest.raises(ValueError, match="unique"):
        parse_amount_changes(
            {
                "amount_changes": [
                    {"start": "2026-04-05", "amount": "1"},
                    {"start": "2026-04-05", "amount": "2"},
                ]
            },
            recurrence.start,
        )
