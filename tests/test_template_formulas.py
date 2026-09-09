"""Template formulas are whatever the user typed, in whatever locale.

A GnuCash scheduled transaction stores its amounts as *formula text*, not as
numbers. That text may be a plain amount, an amount written in a different
locale, an arithmetic expression, or a reference to something only GnuCash can
resolve. An importer that assumes "number" crashes on the rest — which is what
happened: ``decimal.InvalidOperation`` out of ``Money``, taking the whole import
with it.

Two failure modes are covered here, and the quiet one is the worse of the two:
``1.800,00`` read with English rules is 1.8 rather than 1800, a wrong number
rather than an error.
"""

from __future__ import annotations

from datetime import date

import pytest
from gnucash_fixtures import new_guid
from gnucash_xml_fixtures import XML_WITH_SCHEDULE, create_xml_book

from cashperspective.gen.lib import Money
from cashperspective.plugins.importer import gnucash_xml
from cashperspective.plugins.importer.gnucash_common import parse_amount_text


def book_with_formula(tmp_path, debit: str, credit: str = "") -> str:
    """Write an XML book whose template split carries ``debit`` as its formula."""
    ids = {
        name: new_guid()
        for name in (
            "root", "assets", "bank", "expenses", "rent", "txn", "split1", "split2",
            "template_root", "template_account", "template_txn", "tsplit1", "tsplit2",
            "schedule",
        )
    }
    body = XML_WITH_SCHEDULE.format(**ids)
    body = body.replace(
        "<slot:value type=\"string\">1800.00</slot:value>",
        f"<slot:value type=\"string\">{debit}</slot:value>",
        1,
    )
    if credit:
        body = body.replace(
            "<slot:key>credit-formula</slot:key>\n"
            "                      <slot:value type=\"string\">1800.00</slot:value>",
            "<slot:key>credit-formula</slot:key>\n"
            f"                      <slot:value type=\"string\">{credit}</slot:value>",
        )
    path = tmp_path / "formula.gnucash"
    path.write_text(body, encoding="utf-8")
    return str(path)


class TestAmountText:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("1800.00", "1800.00"),
            ("1,800.00", "1800.00"),      # English grouping
            ("1.800,00", "1800.00"),      # European grouping
            ("1 800,00", "1800.00"),      # French grouping, plain space
            ("\u00a01\u202f800,00", "1800.00"),  # non-breaking and narrow spaces
            ("$1,800.00", "1800.00"),
            ("\u20ac250,50", "250.50"),
            ("-250.50", "-250.50"),
            ("1,234", "1234.00"),         # grouping, not a decimal
            ("0.5", "0.50"),
        ],
    )
    def test_amounts_are_read_correctly(self, text, expected):
        assert parse_amount_text(text) == Money(expected)

    @pytest.mark.parametrize("text", ["rent * 2", "-", "", "   ", "n/a", None])
    def test_non_amounts_return_none_rather_than_raising(self, text):
        assert parse_amount_text(text) is None

    def test_a_european_amount_is_not_silently_scaled_down(self):
        """The quiet failure: 1.800,00 read as 1.8 would look like a real figure."""
        assert parse_amount_text("1.800,00") != Money("1.80")


class TestMoneyErrors:
    def test_money_says_what_it_could_not_read(self):
        with pytest.raises(ValueError, match="cannot read"):
            Money("1 800,00")

    def test_it_is_a_value_error_not_a_decimal_error(self):
        """decimal.InvalidOperation tells a caller nothing about the input."""
        import decimal

        try:
            Money("not a number")
        except ValueError as exc:
            assert not isinstance(exc, decimal.InvalidOperation)
            assert "not a number" in str(exc)


class TestFormulaImports:
    def test_a_plain_amount_still_works(self, db, tmp_path):
        path = book_with_formula(tmp_path, "1800.00")
        result = gnucash_xml.import_book(db, path)
        assert result.scheduled == 1
        sched = next(iter(db.iter_scheduled()))
        assert sched.amount() == Money("1800.00")

    def test_a_grouped_amount_does_not_crash(self, db, tmp_path):
        """The reported traceback: InvalidOperation out of Money."""
        path = book_with_formula(tmp_path, "1,800.00")
        result = gnucash_xml.import_book(db, path)
        assert result.scheduled == 1
        assert next(iter(db.iter_scheduled())).amount() == Money("1800.00")

    def test_a_european_amount_is_read_at_full_value(self, db, tmp_path):
        path = book_with_formula(tmp_path, "1.800,00")
        gnucash_xml.import_book(db, path)
        assert next(iter(db.iter_scheduled())).amount() == Money("1800.00")

    def test_a_spaced_amount_does_not_crash(self, db, tmp_path):
        path = book_with_formula(tmp_path, "1 800,00")
        gnucash_xml.import_book(db, path)
        assert next(iter(db.iter_scheduled())).amount() == Money("1800.00")

    def test_an_arithmetic_formula_is_evaluated(self, db, tmp_path):
        path = book_with_formula(tmp_path, "1500 + 300")
        gnucash_xml.import_book(db, path)
        assert next(iter(db.iter_scheduled())).amount() == Money("1800.00")

    def test_a_two_leg_schedule_is_balanced_against_its_good_side(self, db, tmp_path):
        """One unusable leg out of two is not a guess: the other side determines it.

        Left half-resolved, this is the schedule that made a whole projection fail
        with "could not be calculated".
        """
        path = book_with_formula(tmp_path, "rent_amount * 2")
        result = gnucash_xml.import_book(db, path)

        sched = next(iter(db.iter_scheduled()))
        assert sched.imbalance() == Money(0)
        assert sched.instantiate(date(2026, 4, 1)).is_balanced()
        assert any("balanced it against the other" in w for w in result.warnings)

    def test_the_repair_is_reported_not_silent(self, db, tmp_path):
        path = book_with_formula(tmp_path, "rent_amount * 2")
        result = gnucash_xml.import_book(db, path)
        assert any("unusable formula" in w for w in result.warnings)

    def test_the_import_completes_whatever_the_formula(self, db, tmp_path):
        """The ledger must arrive even when a schedule cannot be understood."""
        path = book_with_formula(tmp_path, "%%nonsense%%")
        result = gnucash_xml.import_book(db, path)
        assert result.transactions == 1
        assert result.accounts == 5

    def test_an_empty_formula_does_not_crash(self, db, tmp_path):
        path = book_with_formula(tmp_path, "")
        result = gnucash_xml.import_book(db, path)
        assert result.transactions == 1


class TestOneBadScheduleIsNotFatal:
    def test_a_broken_schedule_element_is_skipped_with_a_warning(
        self, db, tmp_path, monkeypatch
    ):
        """Whatever else goes wrong in a schedule, the book still imports."""
        book = create_xml_book(tmp_path / "book.gnucash", compress=False)

        def explode(*args, **kwargs):
            raise RuntimeError("simulated failure inside schedule parsing")

        monkeypatch.setattr(gnucash_xml, "_read_schedule", explode)
        result = gnucash_xml.import_book(db, book.path)

        assert result.transactions == 1
        assert result.accounts == 5
        assert result.skipped == 1
        assert any("scheduled transaction" in reason or "scheduled" in subject
                   for reason, subject in result.skipped_details)
