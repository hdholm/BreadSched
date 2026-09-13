"""Damaged books must import as far as they can, and say why they could not go further.

An import that aborts on the first bad record costs the user everything and tells
them nothing. Each test here builds a book with one specific defect and asserts
both halves of the contract: the healthy records still arrive, and the report names
the record that did not.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date

import pytest
from gnucash_fixtures import create_book, new_guid, write_transaction

from breadsched.gen.engine import ledger
from breadsched.gen.lib import Money
from breadsched.gen.utils import logs
from breadsched.plugins.importer import gnucash_sqlite

CHART = [
    ("root", "Root Account", "ROOT", None, 0),
    ("bank", "Checking", "BANK", "root", 0),
    ("food", "Groceries", "EXPENSE", "root", 0),
    ("wages", "Salary", "INCOME", "root", 0),
]

HEALTHY = (date(2026, 1, 5), "Weekly shop", [("food", 7250, 100, ""), ("bank", -7250, 100, "")])


def add_raw_transaction(book, description: str, splits: list[tuple]) -> str:
    """Append a transaction that the fixture builder would refuse to create."""
    conn = sqlite3.connect(book.path)
    guid = new_guid()
    write_transaction(
        conn,
        guid,
        book.currency,
        date(2026, 2, 2),
        description,
        [(getattr(book, key), num, denom, memo) for key, num, denom, memo in splits],
    )
    conn.commit()
    conn.close()
    return guid


class TestSingleSplitTransactions:
    """The defect behind 'a transaction needs at least two splits'."""

    def test_the_import_still_succeeds(self, db, tmp_path):
        book = create_book(tmp_path / "one-split.gnucash", CHART, [HEALTHY])
        add_raw_transaction(book, "Lonely", [("bank", 5000, 100, "")])

        result = gnucash_sqlite.import_book(db, book.path)

        assert result.transactions == 2
        assert ledger.balance(db, book.food) == Money("72.50")

    def test_a_lone_split_is_balanced_to_imbalance(self, db, tmp_path):
        book = create_book(tmp_path / "one-split.gnucash", CHART, [HEALTHY])
        add_raw_transaction(book, "Lonely", [("bank", 5000, 100, "")])

        gnucash_sqlite.import_book(db, book.path)

        imbalance = db.get_account_by_name("Imbalance")
        assert imbalance is not None
        assert ledger.balance(db, imbalance.handle, natural_sign=False) == Money("-50.00")

    def test_the_warning_identifies_the_transaction(self, db, tmp_path):
        book = create_book(tmp_path / "one-split.gnucash", CHART, [HEALTHY])
        guid = add_raw_transaction(book, "Lonely", [("bank", 5000, 100, "")])

        result = gnucash_sqlite.import_book(db, book.path)

        assert result.warnings, "a repaired transaction must be reported"
        offending = " ".join(result.warnings)
        assert "Lonely" in offending
        assert "2026-02-02" in offending
        assert guid[:8] in offending

    def test_a_zero_value_lone_split_is_skipped_not_repaired(self, db, tmp_path):
        """Balancing a zero-value orphan to Imbalance would invent a record."""
        book = create_book(tmp_path / "zero.gnucash", CHART, [HEALTHY])
        add_raw_transaction(book, "Empty husk", [("bank", 0, 100, "")])

        result = gnucash_sqlite.import_book(db, book.path)

        assert result.transactions == 1
        assert result.skipped == 1
        assert "only one split, with no value" in result.reasons()


class TestOtherDefects:
    def test_a_transaction_with_no_splits_is_skipped(self, db, tmp_path):
        book = create_book(tmp_path / "empty.gnucash", CHART, [HEALTHY])
        conn = sqlite3.connect(book.path)
        conn.execute(
            "INSERT INTO transactions VALUES (?,?,?,?,?,?)",
            (new_guid(), book.currency, "", "20260303120000", "20260303120000", "No splits at all"),
        )
        conn.commit()
        conn.close()

        result = gnucash_sqlite.import_book(db, book.path)

        assert result.transactions == 1
        assert result.skipped == 1

    def test_several_defects_are_counted_by_reason(self, db, tmp_path):
        book = create_book(tmp_path / "messy.gnucash", CHART, [HEALTHY])
        add_raw_transaction(book, "Empty one", [("bank", 0, 100, "")])
        add_raw_transaction(book, "Empty two", [("food", 0, 100, "")])

        result = gnucash_sqlite.import_book(db, book.path)

        assert result.reasons() == {"only one split, with no value": 2}

    def test_the_report_lists_reasons_and_warnings(self, db, tmp_path):
        book = create_book(tmp_path / "messy.gnucash", CHART, [HEALTHY])
        add_raw_transaction(book, "Empty one", [("bank", 0, 100, "")])

        result = gnucash_sqlite.import_book(db, book.path)
        detail = result.detail()

        assert "Skipped records by reason:" in detail
        assert "1 x only one split, with no value" in detail

    def test_a_clean_book_reports_no_problems(self, db, tmp_path):
        book = create_book(tmp_path / "clean.gnucash", CHART, [HEALTHY])
        result = gnucash_sqlite.import_book(db, book.path)
        assert result.skipped == 0
        assert "No problems found." in result.detail()

    def test_reimport_distinguishes_new_repeated_and_resolved_skips(self, db, tmp_path):
        book = create_book(tmp_path / "history.gnucash", CHART, [HEALTHY])
        add_raw_transaction(book, "Empty husk", [("bank", 0, 100, "")])

        first = gnucash_sqlite.import_book(db, book.path)
        second = gnucash_sqlite.import_book(db, book.path)
        conn = sqlite3.connect(book.path)
        conn.execute("DELETE FROM transactions WHERE description = ?", ("Empty husk",))
        conn.commit()
        conn.close()
        third = gnucash_sqlite.import_book(db, book.path)

        assert (first.skipped_new, first.skipped_repeated, first.skipped_resolved) == (1, 0, 0)
        assert (second.skipped_new, second.skipped_repeated, second.skipped_resolved) == (0, 1, 0)
        assert (third.skipped_new, third.skipped_repeated, third.skipped_resolved) == (0, 0, 1)
        assert len(third.resolved_skipped_details) == 1
        assert third.resolved_skipped_details[0][0] == "only one split, with no value"
        assert "Empty husk" in third.resolved_skipped_details[0][1]


class TestNothingIsLost:
    def test_one_bad_record_does_not_roll_back_the_batch(self, db, tmp_path):
        """The original failure: the whole import was discarded."""
        transactions = [
            (
                date(2026, 1, day),
                f"Shop {day}",
                [("food", 1000 * day, 100, ""), ("bank", -1000 * day, 100, "")],
            )
            for day in range(1, 6)
        ]
        book = create_book(tmp_path / "mixed.gnucash", CHART, transactions)
        add_raw_transaction(book, "Broken", [("bank", 0, 100, "")])

        result = gnucash_sqlite.import_book(db, book.path)

        assert result.transactions == 5
        assert db.summary()["txn"] == 5
        assert len(list(db.iter_accounts())) == 4

    def test_the_partial_import_is_still_one_undo_step(self, db, tmp_path):
        book = create_book(tmp_path / "mixed.gnucash", CHART, [HEALTHY])
        add_raw_transaction(book, "Broken", [("bank", 0, 100, "")])

        gnucash_sqlite.import_book(db, book.path)
        assert db.summary()["txn"] == 1
        assert db.undo() is True
        assert db.summary()["txn"] == 0


class TestLogging:
    def test_debug_logging_names_each_transaction(self, db, tmp_path, breadsched_logs):
        book = create_book(tmp_path / "logged.gnucash", CHART, [HEALTHY])
        gnucash_sqlite.import_book(db, book.path)
        assert breadsched_logs.containing("Weekly shop")
        assert breadsched_logs.containing("2 split(s)")

    def test_capture_survives_a_logger_left_unpropagating(self, db, tmp_path, breadsched_logs):
        """The order dependence that failed on one machine and not another.

        Once any test configures logging, records stop reaching the root logger,
        where pytest's caplog listens. Capturing on the breadsched logger itself is
        independent of that.
        """
        logging.getLogger("breadsched").propagate = False  # as an earlier test leaves it

        book = create_book(tmp_path / "logged.gnucash", CHART, [HEALTHY])
        gnucash_sqlite.import_book(db, book.path)
        assert breadsched_logs.containing("Weekly shop")

    def test_configure_leaves_foreign_handlers_alone(self, db, tmp_path, breadsched_logs):
        """Reconfiguring must not silently detach someone else's handler."""
        logs.configure(verbosity=2, stream=False)
        book = create_book(tmp_path / "logged.gnucash", CHART, [HEALTHY])
        gnucash_sqlite.import_book(db, book.path)
        assert breadsched_logs.containing("Weekly shop")

    def test_configure_still_replaces_its_own_handlers(self, tmp_path):
        for _ in range(4):
            logs.configure(verbosity=1, path=tmp_path / "y.log", stream=True)
        logger = logging.getLogger("breadsched")
        assert len(logger.handlers) == 2  # one console, one file

    def test_a_log_file_captures_the_detail(self, db, tmp_path):
        book = create_book(tmp_path / "logged.gnucash", CHART, [HEALTHY])
        target = tmp_path / "import.log"
        logs.configure(verbosity=0, path=target, stream=False)
        try:
            gnucash_sqlite.import_book(db, book.path)
        finally:
            logs.configure(verbosity=0)

        contents = target.read_text()
        assert "importing GnuCash SQLite book" in contents
        assert "account Checking (BANK)" in contents

    def test_reconfiguring_does_not_duplicate_handlers(self, tmp_path):
        for _ in range(3):
            logs.configure(verbosity=1, path=tmp_path / "x.log", stream=False)
        logger = logging.getLogger("breadsched")
        assert len(logger.handlers) == 1
        logs.configure(verbosity=0)

    def test_the_library_does_not_configure_logging_itself(self):
        """Handlers are the application's business, never the library's."""
        logging.getLogger("breadsched").handlers.clear()
        logs.configure(verbosity=0, stream=False)
        assert logging.getLogger("breadsched").propagate is False


class TestCliDiagnostics:
    def test_debug_flag_emits_detail(self, tmp_path, capsys, gnucash_sqlite_path):
        from breadsched.cli.main import main as cli

        path = tmp_path / "book.breadsched"
        cli(["init", str(path)])
        capsys.readouterr()
        cli(["import", str(path), gnucash_sqlite_path.path, "--debug"])
        captured = capsys.readouterr()
        assert "importing GnuCash SQLite book" in captured.err

    def test_quiet_by_default_even_when_there_are_warnings(self, tmp_path, capsys):
        """Warnings appear once, in the report -- not again on stderr."""
        from breadsched.cli.main import main as cli

        book = create_book(tmp_path / "messy.gnucash", CHART, [HEALTHY])
        add_raw_transaction(book, "Lonely", [("bank", 5000, 100, "")])
        path = tmp_path / "book.breadsched"
        cli(["init", str(path)])
        capsys.readouterr()
        cli(["import", str(path), book.path])
        captured = capsys.readouterr()
        assert captured.err == ""
        assert captured.out.count("Lonely") == 1

    def test_verbose_puts_warnings_on_stderr_too(self, tmp_path, capsys):
        from breadsched.cli.main import main as cli

        book = create_book(tmp_path / "messy.gnucash", CHART, [HEALTHY])
        add_raw_transaction(book, "Lonely", [("bank", 5000, 100, "")])
        path = tmp_path / "book.breadsched"
        cli(["init", str(path)])
        capsys.readouterr()
        cli(["import", str(path), book.path, "-v"])
        assert "Lonely" in capsys.readouterr().err

    def test_log_file_flag_writes_a_file(self, tmp_path, capsys, gnucash_sqlite_path):
        from breadsched.cli.main import main as cli

        path = tmp_path / "book.breadsched"
        target = tmp_path / "run.log"
        cli(["init", str(path)])
        cli(["import", str(path), gnucash_sqlite_path.path, "--log-file", str(target)])
        capsys.readouterr()
        assert target.exists()
        assert "import finished" in target.read_text()
        from breadsched.gen.utils import logs as log_module

        log_module.configure(verbosity=0)

    def test_json_output_carries_the_skip_reasons(self, tmp_path, capsys):
        import json

        from breadsched.cli.main import main as cli

        book = create_book(tmp_path / "messy.gnucash", CHART, [HEALTHY])
        add_raw_transaction(book, "Empty", [("bank", 0, 100, "")])
        path = tmp_path / "book.breadsched"
        cli(["init", str(path)])
        capsys.readouterr()
        cli(["import", str(path), book.path, "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["skipped"] == 1
        assert payload["skipped_by_reason"]

    def test_a_missing_source_file_is_a_clean_error(self, tmp_path, capsys):
        from breadsched.cli.main import main as cli

        path = tmp_path / "book.breadsched"
        cli(["init", str(path)])
        capsys.readouterr()
        assert cli(["import", str(path), str(tmp_path / "absent.gnucash")]) == 2
        assert "no file at" in capsys.readouterr().err

    def test_a_file_that_is_not_a_book_is_a_clean_error(self, tmp_path, capsys):
        from breadsched.cli.main import main as cli

        junk = tmp_path / "notes.txt"
        junk.write_text("this is not a GnuCash book")
        path = tmp_path / "book.breadsched"
        cli(["init", str(path)])
        capsys.readouterr()
        assert cli(["import", str(path), str(junk)]) == 2


class TestErrorMessages:
    def test_validation_errors_name_the_transaction(self):
        from breadsched.gen.lib import Split, Transaction, UnbalancedError

        txn = Transaction(post_date=date(2026, 5, 5), description="Half a thing")
        txn.add_split(Split("abc", Money("10.00")))
        with pytest.raises(UnbalancedError) as caught:
            txn.validate()
        message = str(caught.value)
        assert "Half a thing" in message
        assert "2026-05-05" in message
        assert "found 1" in message

    def test_imbalance_errors_report_the_residual(self):
        from breadsched.gen.lib import Split, Transaction, UnbalancedError

        txn = Transaction(post_date=date(2026, 5, 5), description="Wonky")
        txn.add_split(Split("abc", Money("10.00")))
        txn.add_split(Split("def", Money("-9.00")))
        with pytest.raises(UnbalancedError) as caught:
            txn.validate()
        assert "residual 1.00" in str(caught.value)
