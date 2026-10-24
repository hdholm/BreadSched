"""Schedule editor requests travel as JSON without loss (#356, #357, experimental).

The browser, GTK, and an HTTP client save fixed and formula schedules through the
book server with the services' own typed requests; ``api.schedule_codec`` gives
those requests a strict JSON form so the HTTP path saves exactly what the
in-process path does.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from breadsched.api.schedule_codec import (
    fixed_request_from_json,
    fixed_request_json,
    formula_request_from_json,
    formula_request_json,
)
from breadsched.gen.lib import (
    Money,
    PeriodType,
    Recurrence,
    ScheduledSplit,
    ScheduledTransaction,
)
from breadsched.gen.lib.scheduled import (
    ScheduledAmountChange,
    ScheduledMonthAmount,
    ScheduledOccurrenceAdjustment,
    ScheduledSplitAmountChange,
    ScheduleGrowthPolicy,
)
from breadsched.gen.lib.transaction import InvestmentActivityKind, PlanningFlowKind
from breadsched.gen.services import (
    FixedScheduleInput,
    FixedSplitInput,
    FormulaScheduleInput,
    SaveFixedSchedule,
)

RECURRENCE = Recurrence(period=PeriodType.MONTH, interval=1, start=date(2026, 1, 5))


def _everything() -> SaveFixedSchedule:
    source = ScheduledTransaction(
        name="Rent",
        recurrence=RECURRENCE,
        splits=[ScheduledSplit("rent", Money("1800")), ScheduledSplit("bank", Money("-1800"))],
    )
    return SaveFixedSchedule(
        FixedScheduleInput(
            name="Rent and parking",
            recurrence=RECURRENCE,
            category="rent",
            funding="bank",
            amount=Money("1850.25"),
            category_planning_flow=PlanningFlowKind.DEBT_PRINCIPAL,
            funding_planning_flow=None,
            investment_activity=InvestmentActivityKind.CONTRIBUTION,
            category_ledger_direction=-1,
            additional_splits=(
                FixedSplitInput(
                    "parking",
                    Money("50"),
                    planning_flow=PlanningFlowKind.DEBT_PRINCIPAL,
                    memo="bay 4",
                    opposite_direction=True,
                ),
            ),
            category_memo="flat 2",
            funding_memo="checking",
            enabled=False,
            auto_create=True,
            placeholder=False,
            growth_policy=ScheduleGrowthPolicy.INFLATION,
            amount_changes=(ScheduledAmountChange(date(2026, 6, 5), Money("1900")),),
            split_amount_changes={
                "parking": (ScheduledSplitAmountChange(date(2026, 6, 5), Money("-60")),)
            },
            seasonal_amounts=(ScheduledMonthAmount(12, Money("2000")),),
            skipped=(date(2026, 3, 5),),
            occurrence_adjustments=(
                ScheduledOccurrenceAdjustment(date(2026, 4, 5), Money("1700")),
            ),
            estimate_evidence={"months": 6},
            advance_days=3,
            remind_days=9,
        ),
        existing_handle="rent-1",
        source=source,
    )


def _over_the_wire(data):
    return json.loads(json.dumps(data))


def test_a_fixed_request_survives_json_unchanged():
    request = _everything()
    encoded = fixed_request_json(request)
    decoded = fixed_request_from_json(_over_the_wire(encoded))
    assert fixed_request_json(decoded) == encoded
    item = decoded.definition
    assert item.amount == Money("1850.25") and item.growth_policy is ScheduleGrowthPolicy.INFLATION
    assert item.additional_splits[0].opposite_direction is True
    assert item.split_amount_changes["parking"][0].amount == Money("-60")
    assert decoded.source.serialize() == request.source.serialize()


def test_a_formula_request_survives_json_unchanged():
    request = FormulaScheduleInput(
        existing_handle="loan",
        name="Loan",
        recurrence=RECURRENCE,
        formulas={0: "payment + 25", 1: "-(payment + 25)"},
        variables={"payment": "100"},
        enabled=True,
        auto_create=False,
        placeholder=False,
        growth_policy=ScheduleGrowthPolicy.AUTO,
        skipped=(date(2026, 2, 5),),
        advance_days=None,
        remind_days=4,
    )
    encoded = formula_request_json(request)
    assert set(encoded["formulas"]) == {"0", "1"}
    decoded = formula_request_from_json(_over_the_wire(encoded))
    assert formula_request_json(decoded) == encoded
    assert decoded.formulas == {0: "payment + 25", 1: "-(payment + 25)"}


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda d: d["definition"].update(amount=12.5), "amount"),
        (lambda d: d["definition"].update(amount="lots"), "amount"),
        (lambda d: d["definition"].update(enabled="yes"), "enabled"),
        (lambda d: d["definition"].update(growth_policy="sideways"), "growth_policy"),
        (lambda d: d["definition"].update(additional_splits={}), "additional_splits"),
        (lambda d: d["definition"].update(skipped=["soon"]), "skipped"),
        (lambda d: d["definition"].update(recurrence=None), "recurrence"),
        (lambda d: d["definition"].update(advance_days=True), "advance_days"),
        (lambda d: d.update(definition=[]), "definition"),
        (lambda d: d.update(source="rent"), "source"),
    ],
)
def test_a_malformed_fixed_request_is_refused(change, message):
    data = _over_the_wire(fixed_request_json(_everything()))
    change(data)
    with pytest.raises(ValueError, match=message):
        fixed_request_from_json(data)


def test_a_malformed_formula_request_is_refused():
    good = _over_the_wire(
        formula_request_json(
            FormulaScheduleInput(
                existing_handle="loan",
                name="Loan",
                recurrence=RECURRENCE,
                formulas={0: "1"},
                variables={},
                enabled=True,
                auto_create=False,
                placeholder=False,
                growth_policy=ScheduleGrowthPolicy.AUTO,
            )
        )
    )
    for key, value, message in (
        ("formulas", {"first": "1"}, "split position"),
        ("variables", {"payment": 100}, "variable"),
        ("name", None, "name"),
    ):
        with pytest.raises(ValueError, match=message):
            formula_request_from_json({**good, key: value})
