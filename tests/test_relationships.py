"""Account relationships, inference, and scheduled planning events.

An imported book states what happened, not what it means. A mortgage never names
the house it bought; a card's due date exists only as a pattern in its payments.
Both are inferred here — and offered rather than applied, because a wrong guess
that announces itself costs a moment while one applied quietly becomes a figure
somebody trusts.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from breadsched.cli.main import main as cli
from breadsched.gen.db.sqlite import DbSQLite
from breadsched.gen.engine import inference
from breadsched.gen.lib import Money


@pytest.fixture
def book_path(tmp_path, capsys):
    path = tmp_path / "book.breadsched"
    cli(["init", str(path)])
    for name, kind, parent in (
        ("Home Easton", "ASSET", "Assets"),
        ("Mortgage Easton", "LIABILITY", "Liabilities"),
        ("Visa", "CREDIT CARD", "Liabilities"),
        ("Checking", "BANK", "Assets"),
    ):
        cli(["account", str(path), "add", "--name", name, "--type", kind, "--parent", parent])
    cli(
        [
            "add",
            str(path),
            "--date",
            "2026-01-01",
            "--description",
            "Drawdown",
            "--from",
            "Liabilities:Mortgage Easton",
            "--to",
            "Assets:Home Easton",
            "--amount",
            "385938.50",
        ]
    )
    for month in ("03", "04", "05"):
        cli(
            [
                "add",
                str(path),
                "--date",
                f"2026-{month}-14",
                "--description",
                "Spend",
                "--from",
                "Liabilities:Visa",
                "--to",
                "Expenses",
                "--amount",
                "900.00",
            ]
        )
        cli(
            [
                "add",
                str(path),
                "--date",
                f"2026-{month}-22",
                "--description",
                "Payment",
                "--from",
                "Assets:Checking",
                "--to",
                "Liabilities:Visa",
                "--amount",
                "400.00",
            ]
        )
    capsys.readouterr()
    return path


def open_book(path) -> DbSQLite:
    db = DbSQLite()
    db.load(str(path))
    return db


class TestAccountManagement:
    def test_an_account_can_be_added(self, book_path, capsys):
        cli(
            [
                "account",
                str(book_path),
                "add",
                "--name",
                "Savings",
                "--type",
                "BANK",
                "--parent",
                "Assets",
            ]
        )
        capsys.readouterr()
        db = open_book(book_path)
        assert db.get_account_by_name("Assets:Savings") is not None
        db.close()

    def test_an_opening_balance_can_be_posted_with_it(self, book_path, capsys):
        from breadsched.gen.engine import ledger

        cli(
            [
                "account",
                str(book_path),
                "add",
                "--name",
                "Savings",
                "--type",
                "BANK",
                "--parent",
                "Assets",
                "--opening",
                "1500.00",
            ]
        )
        capsys.readouterr()
        db = open_book(book_path)
        handle = db.get_account_by_name("Assets:Savings").handle
        assert ledger.balance(db, handle) == Money("1500.00")
        db.close()

    def test_an_account_can_be_renamed_and_regrouped(self, book_path, capsys):
        cli(
            [
                "account",
                str(book_path),
                "edit",
                "--name",
                "Assets:Checking",
                "--rename",
                "Everyday",
                "--group",
                "Cash",
            ]
        )
        capsys.readouterr()
        db = open_book(book_path)
        account = db.get_account_by_name("Assets:Everyday")
        assert account is not None and account.group == "Cash"
        db.close()

    def test_an_account_with_history_cannot_be_removed(self, book_path):
        assert cli(["account", str(book_path), "remove", "--name", "Assets:Checking"]) == 2

    def test_an_unused_account_can_be_removed(self, book_path, capsys):
        cli(
            [
                "account",
                str(book_path),
                "add",
                "--name",
                "Spare",
                "--type",
                "BANK",
                "--parent",
                "Assets",
            ]
        )
        cli(["account", str(book_path), "remove", "--name", "Assets:Spare"])
        capsys.readouterr()
        db = open_book(book_path)
        assert db.get_account_by_name("Assets:Spare") is None
        db.close()

    def test_a_loan_can_name_the_asset_behind_it(self, book_path, capsys):
        cli(
            [
                "account",
                str(book_path),
                "edit",
                "--name",
                "Liabilities:Mortgage Easton",
                "--linked-asset",
                "Assets:Home Easton",
            ]
        )
        capsys.readouterr()
        db = open_book(book_path)
        loan = db.get_account_by_name("Liabilities:Mortgage Easton")
        assert loan.linked_asset == db.get_account_by_name("Assets:Home Easton").handle
        db.close()

    def test_a_card_records_how_it_is_used(self, book_path, capsys):
        cli(
            [
                "account",
                str(book_path),
                "edit",
                "--name",
                "Liabilities:Visa",
                "--carries-balance",
                "yes",
                "--usual-payment",
                "400.00",
                "--payment-day",
                "22",
            ]
        )
        capsys.readouterr()
        db = open_book(book_path)
        card = db.get_account_by_name("Liabilities:Visa")
        assert card.carries_balance is True
        assert card.usual_payment == Money("400.00")
        assert card.payment_day == 22
        db.close()

    def test_a_card_paid_in_full_is_not_a_carried_balance(self, book_path, capsys):
        db = open_book(book_path)
        card = db.get_account_by_name("Liabilities:Visa")
        assert card.pays_in_full is True
        assert card.carries_balance is False
        db.close()

    def test_listing_shows_the_relationships(self, book_path, capsys):
        cli(["account", str(book_path), "list", "--json"])
        rows = json.loads(capsys.readouterr().out)
        assert any(r["name"] == "Liabilities:Visa" for r in rows)
        assert all({"group", "linked_asset", "card"} <= set(r) for r in rows)


class TestInference:
    def test_a_mortgage_is_paired_with_the_asset_it_bought(self, book_path):
        db = open_book(book_path)
        try:
            links = inference.infer_asset_links(db)
            mortgage = db.get_account_by_name("Liabilities:Mortgage Easton")
            house = db.get_account_by_name("Assets:Home Easton")
            match = next(s for s in links if s.account == mortgage.handle)
            assert match.value == house.handle
            assert "transaction" in match.reason
        finally:
            db.close()

    def test_names_are_a_weaker_fallback_than_transactions(self, tmp_path, capsys):
        path = tmp_path / "names.breadsched"
        cli(["init", str(path)])
        cli(
            [
                "account",
                str(path),
                "add",
                "--name",
                "Home Evans",
                "--type",
                "ASSET",
                "--parent",
                "Assets",
            ]
        )
        cli(
            [
                "account",
                str(path),
                "add",
                "--name",
                "Mortgage Evans",
                "--type",
                "LIABILITY",
                "--parent",
                "Liabilities",
            ]
        )
        capsys.readouterr()

        db = open_book(path)
        try:
            links = inference.infer_asset_links(db)
            assert links, "a shared place name should still be noticed"
            assert links[0].confidence < 0.6, "a name match is not strong evidence"
        finally:
            db.close()

    def test_a_card_payment_day_comes_from_its_payments(self, book_path):
        db = open_book(book_path)
        try:
            found = inference.infer_card_settings(db)
            day = next(s for s in found if s.field == "payment_day")
            assert day.value == 22
            assert day.confidence > 0.9
        finally:
            db.close()

    def test_a_card_that_never_clears_is_marked_as_carrying(self, book_path):
        db = open_book(book_path)
        try:
            found = inference.infer_card_settings(db)
            carrying = next(s for s in found if s.field == "pays_in_full")
            assert carrying.value is False
        finally:
            db.close()

    def test_the_usual_payment_is_the_median(self, book_path):
        db = open_book(book_path)
        try:
            found = inference.infer_card_settings(db)
            usual = next(s for s in found if s.field == "usual_payment")
            assert usual.value == Money("400.00")
        finally:
            db.close()

    def test_nothing_changes_until_the_suggestions_are_applied(self, book_path, capsys):
        cli(["infer", str(book_path)])
        assert "Nothing is changed" in capsys.readouterr().out
        db = open_book(book_path)
        assert db.get_account_by_name("Liabilities:Mortgage Easton").linked_asset is None
        db.close()

    def test_applying_writes_them(self, book_path, capsys):
        cli(["infer", str(book_path), "--apply"])
        capsys.readouterr()
        db = open_book(book_path)
        try:
            loan = db.get_account_by_name("Liabilities:Mortgage Easton")
            card = db.get_account_by_name("Liabilities:Visa")
            assert loan.linked_asset is not None
            assert card.payment_day == 22
            assert card.carries_balance is True
        finally:
            db.close()

    def test_applying_is_a_single_undo_step(self, book_path):
        """Undo lives in the open book, so the apply and the undo share a session."""
        db = open_book(book_path)
        try:
            suggestions = inference.infer_all(db).suggestions
            assert inference.apply_suggestions(db, suggestions) == len(suggestions)
            assert db.get_account_by_name("Liabilities:Visa").payment_day == 22

            assert db.undo() is True
            assert db.get_account_by_name("Liabilities:Visa").payment_day is None
        finally:
            db.close()

    def test_a_carried_balance_becomes_a_scheduled_estimate(self, book_path, capsys):
        cli(["infer", str(book_path), "--apply"])
        capsys.readouterr()
        db = open_book(book_path)
        try:
            card = db.get_account_by_name("Liabilities:Visa")
            estimate = inference.card_estimate(db, card)
            assert estimate is not None
            assert estimate.placeholder is True
            assert estimate.amount() == Money("400.00")
        finally:
            db.close()

    def test_a_card_paid_in_full_gets_no_estimate(self, book_path):
        db = open_book(book_path)
        try:
            card = db.get_account_by_name("Liabilities:Visa")
            card.pays_in_full = True
            assert inference.card_estimate(db, card) is None
        finally:
            db.close()


@pytest.fixture
def planned(book_path, capsys):
    """A book with one monthly estimate."""
    cli(
        [
            "estimate",
            str(book_path),
            "add",
            "--name",
            "Groceries",
            "--account",
            "Expenses",
            "--funded-from",
            "Assets:Checking",
            "--amount",
            "600.00",
            "--every",
            "month",
            "--start",
            "2026-01-01",
        ]
    )
    capsys.readouterr()
    return book_path


class TestBillFrequency:
    def test_the_cycle_reads_as_the_schedules_own_frequency(self, planned, capsys):
        from breadsched.gen.engine import dashboard

        db = open_book(planned)
        try:
            board = dashboard.build(db, as_of=date(2026, 6, 1))
            bill = next(b for b in board.bills if b.name == "Groceries")
            assert bill.frequency == "every month"
        finally:
            db.close()

    def test_a_quarterly_flow_says_so(self, book_path, capsys):
        from breadsched.gen.engine import dashboard

        cli(
            [
                "estimate",
                str(book_path),
                "add",
                "--name",
                "HOA",
                "--account",
                "Expenses",
                "--funded-from",
                "Assets:Checking",
                "--amount",
                "619.00",
                "--every",
                "month",
                "--interval",
                "3",
                "--start",
                "2026-01-01",
            ]
        )
        capsys.readouterr()
        db = open_book(book_path)
        try:
            board = dashboard.build(db, as_of=date(2026, 6, 1))
            bill = next(b for b in board.bills if b.name == "HOA")
            assert bill.frequency == "every 3 months"
            assert bill.monthly == Money("206.33")
        finally:
            db.close()

    def test_a_bill_carries_the_schedule_it_came_from(self, planned):
        from breadsched.gen.engine import dashboard

        db = open_book(planned)
        try:
            board = dashboard.build(db, as_of=date(2026, 6, 1))
            bill = next(b for b in board.bills if b.name == "Groceries")
            assert bill.schedule is not None
            assert db.get_scheduled(bill.schedule.handle) is not None
        finally:
            db.close()
