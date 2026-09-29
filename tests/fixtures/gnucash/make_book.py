"""Create the small household book in this folder with real GnuCash.

Run with a Python that has GnuCash's bindings (``python3-gnucash`` and, for SQLite,
``libdbd-sqlite3``), then store the result the way the tests read it::

    /usr/bin/python3 make_book.py "xml://$PWD/book.gnucash"
    zcat book.gnucash > household-gnucash-5.5.xml
    /usr/bin/python3 make_book.py "sqlite3://$PWD/book.sqlite"
    python3 -c "import sqlite3; print('\n'.join(sqlite3.connect('book.sqlite').iterdump()))" \
        > household-gnucash-5.5.sql   # then restore the two comment lines at the top
"""

import sys
from datetime import datetime

from gnucash import Account, GncNumeric, Session, SessionOpenMode, Split, Transaction

uri = sys.argv[1]
if uri.startswith("sqlite3"):
    # A new SQL book must be created, closed, and reopened before it accepts data.
    Session(uri, SessionOpenMode.SESSION_NEW_OVERWRITE).end()
    session = Session(uri, SessionOpenMode.SESSION_NORMAL_OPEN)
else:
    session = Session(uri, SessionOpenMode.SESSION_NEW_OVERWRITE)
try:
    book = session.book
    table = book.get_table()
    usd = table.lookup("CURRENCY", "USD")
    root = book.get_root_account()

    def account(name, kind, parent=root, placeholder=False):
        acc = Account(book)
        acc.BeginEdit()
        acc.SetName(name)
        acc.SetType(kind)
        acc.SetCommodity(usd)
        acc.SetPlaceholder(placeholder)
        parent.append_child(acc)
        acc.CommitEdit()
        return acc

    from gnucash.gnucash_core_c import (
        ACCT_TYPE_ASSET,
        ACCT_TYPE_BANK,
        ACCT_TYPE_CREDIT,
        ACCT_TYPE_EXPENSE,
        ACCT_TYPE_INCOME,
    )

    assets = account("Assets", ACCT_TYPE_ASSET, placeholder=True)
    checking = account("Checking", ACCT_TYPE_BANK, assets)
    savings = account("Savings", ACCT_TYPE_BANK, assets)
    card = account("Credit Card", ACCT_TYPE_CREDIT)
    income = account("Income", ACCT_TYPE_INCOME, placeholder=True)
    salary = account("Salary", ACCT_TYPE_INCOME, income)
    expenses = account("Expenses", ACCT_TYPE_EXPENSE, placeholder=True)
    rent = account("Rent", ACCT_TYPE_EXPENSE, expenses)
    food = account("Groceries", ACCT_TYPE_EXPENSE, expenses)
    utilities = account("Utilities", ACCT_TYPE_EXPENSE, expenses)

    def txn(when, desc, legs, num=""):
        t = Transaction(book)
        t.BeginEdit()
        t.SetCurrency(usd)
        t.SetDate(when.day, when.month, when.year)
        t.SetDescription(desc)
        t.SetNum(num)
        for acc, cents, memo in legs:
            s = Split(book)
            s.SetParent(t)
            s.SetAccount(acc)
            s.SetValue(GncNumeric(cents, 100))
            s.SetAmount(GncNumeric(cents, 100))
            s.SetMemo(memo)
        t.CommitEdit()
        return t

    txn(datetime(2026, 1, 25), "Payroll deposit", [(checking, 420000, ""), (salary, -420000, "")])
    txn(datetime(2026, 2, 1), "Rent", [(rent, 180000, "February"), (checking, -180000, "")], "1001")
    txn(
        datetime(2026, 2, 6),
        "Supermarket",
        [(food, 12550, ""), (checking, -5000, ""), (card, -7550, "")],
    )
    txn(datetime(2026, 2, 10), "Electric", [(utilities, 9312, ""), (checking, -9312, "")])
    txn(datetime(2026, 2, 12), "To savings", [(savings, 50000, ""), (checking, -50000, "")])
    if uri.startswith("xml"):
        session.save()
finally:
    session.end()
