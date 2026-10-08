"""Register typing shared by GTK and web: dates, amounts, accounts, check numbers."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from breadsched.gen.db.sqlite import DbSQLite
from breadsched.gen.engine.entry_input import (
    DATE_KEYS,
    EntryInputError,
    complete_account,
    parse_entry_amount,
    parse_entry_date,
    step_num,
)
from breadsched.gen.lib.account import Account, AccountType
from breadsched.gen.lib.money import Money
from breadsched.gen.lib.transaction import Transaction
from breadsched.gen.services.entry_input import (
    complete_entry_account,
    latest_num,
    next_entry_num,
    read_entry_amount,
    read_entry_date,
)

BASE = date(2026, 1, 31)
TODAY = date(2026, 10, 8)


class TestDates:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("t", TODAY),
            ("T", TODAY),
            ("+", date(2026, 2, 1)),
            ("=", date(2026, 2, 1)),
            ("+++", date(2026, 2, 3)),
            ("+7", date(2026, 2, 7)),
            ("-", date(2026, 1, 30)),
            ("_", date(2026, 1, 30)),
            ("-31", date(2025, 12, 31)),
            # A month on from the 31st is the last day of the shorter month.
            ("]", date(2026, 2, 28)),
            ("]]", date(2026, 3, 31)),
            ("[", date(2025, 12, 31)),
            ("]12", date(2027, 1, 31)),
            ("m", date(2026, 1, 1)),
            ("h", date(2026, 1, 31)),
            ("y", date(2026, 1, 1)),
            ("r", date(2026, 12, 31)),
            ("15", date(2026, 1, 15)),
            ("3/15", date(2026, 3, 15)),
            ("3.15", date(2026, 3, 15)),
            ("3/15/27", date(2027, 3, 15)),
            ("3/15/1999", date(1999, 3, 15)),
            ("12/31/80", date(1980, 12, 31)),
            ("12/31/77", date(1977, 12, 31)),
            ("12/31/76", date(2076, 12, 31)),
            ("2026-03-15", date(2026, 3, 15)),
            ("2026-3-5", date(2026, 3, 5)),
            ("  2026-03-15 ", date(2026, 3, 15)),
        ],
    )
    def test_shortcuts_and_forms(self, text, expected):
        assert parse_entry_date(text, BASE, TODAY) == expected

    def test_two_digit_years_fall_in_the_nearest_century(self):
        assert parse_entry_date("1/1/99", date(2026, 1, 1)) == date(1999, 1, 1)
        assert parse_entry_date("1/1/30", date(2026, 1, 1)) == date(2030, 1, 1)

    @pytest.mark.parametrize(
        "text", ["", "  ", "x", "32", "2/30", "13/1", "2026-02-30", "+x", "1/2/3/4", "]]x"]
    )
    def test_unreadable_text_is_refused(self, text):
        with pytest.raises(EntryInputError):
            parse_entry_date(text, BASE, TODAY)

    def test_moving_past_the_calendar_is_refused(self):
        with pytest.raises(EntryInputError):
            parse_entry_date("+", date.max, TODAY)
        with pytest.raises(EntryInputError):
            parse_entry_date("[", date.min, TODAY)

    def test_every_single_key_is_a_shortcut(self):
        for key in DATE_KEYS:
            parse_entry_date(key, BASE, TODAY)

    @given(st.text(max_size=30), st.dates())
    def test_any_text_is_a_date_or_refused(self, text, base):
        try:
            assert isinstance(parse_entry_date(text, base, TODAY), date)
        except EntryInputError:
            pass


class TestAmounts:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("12.50", Decimal("12.50")),
            ("$1,234.56", Decimal("1234.56")),
            ("-3", Decimal("-3")),
            ("(4.25)", Decimal("-4.25")),
            # A single number keeps every digit given.
            ("1.005", Decimal("1.005")),
            ("12.50+3*2", Decimal("18.50")),
            ("(100-20)/4", Decimal("20.00")),
            ("10/3", Decimal("3.33")),
            ("2/3", Decimal("0.67")),
            ("0.125*1", Decimal("0.13")),
            ("1,000+1", Decimal("1001.00")),
            ("-5+2", Decimal("-3.00")),
            ("$5 + $2.25", Decimal("7.25")),
            # Larger than the default 28-digit context can round to cents.
            ("1000**9", Decimal(10) ** 27),
        ],
    )
    def test_numbers_and_arithmetic(self, text, expected):
        assert parse_entry_amount(text, "dot") == expected

    def test_comma_decimal_arithmetic(self):
        assert parse_entry_amount("1,5+2,25", "comma") == Decimal("3.75")
        assert parse_entry_amount("1.000,5*2", "comma") == Decimal("2001.00")

    def test_auto_reads_the_convention_from_the_whole_expression(self):
        assert parse_entry_amount("1,50+1,25") == Decimal("2.75")
        with pytest.raises(EntryInputError):
            parse_entry_amount("1,50+1.25")

    def test_results_round_to_the_currency_unit(self):
        assert parse_entry_amount("10/3", "dot", fraction=1) == Decimal("3")
        assert parse_entry_amount("10/3", "dot", fraction=1000) == Decimal("3.333")

    def test_blank_is_none(self):
        assert parse_entry_amount("  ", "dot") is None

    @pytest.mark.parametrize(
        "text",
        ["abc", "1/0", "pmt(1,2,3)", "2**999999", "1..2", "x+1", "1;2", "__import__", "9**999"],
    )
    def test_unsafe_or_unreadable_text_is_refused(self, text):
        with pytest.raises(EntryInputError):
            parse_entry_amount(text, "dot")

    @given(st.text(alphabet="0123456789.,+-*/() $", max_size=30))
    def test_any_arithmetic_text_is_an_amount_or_refused(self, text):
        try:
            result = parse_entry_amount(text, "dot")
        except EntryInputError:
            return
        assert result is None or result.is_finite()


NAMES = [
    "Assets:Checking",
    "Assets:Savings",
    "Expenses:Groceries",
    "Expenses:Gas",
    "Expenses:Gifts:Family",
    "Income:Salary",
]


class TestAccounts:
    def test_each_segment_begins_the_name_at_its_depth(self):
        assert complete_account("Ex:Gr", NAMES) == ["Expenses:Groceries"]
        assert complete_account("ex:g", NAMES) == [
            "Expenses:Gas",
            "Expenses:Groceries",
            "Expenses:Gifts:Family",
        ]

    def test_exact_depth_comes_before_deeper_accounts(self):
        assert complete_account("Ex", NAMES)[0] == "Expenses:Gas"
        assert complete_account("E:G:F", NAMES) == ["Expenses:Gifts:Family"]
        assert complete_account("Ex:", NAMES)[:1] == ["Expenses:Gas"]

    def test_no_match_and_blank(self):
        assert complete_account("Zz", NAMES) == []
        assert complete_account("", NAMES) == []
        assert complete_account("Assets:Checking:Old", NAMES) == []


class TestNum:
    @pytest.mark.parametrize(
        ("text", "step", "latest", "expected"),
        [
            ("101", 1, "", "102"),
            ("101", -1, "", "100"),
            ("0099", 1, "", "0100"),
            ("CHK7", 1, "", "CHK8"),
            ("0", -1, "", "0"),
            ("", 1, "1042", "1043"),
            ("", 1, "", ""),
            ("ATM", 1, "", "ATM"),
        ],
    )
    def test_stepping(self, text, step, latest, expected):
        assert step_num(text, step, latest) == expected


@pytest.fixture
def book(tmp_path):
    db = DbSQLite()
    db.load(str(tmp_path / "book.bsched"))
    yield db
    db.close()


def _account(db, name, kind, parent=None, **flags):
    account = Account(name=name, atype=kind, parent=parent, **flags)
    with db.transaction("Account") as txn:
        db.add_account(account, txn)
    return account.handle


@pytest.fixture
def accounts(book):
    root = _account(book, "Root", AccountType.ROOT)
    expenses = _account(book, "Expenses", AccountType.EXPENSE, root, placeholder=True)
    handles = {
        "checking": _account(book, "Checking", AccountType.BANK, root),
        "groceries": _account(book, "Groceries", AccountType.EXPENSE, expenses),
        "gym": _account(book, "Gym", AccountType.EXPENSE, expenses, hidden=True),
        "gas": _account(book, "Gas", AccountType.EXPENSE, expenses),
    }
    return handles


class TestService:
    def test_completion_offers_only_accounts_that_take_new_splits(self, book, accounts):
        found = complete_entry_account(book, "Ex:G").value
        assert [item.full_name for item in found] == ["Expenses:Gas", "Expenses:Groceries"]
        assert found[1].handle == accounts["groceries"]
        names = [item.full_name for item in complete_entry_account(book, "Ex").value]
        assert "Expenses" not in names  # a placeholder
        assert complete_entry_account(book, "Ex:Gy").value == ()  # hidden
        excluded = complete_entry_account(book, "Ex:G", exclude=accounts["gas"]).value
        assert [item.full_name for item in excluded] == ["Expenses:Groceries"]
        assert complete_entry_account(book, "Ex:G", limit=1).value[0].full_name == "Expenses:Gas"

    def test_an_empty_num_continues_the_register(self, book, accounts):
        for day, num in ((1, "100"), (3, "101"), (5, "")):
            transaction = Transaction.simple(
                date(2026, 1, day), "Store", accounts["groceries"], accounts["checking"], Money("5")
            )
            transaction.num = num
            with book.transaction("Entry") as txn:
                book.add_transaction(transaction, txn)
        assert latest_num(book, accounts["checking"]) == "101"
        assert next_entry_num(book, accounts["checking"], "", 1).value == "102"
        assert next_entry_num(book, accounts["checking"], "7", -1).value == "6"
        assert next_entry_num(book, accounts["gas"], "", 1).value == ""
        refused = next_entry_num(book, "missing", "", 1)
        assert refused.errors[0].code == "entry.account.not_found"

    def test_amounts_round_to_the_entry_currency(self, book, accounts):
        assert read_entry_amount(book, "10/3", number_format="dot").value.value == Decimal("3.33")
        assert read_entry_amount(book, "", number_format="dot").value.value is None
        refused = read_entry_amount(book, "1/0", number_format="dot")
        assert refused.errors[0].code == "entry.amount.invalid"
        missing = read_entry_amount(book, "1", currency="missing")
        assert missing.errors[0].code == "entry.currency.not_found"

    def test_dates(self):
        assert read_entry_date("+", BASE, TODAY).value == date(2026, 2, 1)
        assert read_entry_date("x", BASE, TODAY).errors[0].code == "entry.date.invalid"
