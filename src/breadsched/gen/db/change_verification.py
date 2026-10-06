"""Commit-time checks of the rows one write batch changed.

``verify_book()`` is the exhaustive diagnostic. A normal commit, undo, or redo
already knows which rows it changed, so it checks only those objects, their derived
index rows, and the references that point at a deleted row, and refuses to commit
a batch that would leave the book inconsistent. The checks read through the
backend's public object API plus its open connection, so they are a base class of
the SQLite backend rather than part of its storage code.
"""

from __future__ import annotations

import sqlite3
from abc import abstractmethod
from typing import Any

from ..lib.account import Account
from ..lib.commodity import CommodityPrice
from ..lib.fsa_claim import FsaClaim
from ..lib.receivable import Receivable
from ..lib.reconciliation import Reconciliation
from ..lib.savings_goal import SavingsGoal
from ..lib.scenario import Scenario
from ..lib.scheduled import ScheduledTransaction
from ..lib.transaction import Transaction, UnbalancedError
from .base import DbBase
from .verification import BookIssue

__all__ = ["ChangeVerification"]


class ChangeVerification(DbBase):
    """Refuse a write batch whose changed rows would leave the book inconsistent."""

    _accounts: dict[str, Account]

    @abstractmethod
    def _require(self) -> sqlite3.Connection:
        """The open connection, or an error when the book is closed."""

    def _verify_changes(
        self,
        records: list[tuple[str, str, dict | None, dict | None]],
        *,
        reverse: bool = False,
    ) -> list[BookIssue]:
        """Verify only objects and reverse references affected by one write batch.

        ``verify_book()`` remains the exhaustive diagnostic. Normal commits and
        undo/redo already know which rows changed, so rebuilding every transaction
        and the complete split index on each edit is unnecessary work.
        """
        states: dict[tuple[str, str], tuple[dict | None, dict | None]] = {}
        for table, handle, before, after in records:
            key = (table, handle)
            original = states.get(key, (before, before))[0]
            states[key] = (original, after)

        targets = {key: (before if reverse else after) for key, (before, after) in states.items()}
        issues: list[BookIssue] = []
        for (table, handle), data in targets.items():
            if data is None:
                issues.extend(self._verify_deleted_reference(table, handle))
                continue
            issues.extend(self._verify_changed_object(table, handle, data))
        return issues

    def _verify_changed_object(
        self, table: str, handle: str, data: dict[str, Any]
    ) -> list[BookIssue]:
        if table == "metadata":
            return []
        issues = self._verify_derived_row(table, handle, data)

        if table == "account":
            account = Account.from_dict(data)
            if account.parent is not None and self.get_account(account.parent) is None:
                issues.append(
                    BookIssue(
                        "account.missing_parent",
                        f"account {account.name!r} refers to missing parent {account.parent}",
                        handle,
                    )
                )
            if account.commodity is not None and self.get_commodity(account.commodity) is None:
                issues.append(
                    BookIssue(
                        "account.missing_commodity",
                        f"account {account.name!r} refers to missing commodity {account.commodity}",
                        handle,
                    )
                )
            if account.linked_asset is not None and self.get_account(account.linked_asset) is None:
                issues.append(
                    BookIssue(
                        "account.missing_linked_asset",
                        f"account {account.name!r} refers to missing linked asset "
                        f"{account.linked_asset}",
                        handle,
                    )
                )
            if (
                account.card_payment_account is not None
                and self.get_account(account.card_payment_account) is None
            ):
                issues.append(
                    BookIssue(
                        "account.missing_card_payment_account",
                        f"account {account.name!r} refers to missing card payment account "
                        f"{account.card_payment_account}",
                        handle,
                    )
                )
            seen: set[str] = set()
            current = account
            while current.parent is not None:
                if current.handle in seen:
                    issues.append(
                        BookIssue(
                            "account.parent_cycle",
                            f"account hierarchy contains a cycle involving {account.name!r}",
                            handle,
                        )
                    )
                    break
                seen.add(current.handle)
                parent = self.get_account(current.parent)
                if parent is None:
                    break
                current = parent

        elif table == "price":
            price = CommodityPrice.from_dict(data)
            if self.get_commodity(price.commodity) is None:
                issues.append(
                    BookIssue(
                        "price.missing_commodity",
                        f"price {handle} refers to missing commodity {price.commodity}",
                        handle,
                    )
                )
            currency = self.get_commodity(price.currency)
            if currency is None:
                issues.append(
                    BookIssue(
                        "price.missing_currency",
                        f"price {handle} refers to missing currency {price.currency}",
                        handle,
                    )
                )
            elif not currency.is_currency:
                issues.append(
                    BookIssue(
                        "price.non_currency_quote",
                        f"price {handle} quote commodity is not a currency",
                        handle,
                    )
                )

        elif table == "txn":
            transaction = Transaction.from_dict(data)
            if (
                transaction.currency is not None
                and self.get_commodity(transaction.currency) is None
            ):
                issues.append(
                    BookIssue(
                        "transaction.missing_currency",
                        f"transaction {transaction.describe()} refers to missing currency "
                        f"{transaction.currency}",
                        handle,
                    )
                )
            try:
                transaction.validate()
            except UnbalancedError as exc:
                issues.append(BookIssue("transaction.unbalanced", str(exc), handle))
            split_handles: set[str] = set()
            for transaction_split in transaction.splits:
                if transaction_split.handle in split_handles:
                    issues.append(
                        BookIssue(
                            "transaction.duplicate_split_handle",
                            f"transaction {transaction.describe()} contains duplicate split handle "
                            f"{transaction_split.handle}",
                            handle,
                        )
                    )
                split_handles.add(transaction_split.handle)
                if self.get_account(transaction_split.account) is None:
                    issues.append(
                        BookIssue(
                            "transaction.missing_account",
                            f"transaction {transaction.describe()} has a split for missing account "
                            f"{transaction_split.account}",
                            handle,
                        )
                    )
            issues.extend(self._verify_transaction_index(transaction))

        elif table == "scheduled":
            scheduled = ScheduledTransaction.from_dict(data)
            if scheduled.currency is not None and self.get_commodity(scheduled.currency) is None:
                issues.append(
                    BookIssue(
                        "scheduled.missing_currency",
                        f"scheduled transaction {scheduled.name!r} refers to missing currency "
                        f"{scheduled.currency}",
                        handle,
                    )
                )
            for scheduled_split in scheduled.splits:
                if self.get_account(scheduled_split.account) is None:
                    issues.append(
                        BookIssue(
                            "scheduled.missing_account",
                            f"scheduled transaction {scheduled.name!r} refers to missing account "
                            f"{scheduled_split.account}",
                            handle,
                        )
                    )
        elif table == "scenario":
            scenario = Scenario.from_dict(data)
            refs = scenario.account_references()
            for account_handle in sorted(refs):
                if self.get_account(account_handle) is None:
                    issues.append(
                        BookIssue(
                            "scenario.missing_account",
                            f"scenario {scenario.name!r} "
                            f"refers to missing account {account_handle}",
                            handle,
                        )
                    )

        elif table == "fsa_claim":
            issues.extend(self._verify_fsa_claim_references(FsaClaim.from_dict(data)))

        elif table == "receivable":
            issues.extend(self._verify_receivable_references(Receivable.from_dict(data)))

        elif table == "savings_goal":
            goal = SavingsGoal.from_dict(data)
            if self.get_account(goal.account) is None:
                issues.append(
                    BookIssue(
                        "savings_goal.missing_account",
                        f"savings goal {goal.name!r} refers to missing account {goal.account}",
                        handle,
                    )
                )

        elif table == "reconciliation":
            issues.extend(self._verify_reconciliation_references(Reconciliation.from_dict(data)))

        return issues

    def _verify_reconciliation_references(self, reconciliation: Reconciliation) -> list[BookIssue]:
        issues: list[BookIssue] = []
        if self.get_account(reconciliation.account) is None:
            issues.append(
                BookIssue(
                    "reconciliation.missing_account",
                    f"reconciliation {reconciliation.handle} refers to missing account "
                    f"{reconciliation.account}",
                    reconciliation.handle,
                )
            )
            return issues
        split_accounts = {
            split.handle: split.account
            for transaction in self.iter_transactions()
            for split in transaction.splits
        }
        for split_handle in reconciliation.selected_splits:
            split_account = split_accounts.get(split_handle)
            if split_account is None:
                issues.append(
                    BookIssue(
                        "reconciliation.missing_split",
                        f"reconciliation {reconciliation.handle} refers to missing split "
                        f"{split_handle}",
                        reconciliation.handle,
                    )
                )
            elif split_account != reconciliation.account:
                issues.append(
                    BookIssue(
                        "reconciliation.wrong_account",
                        f"reconciliation {reconciliation.handle} includes split {split_handle} "
                        f"from another account",
                        reconciliation.handle,
                    )
                )
        return issues

    def _verify_fsa_claim_references(self, claim: FsaClaim) -> list[BookIssue]:
        issues: list[BookIssue] = []
        for link in claim.links():
            transaction = self.get_transaction(link.transaction)
            if transaction is None:
                issues.append(
                    BookIssue(
                        "fsa_claim.missing_transaction",
                        f"FSA claim {claim.handle} refers to missing transaction "
                        f"{link.transaction}",
                        claim.handle,
                    )
                )
            elif not any(split.handle == link.split for split in transaction.splits):
                issues.append(
                    BookIssue(
                        "fsa_claim.missing_split",
                        f"FSA claim {claim.handle} refers to missing split {link.split}",
                        claim.handle,
                    )
                )
        for allocation in claim.allocations:
            if self.get_account(allocation.account) is None:
                issues.append(
                    BookIssue(
                        "fsa_claim.missing_account",
                        f"FSA claim {claim.handle} refers to missing account {allocation.account}",
                        claim.handle,
                    )
                )
        if claim.receivable is not None and self.get_receivable(claim.receivable) is None:
            issues.append(
                BookIssue(
                    "fsa_claim.missing_receivable",
                    f"FSA claim {claim.handle} refers to missing receivable {claim.receivable}",
                    claim.handle,
                )
            )
        return issues

    def _verify_receivable_references(self, receivable: Receivable) -> list[BookIssue]:
        issues: list[BookIssue] = []
        if receivable.account is not None and self.get_account(receivable.account) is None:
            issues.append(
                BookIssue(
                    "receivable.missing_account",
                    f"receivable {receivable.handle} refers to missing account "
                    f"{receivable.account}",
                    receivable.handle,
                )
            )
        for link in [*receivable.expenses, *receivable.reimbursements]:
            transaction = self.get_transaction(link.transaction)
            if transaction is None:
                issues.append(
                    BookIssue(
                        "receivable.missing_transaction",
                        f"receivable {receivable.handle} refers to missing transaction "
                        f"{link.transaction}",
                        receivable.handle,
                    )
                )
            elif not any(split.handle == link.split for split in transaction.splits):
                issues.append(
                    BookIssue(
                        "receivable.missing_split",
                        f"receivable {receivable.handle} refers to missing split {link.split}",
                        receivable.handle,
                    )
                )
        return issues

    def _verify_derived_row(self, table: str, handle: str, data: dict[str, Any]) -> list[BookIssue]:
        columns_by_table: dict[str, tuple[str, ...]] = {
            "commodity": ("mnemonic",),
            "price": ("commodity", "currency", "quote_date", "source"),
            "account": ("parent", "name", "atype"),
            "txn": ("post_date", "description"),
            "scheduled": ("name",),
            "scenario": ("name",),
            "fsa_claim": ("service_date", "provider"),
            "receivable": ("incurred_date", "payer"),
            "reconciliation": ("account", "statement_date", "status"),
            "payee": ("name",),
            "savings_goal": ("name",),
        }
        defaults: dict[tuple[str, str], Any] = {
            ("commodity", "mnemonic"): "",
            ("price", "commodity"): "",
            ("price", "currency"): "",
            ("price", "quote_date"): "",
            ("price", "source"): "",
            ("account", "parent"): None,
            ("account", "name"): "",
            ("account", "atype"): "",
            ("txn", "description"): "",
            ("scheduled", "name"): "",
            ("scenario", "name"): "",
            ("fsa_claim", "service_date"): "",
            ("receivable", "incurred_date"): "",
            ("fsa_claim", "provider"): "",
            ("receivable", "payer"): "",
            ("reconciliation", "account"): "",
            ("reconciliation", "statement_date"): "",
            ("reconciliation", "status"): "",
            ("payee", "name"): "",
            ("savings_goal", "name"): "",
        }
        columns = columns_by_table[table]
        selected = ", ".join(("handle", *columns))
        row = (
            self._require()
            .execute(f"SELECT {selected} FROM {table} WHERE handle=?", (handle,))
            .fetchone()
        )
        if row is None:
            return [
                BookIssue(f"{table}.missing_row", f"{table} object {handle} was not stored", handle)
            ]
        issues: list[BookIssue] = []
        if data.get("handle") != handle:
            issues.append(
                BookIssue(
                    f"{table}.handle_mismatch",
                    f"{table} row {handle} contains object handle {data.get('handle')!r}",
                    handle,
                )
            )
        for column in columns:
            expected = data.get(column, defaults.get((table, column)))
            if table == "txn" and column == "post_date" and expected is not None:
                expected = str(expected)
            if row[column] != expected:
                issues.append(
                    BookIssue(
                        f"{table}.index_mismatch",
                        f"{table} derived column {column} disagrees with blob for {handle}",
                        handle,
                    )
                )
        return issues

    def _verify_transaction_index(self, transaction: Transaction) -> list[BookIssue]:
        expected = {
            split.handle: (
                transaction.handle,
                split.account,
                transaction.post_date.isoformat(),
                split.value.numerator,
                split.value.denominator,
                split.quantity.numerator,
                split.quantity.denominator,
            )
            for split in transaction.splits
        }
        actual = {
            row["handle"]: (
                row["txn"],
                row["account"],
                row["post_date"],
                row["value_num"],
                row["value_den"],
                row["quantity_num"],
                row["quantity_den"],
            )
            for row in self._require().execute(
                "SELECT handle, txn, account, post_date, value_num, value_den, "
                "quantity_num, quantity_den "
                "FROM split_index WHERE txn=?",
                (transaction.handle,),
            )
        }
        issues: list[BookIssue] = []
        for split_handle in sorted(expected.keys() - actual.keys()):
            issues.append(
                BookIssue(
                    "split_index.missing",
                    f"split {split_handle} is missing from split_index",
                    split_handle,
                )
            )
        for split_handle in sorted(actual.keys() - expected.keys()):
            issues.append(
                BookIssue(
                    "split_index.orphan",
                    f"split_index contains unknown split {split_handle}",
                    split_handle,
                )
            )
        for split_handle in sorted(expected.keys() & actual.keys()):
            if expected[split_handle] != actual[split_handle]:
                issues.append(
                    BookIssue(
                        "split_index.mismatch",
                        f"split_index disagrees with transaction data for split {split_handle}",
                        split_handle,
                    )
                )
        return issues

    def _verify_deleted_reference(self, table: str, handle: str) -> list[BookIssue]:
        issues: list[BookIssue] = []
        if table == "metadata":
            return issues
        if table == "account":
            for account in self._accounts.values():
                if account.parent == handle:
                    issues.append(
                        BookIssue(
                            "account.missing_parent",
                            f"account {account.name!r} refers to missing parent {handle}",
                            account.handle,
                        )
                    )
                if account.linked_asset == handle:
                    issues.append(
                        BookIssue(
                            "account.missing_linked_asset",
                            f"account {account.name!r} refers to missing linked asset {handle}",
                            account.handle,
                        )
                    )
                if account.card_payment_account == handle:
                    issues.append(
                        BookIssue(
                            "account.missing_card_payment_account",
                            f"account {account.name!r} refers to missing card payment account "
                            f"{handle}",
                            account.handle,
                        )
                    )
            for reconciliation in self.iter_reconciliations(account=handle):
                issues.append(
                    BookIssue(
                        "reconciliation.missing_account",
                        f"reconciliation {reconciliation.handle} refers to missing account "
                        f"{handle}",
                        reconciliation.handle,
                    )
                )
            row = (
                self._require()
                .execute("SELECT txn FROM split_index WHERE account=? LIMIT 1", (handle,))
                .fetchone()
            )
            if row is not None:
                issues.append(
                    BookIssue(
                        "transaction.missing_account",
                        f"transaction {row['txn']} has a split for missing account {handle}",
                        row["txn"],
                    )
                )
            for scheduled in self.iter_scheduled():
                if any(split.account == handle for split in scheduled.splits):
                    issues.append(
                        BookIssue(
                            "scheduled.missing_account",
                            f"scheduled transaction {scheduled.name!r} refers to missing account "
                            f"{handle}",
                            scheduled.handle,
                        )
                    )
            for scenario in self.iter_scenarios():
                if handle in scenario.account_references():
                    issues.append(
                        BookIssue(
                            "scenario.missing_account",
                            f"scenario {scenario.name!r} refers to missing account {handle}",
                            scenario.handle,
                        )
                    )
            for claim in self.iter_fsa_claims():
                if any(allocation.account == handle for allocation in claim.allocations):
                    issues.append(
                        BookIssue(
                            "fsa_claim.missing_account",
                            f"FSA claim {claim.handle} refers to missing account {handle}",
                            claim.handle,
                        )
                    )
            for receivable in self.iter_receivables():
                if receivable.account == handle:
                    issues.append(
                        BookIssue(
                            "receivable.missing_account",
                            f"receivable {receivable.handle} refers to missing account {handle}",
                            receivable.handle,
                        )
                    )
            for goal in self.iter_savings_goals():
                if goal.account == handle:
                    issues.append(
                        BookIssue(
                            "savings_goal.missing_account",
                            f"savings goal {goal.name!r} refers to missing account {handle}",
                            goal.handle,
                        )
                    )

        elif table == "txn":
            existing_splits = {
                split.handle
                for transaction in self.iter_transactions()
                for split in transaction.splits
            }
            for reconciliation in self.iter_reconciliations():
                for split_handle in reconciliation.selected_splits:
                    if split_handle not in existing_splits:
                        issues.append(
                            BookIssue(
                                "reconciliation.missing_split",
                                f"reconciliation {reconciliation.handle} refers to missing "
                                f"split {split_handle}",
                                reconciliation.handle,
                            )
                        )
            for claim in self.iter_fsa_claims():
                if any(link.transaction == handle for link in claim.links()):
                    issues.append(
                        BookIssue(
                            "fsa_claim.missing_transaction",
                            f"FSA claim {claim.handle} refers to missing transaction {handle}",
                            claim.handle,
                        )
                    )
            for receivable in self.iter_receivables():
                receivable_links = [*receivable.expenses, *receivable.reimbursements]
                if any(link.transaction == handle for link in receivable_links):
                    issues.append(
                        BookIssue(
                            "receivable.missing_transaction",
                            f"receivable {receivable.handle} refers to missing transaction "
                            f"{handle}",
                            receivable.handle,
                        )
                    )

        elif table == "commodity":
            for account in self._accounts.values():
                if account.commodity == handle:
                    issues.append(
                        BookIssue(
                            "account.missing_commodity",
                            f"account {account.name!r} refers to missing commodity {handle}",
                            account.handle,
                        )
                    )
            for transaction in self.iter_transactions():
                if transaction.currency == handle:
                    issues.append(
                        BookIssue(
                            "transaction.missing_currency",
                            f"transaction {transaction.describe()} "
                            f"refers to missing currency {handle}",
                            transaction.handle,
                        )
                    )
            for scheduled in self.iter_scheduled():
                if scheduled.currency == handle:
                    issues.append(
                        BookIssue(
                            "scheduled.missing_currency",
                            f"scheduled transaction {scheduled.name!r} refers to missing currency "
                            f"{handle}",
                            scheduled.handle,
                        )
                    )
            for price in self.iter_prices():
                if price.commodity == handle or price.currency == handle:
                    issues.append(
                        BookIssue(
                            "price.missing_commodity",
                            f"price {price.handle} refers to deleted commodity {handle}",
                            price.handle,
                        )
                    )

        elif table == "receivable":
            for claim in self.iter_fsa_claims():
                if claim.receivable == handle:
                    issues.append(
                        BookIssue(
                            "fsa_claim.missing_receivable",
                            f"FSA claim {claim.handle} refers to missing receivable {handle}",
                            claim.handle,
                        )
                    )

        return issues
