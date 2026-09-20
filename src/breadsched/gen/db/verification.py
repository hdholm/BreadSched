"""Logical consistency checks for a BreadSched book.

SQLite's integrity checker answers whether the database file is structurally sound.
These checks answer a different question: whether the financial objects still agree
with one another and with the derived indexes used by the ledger.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..lib.account import Account
from ..lib.commodity import Commodity
from ..lib.money import Money
from ..lib.reconciliation import ReconciliationStatus
from ..lib.scenario import Scenario
from ..lib.transaction import ReconcileState, Transaction, UnbalancedError
from ..lib.transaction import Split as TransactionSplit

if TYPE_CHECKING:
    from .base import DbBase

__all__ = ["BookIssue", "BookVerification", "verify_domain"]


def _representable(value: Money, fraction: int) -> bool:
    """Whether ``value`` is an exact multiple of one commodity minor unit."""
    return (value.numerator * fraction) % value.denominator == 0


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
    native_schema_version: int | None = None

    @property
    def ok(self) -> bool:
        return not self.sqlite and not self.issues

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "native_schema_version": self.native_schema_version,
            "sqlite": list(self.sqlite),
            "issues": [issue.as_dict() for issue in self.issues],
        }


@dataclass(frozen=True, slots=True)
class _VerificationState:
    db: DbBase
    accounts: dict[str, Account]
    commodities: dict[str, Commodity]
    scenarios: dict[str, Scenario]
    transactions: list[Transaction]
    splits: dict[str, TransactionSplit]

    @classmethod
    def load(cls, db: DbBase) -> _VerificationState:
        accounts = {account.handle: account for account in db.iter_accounts()}
        commodities = {commodity.handle: commodity for commodity in db.iter_commodities()}
        transactions = list(db.iter_transactions())
        return cls(
            db=db,
            accounts=accounts,
            commodities=commodities,
            scenarios={scenario.handle: scenario for scenario in db.iter_scenarios()},
            transactions=transactions,
            splits={
                split.handle: split for transaction in transactions for split in transaction.splits
            },
        )


def _verify_commodities(state: _VerificationState) -> list[BookIssue]:
    issues: list[BookIssue] = []
    commodity_keys: dict[tuple[str, str], str] = {}
    for commodity in state.commodities.values():
        if commodity.fraction < 1:
            issues.append(
                BookIssue(
                    "commodity.invalid_fraction",
                    f"commodity {commodity.namespace}:{commodity.mnemonic} has non-positive "
                    f"fraction {commodity.fraction}",
                    commodity.handle,
                )
            )
        key = (commodity.namespace.upper(), commodity.mnemonic.upper())
        previous = commodity_keys.get(key)
        if previous is not None and previous != commodity.handle:
            issues.append(
                BookIssue(
                    "commodity.duplicate_identifier",
                    f"commodities {previous} and {commodity.handle} both use "
                    f"{commodity.namespace}:{commodity.mnemonic}",
                    commodity.handle,
                )
            )
        else:
            commodity_keys[key] = commodity.handle
    return issues


def _verify_accounts(state: _VerificationState) -> list[BookIssue]:
    issues: list[BookIssue] = []
    source_guids: dict[str, str] = {}
    for account in state.accounts.values():
        if not account.source_guid:
            continue
        previous = source_guids.get(account.source_guid)
        if previous is not None and previous != account.handle:
            issues.append(
                BookIssue(
                    "account.duplicate_source_guid",
                    f"accounts {previous} and {account.handle} both claim imported source "
                    f"GUID {account.source_guid}",
                    account.handle,
                )
            )
        else:
            source_guids[account.source_guid] = account.handle

    # A normal chart has one explicit ROOT account. Once that root exists, any
    # other parentless account is orphaned rather than another root. Rootless
    # lightweight books remain valid for small tools and tests.
    has_explicit_root = any(account.is_root for account in state.accounts.values())
    for account in state.accounts.values():
        if has_explicit_root and account.parent is None and not account.is_root:
            issues.append(
                BookIssue(
                    "account.orphaned_top_level",
                    f"account {account.name!r} has no parent beneath the chart root",
                    account.handle,
                )
            )
        if account.parent is not None and account.parent not in state.accounts:
            issues.append(
                BookIssue(
                    "account.missing_parent",
                    f"account {account.name!r} refers to missing parent {account.parent}",
                    account.handle,
                )
            )
        if account.commodity is not None and account.commodity not in state.commodities:
            issues.append(
                BookIssue(
                    "account.missing_commodity",
                    f"account {account.name!r} refers to missing commodity {account.commodity}",
                    account.handle,
                )
            )
        if account.commodity_scu is not None and account.commodity_scu < 1:
            issues.append(
                BookIssue(
                    "account.invalid_commodity_scu",
                    f"account {account.name!r} has non-positive commodity SCU "
                    f"{account.commodity_scu}",
                    account.handle,
                )
            )
        if account.linked_asset is not None and account.linked_asset not in state.accounts:
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
            and account.card_payment_account not in state.accounts
        ):
            issues.append(
                BookIssue(
                    "account.missing_card_payment_account",
                    f"account {account.name!r} refers to missing card payment account "
                    f"{account.card_payment_account}",
                    account.handle,
                )
            )

    for account in state.accounts.values():
        seen: set[str] = set()
        current = account
        while current.parent is not None and current.parent in state.accounts:
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
            current = state.accounts[current.parent]
    return issues


def _verify_reconciliations(state: _VerificationState) -> list[BookIssue]:
    issues: list[BookIssue] = []
    for reconciliation in state.db.iter_reconciliations():
        if reconciliation.account not in state.accounts:
            issues.append(
                BookIssue(
                    "reconciliation.missing_account",
                    f"reconciliation {reconciliation.handle} refers to missing account "
                    f"{reconciliation.account}",
                    reconciliation.handle,
                )
            )
        if len(reconciliation.selected_splits) != len(set(reconciliation.selected_splits)):
            issues.append(
                BookIssue(
                    "reconciliation.duplicate_split",
                    f"reconciliation {reconciliation.handle} contains a split more than once",
                    reconciliation.handle,
                )
            )
        for split_handle in reconciliation.selected_splits:
            split = state.splits.get(split_handle)
            if split is None:
                issues.append(
                    BookIssue(
                        "reconciliation.missing_split",
                        f"reconciliation {reconciliation.handle} refers to missing split "
                        f"{split_handle}",
                        reconciliation.handle,
                    )
                )
            elif split.account != reconciliation.account:
                issues.append(
                    BookIssue(
                        "reconciliation.wrong_account",
                        f"reconciliation {reconciliation.handle} includes split {split_handle} "
                        f"from another account",
                        reconciliation.handle,
                    )
                )
            elif reconciliation.status is ReconciliationStatus.COMPLETED and (
                split.reconcile is not ReconcileState.RECONCILED
                or split.reconcile_date != reconciliation.statement_date
            ):
                issues.append(
                    BookIssue(
                        "reconciliation.changed_split",
                        f"completed reconciliation {reconciliation.handle} no longer agrees "
                        f"with split {split_handle}",
                        reconciliation.handle,
                    )
                )
    return issues


def _verify_prices(state: _VerificationState) -> list[BookIssue]:
    issues: list[BookIssue] = []
    for price in state.db.iter_prices():
        if price.commodity not in state.commodities:
            issues.append(
                BookIssue(
                    "price.missing_commodity",
                    f"price {price.handle} refers to missing commodity {price.commodity}",
                    price.handle,
                )
            )
        if price.currency not in state.commodities:
            issues.append(
                BookIssue(
                    "price.missing_currency",
                    f"price {price.handle} refers to missing currency {price.currency}",
                    price.handle,
                )
            )
        else:
            currency = state.db.get_commodity(price.currency)
            if currency is not None and not currency.is_currency:
                issues.append(
                    BookIssue(
                        "price.non_currency_quote",
                        f"price {price.handle} quote commodity is not a currency",
                        price.handle,
                    )
                )
    return issues


def _verify_transactions(state: _VerificationState) -> list[BookIssue]:
    issues: list[BookIssue] = []
    occurrence_owners: dict[str, str] = {}
    global_split_owners: dict[str, str] = {}
    for transaction in state.transactions:
        if transaction.currency is not None and transaction.currency not in state.commodities:
            issues.append(
                BookIssue(
                    "transaction.missing_currency",
                    f"transaction {transaction.describe()} refers to missing currency "
                    f"{transaction.currency}",
                    transaction.handle,
                )
            )
        currency = state.commodities.get(transaction.currency or "")
        if currency is not None and not currency.is_currency:
            issues.append(
                BookIssue(
                    "transaction.non_currency_commodity",
                    f"transaction {transaction.describe()} uses non-currency commodity "
                    f"{currency.namespace}:{currency.mnemonic} as its balancing currency",
                    transaction.handle,
                )
            )
        occurrence_key = transaction.planned_occurrence
        if occurrence_key is None and transaction.scheduled_from is not None:
            planned_for = transaction.planned_for or transaction.post_date
            occurrence_key = f"scheduled:{transaction.scheduled_from}:{planned_for.isoformat()}"
        if occurrence_key is not None:
            previous = occurrence_owners.get(occurrence_key)
            if previous is not None and previous != transaction.handle:
                issues.append(
                    BookIssue(
                        "scheduled.duplicate_occurrence",
                        f"transactions {previous} and {transaction.handle} both realize "
                        f"planned occurrence {occurrence_key}",
                        transaction.handle,
                    )
                )
            else:
                occurrence_owners[occurrence_key] = transaction.handle
        try:
            transaction.validate()
        except UnbalancedError as exc:
            issues.append(BookIssue("transaction.unbalanced", str(exc), transaction.handle))
        split_handles: set[str] = set()
        for transaction_split in transaction.splits:
            previous_owner = global_split_owners.get(transaction_split.handle)
            if previous_owner is not None and previous_owner != transaction.handle:
                issues.append(
                    BookIssue(
                        "transaction.duplicate_split_handle",
                        f"transactions {previous_owner} and {transaction.handle} both contain "
                        f"split handle {transaction_split.handle}",
                        transaction.handle,
                    )
                )
            else:
                global_split_owners[transaction_split.handle] = transaction.handle
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
            if transaction_split.account not in state.accounts:
                issues.append(
                    BookIssue(
                        "transaction.missing_account",
                        f"transaction {transaction.describe()} has a split for missing account "
                        f"{transaction_split.account}",
                        transaction.handle,
                    )
                )
                continue
            account = state.accounts[transaction_split.account]
            account_commodity = state.commodities.get(account.commodity or "")
            quantity_fraction = account.commodity_scu or (
                account_commodity.fraction if account_commodity is not None else None
            )
            if (
                quantity_fraction is not None
                and quantity_fraction > 0
                and not _representable(transaction_split.quantity, quantity_fraction)
            ):
                issues.append(
                    BookIssue(
                        "transaction.quantity_precision",
                        f"split {transaction_split.handle} quantity "
                        f"{transaction_split.quantity} is not representable at account "
                        f"{account.name!r} SCU {quantity_fraction}",
                        transaction.handle,
                    )
                )
            if (
                currency is not None
                and currency.fraction > 0
                and not _representable(transaction_split.value, currency.fraction)
            ):
                issues.append(
                    BookIssue(
                        "transaction.value_precision",
                        f"split {transaction_split.handle} value {transaction_split.value} "
                        f"is not representable at {currency.mnemonic} fraction "
                        f"{currency.fraction}",
                        transaction.handle,
                    )
                )
    return issues


def _verify_schedules(state: _VerificationState) -> list[BookIssue]:
    issues: list[BookIssue] = []
    for sched in state.db.iter_scheduled():
        if sched.currency is not None and sched.currency not in state.commodities:
            issues.append(
                BookIssue(
                    "scheduled.missing_currency",
                    f"scheduled transaction {sched.name!r} refers to missing currency "
                    f"{sched.currency}",
                    sched.handle,
                )
            )
        currency = state.commodities.get(sched.currency or "")
        if currency is not None and not currency.is_currency:
            issues.append(
                BookIssue(
                    "scheduled.non_currency_commodity",
                    f"scheduled transaction {sched.name!r} uses non-currency commodity "
                    f"{currency.namespace}:{currency.mnemonic} as its balancing currency",
                    sched.handle,
                )
            )
        if len(sched.skipped) != len(set(sched.skipped)):
            issues.append(
                BookIssue(
                    "scheduled.duplicate_skip",
                    f"scheduled transaction {sched.name!r} records a skipped occurrence "
                    "more than once",
                    sched.handle,
                )
            )
        if len(sched.occurrence_adjustments) != len(
            {item.when for item in sched.occurrence_adjustments}
        ):
            issues.append(
                BookIssue(
                    "scheduled.duplicate_adjustment",
                    f"scheduled transaction {sched.name!r} has more than one amount "
                    "adjustment for the same occurrence",
                    sched.handle,
                )
            )
        if all(not split.formula for split in sched.splits):
            residual = sum(
                (split.amount or Money(0) for split in sched.splits),
                Money(0),
            )
            if residual:
                issues.append(
                    BookIssue(
                        "scheduled.unbalanced",
                        f"scheduled transaction {sched.name!r} has fixed splits that do not "
                        f"balance; residual {residual}",
                        sched.handle,
                    )
                )
        for scheduled_split in sched.splits:
            if scheduled_split.account not in state.accounts:
                issues.append(
                    BookIssue(
                        "scheduled.missing_account",
                        f"scheduled transaction {sched.name!r} refers to missing account "
                        f"{scheduled_split.account}",
                        sched.handle,
                    )
                )
            if (
                scheduled_split.amount is not None
                and currency is not None
                and currency.fraction > 0
                and not _representable(scheduled_split.amount, currency.fraction)
            ):
                issues.append(
                    BookIssue(
                        "scheduled.value_precision",
                        f"scheduled transaction {sched.name!r} has an amount "
                        f"{scheduled_split.amount} not representable at {currency.mnemonic} "
                        f"fraction {currency.fraction}",
                        sched.handle,
                    )
                )
    return issues


def _verify_scenarios(state: _VerificationState) -> list[BookIssue]:
    issues: list[BookIssue] = []
    for scenario in state.scenarios.values():
        refs = set(scenario.assumptions.per_account) | set(scenario.opening_overrides)
        refs.update(item.account for item in scenario.one_offs)
        for period in scenario.assumption_periods:
            refs.update(period.per_account)
        for account_handle in sorted(refs):
            if account_handle not in state.accounts:
                issues.append(
                    BookIssue(
                        "scenario.missing_account",
                        f"scenario {scenario.name!r} refers to missing account {account_handle}",
                        scenario.handle,
                    )
                )
    return issues


def verify_domain(db: DbBase) -> list[BookIssue]:
    """Return non-destructive logical consistency findings for ``db``."""
    state = _VerificationState.load(db)
    issues = _verify_commodities(state)
    issues.extend(_verify_accounts(state))
    issues.extend(_verify_reconciliations(state))
    issues.extend(_verify_prices(state))
    issues.extend(_verify_transactions(state))
    issues.extend(_verify_schedules(state))
    issues.extend(_verify_scenarios(state))

    return issues
