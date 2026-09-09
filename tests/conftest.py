"""Shared fixtures.

The GnuCash fixtures build real books in both container formats rather than
checking binary files into the repository.  A generated fixture states the schema
it depends on in readable code, so when the importer breaks against a new GnuCash
release the fixture is the diff that explains why.
"""

from __future__ import annotations

import gzip
import logging
import sqlite3
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

# A plain top-level import: pytest puts the tests directory on sys.path, so this
# works regardless of the working directory pytest was launched from.
from gnucash_fixtures import (
    GNUCASH_SCHEMA,
    new_guid,
    write_account,
    write_commodity,
    write_transaction,
)

from cashperspective.gen.db.sqlite import DbSQLite
from cashperspective.gen.lib import (
    Account,
    AccountType,
    Budget,
    Money,
    PeriodType,
    Recurrence,
    ScheduledSplit,
    ScheduledTransaction,
    Transaction,
)


@pytest.fixture(autouse=True)
def isolate_logging():
    """Stop one test's logging setup leaking into the next.

    ``logs.configure`` deliberately sets ``propagate = False`` so an embedding
    application's root handlers do not receive our records. The side effect is that
    pytest's ``caplog``, whose handler lives on the root logger, goes deaf for the
    rest of the session once any test configures logging. That makes failures
    depend on test order, which is the worst kind to debug.
    """
    logger = logging.getLogger("cashperspective")
    handlers = list(logger.handlers)
    level, propagate = logger.level, logger.propagate
    yield
    for handler in list(logger.handlers):
        if handler not in handlers:
            logger.removeHandler(handler)
            handler.close()
    logger.handlers[:] = handlers
    logger.setLevel(level)
    logger.propagate = propagate


class LogCollector(logging.Handler):
    """Collects formatted messages, attached straight to the logger under test."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())

    def containing(self, text: str) -> list[str]:
        return [message for message in self.messages if text in message]


@pytest.fixture
def cashperspective_logs():
    """Capture cashperspective log records regardless of propagation or handlers.

    Attaching to the ``cashperspective`` logger directly, rather than relying on records
    reaching the root, makes the capture independent of whatever
    ``logs.configure`` has done to the logger.
    """
    logger = logging.getLogger("cashperspective")
    collector = LogCollector()
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(collector)
    try:
        yield collector
    finally:
        logger.removeHandler(collector)
        logger.setLevel(previous)


# --------------------------------------------------------------- native books


@pytest.fixture
def db():
    database = DbSQLite.create_memory()
    yield database
    database.close()


@pytest.fixture
def book(db):
    """A small household chart of accounts, returned as a namespace of handles."""
    with db.transaction("Set up chart of accounts") as txn:
        def add(name, atype, parent=None, **kwargs):
            account = Account(name=name, atype=atype, parent=parent, **kwargs)
            db.add_account(account, txn)
            return account

        root = add("Root", AccountType.ROOT)
        assets = add("Assets", AccountType.ASSET, root.handle, placeholder=True)
        checking = add("Checking", AccountType.BANK, assets.handle)
        savings = add("Savings", AccountType.BANK, assets.handle)
        brokerage = add("Brokerage", AccountType.MUTUAL, assets.handle)
        brokerage.annual_return = Decimal("0.07")
        db.commit_account(brokerage, txn)

        liabilities = add("Liabilities", AccountType.LIABILITY, root.handle, placeholder=True)
        card = add("Credit Card", AccountType.CREDIT, liabilities.handle)
        card.annual_interest = Decimal("0.1899")
        db.commit_account(card, txn)

        equity = add("Equity", AccountType.EQUITY, root.handle, placeholder=True)
        opening = add("Opening Balances", AccountType.EQUITY, equity.handle)

        income = add("Income", AccountType.INCOME, root.handle, placeholder=True)
        salary = add("Salary", AccountType.INCOME, income.handle)

        expenses = add("Expenses", AccountType.EXPENSE, root.handle, placeholder=True)
        rent = add("Rent", AccountType.EXPENSE, expenses.handle)
        groceries = add("Groceries", AccountType.EXPENSE, expenses.handle)
        utilities = add("Utilities", AccountType.EXPENSE, expenses.handle)

    return SimpleNamespace(
        root=root.handle,
        assets=assets.handle,
        checking=checking.handle,
        savings=savings.handle,
        brokerage=brokerage.handle,
        liabilities=liabilities.handle,
        card=card.handle,
        equity=equity.handle,
        opening=opening.handle,
        income=income.handle,
        salary=salary.handle,
        expenses=expenses.handle,
        rent=rent.handle,
        groceries=groceries.handle,
        utilities=utilities.handle,
    )


@pytest.fixture
def funded_book(db, book):
    """The chart of accounts with an opening balance and a month of activity."""
    with db.transaction("Opening activity") as txn:
        db.add_transaction(
            Transaction.simple(
                date(2026, 1, 1), "Opening balance", book.checking, book.opening, "5000.00"
            ),
            txn,
        )
        db.add_transaction(
            Transaction.simple(
                date(2026, 1, 25), "January salary", book.checking, book.salary, "4200.00"
            ),
            txn,
        )
        db.add_transaction(
            Transaction.simple(
                date(2026, 1, 2), "January rent", book.rent, book.checking, "1800.00"
            ),
            txn,
        )
        db.add_transaction(
            Transaction.simple(
                date(2026, 1, 12), "Weekly shop", book.groceries, book.checking, "310.55"
            ),
            txn,
        )
        db.add_transaction(
            Transaction.simple(
                date(2026, 2, 3), "February rent", book.rent, book.checking, "1800.00"
            ),
            txn,
        )
        db.add_transaction(
            Transaction.simple(
                date(2026, 2, 14), "Dinner out", book.groceries, book.card, "86.40"
            ),
            txn,
        )
    return book


@pytest.fixture
def monthly_budget(db, book):
    """A twelve-month cash-flow budget for 2026."""
    budget = Budget(name="2026", start=date(2026, 1, 1), periods=12)
    budget.set_monthly(book.salary, "4200.00")
    budget.set_monthly(book.rent, "1800.00")
    budget.set_monthly(book.groceries, "600.00")
    budget.set_monthly(book.utilities, "150.00")
    # Heating is seasonal: the point of per-period amounts.
    budget.set_amount(book.utilities, 0, "310.00")
    budget.set_amount(book.utilities, 1, "290.00")
    with db.transaction("Add budget") as txn:
        db.add_budget(budget, txn)
    return budget


@pytest.fixture
def payday_schedule(db, book):
    """Fortnightly salary, adjusted back off weekends."""
    sched = ScheduledTransaction(
        name="Salary",
        description="Fortnightly pay",
        recurrence=Recurrence(
            period=PeriodType.WEEK, interval=2, start=date(2026, 1, 2)
        ),
        splits=[
            ScheduledSplit(book.checking, Money("1938.46")),
            ScheduledSplit(book.salary, Money("-1938.46")),
        ],
        auto_create=True,
    )
    with db.transaction("Add payday") as txn:
        db.add_scheduled(sched, txn)
    return sched


# -------------------------------------------------------------- GnuCash books

@pytest.fixture
def gnucash_sqlite_path(tmp_path):
    """A GnuCash SQLite3 book with accounts, transactions and a schedule."""
    path = tmp_path / "household.gnucash"
    conn = sqlite3.connect(path)
    conn.executescript(GNUCASH_SCHEMA)

    usd = new_guid()
    write_commodity(conn, usd)

    ids = SimpleNamespace(
        currency=usd, root=new_guid(), assets=new_guid(), checking=new_guid(), card=new_guid(),
        income=new_guid(), salary=new_guid(), expenses=new_guid(), rent=new_guid(), food=new_guid(),
        template_root=new_guid(), template=new_guid(), sched=new_guid(),
    )
    write_account(conn, ids.root, "Root Account", "ROOT", None, usd)
    write_account(conn, ids.assets, "Assets", "ASSET", ids.root, usd, placeholder=1)
    write_account(conn, ids.checking, "Checking Account", "BANK", ids.assets, usd,
                 code="1010", description="Everyday account")
    write_account(conn, ids.card, "Credit Card", "CREDIT", ids.root, usd)
    write_account(conn, ids.income, "Income", "INCOME", ids.root, usd, placeholder=1)
    write_account(conn, ids.salary, "Salary", "INCOME", ids.income, usd)
    write_account(conn, ids.expenses, "Expenses", "EXPENSE", ids.root, usd, placeholder=1)
    write_account(conn, ids.rent, "Rent", "EXPENSE", ids.expenses, usd)
    write_account(conn, ids.food, "Groceries", "EXPENSE", ids.expenses, usd)

    write_transaction(conn, new_guid(), usd, date(2026, 1, 25), "Payroll deposit", [
        (ids.checking, 420000, 100, ""),
        (ids.salary, -420000, 100, ""),
    ])
    write_transaction(conn, new_guid(), usd, date(2026, 1, 2), "Rent", [
        (ids.rent, 180000, 100, "January"),
        (ids.checking, -180000, 100, ""),
    ])
    # A three-split transaction: the card pays part of a grocery run.
    write_transaction(conn, new_guid(), usd, date(2026, 1, 14), "Supermarket", [
        (ids.food, 12550, 100, ""),
        (ids.checking, -5000, 100, ""),
        (ids.card, -7550, 100, ""),
    ])

    # Scheduled transaction: monthly rent, with the real account carried on a slot.
    write_account(conn, ids.template_root, "Template Root", "ROOT", None, usd)
    write_account(conn, ids.template, "Rent template", "BANK", ids.template_root, usd)
    template_txn = new_guid()
    conn.execute(
        "INSERT INTO transactions VALUES (?,?,?,?,?,?)",
        (template_txn, usd, "", "20260101000000", "20260101000000", "Rent"),
    )
    for account, value, real in (
        (ids.template, 180000, ids.rent),
        (ids.template, -180000, ids.checking),
    ):
        split_guid = new_guid()
        conn.execute(
            "INSERT INTO splits VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (split_guid, template_txn, account, "", "", "n", None, value, 100,
             value, 100, None),
        )
        conn.execute(
            "INSERT INTO slots (obj_guid,name,slot_type,guid_val) VALUES (?,?,?,?)",
            (split_guid, "sched-xaction/account", 10, real),
        )
    conn.execute(
        "INSERT INTO schedxactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ids.sched, "Monthly rent", 1, "20260101", None, None, 0, 0, 1, 0, 3, 0, 0,
         ids.template),
    )
    conn.execute(
        "INSERT INTO recurrences (obj_guid,recurrence_mult,recurrence_period_type,"
        "recurrence_period_start,recurrence_weekend_adjust) VALUES (?,?,?,?,?)",
        (ids.sched, 1, "month", "20260101", "none"),
    )

    conn.commit()
    conn.close()
    return SimpleNamespace(path=str(path), ids=ids)


GNUCASH_XML = """<?xml version="1.0" encoding="utf-8" ?>
<gnc-v2
     xmlns:gnc="http://www.gnucash.org/XML/gnc"
     xmlns:act="http://www.gnucash.org/XML/act"
     xmlns:trn="http://www.gnucash.org/XML/trn"
     xmlns:split="http://www.gnucash.org/XML/split"
     xmlns:cmdty="http://www.gnucash.org/XML/cmdty"
     xmlns:ts="http://www.gnucash.org/XML/ts"
     xmlns:cd="http://www.gnucash.org/XML/cd"
     xmlns:slot="http://www.gnucash.org/XML/slot"
     xmlns:sx="http://www.gnucash.org/XML/sx"
     xmlns:recurrence="http://www.gnucash.org/XML/recurrence">
<gnc:count-data cd:type="book">1</gnc:count-data>
<gnc:book version="2.0.0">
  <gnc:commodity version="2.0.0">
    <cmdty:space>CURRENCY</cmdty:space>
    <cmdty:id>GBP</cmdty:id>
    <cmdty:name>Pound Sterling</cmdty:name>
    <cmdty:fraction>100</cmdty:fraction>
  </gnc:commodity>
  <gnc:account version="2.0.0">
    <act:name>Root Account</act:name>
    <act:id type="guid">{root}</act:id>
    <act:type>ROOT</act:type>
  </gnc:account>
  <gnc:account version="2.0.0">
    <act:name>Current Account</act:name>
    <act:id type="guid">{bank}</act:id>
    <act:type>BANK</act:type>
    <act:commodity><cmdty:space>CURRENCY</cmdty:space><cmdty:id>GBP</cmdty:id></act:commodity>
    <act:code>1200</act:code>
    <act:description>Day to day</act:description>
    <act:parent type="guid">{root}</act:parent>
  </gnc:account>
  <gnc:account version="2.0.0">
    <act:name>Wages</act:name>
    <act:id type="guid">{wages}</act:id>
    <act:type>INCOME</act:type>
    <act:parent type="guid">{root}</act:parent>
  </gnc:account>
  <gnc:account version="2.0.0">
    <act:name>Utilities</act:name>
    <act:id type="guid">{util}</act:id>
    <act:type>EXPENSE</act:type>
    <act:parent type="guid">{root}</act:parent>
    <act:slots>
      <slot><slot:key>placeholder</slot:key><slot:value type="string">false</slot:value></slot>
    </act:slots>
  </gnc:account>
  <gnc:transaction version="2.0.0">
    <trn:id type="guid">{txn1}</trn:id>
    <trn:currency><cmdty:space>CURRENCY</cmdty:space><cmdty:id>GBP</cmdty:id></trn:currency>
    <trn:date-posted><ts:date>2026-03-25 10:59:00 +0000</ts:date></trn:date-posted>
    <trn:description>March salary</trn:description>
    <trn:splits>
      <trn:split>
        <split:id type="guid">{split1}</split:id>
        <split:reconciled-state>c</split:reconciled-state>
        <split:value>318750/100</split:value>
        <split:quantity>318750/100</split:quantity>
        <split:account type="guid">{bank}</split:account>
      </trn:split>
      <trn:split>
        <split:id type="guid">{split2}</split:id>
        <split:reconciled-state>n</split:reconciled-state>
        <split:value>-318750/100</split:value>
        <split:quantity>-318750/100</split:quantity>
        <split:account type="guid">{wages}</split:account>
      </trn:split>
    </trn:splits>
  </gnc:transaction>
  <gnc:transaction version="2.0.0">
    <trn:id type="guid">{txn2}</trn:id>
    <trn:currency><cmdty:space>CURRENCY</cmdty:space><cmdty:id>GBP</cmdty:id></trn:currency>
    <trn:date-posted><ts:date>2026-03-28 10:59:00 +0000</ts:date></trn:date-posted>
    <trn:description>Electricity</trn:description>
    <trn:num>DD</trn:num>
    <trn:splits>
      <trn:split>
        <split:id type="guid">{split3}</split:id>
        <split:memo>quarterly</split:memo>
        <split:reconciled-state>n</split:reconciled-state>
        <split:value>9433/100</split:value>
        <split:quantity>9433/100</split:quantity>
        <split:account type="guid">{util}</split:account>
      </trn:split>
      <trn:split>
        <split:id type="guid">{split4}</split:id>
        <split:reconciled-state>n</split:reconciled-state>
        <split:value>-9433/100</split:value>
        <split:quantity>-9433/100</split:quantity>
        <split:account type="guid">{bank}</split:account>
      </trn:split>
    </trn:splits>
  </gnc:transaction>
  <gnc:template-transactions>
    <gnc:account version="2.0.0">
      <act:name>Template Root</act:name>
      <act:id type="guid">{tmpl_root}</act:id>
      <act:type>ROOT</act:type>
    </gnc:account>
    <gnc:account version="2.0.0">
      <act:name>{tmpl_acct}</act:name>
      <act:id type="guid">{tmpl_acct}</act:id>
      <act:type>BANK</act:type>
      <act:parent type="guid">{tmpl_root}</act:parent>
    </gnc:account>
    <gnc:transaction version="2.0.0">
      <trn:id type="guid">{tmpl_txn}</trn:id>
      <trn:currency><cmdty:space>CURRENCY</cmdty:space><cmdty:id>GBP</cmdty:id></trn:currency>
      <trn:date-posted><ts:date>2026-01-01 00:00:00 +0000</ts:date></trn:date-posted>
      <trn:description>Rent</trn:description>
      <trn:splits>
        <trn:split>
          <split:id type="guid">{tmpl_split1}</split:id>
          <split:reconciled-state>n</split:reconciled-state>
          <split:value>0/100</split:value>
          <split:quantity>0/100</split:quantity>
          <split:account type="guid">{tmpl_acct}</split:account>
          <split:slots>
            <slot>
              <slot:key>sched-xaction</slot:key>
              <slot:value type="frame">
                <slot><slot:key>account</slot:key>
                  <slot:value type="guid">{util}</slot:value></slot>
                <slot><slot:key>debit-formula</slot:key>
                  <slot:value type="string">825.00</slot:value></slot>
              </slot:value>
            </slot>
          </split:slots>
        </trn:split>
        <trn:split>
          <split:id type="guid">{tmpl_split2}</split:id>
          <split:reconciled-state>n</split:reconciled-state>
          <split:value>0/100</split:value>
          <split:quantity>0/100</split:quantity>
          <split:account type="guid">{tmpl_acct}</split:account>
          <split:slots>
            <slot>
              <slot:key>sched-xaction</slot:key>
              <slot:value type="frame">
                <slot><slot:key>account</slot:key>
                  <slot:value type="guid">{bank}</slot:value></slot>
                <slot><slot:key>credit-formula</slot:key>
                  <slot:value type="string">825.00</slot:value></slot>
              </slot:value>
            </slot>
          </split:slots>
        </trn:split>
      </trn:splits>
    </gnc:transaction>
  </gnc:template-transactions>
  <gnc:schedxaction version="2.0.0">
    <sx:id type="guid">{sx}</sx:id>
    <sx:name>Monthly rent</sx:name>
    <sx:enabled>y</sx:enabled>
    <sx:autoCreate>y</sx:autoCreate>
    <sx:advanceCreateDays>2</sx:advanceCreateDays>
    <sx:instanceCount>3</sx:instanceCount>
    <sx:start><gdate>2026-01-01</gdate></sx:start>
    <sx:templ-acct type="guid">{tmpl_acct}</sx:templ-acct>
    <sx:schedule>
      <gnc:recurrence version="1.0.0">
        <recurrence:mult>1</recurrence:mult>
        <recurrence:period_type>month</recurrence:period_type>
        <recurrence:start><gdate>2026-01-01</gdate></recurrence:start>
      </gnc:recurrence>
    </sx:schedule>
  </gnc:schedxaction>
</gnc:book>
</gnc-v2>
"""


@pytest.fixture
def gnucash_xml_path(tmp_path):
    """A gzip-compressed GnuCash XML book, the format GnuCash saves by default."""
    ids = SimpleNamespace(
        root=new_guid(), bank=new_guid(), wages=new_guid(), util=new_guid(),
        txn1=new_guid(), txn2=new_guid(),
        split1=new_guid(), split2=new_guid(), split3=new_guid(), split4=new_guid(),
        # GnuCash names each template account after the schedule it backs.
        tmpl_root=new_guid(), tmpl_acct=new_guid(), tmpl_txn=new_guid(),
        tmpl_split1=new_guid(), tmpl_split2=new_guid(), sx=new_guid(),
    )
    body = GNUCASH_XML.format(**vars(ids))
    path = tmp_path / "household-xml.gnucash"
    with gzip.open(path, "wb") as handle:
        handle.write(body.encode("utf-8"))
    return SimpleNamespace(path=str(path), ids=ids, plain=body)
