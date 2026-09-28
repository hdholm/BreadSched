"""The account structure shared by OFX and QIF investment imports.

A brokerage account becomes an Assets child with a ``Cash`` bank account and one
``MUTUAL``/``STOCK`` sub-account per security traded in it. Every account has a
stable handle supplied by the caller's format, so a re-import refreshes rather than
duplicates. A security is matched by ticker to the book's one security with that
symbol, else created with the given namespace.
"""

from __future__ import annotations

from collections.abc import Callable

from ...gen.db.sqlite import DbSQLite
from ...gen.lib.money import Money
from .gnucash_common import ImportSink
from .quotes import security_by_symbol

__all__ = ["Brokerage"]


class Brokerage:
    """One imported brokerage account and its cash and security sub-accounts."""

    def __init__(
        self,
        sink: ImportSink,
        db: DbSQLite,
        *,
        handle: Callable[..., str],
        identity: tuple[object, ...],
        label: str,
        description: str,
        currency: str | None,
        category: Callable[[Money, str], str],
    ) -> None:
        self.sink = sink
        self.db = db
        self._handle = handle
        self._category = category
        self.currency = currency
        assets = db.get_account_by_name("Assets")
        self.parent = sink.account(
            handle("investment-account", *identity),
            label,
            "ASSET",
            assets.handle if assets is not None else None,
            commodity=currency,
            description=description,
        ).handle
        self.cash = sink.account(
            handle("investment-cash", *identity),
            "Cash",
            "BANK",
            self.parent,
            commodity=currency,
        ).handle
        self._securities: dict[tuple[object, ...], str] = {}

    def category(self, amount: Money, name: str) -> str:
        """An expense category for a positive ``amount``, else an income one."""
        return self._category(amount, name)

    def security(
        self,
        identity: tuple[object, ...],
        ticker: str,
        name: str,
        *,
        fund: bool,
        namespace: str,
    ) -> str | None:
        """The sub-account holding one security, creating it (and it) if needed."""
        if identity in self._securities:
            return self._securities[identity]
        if not ticker:
            return None
        existing = security_by_symbol(self.db, ticker)
        commodity = (
            existing.handle
            if existing is not None
            else self.sink.commodity(namespace, ticker, name or ticker, fraction=10000)
        )
        account = self.sink.account(
            self._handle("security-account", *identity),
            ticker,
            "MUTUAL" if fund else "STOCK",
            self.parent,
            commodity=commodity,
            description=name or ticker,
            commodity_scu=10000,
        ).handle
        self._securities[identity] = account
        return account
