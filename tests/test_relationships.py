"""Account relationships, inference, and budgets that can differ from each other.

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
from breadsched.gen.engine import budgeting, inference
from breadsched.gen.lib import Money


@pytest.fixture
def book_path(tmp_path, capsys):
    path = tmp_path / "book.breadsched"
    cli(["init", str(path)])
    for name, kind, parent in (
        ("Home Easton", "ASSET", "Assets"),
        ("Mortgage Easton", "LIABILITY", "Liabilities"),
        ("Visa", "CREDIT", "Liabilities"),
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
def budgeted(book_path, capsys):
    """A book with one estimate and one budget built from it."""
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
    cli(["budget-new", str(book_path), "--name", "Base", "--start", "2026-01-01"])
    capsys.readouterr()
    return book_path


class TestMultipleBudgets:
    def test_a_budget_can_be_cloned(self, budgeted, capsys):
        cli(["budget-clone", str(budgeted), "Base", "Tighter"])
        capsys.readouterr()
        db = open_book(budgeted)
        try:
            assert {b.name for b in db.iter_budgets()} == {"Base", "Tighter"}
        finally:
            db.close()

    def test_the_clone_carries_the_figures_but_its_own_identity(self, budgeted, capsys):
        cli(["budget-clone", str(budgeted), "Base", "Tighter"])
        capsys.readouterr()
        db = open_book(budgeted)
        try:
            base = next(b for b in db.iter_budgets() if b.name == "Base")
            clone = next(b for b in db.iter_budgets() if b.name == "Tighter")
            assert clone.handle != base.handle
            assert clone.lines.keys() == base.lines.keys()
        finally:
            db.close()

    def test_editing_the_clone_leaves_the_original_alone(self, budgeted, capsys):
        cli(["budget-clone", str(budgeted), "Base", "Tighter"])
        capsys.readouterr()
        db = open_book(budgeted)
        try:
            clone = next(b for b in db.iter_budgets() if b.name == "Tighter")
            account = next(iter(clone.lines))
            before = next(b for b in db.iter_budgets() if b.name == "Base").amount(account, 0)
            clone.set_amount(account, 0, "1.00")
            with db.transaction("edit") as txn:
                db.commit_budget(clone, txn)
            after = next(b for b in db.iter_budgets() if b.name == "Base").amount(account, 0)
            assert after == before
        finally:
            db.close()

    def test_the_clone_inherits_the_schedules(self, budgeted, capsys):
        cli(["budget-clone", str(budgeted), "Base", "Tighter"])
        capsys.readouterr()
        db = open_book(budgeted)
        try:
            clone = next(b for b in db.iter_budgets() if b.name == "Tighter")
            assert any(s.in_budget(clone.handle) for s in db.iter_scheduled())
        finally:
            db.close()

    def test_a_schedule_can_be_removed_from_one_budget_only(self, budgeted, capsys):
        cli(["budget-clone", str(budgeted), "Base", "Tighter"])
        cli(
            [
                "budget-member",
                str(budgeted),
                "remove",
                "--budget",
                "Tighter",
                "--schedule",
                "Groceries",
            ]
        )
        capsys.readouterr()
        db = open_book(budgeted)
        try:
            base = next(b for b in db.iter_budgets() if b.name == "Base")
            clone = next(b for b in db.iter_budgets() if b.name == "Tighter")
            sched = next(s for s in db.iter_scheduled() if s.name == "Groceries")
            assert sched.in_budget(base.handle) is True
            assert sched.in_budget(clone.handle) is False
        finally:
            db.close()

    def test_membership_defaults_to_every_budget(self, budgeted):
        """A schedule created before budgets existed must not vanish from them."""
        db = open_book(budgeted)
        try:
            sched = next(s for s in db.iter_scheduled() if s.name == "Groceries")
            assert sched.budgets == []
            assert sched.in_budget("anything") is True
        finally:
            db.close()

    def test_the_budget_a_flow_is_removed_from_stops_counting_it(self, budgeted, capsys):
        cli(["budget-clone", str(budgeted), "Base", "Tighter"])
        cli(
            [
                "budget-member",
                str(budgeted),
                "remove",
                "--budget",
                "Tighter",
                "--schedule",
                "Groceries",
            ]
        )
        capsys.readouterr()
        db = open_book(budgeted)
        try:
            clone = next(b for b in db.iter_budgets() if b.name == "Tighter")
            rebuilt = budgeting.from_schedules(
                db,
                name="check",
                start=date(2026, 1, 1),
                periods=12,
                budget_handle=clone.handle,
            )
            assert not rebuilt.lines
        finally:
            db.close()

    def test_a_current_budget_can_be_chosen(self, budgeted, capsys):
        cli(["budget-clone", str(budgeted), "Base", "Tighter"])
        cli(["budget-use", str(budgeted), "Tighter"])
        capsys.readouterr()
        db = open_book(budgeted)
        try:
            assert budgeting.current_budget(db).name == "Tighter"
        finally:
            db.close()

    def test_the_dashboard_uses_the_plan_not_legacy_budget_selection(self, budgeted, capsys):
        from breadsched.gen.engine import dashboard

        cli(["budget-clone", str(budgeted), "Base", "Tighter"])
        cli(
            [
                "budget-member",
                str(budgeted),
                "remove",
                "--budget",
                "Tighter",
                "--schedule",
                "Groceries",
            ]
        )
        cli(["budget-use", str(budgeted), "Tighter"])
        capsys.readouterr()

        db = open_book(budgeted)
        try:
            board = dashboard.build(db, as_of=date(2026, 6, 1))
            assert "Groceries" in [bill.name for bill in board.bills]
            assert "budget" not in board.summary()
        finally:
            db.close()

    def test_a_single_budget_needs_no_nomination(self, budgeted):
        db = open_book(budgeted)
        try:
            assert budgeting.current_budget(db).name == "Base"
        finally:
            db.close()


class TestBillFrequency:
    def test_the_cycle_reads_as_the_schedules_own_frequency(self, budgeted, capsys):
        from breadsched.gen.engine import dashboard

        db = open_book(budgeted)
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

    def test_a_bill_carries_the_schedule_it_came_from(self, budgeted):
        from breadsched.gen.engine import dashboard

        db = open_book(budgeted)
        try:
            board = dashboard.build(db, as_of=date(2026, 6, 1))
            bill = next(b for b in board.bills if b.name == "Groceries")
            assert bill.schedule is not None
            assert db.get_scheduled(bill.schedule.handle) is not None
        finally:
            db.close()


class TestMembershipIsDecidable:
    """Removing a flow from the only budget must take it out, not put it back."""

    def test_an_undecided_flow_counts_everywhere(self, budgeted):
        db = open_book(budgeted)
        try:
            sched = next(s for s in db.iter_scheduled() if s.name == "Groceries")
            assert sched.budgets_decided is False
            assert sched.in_budget("anything") is True
        finally:
            db.close()

    def test_removing_from_the_only_budget_excludes_it(self, budgeted, capsys):
        """An empty list used to read as 'every budget', which inverted the answer."""
        cli(
            [
                "budget-member",
                str(budgeted),
                "remove",
                "--budget",
                "Base",
                "--schedule",
                "Groceries",
            ]
        )
        capsys.readouterr()

        db = open_book(budgeted)
        try:
            base = next(b for b in db.iter_budgets() if b.name == "Base")
            sched = next(s for s in db.iter_scheduled() if s.name == "Groceries")
            assert sched.budgets == []
            assert sched.budgets_decided is True
            assert sched.in_budget(base.handle) is False
        finally:
            db.close()

    def test_the_budget_then_has_no_lines(self, budgeted, capsys):
        cli(
            [
                "budget-member",
                str(budgeted),
                "remove",
                "--budget",
                "Base",
                "--schedule",
                "Groceries",
            ]
        )
        capsys.readouterr()

        db = open_book(budgeted)
        try:
            base = next(b for b in db.iter_budgets() if b.name == "Base")
            rebuilt = budgeting.from_schedules(
                db,
                name="check",
                start=date(2026, 1, 1),
                periods=12,
                budget_handle=base.handle,
            )
            assert not rebuilt.lines
        finally:
            db.close()

    def test_adding_it_back_restores_it(self, budgeted, capsys):
        cli(
            [
                "budget-member",
                str(budgeted),
                "remove",
                "--budget",
                "Base",
                "--schedule",
                "Groceries",
            ]
        )
        cli(["budget-member", str(budgeted), "add", "--budget", "Base", "--schedule", "Groceries"])
        capsys.readouterr()

        db = open_book(budgeted)
        try:
            base = next(b for b in db.iter_budgets() if b.name == "Base")
            sched = next(s for s in db.iter_scheduled() if s.name == "Groceries")
            assert sched.in_budget(base.handle) is True
        finally:
            db.close()

    def test_an_older_book_with_a_list_is_read_as_decided(self):
        """Books written before the flag recorded membership only as a list."""
        from breadsched.gen.lib import ScheduledTransaction

        sched = ScheduledTransaction(name="Rent")
        data = sched.serialize()
        data["budgets"] = ["abc"]
        data.pop("budgets_decided")

        restored = ScheduledTransaction.from_dict(data)
        assert restored.budgets_decided is True
        assert restored.in_budget("abc") is True
        assert restored.in_budget("other") is False
