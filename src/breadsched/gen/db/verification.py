"""Logical consistency checks for a BreadSched book.

SQLite's integrity checker answers whether the database file is structurally sound.
These checks answer a different question: whether the financial objects still agree
with one another and with the derived indexes used by the ledger.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..lib.transaction import UnbalancedError

if TYPE_CHECKING:
    from .base import DbBase

__all__ = ["BookIssue", "BookVerification", "verify_domain"]


@dataclass(frozen=True, slots=True)
class BookIssue:
    code: str
    message: str
    handle: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {"code": self.code, "message": self.message, "handle": self.handle}


@dataclass(frozen=True, slots=True)
class BookVerification:
    """Combined physical SQLite and logical financial-book findings."""

    sqlite: tuple[str, ...] = ()
    issues: tuple[BookIssue, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.sqlite and not self.issues

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "sqlite": list(self.sqlite),
            "issues": [issue.as_dict() for issue in self.issues],
        }


def verify_domain(db: DbBase) -> list[BookIssue]:
    """Return non-destructive logical consistency findings for ``db``."""
    issues: list[BookIssue] = []
    accounts = {account.handle: account for account in db.iter_accounts()}
    commodities = {commodity.handle for commodity in db.iter_commodities()}
    scenarios = {scenario.handle: scenario for scenario in db.iter_scenarios()}

    # Account graph and references. A normal chart has one explicit ROOT account;
    # once that root exists, any other parentless account is orphaned rather than
    # another root. Rootless lightweight books remain valid for small tools/tests.
    has_explicit_root = any(account.is_root for account in accounts.values())
    for account in accounts.values():
        if has_explicit_root and account.parent is None and not account.is_root:
            issues.append(
                BookIssue(
                    "account.orphaned_top_level",
                    f"account {account.name!r} has no parent beneath the chart root",
                    account.handle,
                )
            )
        if account.parent is not None and account.parent not in accounts:
            issues.append(
                BookIssue(
                    "account.missing_parent",
                    f"account {account.name!r} refers to missing parent {account.parent}",
                    account.handle,
                )
            )
        if account.commodity is not None and account.commodity not in commodities:
            issues.append(
                BookIssue(
                    "account.missing_commodity",
                    f"account {account.name!r} refers to missing commodity {account.commodity}",
                    account.handle,
                )
            )
        if account.linked_asset is not None and account.linked_asset not in accounts:
            issues.append(
                BookIssue(
                    "account.missing_linked_asset",
                    f"account {account.name!r} "
                    f"refers to missing linked asset {account.linked_asset}",
                    account.handle,
                )
            )
        if (
            account.card_payment_account is not None
            and account.card_payment_account not in accounts
        ):
            issues.append(
                BookIssue(
                    "account.missing_card_payment_account",
                    f"account {account.name!r} refers to missing card payment account "
                    f"{account.card_payment_account}",
                    account.handle,
                )
            )

    for account in accounts.values():
        seen: set[str] = set()
        current = account
        while current.parent is not None and current.parent in accounts:
            if current.handle in seen:
                issues.append(
                    BookIssue(
                        "account.parent_cycle",
                        f"account hierarchy contains a cycle involving {account.name!r}",
                        account.handle,
                    )
                )
                break
            seen.add(current.handle)
            current = accounts[current.parent]

    for price in db.iter_prices():
        if price.commodity not in commodities:
            issues.append(
                BookIssue(
                    "price.missing_commodity",
                    f"price {price.handle} refers to missing commodity {price.commodity}",
                    price.handle,
                )
            )
        if price.currency not in commodities:
            issues.append(
                BookIssue(
                    "price.missing_currency",
                    f"price {price.handle} refers to missing currency {price.currency}",
                    price.handle,
                )
            )
        else:
            currency = db.get_commodity(price.currency)
            if currency is not None and not currency.is_currency:
                issues.append(
                    BookIssue(
                        "price.non_currency_quote",
                        f"price {price.handle} quote commodity is not a currency",
                        price.handle,
                    )
                )

    # Ledger transactions.
    for transaction in db.iter_transactions():
        if transaction.currency is not None and transaction.currency not in commodities:
            issues.append(
                BookIssue(
                    "transaction.missing_currency",
                    f"transaction {transaction.describe()} refers to missing currency "
                    f"{transaction.currency}",
                    transaction.handle,
                )
            )
        try:
            transaction.validate()
        except UnbalancedError as exc:
            issues.append(BookIssue("transaction.unbalanced", str(exc), transaction.handle))
        split_handles: set[str] = set()
        for transaction_split in transaction.splits:
            if transaction_split.handle in split_handles:
                issues.append(
                    BookIssue(
                        "transaction.duplicate_split_handle",
                        f"transaction {transaction.describe()} contains duplicate split handle "
                        f"{transaction_split.handle}",
                        transaction.handle,
                    )
                )
            split_handles.add(transaction_split.handle)
            if transaction_split.account not in accounts:
                issues.append(
                    BookIssue(
                        "transaction.missing_account",
                        f"transaction {transaction.describe()} has a split for missing account "
                        f"{transaction_split.account}",
                        transaction.handle,
                    )
                )

    # Planning objects contain account references too.
    for sched in db.iter_scheduled():
        if sched.currency is not None and sched.currency not in commodities:
            issues.append(
                BookIssue(
                    "scheduled.missing_currency",
                    f"scheduled transaction {sched.name!r} refers to missing currency "
                    f"{sched.currency}",
                    sched.handle,
                )
            )
        for scheduled_split in sched.splits:
            if scheduled_split.account not in accounts:
                issues.append(
                    BookIssue(
                        "scheduled.missing_account",
                        f"scheduled transaction {sched.name!r} refers to missing account "
                        f"{scheduled_split.account}",
                        sched.handle,
                    )
                )
    for scenario in scenarios.values():
        refs = set(scenario.assumptions.per_account) | set(scenario.opening_overrides)
        refs.update(item.account for item in scenario.one_offs)
        for period in scenario.assumption_periods:
            refs.update(period.per_account)
        for account_handle in sorted(refs):
            if account_handle not in accounts:
                issues.append(
                    BookIssue(
                        "scenario.missing_account",
                        f"scenario {scenario.name!r} refers to missing account {account_handle}",
                        scenario.handle,
                    )
                )

    return issues
