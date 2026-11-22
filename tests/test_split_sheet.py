"""One transaction's split lines, as every transaction editor types them.

The split sheet imports no GTK, so these run everywhere. It and the register are
both a ``SplitEntry``: what a split line does is written once, so a split is typed
the same way in the register, the transaction editor, and the window of what the
due review posted (``test_gui`` drives those through the drawn grid).
"""

from __future__ import annotations

from datetime import date

import pytest

from breadsched.api.local import book_server
from breadsched.gen.lib import (
    InvestmentActivityKind,
    Money,
    PlanningFlowKind,
    Split,
    Transaction,
)
from breadsched.gui.register_sheet import RegisterSheet
from breadsched.gui.split_entry import SplitEntry, choose
from breadsched.gui.split_sheet import SplitSheet


def _sheet(db, *, transaction=None, account=None) -> SplitSheet:
    book = book_server(db)
    context = book.transaction_editor(transaction.handle if transaction else None)
    return SplitSheet(
        db,
        context.accounts,
        security_accounts=context.security_accounts,
        transaction=transaction,
        account=account,
    )


def _type(sheet: SplitSheet, line: int, column: str, text: str) -> bool:
    sheet.move_to(line)
    sheet.column = column
    sheet.set_text(text)
    return sheet.settle()


def test_the_register_and_every_editor_share_one_split_line_model():
    assert issubclass(RegisterSheet, SplitEntry)
    assert issubclass(SplitSheet, SplitEntry)
    shared = {
        "_set_line_text",
        "_quickfill_line",
        "_settle_line",
        "_line_splits",
        "_split_input",
        "_fill_trade",
        "residual",
        "balancing_line",
        "line_values",
    }
    for name in shared:
        # Written once, on the base: neither sheet has its own copy.
        assert name in vars(SplitEntry), name
        assert name not in vars(RegisterSheet), name
        assert name not in vars(SplitSheet), name


class TestANewEntry:
    def test_it_opens_on_the_register_account_and_an_empty_line(self, db, funded_book):
        sheet = _sheet(db, account=funded_book.checking)
        assert [line.account for line in sheet.lines] == ["Assets:Checking", ""]
        # The amount columns are named as that register names them.
        headings = sheet.headings()
        assert (headings["increase"], headings["decrease"]) == ("Deposit", "Withdrawal")
        assert headings["description"] == "Memo" and headings["transfer"] == "Account"
        # Opened from nowhere in particular, they are a debit and a credit.
        assert _sheet(db).headings()["increase"] == "Debit"

    def test_the_empty_line_holds_the_rest_and_takes_it_with_an_account(self, db, funded_book):
        sheet = _sheet(db, account=funded_book.checking)
        assert _type(sheet, 0, "decrease", "12.50+2.50")
        assert sheet.lines[0].decrease == "15.00"  # arithmetic, as in the register
        assert sheet.balancing_line() == 1
        assert sheet.text(1, "increase") == "15.00"
        assert _type(sheet, 1, "transfer", "ex:gr")
        assert sheet.lines[1].account == "Expenses:Groceries"
        assert sheet.lines[1].increase == "15.00"
        assert sheet.residual() == Money(0)
        # Another empty line waits for the next split.
        assert len(sheet.lines) == 3 and not sheet.lines[2].has_input()
        splits = sheet.splits("USD")
        assert [(s.account, s.value.value) for s in splits] == [
            (funded_book.checking, Money("-15")),
            (funded_book.groceries, Money("15")),
        ]

    def test_an_account_that_matches_nothing_is_refused_where_it_was_typed(self, db, funded_book):
        sheet = _sheet(db, account=funded_book.checking)
        assert not _type(sheet, 1, "transfer", "Nowhere at all")
        assert sheet.error and "No account matches" in sheet.message

    def test_an_unbalanced_or_single_split_entry_says_why_it_cannot_be_saved(self, db, funded_book):
        sheet = _sheet(db, account=funded_book.checking)
        _type(sheet, 0, "increase", "10")
        assert sheet.splits("USD") is None
        assert "at least two splits" in sheet.message
        _type(sheet, 1, "transfer", "Income:Salary")
        _type(sheet, 1, "decrease", "4")
        assert sheet.splits("USD") is None
        assert "out of balance" in sheet.message and sheet.cursor == 2

    def test_lines_can_start_from_a_registers_blank_entry_or_a_proposal(self, db, funded_book):
        sheet = _sheet(db, account=funded_book.checking)
        assert sheet.untouched()
        sheet.set_lines(
            [(funded_book.checking, Money("-9"), ""), (funded_book.rent, Money("9"), "Jan")]
        )
        assert sheet.line_values() == [
            (funded_book.checking, Money("-9"), ""),
            (funded_book.rent, Money("9"), "Jan"),
        ]
        assert not sheet.untouched()


class TestEditingAStoredTransaction:
    def test_each_split_is_a_line_and_its_identity_is_kept(self, db, funded_book):
        rent = next(t for t in db.iter_transactions() if t.description == "January rent")
        sheet = _sheet(db, transaction=rent, account=funded_book.checking)
        assert len(sheet.lines) == len(rent.splits) + 1
        splits = sheet.splits("USD")
        assert {s.handle for s in splits} == {s.handle for s in rent.splits}

    def test_planning_detail_is_typed_on_the_line_and_saved(self, db, funded_book):
        pay = Transaction(post_date=date(2026, 2, 1), description="Saving")
        pay.splits = [
            Split(
                funded_book.savings,
                Money(100),
                planning_flow=PlanningFlowKind.RETIREMENT_SAVING,
            ),
            Split(funded_book.checking, Money(-100)),
        ]
        with db.transaction("Add") as txn:
            db.add_transaction(pay, txn)
        sheet = _sheet(db, transaction=pay)
        assert sheet.lines[0].purpose == "Retirement saving"
        assert "purpose" not in sheet.columns
        sheet.set_detail(True)
        assert sheet.columns[-2:] == ("purpose", "activity")
        # Activity only for an investment or retirement account, or once set.
        assert "activity" not in sheet.editable_columns(0)
        assert _type(sheet, 0, "purpose", "debt")
        assert sheet.lines[0].purpose == "Debt principal"
        assert _type(sheet, 1, "purpose", "")
        splits = sheet.splits("USD")
        assert splits[0].planning_flow is PlanningFlowKind.DEBT_PRINCIPAL
        assert splits[1].planning_flow is None
        assert splits[1].investment_activity is None
        assert not _type(sheet, 0, "purpose", "Holiday fund")
        assert "No purpose matches" in sheet.message

    def test_the_register_keeps_detail_it_does_not_show(self, db, funded_book):
        """The register shows no planning detail, so it saves the stored detail."""
        pay = Transaction(post_date=date(2026, 2, 2), description="Contribution")
        pay.splits = [
            Split(
                funded_book.savings,
                Money(50),
                investment_activity=InvestmentActivityKind.CONTRIBUTION,
            ),
            Split(funded_book.checking, Money(-50)),
        ]
        with db.transaction("Add") as txn:
            db.add_transaction(pay, txn)
        register = RegisterSheet(db, funded_book.checking, today=date(2026, 2, 20))
        assert register.select_transaction(pay.handle)
        assert register.edits_detail is False
        split = register._split_input(
            funded_book.savings,
            Money(50),
            "USD",
            pay.splits[0].handle,
            line=register._line_of(pay.splits[0]),
        )
        assert split.investment_activity is InvestmentActivityKind.CONTRIBUTION


class TestTheKeyboard:
    @pytest.fixture
    def sheet(self, db, funded_book):
        return _sheet(db, account=funded_book.checking)

    def test_tab_runs_through_every_cell_then_leaves_the_grid(self, sheet):
        order = sheet._tab_order()
        assert order[0] == (0, "description")
        sheet.cursor, sheet.column = order[-1]
        assert sheet.tab() is True and sheet.leaving == 1
        sheet.leaving = 0
        sheet.cursor, sheet.column = order[0]
        assert sheet.tab(backwards=True) is True and sheet.leaving == -1

    def test_enter_asks_the_editor_to_save_and_escape_to_close(self, sheet):
        asked = []
        sheet.on_enter = lambda: asked.append("save") or True
        sheet.on_escape = lambda: asked.append("close")
        assert sheet.enter() is True
        sheet.escape()
        assert asked == ["save", "close"]

    def test_choices_complete_like_accounts(self):
        labels = ["Retirement saving", "Benefit / FSA funding", "Debt principal"]
        assert choose("ret", labels) == ("Retirement saving", ["Retirement saving"])
        assert choose("fsa", labels) == ("fsa", ["Benefit / FSA funding"])
        assert choose("", labels) == ("", labels)
