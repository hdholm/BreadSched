"""The register sheet behind the GTK grid: cursor, typing, quickfill, splits, saving.

The sheet imports no GTK, so these run everywhere; ``test_gui`` drives the same
behaviour through the drawn grid.
"""

from __future__ import annotations

from datetime import date

import pytest

from breadsched.gen.engine.entry_input import quickfill_account, quickfill_description
from breadsched.gen.lib import Money, Split, Transaction
from breadsched.gui.register_sheet import (
    BLANK,
    IMBALANCE,
    SPLIT,
    SPLIT_LABEL,
    TRANSACTION,
    RegisterSheet,
)

TODAY = date(2026, 2, 20)


def _sheet(db, book):
    return RegisterSheet(db, book.checking, today=TODAY)


def _type(sheet, column, text):
    sheet.column = column
    sheet.set_text(text)


def _three_way(db, book):
    """A paycheck split three ways into checking, savings, and salary."""
    pay = Transaction(post_date=date(2026, 2, 1), description="Payday split")
    pay.splits = [
        Split(book.checking, Money(700), memo="To checking"),
        Split(book.savings, Money(300), memo="To savings"),
        Split(book.salary, Money(-1000)),
    ]
    with db.transaction("Split pay") as txn:
        db.add_transaction(pay, txn)
    return pay


class TestQuickfill:
    NAMES = ["Assets:Checking", "Expenses", "Expenses:Gas", "Expenses:Groceries", "Income:Salary"]

    def test_the_segment_being_typed_completes_from_the_best_match(self):
        assert quickfill_account("Ex", self.NAMES)[0] == "Expenses"
        assert quickfill_account("ex:gr", self.NAMES) == (
            "Expenses:Groceries",
            ["Expenses:Groceries"],
        )
        assert quickfill_account("In:S", self.NAMES)[0] == "Income:Salary"

    def test_a_colon_accepts_the_segment_and_lists_the_level_below(self):
        shown, matches = quickfill_account("ex:", self.NAMES)
        assert shown == "Expenses:"
        assert matches == ["Expenses:Gas", "Expenses:Groceries"]
        # An account with nothing below it takes no colon.
        assert quickfill_account("Expenses:Gas:", self.NAMES)[0] == "Expenses:Gas"

    def test_no_match_leaves_the_text_as_typed(self):
        assert quickfill_account("zz", self.NAMES) == ("zz", [])

    def test_descriptions_complete_from_the_most_recent(self):
        recent_first = ["Grocery Outlet", "Groceries", "Gas"]
        assert quickfill_description("gro", recent_first) == "Grocery Outlet"
        assert quickfill_description("Gas", recent_first) is None
        assert quickfill_description("  ", recent_first) is None


class TestOpening:
    def test_the_cursor_starts_on_the_blank_transaction_at_the_end(self, db, funded_book):
        sheet = _sheet(db, funded_book)
        assert sheet.rows[-1].kind == BLANK
        assert sheet.cursor == len(sheet.rows) - 1
        assert sheet.column == "date"
        # The blank transaction offers today's date, ready for a new entry.
        assert sheet.text(sheet.cursor, "date") == TODAY.isoformat()
        assert all(row.kind == TRANSACTION for row in sheet.rows[:-1])

    def test_cells_show_the_ledger_as_plain_text(self, db, funded_book):
        sheet = _sheet(db, funded_book)
        first = sheet.rows[0].register_row
        assert sheet.text(0, "date") == first.post_date.isoformat()
        assert sheet.text(0, "description") == first.description
        assert sheet.text(0, "balance") == first.running.format(parens_negative=True)
        assert sheet.headings()["increase"] == "Deposit"
        assert sheet.headings()["decrease"] == "Withdrawal"


class TestEntering:
    def test_typing_and_enter_post_a_two_split_transaction(self, db, book):
        sheet = _sheet(db, book)
        _type(sheet, "description", "Corner store")
        _type(sheet, "transfer", "Ex:Gr")
        _type(sheet, "decrease", "12.50+3")
        assert sheet.enter()
        assert sheet.message == "Posted Corner store: 15.50 Withdrawal."
        [txn] = [t for t in db.iter_transactions() if t.description == "Corner store"]
        assert {(s.account, s.value) for s in txn.splits} == {
            (book.checking, Money("-15.50")),
            (book.groceries, Money("15.50")),
        }
        # The cursor waits on a new blank transaction, keeping the date entered.
        assert sheet.rows[sheet.cursor].kind == BLANK and sheet.column == "date"
        assert sheet.draft.description == ""

    def test_leaving_the_transfer_cell_resolves_a_partial_name(self, db, book):
        sheet = _sheet(db, book)
        _type(sheet, "transfer", "ex:u")
        assert sheet.settle()
        assert sheet.draft.transfer == "Expenses:Utilities"
        _type(sheet, "transfer", "nothing like it")
        assert not sheet.settle()
        assert sheet.error and "No account matches" in sheet.message

    def test_the_transfer_cell_quickfills_and_never_offers_this_account(self, db, book):
        sheet = _sheet(db, book)
        sheet.column = "transfer"
        shown, matches = sheet.quickfill("As")
        assert shown == "Assets"
        assert "Assets:Checking" not in matches and "Assets:Savings" in matches

    def test_a_description_quickfills_and_proposes_the_earlier_entry(self, db, funded_book):
        sheet = _sheet(db, funded_book)
        sheet.column = "description"
        earlier = sheet.descriptions[0]
        shown, _ = sheet.quickfill(earlier[:3])
        assert shown.casefold().startswith(earlier[:3].casefold())
        sheet.set_text(earlier)
        assert sheet.settle()
        # Leaving the description proposed the earlier transfer and amount.
        assert sheet.draft.transfer
        assert sheet.draft.increase or sheet.draft.decrease

    def test_date_and_number_keys(self, db, book):
        sheet = _sheet(db, book)
        assert sheet.date_key("2026-02-20", "+") == "2026-02-21"
        assert sheet.date_key("2026-02-20", "]") == "2026-03-20"
        assert sheet.date_key("2026-02-20", "x") is None
        assert sheet.num_key("101", 1) == "102"
        assert sheet.num_key("abc", 1) is None

    def test_a_missing_amount_keeps_the_cursor_with_the_reason(self, db, book):
        sheet = _sheet(db, book)
        _type(sheet, "description", "No amount")
        _type(sheet, "transfer", "Expenses:Rent")
        assert not sheet.enter()
        assert sheet.error and "Enter an amount under Deposit or Withdrawal" in sheet.message
        assert sheet.column == "increase"
        assert not any(t.description == "No amount" for t in db.iter_transactions())

    def test_escape_empties_the_blank_transaction(self, db, book):
        sheet = _sheet(db, book)
        _type(sheet, "description", "Changed my mind")
        assert sheet.draft.dirty()
        sheet.escape()
        assert not sheet.draft.dirty() and sheet.draft.description == ""

    def test_a_hidden_register_takes_no_new_entry(self, db, book):
        account = db.get_account(book.checking)
        account.hidden = True
        with db.transaction("Hide") as txn:
            db.commit_account(account, txn)
        sheet = _sheet(db, book)
        assert not sheet.enabled
        assert sheet.editable_columns(sheet.cursor) == ()


class TestMovingAndEditing:
    def test_moving_onto_a_transaction_edits_it_in_place(self, db, funded_book):
        sheet = _sheet(db, funded_book)
        assert sheet.move(-1)
        row = sheet.rows[sheet.cursor]
        assert row.kind == TRANSACTION and row.current
        assert sheet.draft.source.handle == row.register_row.transaction.handle
        _type(sheet, "description", "Renamed")
        assert sheet.move(1)  # leaving saves it
        assert db.get_transaction(row.register_row.transaction.handle).description == "Renamed"
        assert sheet.rows[sheet.cursor].kind == BLANK

    def test_an_edit_that_cannot_be_saved_keeps_the_cursor(self, db, funded_book):
        sheet = _sheet(db, funded_book)
        sheet.move(-1)
        here = sheet.cursor
        _type(sheet, "description", "")
        assert not sheet.move(-1)
        assert sheet.cursor == here and "Enter a description." == sheet.message

    def test_tab_walks_the_cells_and_past_the_last_saves(self, db, book):
        sheet = _sheet(db, book)
        order = []
        for _ in range(5):
            order.append(sheet.column)
            sheet.tab()
        assert order == ["date", "num", "description", "transfer", "increase"]
        assert sheet.column == "decrease"
        sheet.tab(backwards=True)
        assert sheet.column == "increase"

    def test_selecting_a_split_transaction_keeps_the_cursor_on_it(self, db, book):
        """Regression: opening a split transaction must not send the cursor to the top."""
        pay = _three_way(db, book)
        sheet = _sheet(db, book)
        index = next(
            i
            for i, row in enumerate(sheet.rows)
            if row.register_row is not None and row.register_row.transaction.handle == pay.handle
        )
        assert sheet.move_to(index, "description")
        header = sheet.header_index()
        assert sheet.rows[header].register_row.transaction.handle == pay.handle
        assert sheet.cursor == header and sheet.cursor != 0 or header == 0
        # Its split lines open beneath it, in place, with an empty line to add one.
        lines = [row for row in sheet.rows[header + 1 :] if row.kind == SPLIT]
        assert len(lines) == 4
        assert sheet.text(header, "transfer") == SPLIT_LABEL
        assert [sheet.text(header + 1 + i, "transfer") for i in range(3)] == [
            "Assets:Checking",
            "Assets:Savings",
            "Income:Salary",
        ]
        # Moving down walks its lines without leaving the transaction.
        sheet.move(1)
        assert sheet.rows[sheet.cursor].kind == SPLIT and sheet.draft.source.handle == pay.handle

    def test_a_split_line_edit_must_balance_before_it_saves(self, db, book):
        pay = _three_way(db, book)
        sheet = _sheet(db, book)
        sheet.select_transaction(pay.handle)
        header = sheet.header_index()
        sheet.move_to(header + 2, "increase")  # the savings line
        sheet.set_text("250")
        assert sheet.residual() == Money(-50)
        assert any(row.kind == IMBALANCE for row in sheet.rows)
        assert not sheet.enter()
        assert "out of balance by (50.00)" in sheet.message
        # Putting the 50 on a new line to groceries balances it.
        new_line = next(
            i
            for i, row in enumerate(sheet.rows)
            if row.kind == SPLIT and not sheet.draft.lines[row.line].has_input()
        )
        sheet.move_to(new_line, "transfer")
        sheet.set_text("Expenses:Groceries")
        sheet.column = "increase"
        sheet.set_text("50")
        assert sheet.enter()
        stored = db.get_transaction(pay.handle)
        assert sorted(s.value for s in stored.splits) == [
            Money(-1000),
            Money(50),
            Money(250),
            Money(700),
        ]
        # Split identity and memos survive the edit.
        assert {s.memo for s in stored.splits} >= {"To checking", "To savings"}

    def test_a_simple_entry_can_be_split_and_joined_again(self, db, book):
        sheet = _sheet(db, book)
        _type(sheet, "description", "Shop")
        _type(sheet, "transfer", "Expenses:Groceries")
        _type(sheet, "decrease", "40")
        assert sheet.toggle_split()
        header = sheet.header_index()
        assert [sheet.text(header + 1 + i, "transfer") for i in range(2)] == [
            "Assets:Checking",
            "Expenses:Groceries",
        ]
        assert sheet.text(header + 1, "decrease") == "40.00"
        assert sheet.text(header + 2, "increase") == "40.00"
        assert sheet.toggle_split()
        assert sheet.draft.transfer == "Expenses:Groceries" and sheet.draft.decrease == "40.00"

    def test_reconcile_toggles_through_the_service(self, db, funded_book):
        sheet = _sheet(db, funded_book)
        assert sheet.text(0, "reconcile") == "n"
        assert sheet.toggle_reconcile(0)
        assert sheet.text(0, "reconcile") == "c"


@pytest.mark.parametrize("needle,expected", [("rent", True), ("zzz", False)])
def test_the_filter_keeps_the_blank_transaction_last(db, funded_book, needle, expected):
    sheet = _sheet(db, funded_book)
    sheet.set_filter(needle)
    assert sheet.rows[-1].kind == BLANK
    assert (len(sheet.rows) > 1) is expected


def test_viewing_splits_is_not_an_edit(db, funded_book):
    sheet = _sheet(db, funded_book)
    sheet.move(-1)
    assert not sheet.draft.dirty()
    assert sheet.toggle_split()
    assert sheet.draft.expanded and not sheet.draft.dirty()
    assert sheet.toggle_split()
    assert not sheet.draft.dirty()


def test_the_blank_transaction_shows_its_date_while_the_cursor_is_elsewhere(db, funded_book):
    sheet = _sheet(db, funded_book)
    sheet.move(-1)
    assert sheet.rows[-1].kind == BLANK and not sheet.rows[-1].current
    assert sheet.text(len(sheet.rows) - 1, "date") == TODAY.isoformat()
