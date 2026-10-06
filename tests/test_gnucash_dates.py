"""GnuCash SQL books written by older versions keep their calendar dates.

GnuCash 2.6.10 and later store a posting date as 10:59:00 UTC. Earlier versions
stored the user's local midnight converted to UTC, so a household east of UTC has
dates such as ``2026-01-14 22:00:00`` that mean 15 January. Truncating to the UTC
date put every such transaction a day early.
"""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest
from gnucash_fixtures import create_book

from breadsched.plugins.importer import gnucash_sqlite
from breadsched.plugins.importer.gnucash_common import (
    parse_gnc_date,
    parse_gnc_sql_posting_date,
)

JAN_15 = date(2026, 1, 15)


@pytest.mark.parametrize(
    ("stored", "zone"),
    [
        ("2026-01-15 10:59:00", "GnuCash's neutral time"),
        ("20260115105900", "neutral time, 14-digit form"),
        ("2026-01-15 00:00:00", "local midnight in UTC"),
        ("2026-01-15 05:00:00", "local midnight in UTC-5"),
        ("2026-01-15 10:00:00", "local midnight in UTC-10 (Hawaii)"),
        ("2026-01-14 23:00:00", "local midnight in UTC+1"),
        ("2026-01-14 22:00:00", "local midnight in UTC+2"),
        ("20260114220000", "UTC+2, 14-digit form"),
        ("2026-01-14 14:30:00", "local midnight in UTC+9:30"),
        ("2026-01-14 11:00:00", "local midnight in UTC+13 (New Zealand summer)"),
        ("2026-01-15T05:00:00", "an ISO separator"),
        ("2026-01-15", "a date without a time"),
        ("2026-01-15 00:00:00 +0200", "an explicit zone"),
    ],
)
def test_sql_posting_dates_keep_the_calendar_date(stored, zone):
    assert parse_gnc_sql_posting_date(stored) == JAN_15, zone


def test_unreadable_dates_are_refused():
    for raw in ("", "   ", None, "15/01/2026", "2026-13-01 00:00:00"):
        with pytest.raises(ValueError):
            parse_gnc_sql_posting_date(raw)


def test_price_and_xml_timestamps_are_read_as_written():
    # A price's time is when the quote was taken, and XML carries its own zone.
    assert parse_gnc_date("2026-01-14 22:00:00") == date(2026, 1, 14)
    assert parse_gnc_date("2026-01-14 22:00:00 +0000") == date(2026, 1, 14)


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ("2026-01-14 22:00:00", JAN_15),
        ("20260114230000", JAN_15),
        ("2026-01-15 05:00:00", JAN_15),
        ("2026-01-15 10:59:00", JAN_15),
    ],
)
def test_an_imported_book_keeps_its_posting_dates(db, tmp_path, stored, expected):
    chart = [
        ("root", "Root Account", "ROOT", None, 0),
        ("bank", "Checking", "BANK", "root", 0),
        ("food", "Groceries", "EXPENSE", "root", 0),
    ]
    shop = (date(2026, 1, 15), "Weekly shop", [("food", 7250, 100, ""), ("bank", -7250, 100, "")])
    book = create_book(tmp_path / "old.gnucash", chart, [shop])
    conn = sqlite3.connect(book.path)
    conn.execute("UPDATE transactions SET post_date = ?", (stored,))
    conn.commit()
    conn.close()

    result = gnucash_sqlite.import_book(db, book.path)

    assert result.transactions == 1
    [transaction] = list(db.iter_transactions())
    assert transaction.post_date == expected
