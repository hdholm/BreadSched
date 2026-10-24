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
    NOTES,
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

    def test_a_split_can_move_a_transaction_to_another_account(self, db, book):
        """#364: a purchase entered on the wrong card moves from its split lines."""
        shop = Transaction.simple(date(2026, 2, 3), "Shop", book.groceries, book.card, "40")
        with db.transaction("Shop") as txn:
            db.add_transaction(shop, txn)
        sheet = RegisterSheet(db, book.card, today=TODAY)
        sheet.select_transaction(shop.handle)
        assert sheet.toggle_split()
        header = sheet.header_index()
        assert sheet.text(header + 1, "transfer") == "Liabilities:Credit Card"
        sheet.move_to(header + 1, "transfer")
        sheet.set_text("Assets:Checking")
        assert sheet.enter(), sheet.message
        assert sheet.message == (
            "Moved Shop to Assets:Checking; it no longer shows in this register."
        )
        stored = db.get_transaction(shop.handle)
        assert {split.account for split in stored.splits} == {book.groceries, book.checking}
        assert all(
            row.register_row is None or row.register_row.transaction.handle != shop.handle
            for row in sheet.rows
        )

    def test_a_new_transaction_still_needs_a_split_in_this_account(self, db, book):
        sheet = _sheet(db, book)
        _type(sheet, "description", "Shop")
        _type(sheet, "transfer", "Expenses:Groceries")
        _type(sheet, "decrease", "40")
        assert sheet.toggle_split()
        header = sheet.header_index()
        sheet.move_to(header + 1, "transfer")
        sheet.set_text("Liabilities:Credit Card")
        before = sorted(txn.handle for txn in db.iter_transactions())
        assert not sheet.enter()
        assert sheet.message == "One split must be in this register's account."
        assert sorted(txn.handle for txn in db.iter_transactions()) == before

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


def _history(db, book, days=40):
    with db.transaction("History") as txn:
        for day in range(days):
            when = date.fromordinal(date(2026, 1, 1).toordinal() + day)
            db.add_transaction(
                Transaction.simple(when, f"Shop {day}", book.groceries, book.checking, "4"), txn
            )


class TestSavingStaysQuick:
    """#319: a save read the whole account twice, then every view reloaded it again."""

    def test_the_in_place_update_equals_a_full_rebuild(self, db, book):
        from breadsched.gen.engine import ledger

        _history(db, book, days=12)
        rows = ledger.register(db, book.checking)
        middle = rows[5].transaction
        with db.transaction("Edit") as txn:
            middle.post_date = date(2026, 1, 30)
            middle.splits[0].value = (
                Money("-9.25")
                if middle.splits[0].account == book.checking
                else middle.splits[0].value
            )
            middle.splits[1].value = -middle.splits[0].value
            db.commit_transaction(middle, txn)
            late = Transaction.simple(date(2026, 1, 2), "Late", book.checking, book.salary, "100")
            db.add_transaction(late, txn)
            gone = rows[0].transaction
            db.remove_transaction(gone.handle, txn)
        updated = ledger.update_register(
            db, book.checking, rows, {middle.handle, late.handle, gone.handle}
        )
        full = ledger.register(db, book.checking)
        assert [(r.transaction.handle, r.split.handle, r.running) for r in updated] == [
            (r.transaction.handle, r.split.handle, r.running) for r in full
        ]

    def test_a_save_reads_only_the_saved_transaction(self, db, book, monkeypatch):
        _history(db, book)
        sheet = _sheet(db, book)
        decoded = []
        real = db._decode_row

        def counting(table, handle, blob, cls):
            if table == "transaction":
                decoded.append(handle)
            return real(table, handle, blob, cls)

        monkeypatch.setattr(db, "_decode_row", counting)
        _type(sheet, "description", "Corner store")
        _type(sheet, "transfer", "Expenses:Groceries")
        _type(sheet, "decrease", "7")
        assert sheet.enter()
        # Before #319 this decoded every transaction in the account twice (80+).
        assert len(decoded) <= 3, decoded
        assert sheet.closing_balance() == sheet.ledger_rows[-1].running
        assert sheet.ledger_rows[-1].transaction.description == "Corner store"

    def test_a_save_keeps_the_rows_it_did_not_change(self, db, book):
        # Making a new object for every line of a long register on each save set
        # off full garbage collections, so an occasional save was several times
        # slower than the rest (seen in CI as 0.10s, 0.83s, 0.10s).
        _history(db, book)
        sheet = _sheet(db, book)
        ledger_before = list(sheet.ledger_rows)
        lines_before = [row for row in sheet.rows if row.register_row is not None]
        _type(sheet, "description", "Corner store")
        _type(sheet, "transfer", "Expenses:Groceries")
        _type(sheet, "decrease", "7")
        assert sheet.enter()
        assert all(
            after is before
            for after, before in zip(
                sheet.ledger_rows[: len(ledger_before)], ledger_before, strict=True
            )
        )
        lines_after = [row for row in sheet.rows if row.register_row is not None]
        assert len(lines_after) == len(lines_before) + 1
        kept = lines_after[: len(lines_before)]
        assert all(after is before for after, before in zip(kept, lines_before, strict=True))


class TestDoubleLine:
    """GnuCash's Double Line mode: a notes line under every transaction (#319)."""

    def test_notes_are_typed_on_the_blank_rows_second_line_and_saved(self, db, book):
        sheet = _sheet(db, book)
        sheet.set_double_line(True)
        assert [row.kind for row in sheet.rows[-2:]] == [BLANK, NOTES]
        _type(sheet, "description", "Hardware store")
        _type(sheet, "transfer", "Expenses:Groceries")
        _type(sheet, "decrease", "20")
        # Tab goes from the last amount to the notes line, then saves past it.
        sheet.column = "decrease"
        assert sheet.tab()
        assert sheet.rows[sheet.cursor].kind == NOTES and sheet.column == "description"
        sheet.set_text("Hinges for the gate")
        assert sheet.tab()
        [txn] = [t for t in db.iter_transactions() if t.description == "Hardware store"]
        assert txn.notes == "Hinges for the gate"
        header = next(
            i
            for i, row in enumerate(sheet.rows)
            if row.kind == TRANSACTION and row.register_row.transaction.handle == txn.handle
        )
        assert sheet.rows[header + 1].kind == NOTES
        assert sheet.text(header + 1, "description") == "Hinges for the gate"
        assert sheet.text(header + 1, "transfer") == ""
        assert sheet.editable_columns(header + 1) == ("description",)

    def test_editing_notes_of_an_existing_transaction_keeps_everything_else(self, db, book):
        pay = Transaction.simple(date(2026, 2, 3), "Pay", book.checking, book.salary, "900")
        pay.notes = "January"
        with db.transaction("Pay") as txn:
            db.add_transaction(pay, txn)
        sheet = _sheet(db, book)
        sheet.set_double_line(True)
        header = next(i for i, row in enumerate(sheet.rows) if row.kind == TRANSACTION)
        assert sheet.text(header + 1, "description") == "January"
        # Clicking its notes line opens that transaction there.
        assert sheet.move_to(header + 1, "description")
        assert sheet.rows[sheet.cursor].kind == NOTES
        assert not sheet.draft.dirty()
        sheet.set_text("January, with bonus")
        assert sheet.draft.dirty()
        assert sheet.enter()
        stored = db.get_transaction(pay.handle)
        assert stored.notes == "January, with bonus"
        assert {(s.account, s.value) for s in stored.splits} == {
            (book.checking, Money(900)),
            (book.salary, Money(-900)),
        }
        # Notes never quickfill or propose like a description.
        assert sheet.quickfill("Jan") == ("Jan", [])

    def test_single_line_mode_keeps_notes_on_save(self, db, book):
        pay = Transaction.simple(date(2026, 2, 3), "Pay", book.checking, book.salary, "900")
        pay.notes = "Kept"
        with db.transaction("Pay") as txn:
            db.add_transaction(pay, txn)
        sheet = _sheet(db, book)
        header = next(i for i, row in enumerate(sheet.rows) if row.kind == TRANSACTION)
        assert sheet.move_to(header, "description")
        sheet.set_text("Pay day")
        assert sheet.enter()
        assert db.get_transaction(pay.handle).notes == "Kept"
        assert NOTES not in {row.kind for row in sheet.rows}

    def test_turning_double_line_off_on_a_notes_line_returns_to_the_transaction(self, db, book):
        sheet = _sheet(db, book)
        sheet.set_double_line(True)
        sheet.move_to(len(sheet.rows) - 1, "description")
        assert sheet.rows[sheet.cursor].kind == NOTES
        sheet.set_double_line(False)
        assert sheet.rows[sheet.cursor].kind == BLANK
        # The notes line takes no reconcile toggle.
        sheet.set_double_line(True)
        assert sheet.toggle_reconcile(len(sheet.rows) - 1) is False


class TestEnterAfterEditing:
    """#325: saving an edit moved to the same transaction's notes or split line."""

    def _first(self, sheet):
        index = next(i for i, row in enumerate(sheet.rows) if row.kind == TRANSACTION)
        assert sheet.move_to(index, "description")
        return sheet.rows[sheet.header_index()].handle

    def _next_handle(self, sheet, handle):
        rows = [row.handle for row in sheet.rows if row.kind in (TRANSACTION, BLANK)]
        return rows[rows.index(handle) + 1]

    @pytest.mark.parametrize("double", [False, True])
    def test_enter_moves_to_the_next_transaction(self, db, funded_book, double):
        sheet = _sheet(db, funded_book)
        sheet.set_double_line(double)
        handle = self._first(sheet)
        following = self._next_handle(sheet, handle)
        _type(sheet, "description", "Renamed")
        assert sheet.enter(), sheet.message
        assert db.get_transaction(handle).description == "Renamed"
        assert sheet.rows[sheet.cursor].kind == TRANSACTION
        assert sheet.rows[sheet.cursor].handle == following
        assert sheet.column == "date"

    def test_tab_past_the_notes_line_saves_and_moves_on(self, db, funded_book):
        sheet = _sheet(db, funded_book)
        sheet.set_double_line(True)
        handle = self._first(sheet)
        following = self._next_handle(sheet, handle)
        _type(sheet, "description", "Tabbed")
        for _ in range(12):
            sheet.tab()
            if sheet.rows[sheet.header_index()].handle != handle:
                break
        assert db.get_transaction(handle).description == "Tabbed"
        assert sheet.rows[sheet.header_index()].handle == following

    def test_enter_on_a_split_transaction_skips_its_split_lines(self, db, funded_book):
        pay = _three_way(db, funded_book)
        sheet = _sheet(db, funded_book)
        index = next(i for i, row in enumerate(sheet.rows) if row.handle == pay.handle)
        assert sheet.move_to(index, "description")
        assert sheet.draft.expanded
        _type(sheet, "description", "Payday, renamed")
        assert sheet.enter(), sheet.message
        assert db.get_transaction(pay.handle).description == "Payday, renamed"
        row = sheet.rows[sheet.cursor]
        assert row.kind in (TRANSACTION, BLANK) and row.handle != pay.handle

    def test_each_row_knows_its_transaction_for_shading(self, db, funded_book):
        sheet = _sheet(db, funded_book)
        sheet.set_double_line(True)
        kinds = [row.kind for row in sheet.rows]
        assert len(sheet.ordinals) == len(sheet.rows)
        for index, kind in enumerate(kinds):
            if kind == NOTES:
                assert sheet.ordinals[index] == sheet.ordinals[index - 1]
        transactions = [sheet.ordinals[i] for i, k in enumerate(kinds) if k in (TRANSACTION, BLANK)]
        assert transactions == list(range(len(transactions)))
