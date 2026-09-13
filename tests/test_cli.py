"""The CLI is the scripting interface, so it is tested as one.

Each test drives ``main()`` with an argv list and reads stdout, which is exactly
what a shell script or a cron job sees.
"""

import json
from pathlib import Path

import pytest

from breadsched.cli.main import main
from breadsched.gen.db.sqlite import DbSQLite
from breadsched.gen.plug import remembered_import_source


@pytest.fixture
def book_path(tmp_path):
    return str(tmp_path / "household.breadsched")


def run(capsys, *argv) -> tuple[int, str]:
    code = main([str(a) for a in argv])
    return code, capsys.readouterr().out


def run_json(capsys, *argv):
    code, out = run(capsys, *argv, "--json")
    assert code == 0, out
    return json.loads(out)


class TestInit:
    def test_creates_a_book_with_top_level_accounts(self, capsys, book_path):
        code, out = run(capsys, "init", book_path)
        assert code == 0
        assert Path(book_path).exists()
        accounts = run_json(capsys, "accounts", book_path)
        assert {a["name"] for a in accounts} == {
            "Assets",
            "Liabilities",
            "Income",
            "Expenses",
            "Equity",
        }

    def test_refuses_to_overwrite(self, capsys, book_path):
        run(capsys, "init", book_path)
        assert main(["init", book_path]) == 2

    def test_version(self, capsys):
        code, out = run(capsys, "--version")
        assert code == 0 and out.startswith("breadsched ")


class TestPostingAndReading:
    @pytest.fixture
    def stocked(self, capsys, book_path):
        run(capsys, "init", book_path)
        run(
            capsys,
            "add",
            book_path,
            "--date",
            "2026-01-01",
            "--description",
            "Opening balance",
            "--from",
            "Equity",
            "--to",
            "Assets",
            "--amount",
            "5000.00",
        )
        run(
            capsys,
            "add",
            book_path,
            "--date",
            "2026-01-05",
            "--description",
            "Rent",
            "--from",
            "Assets",
            "--to",
            "Expenses",
            "--amount",
            "1800.00",
        )
        return book_path

    def test_balances_reflect_postings(self, capsys, stocked):
        result = run_json(capsys, "balance", stocked, "Assets")
        assert result["balance"] == "3200.00"

    def test_register_shows_a_running_balance(self, capsys, stocked):
        rows = run_json(capsys, "register", stocked, "Assets")
        assert [r["balance"] for r in rows] == ["5000.00", "3200.00"]

    def test_register_names_the_other_side(self, capsys, stocked):
        rows = run_json(capsys, "register", stocked, "Assets")
        assert rows[1]["transfer"] == "Expenses"

    def test_table_output_is_aligned_and_readable(self, capsys, stocked):
        code, out = run(capsys, "accounts", stocked)
        assert code == 0
        assert "Assets" in out and "3,200.00" in out

    def test_an_unknown_account_is_a_clean_error(self, stocked):
        assert main(["balance", stocked, "Nonexistent"]) == 2

    def test_export_writes_a_row_per_split(self, capsys, stocked, tmp_path):
        out_file = tmp_path / "txns.csv"
        result = run_json(capsys, "export", stocked, str(out_file))
        assert result["rows"] == 4
        assert "Opening balance" in out_file.read_text()


class TestGnuCashSubcommands:
    def test_info_reports_the_container_format(self, capsys, gnucash_sqlite_path):
        result = run_json(capsys, "gnucash", "info", gnucash_sqlite_path.path)
        assert result["format"] == "sqlite"

    def test_reads_accounts_without_importing(self, capsys, gnucash_sqlite_path):
        rows = run_json(capsys, "gnucash", "accounts", gnucash_sqlite_path.path)
        assert "Assets:Checking Account" in {r["full_name"] for r in rows}

    def test_reads_transactions_without_importing(self, capsys, gnucash_sqlite_path):
        rows = run_json(capsys, "gnucash", "transactions", gnucash_sqlite_path.path)
        assert len(rows) == 3

    def test_filters_transactions_by_date(self, capsys, gnucash_sqlite_path):
        rows = run_json(
            capsys,
            "gnucash",
            "transactions",
            gnucash_sqlite_path.path,
            "--start",
            "2026-01-20",
            "--end",
            "2026-01-31",
        )
        assert [r["description"] for r in rows] == ["Payroll deposit"]

    def test_an_xml_book_is_directed_to_import(self, gnucash_xml_path):
        assert main(["gnucash", "accounts", gnucash_xml_path.path]) == 2


class TestImport:
    def test_detects_the_format_and_imports(self, capsys, book_path, gnucash_sqlite_path):
        run(capsys, "init", book_path)
        code, out = run(capsys, "import", book_path, gnucash_sqlite_path.path)
        assert code == 0
        assert "3 transactions" in out
        db = DbSQLite()
        db.load(book_path)
        try:
            assert remembered_import_source(db) == str(Path(gnucash_sqlite_path.path).resolve())
        finally:
            db.close()

    def test_imports_a_compressed_xml_book(self, capsys, book_path, gnucash_xml_path):
        run(capsys, "init", book_path)
        result = run_json(capsys, "import", book_path, gnucash_xml_path.path)
        assert result["transactions"] == 2

    def test_an_unreadable_file_is_a_clean_error(self, book_path, tmp_path, capsys):
        run(capsys, "init", book_path)
        junk = tmp_path / "notes.txt"
        junk.write_text("hello")
        assert main(["import", book_path, str(junk)]) == 2

    def test_plugins_are_listed(self, capsys):
        rows = run_json(capsys, "plugins")
        assert {"gnucash-sqlite", "gnucash-xml"} <= {r["id"] for r in rows}


class TestPlanningAndProjection:
    @pytest.fixture
    def planned(self, capsys, book_path, gnucash_sqlite_path):
        run(capsys, "init", book_path)
        run(capsys, "import", book_path, gnucash_sqlite_path.path)
        run(
            capsys,
            "estimate",
            book_path,
            "add",
            "--name",
            "Salary",
            "--account",
            "Income:Salary",
            "--funded-from",
            "Assets:Checking Account",
            "--amount",
            "-4200.00",
            "--every",
            "month",
            "--start",
            "2026-01-01",
        )
        run(
            capsys,
            "estimate",
            book_path,
            "add",
            "--name",
            "Rent",
            "--account",
            "Expenses:Rent",
            "--funded-from",
            "Assets:Checking Account",
            "--amount",
            "1800.00",
            "--every",
            "month",
            "--start",
            "2026-01-01",
        )
        return book_path

    def test_projection_summarises_by_year(self, capsys, planned):
        result = run_json(
            capsys,
            "project",
            planned,
            "--years",
            "3",
            "--start",
            "2026-01-01",
            "--income-growth",
            "0",
            "--inflation",
            "0",
            "--investment-return",
            "0",
            "--cash-interest",
            "0",
        )
        assert result["summary"]["months"] == 36
        assert result["summary"]["total_income"] == "151200.00"

    def test_projection_writes_csv(self, capsys, planned, tmp_path):
        out_file = tmp_path / "projection.csv"
        code, out = run(
            capsys,
            "project",
            planned,
            "--years",
            "2",
            "--csv",
            str(out_file),
        )
        assert code == 0
        lines = out_file.read_text().splitlines()
        assert lines[0].startswith("month,cash_open")
        assert len(lines) == 25

    def test_monthly_view_prints_every_month(self, capsys, planned):
        code, out = run(
            capsys,
            "project",
            planned,
            "--years",
            "1",
            "--monthly",
            "--start",
            "2026-01-01",
        )
        assert code == 0
        assert "Jan 2026" in out and "Dec 2026" in out

    def test_activity_reports_periods_without_making_them_the_plan(self, capsys, book_path):
        run(capsys, "init", book_path)
        run(
            capsys,
            "add",
            book_path,
            "--date",
            "2026-01-05",
            "--description",
            "Unexpected purchase",
            "--from",
            "Assets",
            "--to",
            "Expenses",
            "--amount",
            "25.00",
        )
        posted = run_json(capsys, "register", book_path, "Assets")[0]
        run(capsys, "plan-unexpected", book_path, posted["handle"])
        result = run_json(
            capsys,
            "activity",
            book_path,
            "--start",
            "2026-01-01",
            "--end",
            "2026-03-31",
            "--period",
            "quarter",
        )
        assert result["period"] == "quarter"
        assert len(result["periods"]) == 1
        assert result["actual_amount"] == "25.00"
        assert result["unexpected_count"] == 1

    def test_plan_resolution_commands_preserve_user_decisions(self, capsys, book_path):
        from datetime import date

        from breadsched.gen.db.sqlite import DbSQLite
        from breadsched.gen.lib import (
            Money,
            PeriodType,
            Recurrence,
            ScheduledSplit,
            ScheduledTransaction,
        )

        run(capsys, "init", book_path)
        db = DbSQLite()
        db.load(book_path)
        try:
            assets = db.get_account_by_name("Assets")
            expenses = db.get_account_by_name("Expenses")
            assert assets is not None and expenses is not None
            bill = ScheduledTransaction(
                name="Estimated bill",
                recurrence=Recurrence(PeriodType.ONCE, start=date(2026, 1, 5)),
                splits=[
                    ScheduledSplit(expenses.handle, Money("25.00")),
                    ScheduledSplit(assets.handle, Money("-25.00")),
                ],
            )
            with db.transaction("plan") as txn:
                db.add_scheduled(bill, txn)
        finally:
            db.close()

        unresolved = run_json(
            capsys,
            "plan-unresolved",
            book_path,
            "--start",
            "2026-01-01",
            "--end",
            "2026-01-31",
        )
        occurrence = unresolved[0]["key"]

        run(
            capsys,
            "add",
            book_path,
            "--date",
            "2026-01-06",
            "--description",
            "Actual bill",
            "--from",
            "Assets",
            "--to",
            "Expenses",
            "--amount",
            "27.00",
        )
        posted = run_json(capsys, "register", book_path, "Assets")[0]

        matches = run_json(capsys, "plan-matches", book_path, posted["handle"])
        assert matches[0]["occurrence"]["key"] == occurrence

        run(capsys, "plan-reject", book_path, posted["handle"], occurrence)
        assert run_json(capsys, "plan-matches", book_path, posted["handle"]) == []

        resolved = run_json(capsys, "plan-resolve", book_path, posted["handle"], occurrence)
        assert resolved["resolution"] == "matched"
        assert resolved["occurrence"] == occurrence
        assert (
            run_json(
                capsys,
                "plan-unresolved",
                book_path,
                "--start",
                "2026-01-01",
                "--end",
                "2026-01-31",
            )
            == []
        )


class TestScenarios:
    @pytest.fixture
    def planned(self, capsys, book_path, gnucash_sqlite_path):
        run(capsys, "init", book_path)
        run(capsys, "import", book_path, gnucash_sqlite_path.path)
        run(
            capsys,
            "estimate",
            book_path,
            "add",
            "--name",
            "Salary",
            "--account",
            "Income:Salary",
            "--funded-from",
            "Assets:Checking Account",
            "--amount",
            "-4200.00",
            "--every",
            "month",
            "--start",
            "2026-01-01",
        )
        run(
            capsys,
            "estimate",
            book_path,
            "add",
            "--name",
            "Rent",
            "--account",
            "Expenses:Rent",
            "--funded-from",
            "Assets:Checking Account",
            "--amount",
            "1800.00",
            "--every",
            "month",
            "--start",
            "2026-01-01",
        )
        return book_path

    def test_save_and_list(self, capsys, planned):
        run(
            capsys,
            "scenario",
            planned,
            "save",
            "--name",
            "Base",
            "--years",
            "10",
            "--income-growth",
            "0.03",
        )
        rows = run_json(capsys, "scenario", planned, "list")
        assert rows[0]["name"] == "Base"
        assert rows[0]["years"] == 10

    def test_saving_twice_updates_rather_than_duplicates(self, capsys, planned):
        run(capsys, "scenario", planned, "save", "--name", "Base", "--years", "5")
        run(capsys, "scenario", planned, "save", "--name", "Base", "--years", "8")
        rows = run_json(capsys, "scenario", planned, "list")
        assert len(rows) == 1 and rows[0]["years"] == 8

    def test_projecting_from_a_saved_scenario(self, capsys, planned):
        run(
            capsys,
            "scenario",
            planned,
            "save",
            "--name",
            "Base",
            "--years",
            "4",
        )
        result = run_json(capsys, "project", planned, "--scenario", "Base")
        assert result["summary"]["scenario"] == "Base"
        assert result["summary"]["months"] == 48

    def test_comparing_two_scenarios(self, capsys, planned):
        run(
            capsys,
            "scenario",
            planned,
            "save",
            "--name",
            "Careful",
            "--years",
            "5",
            "--inflation",
            "0.06",
        )
        run(
            capsys,
            "scenario",
            planned,
            "save",
            "--name",
            "Hopeful",
            "--years",
            "5",
            "--income-growth",
            "0.06",
        )
        rows = run_json(capsys, "compare", planned, "Careful", "Hopeful")
        assert len(rows) == 5
        assert float(rows[-1]["net_worth_delta"]) > 0

    def test_deleting_a_scenario(self, capsys, planned):
        run(capsys, "scenario", planned, "save", "--name", "Temp")
        run(capsys, "scenario", planned, "delete", "--name", "Temp")
        assert run_json(capsys, "scenario", planned, "list") == []

    def test_an_unknown_scenario_is_a_clean_error(self, planned):
        assert main(["project", planned, "--scenario", "Ghost"]) == 2


class TestScheduledCommands:
    def test_lists_and_posts_due_occurrences(self, capsys, tmp_path, db, book_path):
        # Build a book with a schedule through the API, then drive the CLI over it.
        from datetime import date

        from breadsched.gen.db.sqlite import DbSQLite
        from breadsched.gen.lib import (
            Account,
            AccountType,
            Money,
            PeriodType,
            Recurrence,
            ScheduledSplit,
            ScheduledTransaction,
        )

        database = DbSQLite()
        database.load(book_path)
        with database.transaction("Set up") as txn:
            root = Account(name="Root", atype=AccountType.ROOT)
            database.add_account(root, txn)
            bank = Account(name="Checking", atype=AccountType.BANK, parent=root.handle)
            wages = Account(name="Wages", atype=AccountType.INCOME, parent=root.handle)
            database.add_account(bank, txn)
            database.add_account(wages, txn)
            database.add_scheduled(
                ScheduledTransaction(
                    name="Payday",
                    recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 1, 1)),
                    splits=[
                        ScheduledSplit(bank.handle, Money("3000.00")),
                        ScheduledSplit(wages.handle, Money("-3000.00")),
                    ],
                    auto_create=True,
                ),
                txn,
            )
        database.close()

        due = run_json(capsys, "scheduled", book_path, "--as-of", "2026-03-15", "--days", "0")
        assert [d["date"] for d in due] == ["2026-01-01", "2026-02-01", "2026-03-01"]

        code, out = run(capsys, "scheduled", book_path, "--as-of", "2026-03-15", "--post")
        assert "Posted 3" in out
        assert run_json(capsys, "balance", book_path, "Checking")["balance"] == "9000.00"

        again = run_json(capsys, "scheduled", book_path, "--as-of", "2026-03-15", "--days", "0")
        assert again == []


class TestVerify:
    def test_verify_clean_book(self, tmp_path, capsys):
        path = tmp_path / "clean.cp"
        assert main(["init", str(path)]) == 0
        capsys.readouterr()
        assert main(["verify", str(path)]) == 0
        assert "verification passed" in capsys.readouterr().out.lower()

    def test_verify_json_reports_logical_damage(self, tmp_path, capsys):
        import json
        import sqlite3

        path = tmp_path / "damaged.cp"
        assert main(["init", str(path)]) == 0
        capsys.readouterr()
        conn = sqlite3.connect(path)
        try:
            row = conn.execute(
                "SELECT handle, blob FROM account WHERE parent IS NOT NULL LIMIT 1"
            ).fetchone()
            data = json.loads(row[1])
            data["parent"] = "missing-parent"
            conn.execute(
                "UPDATE account SET parent=?, blob=? WHERE handle=?",
                ("missing-parent", json.dumps(data, separators=(",", ":")), row[0]),
            )
            conn.commit()
        finally:
            conn.close()

        assert main(["verify", str(path), "--json"]) == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is False
        assert any(issue["code"] == "account.missing_parent" for issue in payload["issues"])

    def test_verify_reports_malformed_objects_instead_of_crashing(self, tmp_path, capsys):
        import json
        import sqlite3

        path = tmp_path / "malformed.cp"
        assert main(["init", str(path)]) == 0
        capsys.readouterr()
        conn = sqlite3.connect(path)
        try:
            row = conn.execute("SELECT handle FROM account LIMIT 1").fetchone()
            conn.execute("UPDATE account SET blob='{bad-json' WHERE handle=?", (row[0],))
            conn.commit()
        finally:
            conn.close()

        assert main(["verify", str(path), "--json"]) == 1
        payload = json.loads(capsys.readouterr().out)
        assert any(issue["code"] == "account.malformed" for issue in payload["issues"])


class TestBackupAndRestore:
    def test_backup_and_restore_round_trip(self, capsys, book_path, tmp_path):
        run(capsys, "init", book_path)
        run(
            capsys,
            "add",
            book_path,
            "--date",
            "2026-01-01",
            "--description",
            "Opening",
            "--from",
            "Equity",
            "--to",
            "Assets",
            "--amount",
            "1234.00",
        )
        backup = tmp_path / "household.backup"
        result = run_json(capsys, "backup", book_path, backup)
        assert result["backup"] == str(backup)
        assert backup.exists()

        restored = tmp_path / "restored.breadsched"
        result = run_json(capsys, "restore", backup, restored)
        assert result["book"] == str(restored)
        balance = run_json(capsys, "balance", restored, "Assets")
        assert balance["balance"] == "1234.00"

    def test_restore_refuses_to_overwrite_without_flag(self, capsys, book_path, tmp_path):
        run(capsys, "init", book_path)
        backup = tmp_path / "book.backup"
        run(capsys, "backup", book_path, backup)
        destination = tmp_path / "existing.breadsched"
        run(capsys, "init", destination)
        assert main(["restore", str(backup), str(destination)]) == 2

    def test_overwriting_restore_preserves_previous_book(self, capsys, book_path, tmp_path):
        run(capsys, "init", book_path)
        backup = tmp_path / "book.backup"
        run(capsys, "backup", book_path, backup)
        destination = tmp_path / "existing.breadsched"
        run(capsys, "init", destination)

        code, _ = run(capsys, "restore", backup, destination, "--overwrite")
        assert code == 0
        assert Path(str(destination) + ".pre-restore.bak").exists()
