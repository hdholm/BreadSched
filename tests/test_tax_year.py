"""Tax-year outputs: gains by term, tax-relevant accounts and tags, income by source."""

from __future__ import annotations

import json
from datetime import date

import pytest
from test_specific_lots import _fund, _trade

from breadsched.gen.engine.tax_year import (
    TAX_TAGS_KEY,
    GainTerm,
    gnucash_tax_related,
    holding_term,
    is_tax_relevant,
    tax_year,
    tax_years,
)
from breadsched.gen.lib import Account, AccountType, LotPick, Money, Split, Transaction
from breadsched.gen.lib.account import GnuCashAccountField
from breadsched.gen.services import SetTaxMarks, set_tax_marks, tax_marks


def _spend(db, book, when, account, amount, *, tags=(), income=False, currency=None):
    txn = Transaction(post_date=when, description="Entry")
    txn.currency = currency
    value = Money(amount)
    txn.splits = [
        Split(account, -value if income else value),
        Split(book.checking, value if income else -value),
    ]
    txn.tags = list(tags)
    with db.transaction("Entry") as handle:
        db.add_transaction(txn, handle)
    return txn


def _mark(db, handle, mark):
    account = db.get_account(handle)
    account.tax_relevant_override = mark
    with db.transaction("Mark") as txn:
        db.commit_account(account, txn)


class TestHoldingTerm:
    @pytest.mark.parametrize(
        ("acquired", "sold", "term"),
        [
            (date(2024, 1, 5), date(2025, 1, 5), GainTerm.SHORT),  # exactly one year
            (date(2024, 1, 5), date(2025, 1, 6), GainTerm.LONG),  # more than a year
            (date(2024, 2, 29), date(2025, 2, 28), GainTerm.SHORT),
            (date(2024, 2, 29), date(2025, 3, 1), GainTerm.LONG),
            (date(2024, 6, 1), date(2024, 6, 1), GainTerm.SHORT),
        ],
    )
    def test_long_term_only_after_the_anniversary(self, acquired, sold, term):
        assert holding_term(acquired, sold) is term


class TestGainsByTerm:
    def test_a_sale_splits_into_short_and_long_parts_that_add_up(self, db, book):
        account, _fund_commodity, usd = _fund(db, book)
        _trade(db, account, usd, book.checking, date(2024, 3, 1), "10", "1000")
        _trade(db, account, usd, book.checking, date(2025, 2, 1), "20", "3000")
        # Sells all 10 old shares and 5 of the new: 15 shares for 2000.00.
        _trade(db, account, usd, book.checking, date(2025, 6, 2), "-15", "-2000")

        report = tax_year(db, 2025)
        long, short = sorted(report.gains, key=lambda line: line.term.value)
        assert (long.term, long.acquired, long.quantity, long.cost) == (
            GainTerm.LONG,
            date(2024, 3, 1),
            Money(10),
            Money(1000),
        )
        assert (short.term, short.acquired, short.quantity, short.cost) == (
            GainTerm.SHORT,
            date(2025, 2, 1),
            Money(5),
            Money(750),
        )
        # 5 of 15 shares: 666.67 short-term to the cent, the rest long-term.
        assert short.proceeds == Money("666.67")
        assert long.proceeds + short.proceeds == Money(2000)
        assert {total.term: total.gain for total in report.gain_totals} == {
            GainTerm.SHORT: Money("666.67") - Money(750),
            GainTerm.LONG: Money("1333.33") - Money(1000),
        }
        assert not report.problems
        assert tax_year(db, 2024).gains == ()

    def test_named_lots_decide_the_term(self, db, book):
        account, _fund_commodity, usd = _fund(db, book)
        old = _trade(db, account, usd, book.checking, date(2023, 1, 10), "10", "1000")
        _trade(db, account, usd, book.checking, date(2025, 1, 10), "10", "2000")
        _trade(
            db,
            account,
            usd,
            book.checking,
            date(2025, 3, 1),
            "-10",
            "-2500",
            picks=[LotPick(old.handle, Money(10))],
        )
        [line] = tax_year(db, 2025).gains
        assert (line.term, line.cost, line.proceeds) == (GainTerm.LONG, Money(1000), Money(2500))

    def test_several_purchase_dates_in_one_part_are_various(self, db, book):
        account, _fund_commodity, usd = _fund(db, book)
        _trade(db, account, usd, book.checking, date(2025, 1, 10), "5", "500")
        _trade(db, account, usd, book.checking, date(2025, 2, 10), "5", "600")
        _trade(db, account, usd, book.checking, date(2025, 3, 10), "-10", "-1200")
        [line] = tax_year(db, 2025).gains
        assert line.acquired is None and line.term is GainTerm.SHORT
        assert line.as_dict("USD")["acquired"] is None

    def test_shares_beyond_the_purchases_are_named_not_counted(self, db, book):
        account, _fund_commodity, usd = _fund(db, book)
        _trade(db, account, usd, book.checking, date(2025, 1, 10), "5", "500")
        _trade(db, account, usd, book.checking, date(2025, 3, 10), "-10", "-1200")
        report = tax_year(db, 2025)
        [line] = report.gains
        assert (line.quantity, line.proceeds) == (Money(5), Money(600))
        assert any("term are unknown" in problem for problem in report.problems)


class TestTaxRelevantTotals:
    def test_marked_account_totals_include_those_beneath_it(self, db, book):
        medical = Account(name="Medical", atype=AccountType.EXPENSE, parent=book.expenses)
        with db.transaction("Medical") as txn:
            db.add_account(medical, txn)
        doctor = Account(name="Doctor", atype=AccountType.EXPENSE, parent=medical.handle)
        with db.transaction("Doctor") as txn:
            db.add_account(doctor, txn)
        _mark(db, medical.handle, True)
        _spend(db, book, date(2025, 2, 1), medical.handle, "40")
        _spend(db, book, date(2025, 3, 1), doctor.handle, "60")
        _spend(db, book, date(2024, 12, 31), doctor.handle, "1000")  # another year
        _spend(db, book, date(2025, 3, 1), book.rent, "900")  # not marked

        [total] = tax_year(db, 2025).accounts
        assert (total.full_name, total.amount, total.transactions, total.marked_by) == (
            "Expenses:Medical",
            Money(100),
            2,
            "BreadSched",
        )

    def test_gnucash_mark_applies_until_breadsched_overrides_it(self, db, book):
        account = db.get_account(book.salary)
        account.source_fields = [GnuCashAccountField("slot:tax-related", "int64", "1")]
        with db.transaction("Imported mark") as txn:
            db.commit_account(account, txn)
        assert gnucash_tax_related(account) and is_tax_relevant(account)
        _spend(db, book, date(2025, 1, 31), book.salary, "5000", income=True)
        [total] = tax_year(db, 2025).accounts
        assert (total.amount, total.marked_by) == (Money(5000), "GnuCash")

        result = set_tax_marks(db, SetTaxMarks(accounts={book.salary: False}))
        assert result.ok
        assert tax_year(db, 2025).accounts == ()
        assert set_tax_marks(db, SetTaxMarks(accounts={book.salary: None})).ok
        assert len(tax_year(db, 2025).accounts) == 1

    def test_tagged_transactions_total_what_they_spent_and_received(self, db, book):
        db.set_metadata(TAX_TAGS_KEY, ["Charity"])
        _spend(db, book, date(2025, 4, 1), book.groceries, "25", tags=["charity"])
        _spend(db, book, date(2025, 5, 1), book.utilities, "75", tags=["Charity", "Home"])
        _spend(db, book, date(2025, 6, 1), book.salary, "10", tags=["Charity"], income=True)
        _spend(db, book, date(2025, 6, 1), book.rent, "500", tags=["Home"])
        [total] = tax_year(db, 2025).tags
        assert (total.tag, total.spent, total.received, total.transactions) == (
            "Charity",
            Money(100),
            Money(10),
            3,
        )

    def test_a_marked_tag_without_transactions_still_shows(self, db, book):
        db.set_metadata(TAX_TAGS_KEY, ["Medical"])
        [total] = tax_year(db, 2025).tags
        assert (total.tag, total.spent, total.transactions) == ("Medical", Money(0), 0)


class TestIncomeBySource:
    def test_each_income_account_is_a_source(self, db, book):
        bonus = Account(name="Bonus", atype=AccountType.INCOME, parent=book.income)
        with db.transaction("Bonus") as txn:
            db.add_account(bonus, txn)
        _spend(db, book, date(2025, 1, 31), book.salary, "5000", income=True)
        _spend(db, book, date(2025, 2, 28), book.salary, "5000", income=True)
        _spend(db, book, date(2025, 12, 15), bonus.handle, "1200", income=True)
        _spend(db, book, date(2026, 1, 1), bonus.handle, "999", income=True)
        report = tax_year(db, 2025)
        assert [(item.full_name, item.amount, item.transactions) for item in report.income] == [
            ("Income:Bonus", Money(1200), 1),
            ("Income:Salary", Money(10000), 2),
        ]
        assert report.income_totals == ((None, Money(11200)),)
        assert tax_years(db) == (2026, 2025)


class TestTaxMarksService:
    def test_marks_list_accounts_and_tags(self, db, book):
        _spend(db, book, date(2025, 4, 1), book.groceries, "25", tags=["Charity"])
        result = set_tax_marks(db, SetTaxMarks(tags={"charity": True, "Medical": True}))
        assert result.ok
        marks = tax_marks(db)
        assert marks.tags == (("Charity", True), ("Medical", True))
        assert db.get_metadata(TAX_TAGS_KEY) == ["Charity", "Medical"]
        assert set_tax_marks(db, SetTaxMarks(tags={"Medical": False})).ok
        assert tax_marks(db).tags == (("Charity", True),)
        names = [mark.full_name for mark in marks.accounts]
        assert "Expenses:Rent" in names and "Root" not in names

    def test_one_request_is_one_undo_step(self, db, book):
        before = db.undo_message()
        result = set_tax_marks(
            db, SetTaxMarks(accounts={book.rent: True, book.salary: True}, tags={"Home": True})
        )
        assert result.ok and db.undo_message() == "Change tax marks"
        db.undo()
        assert db.undo_message() == before
        assert db.get_account(book.rent).tax_relevant_override is None
        assert db.get_metadata(TAX_TAGS_KEY, []) == []

    def test_a_rejected_request_changes_nothing(self, db, book):
        result = set_tax_marks(
            db,
            SetTaxMarks(accounts={book.rent: True, "missing": True}, tags={"a,b": True}),
        )
        assert not result.ok
        assert {error.code for error in result.errors} == {"tax.account.not_found", "tag.invalid"}
        assert db.get_account(book.rent).tax_relevant_override is None
        assert db.get_metadata(TAX_TAGS_KEY, []) == []
        assert not set_tax_marks(db, SetTaxMarks(accounts={book.root: True})).ok

    def test_the_mark_is_stored_only_when_set_and_survives_reimport(self, db, book):
        rent = db.get_account(book.rent)
        assert "tax_relevant" not in rent.serialize()
        assert set_tax_marks(db, SetTaxMarks(accounts={book.rent: False})).ok
        stored = db.get_account(book.rent)
        assert stored.serialize()["tax_relevant"] is False
        assert (
            Account.from_dict(json.loads(json.dumps(stored.serialize()))).tax_relevant_override
            is False
        )

        from breadsched.plugins.importer.gnucash_common import ImportSink

        incoming = Account.from_dict(stored.serialize())
        incoming.tax_relevant_override = None
        ImportSink._preserve_breadsched_account_state(incoming, stored)
        assert incoming.tax_relevant_override is False


def test_the_layout_prints_every_section(db, book):
    from breadsched.gen.engine.tax_year import currency_labels
    from breadsched.plugins.export.report_layout import tax_year_layout

    account, _fund_commodity, usd = _fund(db, book)
    _trade(db, account, usd, book.checking, date(2024, 1, 10), "10", "1000")
    _trade(db, account, usd, book.checking, date(2025, 3, 10), "-10", "-1500")
    _spend(db, book, date(2025, 1, 31), book.salary, "5000", income=True)
    db.set_metadata(TAX_TAGS_KEY, ["Charity"])
    _mark(db, book.rent, True)
    report = tax_year(db, 2025)
    document = tax_year_layout(report, currency_labels(db, report))
    assert (document.title, document.kind) == ("Tax year 2025", "tax-year")
    text = document.text()
    for expected in (
        "Realized gains by term",
        "Long-term",
        "2024-01-10",
        "Tax-relevant accounts",
        "Expenses:Rent",
        "Tax-relevant tags",
        "Charity",
        "Income by source",
        "Income:Salary",
        "5,000.00",
        "not tax advice",
    ):
        assert expected in text, expected


def test_the_command_line_reports_and_marks(tmp_path, capsys):
    from types import SimpleNamespace

    from breadsched.cli.main import main as cli
    from breadsched.gen.db.sqlite import DbSQLite

    path = tmp_path / "tax.breadsched"
    assert cli(["init", str(path)]) == 0
    db = DbSQLite()
    db.load(str(path))
    try:
        accounts = {account.name: account for account in db.iter_accounts()}
        cash = Account(name="Cash", atype=AccountType.BANK, parent=accounts["Assets"].handle)
        with db.transaction("Cash") as txn:
            db.add_account(cash, txn)
        holder = SimpleNamespace(assets=accounts["Assets"].handle, checking=cash.handle)
        account, _fund_commodity, usd = _fund(db, holder)
        _trade(db, account, usd, cash.handle, date(2024, 1, 10), "10", "1000")
        _trade(db, account, usd, cash.handle, date(2025, 3, 10), "-10", "-1500")
        expense = next(a for a in db.iter_accounts() if a.account_class.value == "expense")
        expense_name = db.full_name(expense)
        gift = Transaction(post_date=date(2025, 5, 1), description="Gift")
        gift.splits = [Split(expense.handle, Money(50)), Split(cash.handle, Money(-50))]
        gift.tags = ["Charity"]
        with db.transaction("Gift") as txn:
            db.add_transaction(gift, txn)
    finally:
        db.close()
    capsys.readouterr()

    assert cli(["tax-year", str(path), "--year", "2025", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    [total] = report["gain_totals"]
    assert (total["term"], total["gain"]) == ("long", "500.00")
    assert report["accounts"] == [] and report["tags"] == []

    assert (
        cli(["tax-marks", str(path), "--account", f"{expense_name}=on", "--tag", "charity=on"]) == 0
    )
    text = capsys.readouterr().out
    assert f"{expense_name}: yes (marked by BreadSched)" in text
    assert "Tax-relevant tags: Charity" in text

    assert cli(["tax-year", str(path), "--year", "2025"]) == 0
    text = capsys.readouterr().out
    assert "Long-term" in text and "Charity" in text and expense_name in text

    assert cli(["tax-marks", str(path), "--account", f"{expense_name}=maybe"]) == 2
    assert "ACCOUNT=on|off|gnucash" in capsys.readouterr().err
    assert cli(["tax-marks", str(path), "--tag", "a,b=on"]) == 2
    assert "comma" in capsys.readouterr().err
    assert cli(["tax-marks", str(path), "--account", f"{expense_name}=gnucash", "--json"]) == 0
    marks = json.loads(capsys.readouterr().out)
    assert marks["accounts"] == []
    assert marks["tags"] == [{"tag": "Charity", "relevant": True}]
