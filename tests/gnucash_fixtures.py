"""Builders for synthetic GnuCash SQLite books.

Kept in a module of its own rather than in ``conftest.py`` so that both the test
suite and ``examples/demo.py`` can use it without either importing the other's
package path. Tests reach it as a plain top-level import, because pytest puts the
``tests`` directory on ``sys.path``; the demo adds that directory explicitly.

The schema below is GnuCash's own, column for column. Generating fixtures from
readable code rather than checking in binary books means that when a future
GnuCash release changes the schema, the diff that explains the breakage is right
here.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterable, Sequence
from datetime import date
from pathlib import Path
from types import SimpleNamespace

__all__ = [
    "GNUCASH_SCHEMA",
    "new_guid",
    "write_account",
    "write_commodity",
    "write_price",
    "write_transaction",
    "create_book",
]

GNUCASH_SCHEMA = """
CREATE TABLE versions (table_name text(50) PRIMARY KEY NOT NULL, table_version integer NOT NULL);
CREATE TABLE books (
    guid text(32) PRIMARY KEY NOT NULL, root_account_guid text(32) NOT NULL,
    root_template_guid text(32) NOT NULL);
CREATE TABLE commodities (
    guid text(32) PRIMARY KEY NOT NULL, namespace text(2048) NOT NULL,
    mnemonic text(2048) NOT NULL, fullname text(2048), cusip text(2048),
    fraction integer NOT NULL, quote_flag integer NOT NULL,
    quote_source text(2048), quote_tz text(2048));
CREATE TABLE prices (
    guid text(32) PRIMARY KEY NOT NULL, commodity_guid text(32) NOT NULL,
    currency_guid text(32) NOT NULL, date text(19) NOT NULL,
    source text(2048), type text(2048),
    value_num bigint NOT NULL, value_denom bigint NOT NULL);
CREATE TABLE accounts (
    guid text(32) PRIMARY KEY NOT NULL, name text(2048) NOT NULL,
    account_type text(2048) NOT NULL, commodity_guid text(32),
    commodity_scu integer NOT NULL, non_std_scu integer NOT NULL,
    parent_guid text(32), code text(2048), description text(2048),
    hidden integer, placeholder integer);
CREATE TABLE transactions (
    guid text(32) PRIMARY KEY NOT NULL, currency_guid text(32) NOT NULL,
    num text(2048) NOT NULL, post_date text(19), enter_date text(19),
    description text(2048));
CREATE TABLE splits (
    guid text(32) PRIMARY KEY NOT NULL, tx_guid text(32) NOT NULL,
    account_guid text(32) NOT NULL, memo text(2048) NOT NULL,
    action text(2048) NOT NULL, reconcile_state text(1) NOT NULL,
    reconcile_date text(19), value_num bigint NOT NULL, value_denom bigint NOT NULL,
    quantity_num bigint NOT NULL, quantity_denom bigint NOT NULL, lot_guid text(32));
CREATE TABLE slots (
    id integer PRIMARY KEY AUTOINCREMENT NOT NULL, obj_guid text(32) NOT NULL,
    name text(4096) NOT NULL, slot_type integer NOT NULL, int64_val bigint,
    string_val text(4096), double_val float8, timespec_val text(19),
    guid_val text(32), numeric_val_num bigint, numeric_val_denom bigint,
    gdate_val text(8));
CREATE TABLE schedxactions (
    guid text(32) PRIMARY KEY NOT NULL, name text(2048), enabled integer NOT NULL,
    start_date text(8), end_date text(8), last_occur text(8),
    num_occur integer NOT NULL, rem_occur integer NOT NULL,
    auto_create integer NOT NULL, auto_notify integer NOT NULL,
    adv_creation integer NOT NULL, adv_notify integer NOT NULL,
    instance_count integer NOT NULL, template_act_guid text(32) NOT NULL);
CREATE TABLE recurrences (
    id integer PRIMARY KEY AUTOINCREMENT NOT NULL, obj_guid text(32) NOT NULL,
    recurrence_mult integer NOT NULL, recurrence_period_type text(2048) NOT NULL,
    recurrence_period_start text(8) NOT NULL,
    recurrence_weekend_adjust text(2048) NOT NULL);
"""


def new_guid() -> str:
    """A GnuCash GUID: 32 lowercase hex characters."""
    return uuid.uuid4().hex


def write_commodity(
    conn: sqlite3.Connection,
    guid: str,
    namespace: str = "CURRENCY",
    mnemonic: str = "USD",
    fullname: str = "US Dollar",
    fraction: int = 100,
) -> str:
    conn.execute(
        "INSERT INTO commodities VALUES (?,?,?,?,?,?,?,?,?)",
        (guid, namespace, mnemonic, fullname, "840", fraction, 0, None, None),
    )
    return guid


def write_account(
    conn: sqlite3.Connection,
    guid: str,
    name: str,
    atype: str,
    parent: str | None,
    commodity: str | None,
    code: str = "",
    description: str = "",
    placeholder: int = 0,
    hidden: int = 0,
) -> str:
    conn.execute(
        "INSERT INTO accounts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (guid, name, atype, commodity, 100, 0, parent, code, description, hidden, placeholder),
    )
    return guid


def write_price(
    conn: sqlite3.Connection,
    guid: str,
    commodity: str,
    currency: str,
    when: date,
    numerator: int,
    denominator: int = 100,
    source: str = "user:price-editor",
    quote_type: str = "last",
) -> str:
    stamp = when.strftime("%Y%m%d") + "104000"
    conn.execute(
        "INSERT INTO prices VALUES (?,?,?,?,?,?,?,?)",
        (guid, commodity, currency, stamp, source, quote_type, numerator, denominator),
    )
    return guid


def write_transaction(
    conn: sqlite3.Connection,
    guid: str,
    currency: str,
    when: date,
    description: str,
    splits: Iterable[Sequence],
    num: str = "",
) -> str:
    """Write a transaction and its splits.

    ``splits`` items are ``(account_guid, value_num, value_denom, memo)``. The
    timestamp uses the 14-digit form current GnuCash writes.
    """
    stamp = when.strftime("%Y%m%d") + "104000"
    conn.execute(
        "INSERT INTO transactions VALUES (?,?,?,?,?,?)",
        (guid, currency, num, stamp, stamp, description),
    )
    for account, numerator, denominator, memo in splits:
        conn.execute(
            "INSERT INTO splits VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                new_guid(),
                guid,
                account,
                memo,
                "",
                "n",
                None,
                numerator,
                denominator,
                numerator,
                denominator,
                None,
            ),
        )
    return guid


def create_book(
    path: str | Path,
    accounts: Sequence[tuple],
    transactions: Sequence[tuple] = (),
    template_root: bool = True,
) -> SimpleNamespace:
    """Create a complete GnuCash SQLite book from a compact description.

    ``accounts`` items are ``(key, name, type, parent_key, placeholder)``, where
    ``parent_key`` refers to an earlier entry's key or is ``None`` for the root.
    ``transactions`` items are ``(date, description, [(account_key, num, denom,
    memo), ...])``.

    Returns a namespace whose attributes are the keys, mapped to their GUIDs, plus
    ``path`` and ``currency``.
    """
    path = Path(path)
    conn = sqlite3.connect(path)
    conn.executescript(GNUCASH_SCHEMA)

    currency = write_commodity(conn, new_guid())
    ids: dict[str, str] = {}

    for key, name, atype, parent_key, placeholder in accounts:
        ids[key] = write_account(
            conn,
            new_guid(),
            name,
            atype,
            ids[parent_key] if parent_key else None,
            currency,
            placeholder=placeholder,
        )

    for when, description, splits in transactions:
        write_transaction(
            conn,
            new_guid(),
            currency,
            when,
            description,
            [(ids[key], num, denom, memo) for key, num, denom, memo in splits],
        )

    # Every real GnuCash book names its two roots in the books table: the chart of
    # accounts, and the hidden tree holding scheduled-transaction templates.
    book_root = ids[accounts[0][0]] if accounts else new_guid()
    template = new_guid()
    if template_root:
        write_account(conn, template, "Template Root", "ROOT", None, currency)
    conn.execute("INSERT INTO books VALUES (?,?,?)", (new_guid(), book_root, template))

    conn.commit()
    conn.close()
    return SimpleNamespace(path=str(path), currency=currency, template_root=template, **ids)
