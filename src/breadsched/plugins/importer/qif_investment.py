"""Quicken investment (``!Type:Invst``) and security (``!Type:Security``) records.

A QIF investment account becomes a :class:`~.brokerage.Brokerage`: an Assets child
with a ``Cash`` account and one sub-account per security. Securities are named in
investment records by their full name (``Y``); a ``!Type:Security`` list maps that
name to a ticker (``S``) and kind (``T``), else the name itself is the symbol.

Actions map to balanced transactions (amounts are taken as magnitudes, as Quicken
writes them):

- ``Buy``/``Sell``: cash moves by ``T``; commission ``O`` goes to Investment Fees;
  the security takes ``Q`` units and the balancing value. No gain is computed.
- ``Div``, ``IntInc``, ``CGLong``, ``CGMid``, ``CGShort``, ``MiscInc``: cash
  against the ``L`` category or Investment Income.
- ``ReinvDiv``, ``ReinvInt``, ``ReinvLg``, ``ReinvMd``, ``ReinvSh``: units bought
  with income that never passes through cash.
- ``MiscExp``: cash against the ``L`` category or Investment Fees.
- ``XIn``/``XOut`` and ``Cash``: cash against the ``L`` account or category.
- An ``X`` suffix (``BuyX``, ``DivX``, ...) moves the cash through the ``L``
  account (``[Checking]``, amount ``$``) instead of the brokerage's own cash.

Share transfers, splits, options, grants, and reminders are reported as skipped.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any

from ...gen.db.sqlite import DbSQLite
from ...gen.lib.money import Money
from .brokerage import Brokerage
from .gnucash_common import ImportSink

__all__ = ["QifInvestments"]

_INCOME = {"div", "intinc", "cglong", "cgmid", "cgshort", "miscinc"}
_REINVEST = {"reinvdiv", "reinvint", "reinvlg", "reinvmd", "reinvsh"}
_FUND_TYPES = {"mutual fund", "fund", "etf", "index fund", "money market"}
#: Quicken actions not imported, named as the import report shows them.
_SKIPPED_ACTIONS = {
    "shrsin": "a transfer of shares in",
    "shrsout": "a transfer of shares out",
    "stksplit": "a stock split",
    "grant": "an employee stock option grant",
    "vest": "an employee stock option vesting",
    "exercise": "an option exercise",
    "exercisx": "an option exercise",
    "expire": "an option expiry",
    "buyopt": "an option purchase",
    "sellopt": "an option sale",
    "reminder": "a Quicken reminder",
}


def _fields(record: list[str]) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in record:
        if line:
            fields.setdefault(line[0], line[1:].strip())
    return fields


class QifInvestments:
    """Investment accounts, securities, and records for one QIF import."""

    def __init__(
        self,
        sink: ImportSink,
        db: DbSQLite,
        *,
        handle: Callable[..., str],
        category: Callable[[str, Money], str],
        parse_amount: Callable[[str], Money],
        parse_date: Callable[[str], date],
    ) -> None:
        self.sink = sink
        self.db = db
        self._handle = handle
        self._category = category
        self._amount = parse_amount
        self._date = parse_date
        self._brokerages: dict[str, Brokerage] = {}
        self._securities: dict[str, tuple[str, str]] = {}
        self._counts: dict[tuple[object, ...], int] = {}

    # ------------------------------------------------------------ structure

    def learn_security(self, record: list[str]) -> None:
        """Remember a ``!Type:Security`` entry's ticker and kind by its name."""
        fields = _fields(record)
        name = fields.get("N", "")
        if name:
            self._securities[name.casefold()] = (fields.get("S") or name, fields.get("T", ""))

    def brokerage(self, name: str) -> Brokerage:
        key = name.casefold()
        if key not in self._brokerages:
            self._brokerages[key] = Brokerage(
                self.sink,
                self.db,
                handle=self._handle,
                identity=(name,),
                label=name,
                description="Imported QIF investment account",
                currency=None,
                # A split value is the negated register amount QIF categories use.
                category=lambda amount, label: self._category(label, -amount),
            )
        return self._brokerages[key]

    def cash_for(self, name: str) -> str | None:
        """A known investment account's cash, for ``[Name]`` transfers into it."""
        found = self._brokerages.get(name.casefold())
        return found.cash if found is not None else None

    def _security(self, brokerage: Brokerage, account: str, name: str) -> str | None:
        ticker, kind = self._securities.get(name.casefold(), (name, ""))
        fund = kind.casefold() in _FUND_TYPES
        return brokerage.security(
            (account, name.casefold()),
            ticker,
            name,
            fund=fund,
            namespace="FUND" if fund else "STOCK" if kind else "SECURITY",
        )

    # --------------------------------------------------------------- records

    def record(self, account: str, record: list[str]) -> None:
        fields = _fields(record)
        action = fields.get("N", "").strip()
        verb = action.casefold()
        via_transfer = verb.endswith("x") and verb not in {"xin", "xout"} and len(verb) > 1
        base = verb[:-1] if via_transfer else verb
        identity = (
            account,
            fields.get("D", ""),
            action,
            fields.get("Y", ""),
            fields.get("Q", ""),
            fields.get("T", fields.get("U", "")),
            fields.get("M", ""),
            fields.get("L", ""),
        )
        occurrence = self._counts.get(identity, 0) + 1
        self._counts[identity] = occurrence
        handle = self._handle("transaction", *identity, occurrence)
        subject = fields.get("P") or fields.get("M") or action or "QIF investment record"
        result = self.sink.result
        try:
            post_date = self._date(fields.get("D", ""))
            outcome = self._splits(account, base, via_transfer, fields)
        except ValueError as exc:
            outcome = str(exc)
            post_date = date.min
        if isinstance(outcome, str):
            result.skip(outcome, subject, identity=handle, kind="transaction")
            return
        splits, description = outcome
        brokerage = self.brokerage(account)
        kept = self.sink.keep_local_categories(handle, brokerage.cash, splits)
        if kept is None:
            result.observe("transaction", handle)
            result.transactions_unchanged += 1
            return
        self.sink.transaction(handle, post_date, description, None, "", kept)

    def _magnitude(self, fields: dict[str, str], *codes: str) -> Money:
        for code in codes:
            if fields.get(code):
                return abs(self._amount(fields[code]))
        return Money(0)

    def _splits(
        self, account: str, verb: str, via_transfer: bool, fields: dict[str, str]
    ) -> tuple[list[dict[str, Any]], str] | str:
        brokerage = self.brokerage(account)
        memo = fields.get("M", "")
        total = self._magnitude(fields, "T", "U")
        if via_transfer:
            label = fields.get("L", "")
            if not label.startswith("["):
                return f"QIF {verb.capitalize()}X record names no transfer account"
            cash = self._category(label, Money(0))
        else:
            cash = brokerage.cash
        security_name = fields.get("Y", "")
        commission = self._magnitude(fields, "O")
        if verb in {"buy", "sell"} or verb in _REINVEST:
            if not security_name:
                return "QIF trade does not name a security"
            security = self._security(brokerage, account, security_name)
            if security is None:
                return "QIF trade does not name a security"
            units = self._magnitude(fields, "Q")
            if not total:
                total = units * self._magnitude(fields, "I").rate()
                total = total + commission if verb != "sell" else total - commission
            splits: list[dict[str, Any]] = []
            if verb == "sell":
                splits.append({"account": cash, "value": total, "memo": memo})
                splits.append(
                    {"account": security, "value": -(total + commission), "quantity": -units}
                )
            else:
                source = (
                    brokerage.category(-total, fields.get("L") or "Investment Income")
                    if verb in _REINVEST
                    else cash
                )
                splits.append({"account": source, "value": -total, "memo": memo})
                splits.append({"account": security, "value": total - commission, "quantity": units})
            if commission:
                splits.append(
                    {
                        "account": brokerage.category(commission, "Investment Fees"),
                        "value": commission,
                    }
                )
            label = {"buy": "Buy", "sell": "Sell"}.get(verb, "Reinvest")
            return splits, fields.get("P") or f"{label} {security_name}"
        if verb not in _INCOME and verb not in {"miscexp", "xin", "xout", "cash"}:
            action = fields.get("N") or "unnamed"
            label = _SKIPPED_ACTIONS.get(verb, f"a QIF {action} investment action")
            return f"{label[0].upper()}{label[1:]} is not imported; enter it yourself"
        if not total:
            return "QIF investment record has no amount"
        if verb in _INCOME:
            category = self._named_category(fields, -total, "Investment Income")
            return (
                [
                    {"account": cash, "value": total, "memo": memo},
                    {"account": category, "value": -total},
                ],
                fields.get("P") or f"{fields.get('N', 'Income')} {security_name}".strip(),
            )
        if verb == "miscexp":
            category = self._named_category(fields, total, "Investment Fees")
            return (
                [
                    {"account": cash, "value": -total, "memo": memo},
                    {"account": category, "value": total},
                ],
                fields.get("P") or "Investment expense",
            )
        if verb in {"xin", "xout", "cash"}:
            signed = self._amount(fields.get("T") or fields.get("U") or "0")
            if verb == "xin":
                signed = total
            elif verb == "xout":
                signed = -total
            other = self._category(fields.get("L") or "Uncategorized", signed)
            return (
                [
                    {"account": brokerage.cash, "value": signed, "memo": memo},
                    {"account": other, "value": -signed},
                ],
                fields.get("P") or fields.get("M") or "Investment cash",
            )
        raise AssertionError(f"unhandled QIF investment action {verb!r}")

    def _named_category(self, fields: dict[str, str], amount: Money, default: str) -> str:
        """The ``L`` category when it is one (not a transfer), else ``default``.

        ``amount`` is the category split's value (positive for an expense).
        """
        label = fields.get("L", "")
        if not label or label.startswith("["):
            label = default
        return self._category(label, -amount)
