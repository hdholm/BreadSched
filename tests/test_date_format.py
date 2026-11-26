"""The desktop's date display style (Edit → Preferences) and the order a typed date
is read in (``gen.utils.date_format``, ``engine.entry_input.parse_entry_date``)."""

from __future__ import annotations

import locale
from datetime import date

import pytest

from breadsched.gen.engine.entry_input import EntryInputError, parse_entry_date
from breadsched.gen.utils import date_format

DAY = date(2026, 3, 5)


@pytest.mark.parametrize(
    ("style", "written", "hint", "day_first"),
    [
        ("iso", "2026-03-05", "YYYY-MM-DD", False),
        ("mdy", "03/05/2026", "MM/DD/YYYY", False),
        ("dmy", "05/03/2026", "DD/MM/YYYY", True),
        ("dmy-dot", "05.03.2026", "DD.MM.YYYY", True),
    ],
)
def test_each_style_writes_and_reads_its_own_way(style, written, hint, day_first):
    assert date_format.format_date(DAY, style) == written
    assert date_format.pattern(style) == hint
    assert date_format.day_first(style) is day_first
    # What a style writes reads back as the same date in that style.
    assert parse_entry_date(written, date(2000, 1, 1), day_first=day_first) == DAY


def test_locale_is_the_default_and_unknown_styles_follow_it(monkeypatch):
    monkeypatch.setattr(date_format, "_locale_style", "dmy")
    assert date_format.DEFAULT_STYLE == "locale"
    assert date_format.STYLES[0] == "locale"
    assert date_format.format_date(DAY) == "05/03/2026"
    assert date_format.resolve("no-such-style") == "dmy"
    assert "15/03/2026" in date_format.style_label("locale")
    assert date_format.style_label("mdy") == "03/15/2026 (MM/DD/YYYY)"


@pytest.mark.parametrize(
    ("written", "style"),
    [
        ("02/03/2001", "mdy"),  # en_US
        ("02/03/01", "mdy"),
        ("03/02/2001", "dmy"),  # en_GB, fr_FR
        ("03.02.2001", "dmy-dot"),  # de_DE
        ("2001-02-03", "iso"),  # sv_SE
        ("2001/02/03", "iso"),  # ja_JP, zh_CN
        ("2001年02月03日", "iso"),
        ("Saturday", "iso"),
    ],
)
def test_the_locale_style_is_read_from_how_it_writes_a_known_date(written, style):
    assert date_format._style_of(written) == style


def test_the_neutral_c_locale_and_an_unusable_one_mean_iso(monkeypatch):
    calls = []

    def c_locale(category, value=None):
        calls.append(value)
        return "C"

    monkeypatch.setattr(locale, "setlocale", c_locale)
    assert date_format._read_locale_style() == "iso"

    def broken(category, value=None):
        raise locale.Error("unsupported locale setting")

    monkeypatch.setattr(locale, "setlocale", broken)
    assert date_format._read_locale_style() == "iso"


def test_reading_the_locale_puts_the_process_locale_back():
    before = locale.setlocale(locale.LC_TIME)
    date_format._read_locale_style()
    assert locale.setlocale(locale.LC_TIME) == before


def test_iso_dates_in_text_are_rewritten_and_nothing_else():
    text = "Paid 2026-03-05 (due 2026-02-30); ref 12026-03-05 and 2026-03-05-01"
    assert date_format.rewrite_iso_dates(text, "dmy") == (
        "Paid 05/03/2026 (due 2026-02-30); ref 12026-03-05 and 2026-03-05-01"
    )
    assert date_format.rewrite_iso_dates(text, "iso") is text


def test_typed_short_dates_follow_the_style_and_iso_never_changes():
    base = date(2026, 1, 20)
    assert parse_entry_date("3/4", base) == date(2026, 3, 4)
    assert parse_entry_date("3/4", base, day_first=True) == date(2026, 4, 3)
    assert parse_entry_date("15.3.26", base, day_first=True) == date(2026, 3, 15)
    assert parse_entry_date("2026-03-04", base, day_first=True) == date(2026, 3, 4)
    assert parse_entry_date("t", base, date(2026, 5, 6), day_first=True) == date(2026, 5, 6)
    with pytest.raises(EntryInputError):
        parse_entry_date("15/3", base)  # no 15th month when month comes first


def test_error_examples_are_written_in_the_style():
    assert date_format.entry_examples("mdy") == "03/15/2026, 3/15, 15, or t"
    assert date_format.entry_examples("dmy-dot") == "15.03.2026, 15/3, 15, or t"


def test_the_process_style_is_kept_and_an_unknown_one_is_the_default():
    date_format.set_current("dmy")
    assert date_format.current() == "dmy"
    date_format.set_current("bogus")
    assert date_format.current() == date_format.DEFAULT_STYLE
