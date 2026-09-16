"""Representative schedule fidelity and source-ownership contracts.

The small generated fixtures in this module intentionally exercise several
features together.  Isolated parser tests are useful, but they do not prove that a
realistic definition remains coherent after persistence or source refresh.
"""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest
from gnucash_xml_fixtures import create_xml_book

from breadsched.gen.engine import schedule
from breadsched.gen.lib import (
    Money,
    PeriodType,
    PlanningFlowKind,
    Recurrence,
    ScheduledAmountChange,
    ScheduledMonthAmount,
    ScheduledOccurrenceAdjustment,
    ScheduledSplit,
    ScheduledSplitAmountChange,
    ScheduledTransaction,
    ScheduleGrowthPolicy,
    WeekendAdjust,
)
from breadsched.plugins.importer import gnucash_sqlite, gnucash_xml


def test_native_schedule_matrix_round_trips_without_normalization(db, book):
    source = ScheduledTransaction(
        name="Semi-monthly benefit funding",
        description="Native schedule with every supported local extension",
        recurrence=Recurrence(
            period=PeriodType.SEMI_MONTH,
            interval=2,
            start=date(2026, 1, 15),
            count=7,
            day_of_month=1,
            second_day_of_month=15,
            weekend_adjust=WeekendAdjust.NEXT,
        ),
        splits=[
            ScheduledSplit(
                book.savings,
                "525",
                memo="restricted contribution",
                planning_flow=PlanningFlowKind.BENEFIT_FUNDING,
            ),
            ScheduledSplit(book.checking, "-525", memo="cash funding"),
        ],
        auto_create=True,
        advance_days=9,
        growth_policy=ScheduleGrowthPolicy.NONE,
        amount_changes=[ScheduledAmountChange(date(2026, 7, 1), "550")],
        seasonal_amounts=[ScheduledMonthAmount(12, "600")],
        skipped=[date(2026, 3, 2)],
        occurrence_adjustments=[ScheduledOccurrenceAdjustment(date(2026, 5, 1), "575")],
    )
    source.variables = {"employee_rate": "0.05"}
    source.last_posted = date(2026, 1, 15)

    with db.transaction("store fidelity fixture") as txn:
        db.add_scheduled(source, txn)

    restored = db.get_scheduled(source.handle)
    assert restored is not None
    assert restored.serialize() == source.serialize()
    assert restored.recurrence.occurrences(date(2026, 8, 31)) == [
        date(2026, 1, 15),
        date(2026, 3, 2),
        date(2026, 3, 16),
        date(2026, 5, 1),
        date(2026, 5, 15),
        date(2026, 7, 1),
        date(2026, 7, 15),
    ]


def test_sqlite_import_matrix_preserves_bounds_flags_formulas_and_adjustment(
    db, gnucash_sqlite_path
):
    with sqlite3.connect(gnucash_sqlite_path.path) as conn:
        conn.execute(
            "UPDATE schedxactions SET auto_create=0, adv_creation=9, "
            "end_date='20260301', num_occur=4"
        )
        conn.execute(
            "UPDATE recurrences SET recurrence_mult=2, recurrence_period_type='week', "
            "recurrence_period_start='20260103', recurrence_weekend_adjust='forward'"
        )
        for split_guid, value in conn.execute(
            "SELECT guid, value_num FROM splits WHERE account_guid=?",
            (gnucash_sqlite_path.ids.template,),
        ):
            key = "debit-formula" if value > 0 else "credit-formula"
            conn.execute(
                "INSERT INTO slots (obj_guid,name,slot_type,string_val) VALUES (?,?,?,?)",
                (split_guid, f"sched-xaction/{key}", 4, "1800 + 25"),
            )

    gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)
    imported = db.get_scheduled(gnucash_sqlite_path.ids.sched)

    assert imported is not None
    assert imported.auto_create is False
    assert imported.advance_days == 9
    assert imported.recurrence.period is PeriodType.WEEK
    assert imported.recurrence.interval == 2
    assert imported.recurrence.end == date(2026, 3, 1)
    assert imported.recurrence.count == 4
    assert imported.recurrence.weekend_adjust is WeekendAdjust.NEXT
    assert imported.recurrence.occurrences(date(2026, 3, 31)) == [
        date(2026, 1, 5),
        date(2026, 1, 19),
        date(2026, 2, 2),
        date(2026, 2, 16),
    ]
    assert {split.formula for split in imported.splits} == {
        "1800 + 25",
        "-(1800 + 25)",
    }
    assert imported.imbalance() == Money(0)


def test_xml_import_matrix_preserves_month_end_bounds_and_weekend_adjustment(db, tmp_path):
    source = create_xml_book(tmp_path / "matrix.gnucash", compress=False)
    body = source.body.replace(
        "<sx:start><gdate>2026-01-01</gdate></sx:start>",
        "<sx:start><gdate>2026-01-31</gdate></sx:start>\n"
        "    <sx:end><gdate>2026-04-30</gdate></sx:end>\n"
        "    <sx:num-occur>4</sx:num-occur>",
    ).replace(
        "<recurrence:period_type>month</recurrence:period_type>\n"
        "        <recurrence:start><gdate>2026-01-01</gdate></recurrence:start>",
        "<recurrence:period_type>end of month</recurrence:period_type>\n"
        "        <recurrence:start><gdate>2026-01-31</gdate></recurrence:start>\n"
        "        <recurrence:weekend_adj>back</recurrence:weekend_adj>",
    )
    (tmp_path / "matrix.gnucash").write_text(body, encoding="utf-8")

    gnucash_xml.import_book(db, tmp_path / "matrix.gnucash")
    imported = db.get_scheduled(source.schedule)

    assert imported is not None
    assert imported.recurrence.period is PeriodType.MONTH
    assert imported.recurrence.day_of_month == -1
    assert imported.recurrence.end == date(2026, 4, 30)
    assert imported.recurrence.count == 4
    assert imported.recurrence.weekend_adjust is WeekendAdjust.PREVIOUS
    assert imported.recurrence.occurrences(date(2026, 5, 31)) == [
        date(2026, 1, 30),
        date(2026, 2, 27),
        date(2026, 3, 31),
        date(2026, 4, 30),
    ]


@pytest.mark.parametrize("source_kind", ["sqlite", "xml"])
def test_multiple_source_recurrences_are_preserved_but_never_executed(
    db, tmp_path, gnucash_sqlite_path, source_kind
):
    if source_kind == "sqlite":
        path = gnucash_sqlite_path.path
        handle = gnucash_sqlite_path.ids.sched
        with sqlite3.connect(path) as conn:
            conn.execute(
                "INSERT INTO recurrences (obj_guid,recurrence_mult,"
                "recurrence_period_type,recurrence_period_start,"
                "recurrence_weekend_adjust) VALUES (?,?,?,?,?)",
                (handle, 1, "week", "20260115", "none"),
            )
        result = gnucash_sqlite.import_book(db, path)
    else:
        source = create_xml_book(tmp_path / "multiple.gnucash", compress=False)
        path = tmp_path / "multiple.gnucash"
        handle = source.schedule
        recurrence = (
            '      <gnc:recurrence version="1.0.0">\n'
            "        <recurrence:mult>1</recurrence:mult>\n"
            "        <recurrence:period_type>week</recurrence:period_type>\n"
            "        <recurrence:start><gdate>2026-01-15</gdate></recurrence:start>\n"
            "      </gnc:recurrence>\n"
        )
        path.write_text(
            source.body.replace("    </sx:schedule>", recurrence + "    </sx:schedule>"),
            encoding="utf-8",
        )
        result = gnucash_xml.import_book(db, path)

    imported = db.get_scheduled(handle)
    assert imported is not None
    assert isinstance(imported.source_recurrence, list)
    assert len(imported.source_recurrence) == 2
    assert "multiple recurrence rules" in imported.unsupported_reason
    assert imported.usable is False
    assert schedule.forecast_occurrences(db, date(2026, 1, 1), date(2027, 1, 1)) == []
    assert any("excluded from planning and posting" in item for item in result.warnings)


def test_source_refresh_retains_only_breadsched_owned_schedule_state(db, gnucash_sqlite_path):
    gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)
    imported = db.get_scheduled(gnucash_sqlite_path.ids.sched)
    assert imported is not None
    expense = next(split for split in imported.splits if split.resolve() > 0)
    expense.planning_flow = PlanningFlowKind.BENEFIT_FUNDING
    expense.amount_changes = [ScheduledSplitAmountChange(date(2026, 7, 1), "1950")]
    imported.description = "Local planning note"
    imported.growth_policy = ScheduleGrowthPolicy.NONE
    imported.amount_changes = [ScheduledAmountChange(date(2026, 7, 1), "1900")]
    imported.seasonal_amounts = [ScheduledMonthAmount(12, "2000")]
    imported.occurrence_adjustments = [ScheduledOccurrenceAdjustment(date(2026, 5, 1), "1850")]
    imported.variables = {"annual_rate": "0.04"}
    imported.skipped = [date(2026, 2, 1)]
    imported.last_posted = date(2026, 1, 1)
    with db.transaction("add local schedule state") as txn:
        db.commit_scheduled(imported, txn)

    with sqlite3.connect(gnucash_sqlite_path.path) as conn:
        conn.execute(
            "UPDATE schedxactions SET name='Rent refreshed by source', enabled=0, adv_creation=12"
        )
        conn.execute("UPDATE recurrences SET recurrence_mult=2")
    gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)
    refreshed = db.get_scheduled(imported.handle)

    assert refreshed is not None
    assert refreshed.name == "Rent refreshed by source"
    assert refreshed.enabled is False
    assert refreshed.advance_days == 12
    assert refreshed.recurrence.interval == 2
    assert refreshed.description == "Local planning note"
    assert refreshed.growth_policy is ScheduleGrowthPolicy.NONE
    assert refreshed.amount_changes[0].amount == Money("1900")
    assert refreshed.seasonal_amounts[0].amount == Money("2000")
    assert refreshed.occurrence_adjustments[0].amount == Money("1850")
    assert refreshed.variables == {"annual_rate": "0.04"}
    assert refreshed.skipped == [date(2026, 2, 1)]
    assert refreshed.last_posted == date(2026, 1, 1)
    retained_expense = next(split for split in refreshed.splits if split.resolve() > 0)
    assert retained_expense.planning_flow is PlanningFlowKind.BENEFIT_FUNDING
    assert retained_expense.amount_changes[0].amount == Money("1950")


@pytest.mark.parametrize("source_kind", ["sqlite", "xml"])
def test_imported_formula_edits_round_trip_and_source_refresh_respects_ownership(
    db, tmp_path, gnucash_sqlite_path, source_kind
):
    if source_kind == "sqlite":
        path = gnucash_sqlite_path.path
        handle = gnucash_sqlite_path.ids.sched
        with sqlite3.connect(path) as conn:
            for split_guid, value in conn.execute(
                "SELECT guid, value_num FROM splits WHERE account_guid=?",
                (gnucash_sqlite_path.ids.template,),
            ):
                key = "debit-formula" if value > 0 else "credit-formula"
                conn.execute(
                    "INSERT INTO slots (obj_guid,name,slot_type,string_val) VALUES (?,?,?,?)",
                    (split_guid, f"sched-xaction/{key}", 4, "100 + period"),
                )
        importer = gnucash_sqlite.import_book
    else:
        source = create_xml_book(tmp_path / "formula-matrix.gnucash", compress=False)
        path = source.path
        handle = source.schedule
        (tmp_path / "formula-matrix.gnucash").write_text(
            source.body.replace(">1800.00<", ">100 + period<"),
            encoding="utf-8",
        )
        importer = gnucash_xml.import_book

    importer(db, path)
    imported = db.get_scheduled(handle)
    assert imported is not None
    projection = schedule.schedule_edit_projection(db, imported)
    assert projection.editability.mode is schedule.ScheduleEditorMode.FORMULA
    assert set(projection.formula_split_indices) == {0, 1}

    positive = next(
        index
        for index in projection.formula_split_indices
        if imported.splits[index].resolve(imported.context(imported.recurrence.start)) > 0
    )
    formulas = {
        index: "base + period" if index == positive else "-(base + period)"
        for index in projection.formula_split_indices
    }
    edited = schedule.apply_formula_inputs(db, imported, formulas, {"base": "120"})
    edited.skipped = [edited.recurrence.start]
    with db.transaction("Edit imported formula inputs") as txn:
        db.commit_scheduled(edited, txn)

    restored = db.get_scheduled(handle)
    assert restored is not None
    assert [split.formula for split in restored.splits] == [
        formulas[index] for index in range(len(restored.splits))
    ]
    assert restored.variables == {"base": "120"}
    assert restored.skipped == [restored.recurrence.start]

    importer(db, path)
    refreshed = db.get_scheduled(handle)
    assert refreshed is not None
    assert {split.formula for split in refreshed.splits} == {
        "100 + period",
        "-(100 + period)",
    }
    assert refreshed.variables == {"base": "120"}
    assert refreshed.skipped == [refreshed.recurrence.start]
