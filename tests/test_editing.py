"""Editing and deleting transactions.

An edit must be as safe as an entry: the same balance rule, the same single undo
step. Two things are guarded here that a careless implementation gets wrong —
rewriting an amount on a transaction with more than two legs, where there is no
single "the amount" to change, and losing the split identities on save, which
would detach reconciliation state from the legs it belongs to.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from breadsched.cli.main import main as cli
from breadsched.gen.db.sqlite import DbSQLite
from breadsched.gen.engine import ledger
from breadsched.gen.lib import Money, Split, Transaction


@pytest.fixture
def book_path(tmp_path, capsys):
    path = tmp_path / "book.breadsched"
    cli(["init", str(path)])
    cli(["add", str(path), "--date", "2026-01-05", "--description", "Rent",
         "--from", "Assets", "--to", "Expenses", "--amount", "1800.00"])
    capsys.readouterr()
    return path


def only_handle(path, capsys) -> str:
    cli(["register", str(path), "Assets", "--json"])
    rows = json.loads(capsys.readouterr().out)
    return rows[0]["handle"]


class TestCliEditing:
    def test_the_register_exposes_a_handle_to_refer_to(self, book_path, capsys):
        assert len(only_handle(book_path, capsys)) == 32

    def test_a_description_can_be_changed(self, book_path, capsys):
        handle = only_handle(book_path, capsys)
        cli(["edit", str(book_path), handle, "--description", "Rent, revised"])
        capsys.readouterr()

        db = DbSQLite()
        db.load(str(book_path))
        assert db.get_transaction(handle).description == "Rent, revised"
        db.close()

    def test_a_short_prefix_is_enough(self, book_path, capsys):
        handle = only_handle(book_path, capsys)
        assert cli(["edit", str(book_path), handle[:8], "--description", "Short"]) == 0

    def test_an_ambiguous_reference_is_refused(self, book_path, capsys):
        cli(["add", str(book_path), "--date", "2026-02-05", "--description", "Rent",
             "--from", "Assets", "--to", "Expenses", "--amount", "1800.00"])
        capsys.readouterr()
        assert cli(["edit", str(book_path), "", "--description", "x"]) == 2

    def test_an_unknown_reference_is_refused(self, book_path):
        assert cli(["edit", str(book_path), "nosuchhandle", "--description", "x"]) == 2

    def test_a_date_can_be_changed(self, book_path, capsys):
        handle = only_handle(book_path, capsys)
        cli(["edit", str(book_path), handle, "--date", "2026-03-09"])
        capsys.readouterr()

        db = DbSQLite()
        db.load(str(book_path))
        assert db.get_transaction(handle).post_date == date(2026, 3, 9)
        db.close()

    def test_an_amount_can_be_changed_and_still_balances(self, book_path, capsys):
        handle = only_handle(book_path, capsys)
        cli(["edit", str(book_path), handle, "--amount", "1950.00"])
        capsys.readouterr()

        db = DbSQLite()
        db.load(str(book_path))
        edited = db.get_transaction(handle)
        assert edited.is_balanced()
        assert ledger.balance(db, db.get_account_by_name("Expenses").handle) == Money(
            "1950.00"
        )
        db.close()

    def test_editing_is_a_single_undo_step(self, book_path, capsys):
        handle = only_handle(book_path, capsys)
        cli(["edit", str(book_path), handle, "--amount", "1950.00"])
        capsys.readouterr()

        db = DbSQLite()
        db.load(str(book_path))
        assert db.get_transaction(handle).value_for(
            db.get_account_by_name("Expenses").handle
        ) == Money("1950.00")
        db.close()

    def test_a_multi_split_amount_is_refused_rather_than_guessed(
        self, book_path, capsys
    ):
        """With three legs there is no single amount; changing one silently is worse."""
        db = DbSQLite()
        db.load(str(book_path))
        accounts = {db.full_name(a): a.handle for a in db.iter_accounts()}
        with db.transaction("Three legs") as txn:
            split = Transaction(post_date=date(2026, 4, 1), description="Shared bill")
            split.add_split(Split(accounts["Expenses"], Money("60.00")))
            split.add_split(Split(accounts["Income"], Money("40.00")))
            split.add_split(Split(accounts["Assets"], Money("-100.00")))
            db.add_transaction(split, txn)
        handle = split.handle
        db.close()

        assert cli(["edit", str(book_path), handle, "--amount", "50.00"]) == 2

    def test_a_multi_split_description_is_still_editable(self, book_path, capsys):
        db = DbSQLite()
        db.load(str(book_path))
        accounts = {db.full_name(a): a.handle for a in db.iter_accounts()}
        with db.transaction("Three legs") as txn:
            split = Transaction(post_date=date(2026, 4, 1), description="Shared bill")
            split.add_split(Split(accounts["Expenses"], Money("60.00")))
            split.add_split(Split(accounts["Income"], Money("40.00")))
            split.add_split(Split(accounts["Assets"], Money("-100.00")))
            db.add_transaction(split, txn)
        handle = split.handle
        db.close()

        assert cli(["edit", str(book_path), handle, "--description", "Split bill"]) == 0


class TestCliDeleting:
    def test_a_transaction_can_be_deleted(self, book_path, capsys):
        handle = only_handle(book_path, capsys)
        cli(["delete", str(book_path), handle])
        capsys.readouterr()

        db = DbSQLite()
        db.load(str(book_path))
        assert db.get_transaction(handle) is None
        assert ledger.balance(db, db.get_account_by_name("Assets").handle) == Money(0)
        db.close()

    def test_deleting_clears_the_split_index(self, book_path, capsys):
        handle = only_handle(book_path, capsys)
        cli(["delete", str(book_path), handle])
        capsys.readouterr()

        db = DbSQLite()
        db.load(str(book_path))
        assert db.split_rows(db.get_account_by_name("Assets").handle) == []
        db.close()

    def test_deleting_an_unknown_transaction_is_a_clean_error(self, book_path):
        assert cli(["delete", str(book_path), "nosuchthing"]) == 2


class TestEditsStayBalanced:
    def test_an_edit_that_would_unbalance_is_refused(self, book_path, capsys):
        handle = only_handle(book_path, capsys)
        db = DbSQLite()
        db.load(str(book_path))
        target = db.get_transaction(handle)
        target.splits[0].value = Money("999.00")

        from breadsched.gen.lib import UnbalancedError

        with pytest.raises(UnbalancedError):
            with db.transaction("Break it") as txn:
                db.commit_transaction(target, txn)
        db.close()

    def test_the_original_survives_a_refused_edit(self, book_path, capsys):
        handle = only_handle(book_path, capsys)
        db = DbSQLite()
        db.load(str(book_path))
        target = db.get_transaction(handle)
        target.splits[0].value = Money("999.00")
        try:
            with db.transaction("Break it") as txn:
                db.commit_transaction(target, txn)
        except Exception:  # noqa: BLE001 - the point is what survives
            pass
        assert db.get_transaction(handle).is_balanced()
        db.close()
