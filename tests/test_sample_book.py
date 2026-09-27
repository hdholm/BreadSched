"""The learning book is synthetic, reproducible in meaning, and isolated."""

from __future__ import annotations

import json
from datetime import date

from breadsched.cli.main import main as cli
from breadsched.gen.db.sqlite import DbSQLite
from breadsched.gen.engine import dashboard, schedule, valuation
from breadsched.gen.lib import Money
from breadsched.gen.sample_book import create_sample_book


def test_sample_book_has_dated_ledger_dashboard_and_plan(tmp_path):
    path = tmp_path / "sample.breadsched"
    create_sample_book(path, as_of=date(2026, 1, 15))
    db = DbSQLite()
    db.load(str(path), "r")
    try:
        assert db.get_metadata("book_name") == "SYNTHETIC SAMPLE — no real financial data"
        assert sum(1 for _ in db.iter_transactions()) == 7
        assert valuation.aggregate_value(
            db, as_of=date(2026, 1, 15), net_worth=True
        ).amount.value == Money(13400)
        board = dashboard.build(db, as_of=date(2026, 1, 15))
        assert board.report_summary()["net_worth"] == Money(13400)
        assert board.report_summary()["months_covered"] is not None
        occurrences = schedule.forecast_occurrences(db, date(2026, 2, 1), date(2026, 2, 28))
        assert len(occurrences) == 4
        assert {item.when for item in occurrences} == {date(2026, 2, day) for day in (5, 6, 12, 20)}
        assert sum(1 for item in db.iter_scheduled() if item.placeholder) == 1
    finally:
        db.close()


def test_sample_cli_reports_identity_and_does_not_replace_an_existing_book(tmp_path, capsys):
    path = tmp_path / "learning.breadsched"
    assert cli(["sample", str(path), "--as-of", "2026-12-31", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["synthetic"] is True
    assert payload["reference_date"] == "2026-12-31"
    db = DbSQLite()
    db.load(str(path), "r")
    try:
        assert {
            item.when
            for item in schedule.forecast_occurrences(db, date(2027, 1, 1), date(2027, 1, 31))
        } == {date(2027, 1, day) for day in (5, 6, 12, 20)}
    finally:
        db.close()
    original = path.read_bytes()
    assert cli(["sample", str(path), "--as-of", "2026-12-31"]) == 2
    assert path.read_bytes() == original
