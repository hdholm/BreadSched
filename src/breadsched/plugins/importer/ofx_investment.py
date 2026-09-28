"""Investment statement transactions from OFX/QFX files.

An ``INVSTMTRS`` statement names a brokerage account (``INVACCTFROM``) and lists
trades, income, and cash activity. Each record becomes one balanced transaction:

- ``BUY*``/``SELL*`` (``INVBUY``/``INVSELL``): the security account receives the
  units as quantity; cash moves by ``TOTAL``; commission, fees, load, and taxes go
  to an expense category. The security split's value is whatever balances the
  cash and costs, so the statement's own rounding is kept. No cost basis or
  realized gain is computed.
- ``REINVEST``: units bought with income that never passes through cash.
- ``INCOME`` and ``INVEXPENSE``: cash against an income or expense category.
- ``INVBANKTRAN``: an ordinary cash deposit or withdrawal.

Options, transfers, splits, journal entries, return of capital, and margin
interest are reported as skipped rather than guessed. Securities are matched by
ticker to one already in the book, else created from the statement's security
list; accounts are created under Assets with stable identities, so a re-import
refreshes rather than duplicates. Categories follow the bank importer: the
statement owns the brokerage side, and a category changed after import is kept.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from ...gen.db.sqlite import DbSQLite
from ...gen.lib.money import Money
from ...gen.utils.amount_input import NumberFormat, parse_decimal_amount
from .gnucash_common import ImportSink
from .quotes import security_by_symbol

__all__ = ["import_investment_statement"]

_TRADES = {
    "BUYDEBT": "INVBUY",
    "BUYMF": "INVBUY",
    "BUYOTHER": "INVBUY",
    "BUYSTOCK": "INVBUY",
    "SELLDEBT": "INVSELL",
    "SELLMF": "INVSELL",
    "SELLOTHER": "INVSELL",
    "SELLSTOCK": "INVSELL",
}
_SKIPPED = (
    "BUYOPT",
    "SELLOPT",
    "CLOSUREOPT",
    "JRNLFUND",
    "JRNLSEC",
    "MARGININTEREST",
    "RETOFCAP",
    "SPLIT",
    "TRANSFER",
)
_COST_TAGS = ("COMMISSION", "FEES", "LOAD", "TAXES")


def _handle(kind: str, *parts: object) -> str:
    text = "|".join(str(part) for part in parts)
    return uuid5(NAMESPACE_URL, f"breadsched:ofx:{kind}:{text}").hex


class _Statement:
    """Parse helpers bound to one statement's number format and accounts."""

    def __init__(
        self,
        sink: ImportSink,
        db: DbSQLite,
        *,
        tag,
        blocks,
        parse_date,
        number_format: NumberFormat,
        account_id: str,
        broker: str,
        currency: str,
    ) -> None:
        self.sink = sink
        self.db = db
        self.tag = tag
        self.blocks = blocks
        self.parse_date = parse_date
        self.number_format = number_format
        self.account_id = account_id
        self.currency = currency
        tail = account_id[-4:] if account_id else "unknown"
        label = f"{broker} Investment {tail}".strip()
        assets = db.get_account_by_name("Assets")
        self.parent = sink.account(
            _handle("investment-account", account_id, broker),
            label,
            "ASSET",
            assets.handle if assets is not None else None,
            commodity=currency,
            description=f"Imported OFX investment account ending {tail}",
        ).handle
        self.cash = sink.account(
            _handle("investment-cash", account_id, broker),
            "Cash",
            "BANK",
            self.parent,
            commodity=currency,
        ).handle
        self._securities: dict[str, str] = {}
        self._security_names: dict[str, tuple[str, str, str]] = {}

    def money(self, raw: str) -> Money:
        return Money(parse_decimal_amount(raw, self.number_format))

    def optional(self, block: str, name: str) -> Money:
        raw = self.tag(block, name)
        return self.money(raw) if raw else Money(0)

    def category(self, amount: Money, name: str) -> str:
        """An expense (positive ``amount``) or income (negative) category."""
        top = "Expenses" if amount > 0 else "Income"
        atype = "EXPENSE" if amount > 0 else "INCOME"
        parent = self.db.get_account_by_name(top)
        return self.sink.account(
            _handle("category", atype, name),
            name,
            atype,
            parent.handle if parent is not None else None,
        ).handle

    def learn_securities(self, text: str) -> None:
        for kind in ("MFINFO", "STOCKINFO", "DEBTINFO", "OPTINFO", "OTHERINFO"):
            for block in self.blocks(text, kind):
                unique = self.tag(block, "UNIQUEID")
                ticker = self.tag(block, "TICKER") or unique
                name = self.tag(block, "SECNAME") or ticker
                if unique:
                    self._security_names[unique] = (ticker, name, kind)

    def security_account(self, unique: str) -> str | None:
        """The brokerage sub-account holding ``unique``, creating it if needed."""
        if unique in self._securities:
            return self._securities[unique]
        ticker, name, kind = self._security_names.get(unique, (unique, unique, "OTHERINFO"))
        if not ticker:
            return None
        existing = security_by_symbol(self.db, ticker)
        if existing is not None:
            commodity = existing.handle
        else:
            namespace = {"MFINFO": "FUND", "STOCKINFO": "STOCK", "DEBTINFO": "BOND"}.get(
                kind, "SECURITY"
            )
            commodity = self.sink.commodity(namespace, ticker, name, fraction=10000)
        account = self.sink.account(
            _handle("security-account", self.account_id, unique),
            ticker,
            "MUTUAL" if kind == "MFINFO" else "STOCK",
            self.parent,
            commodity=commodity,
            description=name,
            commodity_scu=10000,
        ).handle
        self._securities[unique] = account
        return account

    def costs(self, block: str) -> Money:
        total = Money(0)
        for name in _COST_TAGS:
            total = total + self.optional(block, name)
        return total


def _description(stmt: _Statement, block: str, fallback: str) -> str:
    return stmt.tag(block, "MEMO") or stmt.tag(block, "NAME") or fallback


def _trade_splits(
    stmt: _Statement, kind: str, block: str
) -> tuple[list[dict[str, Any]], str] | str:
    """Balanced splits and a description, or the reason the record is skipped."""
    unique = stmt.tag(block, "UNIQUEID")
    security = stmt.security_account(unique) if unique else None
    if security is None:
        return "OFX trade does not name a security"
    units = stmt.money(stmt.tag(block, "UNITS") or "0")
    if not units:
        return "OFX trade has no units"
    if kind.startswith("SELL") and units > 0:
        units = -units  # some institutions report sold units unsigned
    costs = stmt.costs(block)
    raw_total = stmt.tag(block, "TOTAL")
    price = stmt.optional(block, "UNITPRICE")
    total = stmt.money(raw_total) if raw_total else -(units * price.rate()) - costs
    splits: list[dict[str, Any]] = [
        {"account": stmt.cash, "value": total},
        {"account": security, "value": -total - costs, "quantity": units},
    ]
    if costs:
        splits.append({"account": stmt.category(costs, "Investment Fees"), "value": costs})
    ticker = stmt._security_names.get(unique, (unique,))[0]
    verb = "Buy" if kind.startswith("BUY") else "Sell"
    return splits, _description(stmt, block, f"{verb} {ticker}")


def _reinvest_splits(stmt: _Statement, block: str) -> tuple[list[dict[str, Any]], str] | str:
    unique = stmt.tag(block, "UNIQUEID")
    security = stmt.security_account(unique) if unique else None
    if security is None:
        return "OFX reinvestment does not name a security"
    units = stmt.money(stmt.tag(block, "UNITS") or "0")
    costs = stmt.costs(block)
    raw_total = stmt.tag(block, "TOTAL")
    if raw_total:
        income = -abs(stmt.money(raw_total))
    else:
        income = -(units * stmt.optional(block, "UNITPRICE").rate()) - costs
    splits: list[dict[str, Any]] = [
        {"account": stmt.category(income, "Investment Income"), "value": income},
        {"account": security, "value": -income - costs, "quantity": units},
    ]
    if costs:
        splits.append({"account": stmt.category(costs, "Investment Fees"), "value": costs})
    ticker = stmt._security_names.get(unique, (unique,))[0]
    return splits, _description(stmt, block, f"Reinvest {ticker}")


def _cash_splits(
    stmt: _Statement, block: str, name: str, fallback: str
) -> tuple[list[dict[str, Any]], str] | str:
    raw_total = stmt.tag(block, "TOTAL")
    if not raw_total:
        return "OFX investment record has no total"
    total = stmt.money(raw_total)
    if not total:
        return "OFX investment record has a zero total"
    return (
        [
            {"account": stmt.cash, "value": total},
            {"account": stmt.category(-total, name), "value": -total},
        ],
        _description(stmt, block, fallback),
    )


def import_investment_statement(
    sink: ImportSink,
    db: DbSQLite,
    text: str,
    *,
    tag,
    blocks,
    parse_date,
    number_format: NumberFormat,
    currency: str,
    broker: str,
) -> None:
    """Import every brokerage account's transactions in ``text`` into ``sink``."""
    result = sink.result
    for statement in blocks(text, "INVSTMTRS"):
        account_blocks = blocks(statement, "INVACCTFROM")
        if not account_blocks:
            continue
        stmt = _Statement(
            sink,
            db,
            tag=tag,
            blocks=blocks,
            parse_date=parse_date,
            number_format=number_format,
            account_id=tag(account_blocks[0], "ACCTID"),
            broker=broker,
            currency=currency,
        )
        stmt.learn_securities(text)
        records: list[tuple[str, str]] = []
        for kind in (*_TRADES, "REINVEST", "INCOME", "INVEXPENSE", "INVBANKTRAN", *_SKIPPED):
            records.extend((kind, block) for block in blocks(statement, kind))
        for kind, block in records:
            _record(stmt, kind, block)
        if not records:
            result.warn("The OFX investment statement lists no transactions")


def _record(stmt: _Statement, kind: str, block: str) -> None:
    result = stmt.sink.result
    fitid = stmt.tag(block, "FITID")
    raw_date = stmt.tag(block, "DTTRADE") or stmt.tag(block, "DTPOSTED")
    identity = fitid or _handle("fallback", stmt.account_id, kind, block)
    handle = _handle("transaction", stmt.account_id, identity)
    result.scan("transaction")
    if kind in _SKIPPED:
        result.skip(
            f"OFX {kind} investment transactions are not imported yet",
            stmt.tag(block, "MEMO") or kind,
            identity=handle,
            kind="transaction",
        )
        return
    try:
        post_date: date = stmt.parse_date(raw_date)
        if kind in _TRADES:
            outcome = _trade_splits(stmt, kind, block)
        elif kind == "REINVEST":
            outcome = _reinvest_splits(stmt, block)
        elif kind == "INCOME":
            outcome = _cash_splits(stmt, block, "Investment Income", "Investment income")
        elif kind == "INVEXPENSE":
            outcome = _cash_splits(stmt, block, "Investment Fees", "Investment expense")
        else:
            outcome = _bank_splits(stmt, block)
    except ValueError as exc:
        outcome = str(exc)
        post_date = date.min
    if isinstance(outcome, str):
        result.skip(outcome, stmt.tag(block, "MEMO") or kind, identity=handle, kind="transaction")
        return
    splits, description = outcome
    kept = stmt.sink.keep_local_categories(handle, stmt.cash, splits)
    if kept is None:
        result.observe("transaction", handle)
        result.transactions_unchanged += 1
        return
    stmt.sink.transaction(handle, post_date, description, stmt.currency, "", kept)


def _bank_splits(stmt: _Statement, block: str) -> tuple[list[dict[str, Any]], str] | str:
    transactions = stmt.blocks(block, "STMTTRN")
    if not transactions:
        return "OFX investment cash record has no transaction"
    inner = transactions[0]
    amount = stmt.money(stmt.tag(inner, "TRNAMT"))
    if not amount:
        return "OFX investment cash record has a zero amount"
    name = "Uncategorized OFX"
    return (
        [
            {"account": stmt.cash, "value": amount, "memo": stmt.tag(inner, "MEMO")},
            {"account": stmt.category(-amount, name), "value": -amount},
        ],
        stmt.tag(inner, "NAME") or stmt.tag(inner, "MEMO") or "Investment cash",
    )
