"""Application-service contracts shared by presentation adapters."""

from datetime import date

from breadsched.gen.engine.activity import PlanMeasure, ReportingPeriod
from breadsched.gen.lib import (
    Money,
    PeriodType,
    Recurrence,
    Scenario,
    ScenarioSchedule,
    ScheduledSplit,
)
from breadsched.gen.lib.scheduled import ScheduledTransaction
from breadsched.gen.services import (
    BASE_SCENARIO,
    PlanQuery,
    SaveScenarioSchedule,
    SaveSchedule,
    ServiceError,
    query_plan,
    save_scenario_schedule,
    save_schedule,
)


def _monthly_schedule(book) -> ScheduledTransaction:
    return ScheduledTransaction(
        name="Monthly utilities",
        recurrence=Recurrence(period=PeriodType.MONTH, start=date(2026, 1, 1)),
        splits=[
            ScheduledSplit(book.utilities, Money("100")),
            ScheduledSplit(book.checking, Money("-100")),
        ],
    )


def test_schedule_service_owns_the_atomic_create_and_update(db, book):
    candidate = _monthly_schedule(book)

    created = save_schedule(db, SaveSchedule(candidate))

    assert created.value is not None
    assert db.get_scheduled(candidate.handle).name == "Monthly utilities"
    assert db.undo_stack[-1].message == "Add scheduled Monthly utilities"

    candidate.name = "Updated utilities"
    updated = save_schedule(db, SaveSchedule(candidate, existing_handle=candidate.handle))

    assert updated.value is not None
    assert db.get_scheduled(candidate.handle).name == "Updated utilities"
    assert db.undo_stack[-1].message == "Update scheduled Updated utilities"


def test_schedule_service_returns_stable_fields_and_does_not_partially_write(db, book):
    candidate = _monthly_schedule(book)
    candidate.splits[1].account = "missing"
    before = len(db.undo_stack)

    result = save_schedule(db, SaveSchedule(candidate))

    assert result.value is None
    assert ServiceError("schedule.account.not_found", ("splits.1.account",)) in result.errors
    assert db.get_scheduled(candidate.handle) is None
    assert len(db.undo_stack) == before


def test_schedule_service_protects_formula_owned_split_structure(db, book):
    source = ScheduledTransaction(
        name="Formula schedule",
        recurrence=Recurrence(period=PeriodType.MONTH, start=date(2026, 1, 1)),
        splits=[
            ScheduledSplit(book.utilities, formula="payment"),
            ScheduledSplit(book.checking, formula="-payment"),
        ],
    )
    source.variables = {"payment": "100"}
    assert save_schedule(db, SaveSchedule(source)).ok
    changed = ScheduledTransaction.from_dict(source.serialize())
    changed.splits[0].account = book.groceries

    result = save_schedule(db, SaveSchedule(changed, existing_handle=source.handle))

    assert ServiceError("schedule.formula.ownership", ("splits",)) in result.errors
    assert db.get_scheduled(source.handle).splits[0].account == book.utilities


def test_scenario_schedule_service_replaces_source_override_atomically(db, book):
    source = _monthly_schedule(book)
    scenario = Scenario(name="Alternative", start=date(2026, 1, 1), years=1)
    with db.transaction("Scenario fixture") as txn:
        db.add_scheduled(source, txn)
        db.add_scenario(scenario, txn)
    change = ScenarioSchedule.from_scheduled(source)
    change.splits[0].amount = Money("125")
    change.splits[1].amount = Money("-125")

    first = save_scenario_schedule(db, SaveScenarioSchedule(scenario.handle, change))
    change.splits[0].amount = Money("150")
    change.splits[1].amount = Money("-150")
    second = save_scenario_schedule(db, SaveScenarioSchedule(scenario.handle, change))

    assert first.ok and second.ok
    stored = db.get_scenario(scenario.handle)
    assert stored is not None
    assert len(stored.schedule_overrides) == 1
    assert stored.schedule_overrides[0].splits[0].amount == Money("150")
    assert db.get_scheduled(source.handle).splits[0].amount == Money("100")


def test_plan_query_returns_one_typed_result_for_base_and_comparison(db, book):
    schedule = _monthly_schedule(book)
    alternate = Scenario(name="Higher costs", start=date(2026, 1, 1), years=1)
    alternate.schedule_overrides.append(ScenarioSchedule.from_scheduled(schedule))
    alternate.schedule_overrides[0].splits[0].amount = Money("125")
    alternate.schedule_overrides[0].splits[1].amount = Money("-125")
    with db.transaction("Plan service fixture") as txn:
        db.add_scheduled(schedule, txn)
        db.add_scenario(alternate, txn)

    result = query_plan(
        db,
        PlanQuery(
            start=date(2026, 1, 1),
            end=date(2026, 3, 31),
            period=ReportingPeriod.MONTH,
            measure=PlanMeasure.VARIANCE,
            compare=alternate.handle,
            today=date(2026, 1, 15),
        ),
    )

    assert result.ok
    assert result.value is not None
    assert result.value.measure is PlanMeasure.VARIANCE
    assert result.value.report.activity.planned_cash_change == Money("-300")
    assert result.value.comparison is not None
    assert result.value.comparison.scenario.handle == alternate.handle
    assert result.value.comparison.report.activity.planned_cash_change == Money("-375")


def test_plan_query_returns_stable_field_errors_without_interface_text(db, book):
    result = query_plan(
        db,
        PlanQuery(
            start=date(2026, 4, 1),
            end=date(2026, 3, 31),
            today=date(2026, 1, 15),
        ),
    )

    assert not result.ok
    assert result.value is None
    assert result.errors == (ServiceError("plan.end.before_start", ("end", "start")),)


def test_plan_query_rejects_comparing_a_scenario_with_itself(db, book):
    scenario = Scenario(name="Same", start=date(2026, 1, 1), years=1)
    with db.transaction("Scenario") as txn:
        db.add_scenario(scenario, txn)

    result = query_plan(
        db,
        PlanQuery(
            start=date(2026, 1, 1),
            end=date(2026, 12, 31),
            scenario=scenario.handle,
            compare=scenario.handle,
            today=date(2026, 1, 15),
        ),
    )

    assert [error.code for error in result.errors] == ["plan.comparison.same"]


def test_plan_query_uses_base_marker_for_comparison(db, book):
    scenario = Scenario(name="Alternative", start=date(2026, 1, 1), years=1)
    with db.transaction("Scenario") as txn:
        db.add_scenario(scenario, txn)

    result = query_plan(
        db,
        PlanQuery(
            start=date(2026, 1, 1),
            end=date(2026, 12, 31),
            scenario=scenario.handle,
            compare=BASE_SCENARIO,
            today=date(2026, 1, 15),
        ),
    )

    assert result.value is not None
    assert result.value.comparison is not None
    assert result.value.comparison.scenario.handle is None
