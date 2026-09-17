"""Application-service contracts shared by presentation adapters."""

from datetime import date

from breadsched.gen.engine.activity import PlanMeasure, ReportingPeriod
from breadsched.gen.lib import (
    InvestmentActivityKind,
    Money,
    PeriodType,
    PlanningFlowKind,
    Recurrence,
    Scenario,
    ScenarioSchedule,
    ScheduledSplit,
)
from breadsched.gen.lib.scheduled import ScheduledTransaction
from breadsched.gen.services import (
    BASE_SCENARIO,
    FixedScheduleInput,
    FixedSplitInput,
    FormulaScenarioScheduleInput,
    FormulaScheduleInput,
    PlanQuery,
    SaveFixedScenarioSchedule,
    SaveFixedSchedule,
    SaveScenarioSchedule,
    SaveSchedule,
    ServiceError,
    build_fixed_schedule,
    build_formula_scenario_schedule,
    query_plan,
    save_fixed_scenario_schedule,
    save_fixed_schedule,
    save_formula_scenario_schedule,
    save_formula_schedule,
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


def test_fixed_request_constructs_identical_baseline_and_scenario_splits(db, book):
    definition = FixedScheduleInput(
        name="Payroll plan",
        recurrence=Recurrence(period=PeriodType.MONTH, start=date(2026, 2, 1), count=2),
        category=book.salary,
        funding=book.checking,
        amount=Money("5000"),
        additional_splits=(
            FixedSplitInput(book.rent, Money("1000"), memo="deduction"),
            FixedSplitInput(
                book.brokerage,
                Money("500"),
                planning_flow=PlanningFlowKind.RETIREMENT_SAVING,
                investment_activity=InvestmentActivityKind.CONTRIBUTION,
                memo="employee contribution",
            ),
        ),
        placeholder=True,
    )
    scenario = Scenario(name="Alternative", start=date(2026, 1, 1), years=1)
    with db.transaction("Scenario") as txn:
        db.add_scenario(scenario, txn)

    baseline = save_fixed_schedule(db, SaveFixedSchedule(definition))
    scenario_result = save_fixed_scenario_schedule(
        db,
        SaveFixedScenarioSchedule(scenario.handle, definition),
    )

    assert baseline.ok and scenario_result.ok
    saved_baseline = db.get_scheduled(baseline.value.handle)
    saved_scenario = db.get_scenario(scenario.handle).schedule_overrides[0]
    assert [split.serialize() for split in saved_baseline.splits] == [
        split.serialize() for split in saved_scenario.splits
    ]
    assert [split.amount for split in saved_baseline.splits] == [
        Money("-5000"),
        Money("1000"),
        Money("500"),
        Money("3500"),
    ]


def test_fixed_construction_can_preview_an_unsaved_source_snapshot(db, book):
    source = _monthly_schedule(book)
    source.estimate_evidence = {"history_start": "2025-01-01"}
    definition = FixedScheduleInput(
        name="Adjusted preview",
        recurrence=source.recurrence,
        category=book.utilities,
        funding=book.checking,
        amount=Money("125"),
    )

    result = build_fixed_schedule(
        db,
        SaveFixedSchedule(definition, existing_handle=source.handle, source=source),
    )

    assert result.ok
    assert result.value is not None
    assert result.value.handle == source.handle
    assert result.value.estimate_evidence == source.estimate_evidence
    assert db.get_scheduled(source.handle) is None


def test_fixed_construction_preserves_a_proven_repeated_account(db, book):
    source = ScheduledTransaction(
        name="Shared expense",
        recurrence=Recurrence(period=PeriodType.MONTH, start=date(2026, 1, 1)),
        splits=[
            ScheduledSplit(book.utilities, Money("60")),
            ScheduledSplit(book.checking, Money("-100")),
            ScheduledSplit(book.utilities, Money("40")),
        ],
    )
    definition = FixedScheduleInput(
        name=source.name,
        recurrence=source.recurrence,
        category=book.utilities,
        funding=book.checking,
        amount=Money("60"),
        additional_splits=(FixedSplitInput(book.utilities, Money("40")),),
    )

    result = build_fixed_schedule(
        db,
        SaveFixedSchedule(definition, existing_handle=source.handle, source=source),
    )

    assert result.ok
    assert result.value is not None
    assert [split.account for split in result.value.splits] == [
        book.utilities,
        book.utilities,
        book.checking,
    ]


def test_formula_requests_preserve_owned_structure_across_baseline_and_scenario(db, book):
    source = ScheduledTransaction(
        name="Formula loan",
        recurrence=Recurrence(period=PeriodType.MONTH, start=date(2026, 1, 1)),
        splits=[
            ScheduledSplit(book.utilities, formula="payment"),
            ScheduledSplit(book.checking, formula="-payment"),
        ],
    )
    source.variables = {"payment": "100"}
    scenario = Scenario(name="Alternative", start=date(2026, 1, 1), years=1)
    with db.transaction("Formula fixture") as txn:
        db.add_scheduled(source, txn)
        db.add_scenario(scenario, txn)
    common = dict(
        name="Adjusted formula loan",
        recurrence=source.recurrence,
        formulas={0: "payment + 25", 1: "-(payment + 25)"},
        variables={"payment": "100"},
        growth_policy=source.growth_policy,
        skipped=(),
    )

    baseline = save_formula_schedule(
        db,
        FormulaScheduleInput(
            existing_handle=source.handle,
            enabled=True,
            auto_create=False,
            placeholder=False,
            **common,
        ),
    )
    scenario_result = save_formula_scenario_schedule(
        db,
        FormulaScenarioScheduleInput(
            scenario_handle=scenario.handle,
            source_schedule=source.handle,
            **common,
        ),
    )

    assert baseline.ok and scenario_result.ok
    stored = db.get_scheduled(source.handle)
    scenario_change = db.get_scenario(scenario.handle).schedule_overrides[0]
    assert [split.account for split in stored.splits] == [book.utilities, book.checking]
    assert [split.account for split in scenario_change.splits] == [book.utilities, book.checking]
    assert [split.formula for split in stored.splits] == ["payment + 25", "-(payment + 25)"]
    assert [split.formula for split in scenario_change.splits] == [
        "payment + 25",
        "-(payment + 25)",
    ]


def test_formula_scenario_construction_can_preview_unsaved_snapshots(db, book):
    source = ScheduledTransaction(
        name="Transient formula",
        recurrence=Recurrence(period=PeriodType.MONTH, start=date(2026, 1, 1)),
        splits=[
            ScheduledSplit(book.utilities, formula="payment"),
            ScheduledSplit(book.checking, formula="-payment"),
        ],
    )
    source.variables = {"payment": "100"}
    scenario = Scenario(name="Transient scenario", start=date(2026, 1, 1), years=1)
    current = ScenarioSchedule.from_scheduled(source)

    result = build_formula_scenario_schedule(
        db,
        FormulaScenarioScheduleInput(
            scenario_handle=scenario.handle,
            source_schedule=source.handle,
            name="Transient preview",
            recurrence=source.recurrence,
            formulas={0: "payment + 25", 1: "-(payment + 25)"},
            variables={"payment": "100"},
            growth_policy=source.growth_policy,
            source=source,
            current=current,
        ),
    )

    assert result.ok
    assert result.value is not None
    assert [split.formula for split in result.value.splits] == [
        "payment + 25",
        "-(payment + 25)",
    ]
    assert db.get_scenario(scenario.handle) is None


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
