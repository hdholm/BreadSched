"""The CLI is the scripting interface, so it is tested as one.

Each test drives ``main()`` with an argv list and reads stdout, which is exactly
what a shell script or a cron job sees.
"""

import json
from datetime import date
from pathlib import Path

import pytest

from breadsched.cli.main import main
from breadsched.gen.db.sqlite import DbSQLite
from breadsched.gen.engine import valuation
from breadsched.gen.lib import (
    Account,
    AccountType,
    Amount,
    Commodity,
    CommodityPrice,
    Money,
    Split,
    Transaction,
)
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


def test_imported_security_and_unset_card_first_dashboard_cli(
    capsys, book_path, gnucash_household_path
):
    run(capsys, "init", book_path)
    run(capsys, "import", book_path, gnucash_household_path.path, "--no-infer")
    board = run_json(capsys, "dashboard", book_path, "--as-of", "2026-02-15")
    assert board["summary"]["net_worth"] == "109950.00"
    assert board["summary"]["required_liquid"] == "0.00"
    assert board["unavailable_reasons"]["next_income"] == "No scheduled income"
    assert board["unavailable_reasons"]["months_covered"] == "No committed outgoings"
    [note] = board["coverage_notes"]
    assert note.startswith("Card payments not set up: 1 credit card owes a balance")
    code, text = run(capsys, "dashboard", book_path, "--as-of", "2026-02-15")
    assert code == 0
    assert "Card payments not set up" in text
    assert "109,950.00" in text


def test_imported_first_dashboard_cli_preserves_setup_and_missing_quote(
    capsys, book_path, gnucash_sqlite_path
):
    run(capsys, "init", book_path)
    run(capsys, "import", book_path, gnucash_sqlite_path.path)
    command = ("dashboard", book_path, "--as-of", "2026-01-31")
    first = run_json(capsys, *command)
    assert first["summary"]["net_worth"] == "2274.50"
    assert "assets" not in first["unavailable_reasons"]
    assert first["summary"]["assets"] is not None
    assert any(item["name"] == "Monthly rent" for item in first["bills"])

    db = DbSQLite()
    db.load(book_path)
    try:
        assets = db.get_account_by_name("Assets")
        income = db.get_account_by_name("Income")
        assert assets is not None and income is not None
        euro = Commodity(namespace="CURRENCY", mnemonic="EUR", fullname="Euro")
        foreign_cash = Account(
            name="Foreign cash",
            atype=AccountType.BANK,
            parent=assets.handle,
            commodity=euro.handle,
        )
        entry = Transaction(post_date=date(2026, 1, 15), description="Foreign opening")
        entry.currency = euro.handle
        entry.splits = [Split(foreign_cash.handle, Money(10)), Split(income.handle, Money(-10))]
        with db.transaction("Foreign cash") as txn:
            db.add_commodity(euro, txn)
            db.add_account(foreign_cash, txn)
            db.add_transaction(entry, txn)
    finally:
        db.close()

    missing = run_json(capsys, *command)
    assert missing["summary"]["net_worth"] is None
    assert missing["summary"]["liquid"] is None
    assert missing["missing_quotes"] == [foreign_cash.handle]
    assert any(item["name"] == "Monthly rent" for item in missing["bills"])
    code, output = run(capsys, *command)
    assert code == 0
    assert "Missing reporting-currency quote" in output
    assert "Monthly rent" in output


def test_cli_manual_rate_keeps_exact_value_and_imported_quote(capsys, book_path):
    run(capsys, "init", book_path)
    db = DbSQLite()
    db.load(book_path)
    try:
        euro = Commodity(namespace="CURRENCY", mnemonic="EUR")
        usd = db.get_commodity_by_mnemonic("USD")
        assert usd is not None
        imported = CommodityPrice(
            commodity=euro.handle,
            currency=usd.handle,
            quote_date=date(2026, 1, 2),
            value=Money("1.1"),
            source="example-import",
        )
        with db.transaction("Fixture quote") as txn:
            db.add_commodity(euro, txn)
            db.add_price(imported, txn)
    finally:
        db.close()

    command = ("rate", book_path, "--from", "EUR", "--to", "USD", "--date", "2026-01-02")
    first = run_json(capsys, *command, "--value", "1.234567")
    assert first["rate"] == "1234567/1000000"
    assert first["source"] == "breadsched"
    updated = run_json(capsys, *command, "--value", "1.25")
    assert updated["handle"] == first["handle"]
    assert updated["rate"] == "5/4"
    for invalid in ("0", "-1", "not-a-number"):
        code = main([*command, "--value", invalid])
        assert code == 2

    db = DbSQLite()
    db.load(book_path)
    try:
        assert db.get_price(imported.handle) == imported
        quotes = list(db.iter_prices(commodity=euro.handle, currency=usd.handle))
        assert len(quotes) == 2
        assert valuation.convert_currency(
            db, Amount(Money(4), euro.handle), as_of=date(2026, 1, 2)
        ).amount == Amount(Money(5), usd.handle)
    finally:
        db.close()


def test_cli_rate_rejects_unknown_and_ambiguous_currency_without_write(capsys, book_path):
    run(capsys, "init", book_path)
    db = DbSQLite()
    db.load(book_path)
    try:
        first_euro = Commodity(namespace="CURRENCY", mnemonic="EUR")
        with db.transaction("Fixture duplicate code") as txn:
            db.add_commodity(first_euro, txn)
            db.add_commodity(Commodity(namespace="ISO4217", mnemonic="EUR"), txn)
    finally:
        db.close()
    for source in ("EUR", "UNKNOWN"):
        code = main(
            [
                "rate",
                book_path,
                "--from",
                source,
                "--to",
                "USD",
                "--date",
                "2026-01-02",
                "--value",
                "1.25",
            ]
        )
        assert code == 2
    db = DbSQLite()
    db.load(book_path)
    try:
        assert list(db.iter_prices()) == []
    finally:
        db.close()
    saved = run_json(
        capsys,
        "rate",
        book_path,
        "--from",
        first_euro.handle,
        "--to",
        "USD",
        "--date",
        "2026-01-02",
        "--value",
        "1.25",
    )
    assert saved["rate"] == "5/4"


def test_account_summary_cli_discloses_missing_currency_quote(capsys, book_path):
    run(capsys, "init", book_path)
    db = DbSQLite()
    db.load(book_path)
    try:
        assets = db.get_account_by_name("Assets")
        equity = db.get_account_by_name("Equity")
        assert assets is not None and equity is not None
        euro = Commodity(namespace="CURRENCY", mnemonic="EUR", fullname="Euro")
        account = Account(
            name="Foreign cash", atype=AccountType.BANK, parent=assets.handle, commodity=euro.handle
        )
        transaction = Transaction(post_date=date(2026, 1, 2), description="Opening cash")
        transaction.currency = euro.handle
        transaction.splits = [Split(account.handle, Money(10)), Split(equity.handle, Money(-10))]
        with db.transaction("Foreign cash") as txn:
            db.add_commodity(euro, txn)
            db.add_account(account, txn)
            db.add_transaction(transaction, txn)
    finally:
        db.close()

    summary = run_json(capsys, "balance", book_path)
    assert summary["net_worth"] is None
    assert summary["cash"] is None
    assert summary["net_worth_missing_quotes"] == [account.handle]
    rows = run_json(capsys, "accounts", book_path)
    foreign = next(row for row in rows if row["handle"] == account.handle)
    assert foreign["balance"] is None
    assert foreign["missing_quotes"] == [account.handle]

    db = DbSQLite()
    db.load(book_path)
    try:
        usd = db.get_commodity_by_mnemonic("USD")
        assert usd is not None
        with db.transaction("Reverse currency quote") as txn:
            db.add_price(
                CommodityPrice(
                    commodity=usd.handle,
                    currency=euro.handle,
                    quote_date=date(2026, 1, 3),
                    value=Money("0.5"),
                    source="sample-source",
                ),
                txn,
            )
    finally:
        db.close()
    quoted = next(
        row for row in run_json(capsys, "accounts", book_path) if row["handle"] == account.handle
    )
    assert quoted["conversion_path"] == "inverse"
    assert quoted["quote_source"] == "sample-source"
    assert quoted["quote_evidence"].endswith("sample-source · inverse rate")
    assert quoted["balance"] == "20.00"
    dated = next(
        row
        for row in run_json(capsys, "accounts", book_path, "--as-of", "2026-01-10")
        if row["handle"] == account.handle
    )
    assert dated["quote_age_days"] == 7
    code, shown = run(capsys, "accounts", book_path, "--as-of", "2026-01-10")
    assert code == 0
    # The quote date is shown; a relative age is not (#151).
    assert "2026-01-03" in shown
    assert "days old" not in shown


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
        assert "native schema 10" in out
        assert "supports 6–10" in out


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

    def test_edit_and_delete_use_the_shared_transaction_contract(self, capsys, stocked):
        rows = run_json(capsys, "register", stocked, "Assets")
        handle = rows[1]["handle"]

        edited = run_json(
            capsys,
            "edit",
            stocked,
            handle,
            "--description",
            "Adjusted rent",
            "--amount",
            "1750.00",
        )
        assert edited["description"] == "Adjusted rent"
        assert run_json(capsys, "balance", stocked, "Assets")["balance"] == "3250.00"

        deleted = run_json(capsys, "delete", stocked, handle)
        assert deleted == {"deleted": handle}
        assert len(run_json(capsys, "register", stocked, "Assets")) == 1

    def test_table_output_is_aligned_and_readable(self, capsys, stocked):
        code, out = run(capsys, "accounts", stocked)
        assert code == 0
        assert "Assets" in out and "3,200.00" in out

    def test_estimate_suggest_exposes_shared_structured_evidence(self, capsys, book_path):
        from datetime import date

        from breadsched.gen.lib import Account, AccountType, Transaction

        run(capsys, "init", book_path)
        db = DbSQLite()
        db.load(book_path)
        try:
            assets = db.get_account_by_name("Assets")
            expenses = db.get_account_by_name("Expenses")
            assert assets is not None and expenses is not None
            utilities = Account(name="Utilities", atype=AccountType.EXPENSE, parent=expenses.handle)
            with db.transaction("Historical estimator fixture") as txn:
                db.add_account(utilities, txn)
                for month in (1, 2, 3):
                    db.add_transaction(
                        Transaction.simple(
                            date(2026, month, 5),
                            "Utilities",
                            utilities.handle,
                            assets.handle,
                            "100",
                        ),
                        txn,
                    )
        finally:
            db.close()

        proposals = run_json(
            capsys,
            "estimate",
            book_path,
            "suggest",
            "--as-of",
            "2026-04-20",
            "--months",
            "3",
        )

        proposal = next(item for item in proposals if item["account"].endswith("Utilities"))
        assert proposal["amount"] == "100.00"
        assert proposal["evidence"]["selected_months"] == 3
        assert proposal["evidence"]["funding"]["selected_name"] == "Assets"
        assert proposal["evidence"]["confidence"]["score"] == proposal["confidence"]

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

    def test_accounts_json_includes_imported_provenance(
        self, capsys, book_path, gnucash_sqlite_path
    ):
        run(capsys, "init", book_path)
        run(capsys, "import", book_path, gnucash_sqlite_path.path)

        accounts = run_json(capsys, "accounts", book_path, "--all")
        checking = next(row for row in accounts if row["name"].endswith("Checking Account"))
        assert checking["source_guid"] == gnucash_sqlite_path.ids.checking
        assert checking["source_type"] == "BANK"
        assert checking["notes"] == ""
        assert checking["source_notes"] == "Generic account note"
        assert checking["source_fields"][0]["name"] == "account:non-standard-scu"

    def test_imported_source_owned_chart_fields_cannot_be_edited(
        self, capsys, book_path, gnucash_sqlite_path
    ):
        run(capsys, "init", book_path)
        run(capsys, "import", book_path, gnucash_sqlite_path.path)

        code, _out = run(
            capsys,
            "account",
            book_path,
            "edit",
            "--name",
            "Assets:Checking Account",
            "--rename",
            "Local rename",
        )

        assert code == 2
        code, _out = run(
            capsys,
            "account",
            book_path,
            "edit",
            "--name",
            "Assets:Checking Account",
            "--group",
            "Cash:Daily",
        )
        assert code == 0
        db = DbSQLite()
        db.load(book_path, mode="r")
        try:
            assert db.get_account_by_name("Assets:Checking Account") is not None
            assert db.get_account_by_name("Assets:Local rename") is None
            checking = db.get_account_by_name("Assets:Checking Account")
            assert checking is not None
            assert checking.group == "Cash:Daily"
        finally:
            db.close()

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

    def test_net_worth_reports_each_period_end_through_the_as_of_date(self, capsys, book_path):
        run(capsys, "sample", book_path, "--as-of", "2026-09-15")
        result = run_json(
            capsys,
            "net-worth",
            book_path,
            "--start",
            "2026-07-01",
            "--end",
            "2026-12-31",
            "--as-of",
            "2026-09-15",
        )
        assert [point["label"] for point in result["points"]] == [
            "Jul 2026",
            "Aug 2026",
            "Sep 2026",
        ]
        september = result["points"][-1]
        assert september["valued_on"] == "2026-09-15" and september["partial"] is True
        assert Money(september["net_worth"]) == Money(september["assets"]) - Money(
            september["debts"]
        )
        assert september["missing"] == []
        code, out = run(
            capsys,
            "net-worth",
            book_path,
            "--start",
            "2026-09-01",
            "--end",
            "2026-09-30",
            "--as-of",
            "2026-09-15",
        )
        assert code == 0 and "to date" in out and "net worth" in out
        code, _out = run(
            capsys, "net-worth", book_path, "--start", "2026-09-30", "--end", "2026-09-01"
        )
        assert code != 0

    def test_net_worth_change_lists_postings_and_exports_matching_totals(
        self, capsys, book_path, tmp_path
    ):
        run(capsys, "sample", book_path, "--as-of", "2026-09-15")
        arguments = ["--start", "2026-08-01", "--end", "2026-08-31", "--as-of", "2026-09-15"]
        result = run_json(capsys, "net-worth-change", book_path, *arguments)
        history = run_json(capsys, "net-worth", book_path, "--start", "2026-07-01", *arguments[2:])
        assert Money(result["change"]) == Money(history["points"][1]["change"])
        assert Money(result["posted"]) + Money(result["revaluation"]) == Money(result["change"])
        assert result["postings"] and result["missing"] == []
        exported = tmp_path / "change.csv"
        code, out = run(capsys, "net-worth-change", book_path, *arguments, "--csv", str(exported))
        assert code == 0 and "Market and exchange-rate changes" in out
        last = exported.read_text(encoding="utf-8").splitlines()[-1].split(",")
        assert last[1] == "Change" and Money(last[4]) == Money(result["change"])
        code, _out = run(
            capsys, "net-worth-change", book_path, "--start", "2026-09-30", "--end", "2026-09-01"
        )
        assert code != 0

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

    def test_activity_footer_stops_both_sides_at_the_as_of_date(self, capsys, book_path):
        from datetime import date

        from breadsched.gen.db.sqlite import DbSQLite
        from breadsched.gen.lib import Account, AccountType, Transaction

        run(capsys, "init", book_path)
        db = DbSQLite()
        db.load(book_path)
        try:
            assets = db.get_account_by_name("Assets")
            expenses = db.get_account_by_name("Expenses")
            assert assets is not None and expenses is not None
            with db.transaction("September") as txn:
                checking = Account(name="Checking", atype=AccountType.BANK, parent=assets.handle)
                db.add_account(checking, txn)
                for day, amount in ((5, "40.00"), (25, "100.00")):
                    db.add_transaction(
                        Transaction.simple(
                            date(2026, 9, day), "Spending", expenses.handle, checking.handle, amount
                        ),
                        txn,
                    )
        finally:
            db.close()
        argv = ("activity", book_path, "--start", "2026-09-01", "--end", "2026-09-30")
        code, out = run(capsys, *argv, "--as-of", "2026-09-15")
        assert code == 0, out
        assert (
            "Through 2026-09-15: planned cash 0.00, actual cash (40.00), variance (40.00)." in out
        )
        assert "Period columns include everything dated in each period" in out
        result = run_json(capsys, *argv, "--as-of", "2026-09-15")
        assert result["actual_cash_change"] == "-140.00"
        assert result["actual_cash_through_as_of"] == "-40.00"
        assert result["cash_variance_through_as_of"] == "-40.00"

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

    def test_reparenting_a_scenario_is_visible_in_the_listing(self, capsys, planned):
        run(capsys, "scenario", planned, "save", "--name", "Earlier retirement")
        run(capsys, "scenario", planned, "save", "--name", "Lower returns")

        result = run_json(
            capsys,
            "scenario",
            planned,
            "reparent",
            "--name",
            "Lower returns",
            "--parent",
            "Earlier retirement",
        )
        rows = run_json(capsys, "scenario", planned, "list")

        assert result["parent"] == "Earlier retirement"
        child = next(item for item in rows if item["name"] == "Lower returns")
        assert child["parent"] == "Earlier retirement"

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

    def test_a_drawdown_is_saved_listed_projected_and_removed(self, capsys, planned):
        run(
            capsys,
            "account",
            planned,
            "add",
            "--name",
            "IRA",
            "--type",
            "RETIREMENT",
            "--parent",
            "Assets",
            "--opening",
            "240000",
            "--opening-date",
            "2025-12-01",
        )
        run(
            capsys,
            "scenario",
            planned,
            "save",
            "--name",
            "Retire",
            "--years",
            "2",
            "--start",
            "2026-01-01",
            "--investment-return",
            "0",
            "--inflation",
            "0",
        )
        common = ("--scenario", "Retire")
        refused = main(
            [
                "drawdown",
                planned,
                "save",
                *common,
                "--account",
                "Assets:IRA",
                "--into",
                "Assets:Checking Account",
                "--start",
                "2026-01-01",
            ]
        )
        assert refused == 2
        saved = run_json(
            capsys,
            "drawdown",
            planned,
            "save",
            *common,
            "--account",
            "Assets:IRA",
            "--into",
            "Assets:Checking Account",
            "--start",
            "2026-01-01",
            "--annual-amount",
            "24000",
        )
        assert saved["account"] == "Assets:IRA" and saved["escalate"] is True
        rows = run_json(capsys, "drawdown", planned, "list", *common)
        assert [row["annual_amount"] for row in rows] == ["24000.00"]

        projected = run_json(capsys, "project", planned, "--scenario", "Retire", "--monthly")
        first = projected["rows"][0]
        assert first["retirement_distributions"] == "2000.00"

        run_json(
            capsys,
            "drawdown",
            planned,
            "save",
            *common,
            "--account",
            "Assets:IRA",
            "--into",
            "Assets:Checking Account",
            "--start",
            "2026-01-01",
            "--annual-rate",
            "4%",
            "--handle",
            saved["handle"][:8],
        )
        (row,) = run_json(capsys, "drawdown", planned, "list", *common)
        assert row["annual_rate"] == "0.04" and row["annual_amount"] is None
        run_json(capsys, "drawdown", planned, "remove", *common, "--handle", saved["handle"][:8])
        assert run_json(capsys, "drawdown", planned, "list", *common) == []


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
        output = capsys.readouterr().out.lower()
        assert "native schema 10" in output
        assert "verification passed" in output

    def test_verify_json_reports_build_and_schema_versions(self, tmp_path, capsys):
        import json

        from breadsched import __version__

        path = tmp_path / "versions.cp"
        assert main(["init", str(path)]) == 0
        capsys.readouterr()

        assert main(["verify", str(path), "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)

        assert payload["application_version"] == __version__
        assert payload["native_schema_version"] == 10
        assert payload["supported_schema_versions"] == {"minimum": 6, "maximum": 10}

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
