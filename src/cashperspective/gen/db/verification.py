"""Logical consistency checks for a CashPerspective book.

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

__all__ = ["BookIssue", "verify_domain"]


@dataclass(frozen=True, slots=True)
class BookIssue:
    code: str
    message: str
    handle: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {"code": self.code, "message": self.message, "handle": self.handle}


def verify_domain(db: DbBase) -> list[BookIssue]:
    """Return non-destructive logical consistency findings for ``db``."""
    issues: list[BookIssue] = []
    accounts = {account.handle: account for account in db.iter_accounts()}
    commodities = {commodity.handle for commodity in db.iter_commodities()}
    budgets = {budget.handle: budget for budget in db.iter_budgets()}
    scenarios = {scenario.handle: scenario for scenario in db.iter_scenarios()}

    # Account graph and references.
    for account in accounts.values():
        if account.parent is not None and account.parent not in accounts:
            issues.append(BookIssue(
                "account.missing_parent",
                f"account {account.name!r} refers to missing parent {account.parent}",
                account.handle,
            ))
        if account.commodity is not None and account.commodity not in commodities:
            issues.append(BookIssue(
                "account.missing_commodity",
                f"account {account.name!r} refers to missing commodity {account.commodity}",
                account.handle,
            ))
        if account.linked_asset is not None and account.linked_asset not in accounts:
            issues.append(BookIssue(
                "account.missing_linked_asset",
                f"account {account.name!r} refers to missing linked asset {account.linked_asset}",
                account.handle,
            ))

    for account in accounts.values():
        seen: set[str] = set()
        current = account
        while current.parent is not None and current.parent in accounts:
            if current.handle in seen:
                issues.append(BookIssue(
                    "account.parent_cycle",
                    f"account hierarchy contains a cycle involving {account.name!r}",
                    account.handle,
                ))
                break
            seen.add(current.handle)
            current = accounts[current.parent]

    # Ledger transactions.
    for transaction in db.iter_transactions():
        if transaction.currency is not None and transaction.currency not in commodities:
            issues.append(BookIssue(
                "transaction.missing_currency",
                f"transaction {transaction.describe()} refers to missing currency "
                f"{transaction.currency}",
                transaction.handle,
            ))
        try:
            transaction.validate()
        except UnbalancedError as exc:
            issues.append(BookIssue("transaction.unbalanced", str(exc), transaction.handle))
        split_handles: set[str] = set()
        for transaction_split in transaction.splits:
            if transaction_split.handle in split_handles:
                issues.append(BookIssue(
                    "transaction.duplicate_split_handle",
                    f"transaction {transaction.describe()} contains duplicate split handle "
                    f"{transaction_split.handle}",
                    transaction.handle,
                ))
            split_handles.add(transaction_split.handle)
            if transaction_split.account not in accounts:
                issues.append(BookIssue(
                    "transaction.missing_account",
                    f"transaction {transaction.describe()} has a split for missing account "
                    f"{transaction_split.account}",
                    transaction.handle,
                ))

    # Planning objects contain account/budget references too.
    for sched in db.iter_scheduled():
        if sched.currency is not None and sched.currency not in commodities:
            issues.append(BookIssue(
                "scheduled.missing_currency",
                f"scheduled transaction {sched.name!r} refers to missing currency "
                f"{sched.currency}",
                sched.handle,
            ))
        for scheduled_split in sched.splits:
            if scheduled_split.account not in accounts:
                issues.append(BookIssue(
                    "scheduled.missing_account",
                    f"scheduled transaction {sched.name!r} refers to missing account "
                    f"{scheduled_split.account}",
                    sched.handle,
                ))
        if sched.budgets_decided:
            for handle in sched.budgets:
                if handle not in budgets:
                    issues.append(BookIssue(
                        "scheduled.missing_budget",
                        f"scheduled transaction {sched.name!r} refers to missing budget {handle}",
                        sched.handle,
                    ))

    for budget in budgets.values():
        if budget.scenario is not None and budget.scenario not in scenarios:
            issues.append(BookIssue(
                "budget.missing_scenario",
                f"budget {budget.name!r} refers to missing scenario {budget.scenario}",
                budget.handle,
            ))
        for account_handle in budget.lines:
            if account_handle not in accounts:
                issues.append(BookIssue(
                    "budget.missing_account",
                    f"budget {budget.name!r} contains a line for missing account {account_handle}",
                    budget.handle,
                ))

    for scenario in scenarios.values():
        if scenario.budget is not None and scenario.budget not in budgets:
            issues.append(BookIssue(
                "scenario.missing_budget",
                f"scenario {scenario.name!r} refers to missing budget {scenario.budget}",
                scenario.handle,
            ))
        refs = set(scenario.assumptions.per_account) | set(scenario.opening_overrides)
        refs.update(item.account for item in scenario.one_offs)
        for period in scenario.assumption_periods:
            refs.update(period.per_account)
        for account_handle in sorted(refs):
            if account_handle not in accounts:
                issues.append(BookIssue(
                    "scenario.missing_account",
                    f"scenario {scenario.name!r} refers to missing account {account_handle}",
                    scenario.handle,
                ))

    return issues
