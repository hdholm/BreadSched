"""Hold back statement rows that repeat a transaction already in the account.

A bank statement and a transaction entered by hand, or the same activity
downloaded twice under different identifiers, describe one real payment. A row
whose account already has a transaction from elsewhere on the same date for the
same amount is therefore a possible duplicate. Each existing transaction can
answer for at most one row, so two genuinely identical payments on one day are
held back only as often as the book already has them.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date

from ...gen.db.sqlite import DbSQLite
from ...gen.lib.money import Money

__all__ = ["DuplicateGuard"]


class DuplicateGuard:
    """Match new rows one to one with existing transactions in one account."""

    def __init__(self, db: DbSQLite, account: str, own: Iterable[str] = ()) -> None:
        """Index ``account``'s transactions, leaving out ``own`` (the source's rows)."""
        excluded = set(own)
        self._candidates: dict[tuple[date, Money], list[str]] = {}
        for transaction in db.iter_transactions(account=account):
            if transaction.handle in excluded:
                continue
            for split in transaction.splits:
                if split.account == account:
                    self._candidates.setdefault((transaction.post_date, split.value), []).append(
                        transaction.handle
                    )
        self._claimed: set[str] = set()

    def claim(self, when: date, value: Money) -> str | None:
        """The existing transaction this row may duplicate, now claimed; else ``None``."""
        for handle in self._candidates.get((when, value), ()):
            if handle not in self._claimed:
                self._claimed.add(handle)
                return handle
        return None
