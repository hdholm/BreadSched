"""Which actuals count toward which planned occurrence, and for how much (decision 0005).

A split counts toward an occurrence through its ``plan_allocations``. This module
reads them back by occurrence and writes the one kind of allocation earlier
releases already made: a whole transaction counting toward one occurrence (posting
a schedule, or matching in Review), as allocations of its *plan legs* in full.

- **Plan legs** are the splits that say what a transaction was for: those on an
  income or expense account, or carrying a planning purpose. A transaction with
  none (a transfer to savings, say) uses its positive legs instead.
- **A transaction's share** of an occurrence is what it allocates to it over the
  value of its plan legs. The occurrence's applied amount is that share of the
  transaction's gross (its positive legs), so a whole match counts exactly what
  earlier releases counted, a paycheck's withholding included, and a receipt
  split between two estimates counts its part toward each.
- **Older links.** A transaction linked as a whole by ``planned_occurrence`` or
  ``scheduled_from`` with no allocations of its own (a book upgraded in memory,
  or a fixture) counts as allocating its plan legs in full.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date
from fractions import Fraction

from ..db.sqlite import DbSQLite
from ..lib.account import AccountClass
from ..lib.money import Money
from ..lib.scheduled import ScheduledTransaction
from ..lib.transaction import PlanAllocation, Split, Transaction

__all__ = [
    "ClassOf",
    "OccurrenceActual",
    "account_classes",
    "allocate_posted",
    "allocate_whole",
    "clear_allocations",
    "linked_key",
    "occurrence_actuals",
    "plan_legs",
]

ClassOf = Callable[[str], AccountClass | None]

_CATEGORY = frozenset({AccountClass.INCOME, AccountClass.EXPENSE})


@dataclass(frozen=True, slots=True)
class OccurrenceActual:
    """One transaction's part in one occurrence."""

    transaction: Transaction
    share: Fraction
    #: The share of the transaction's gross, in the occurrence's terms.
    amount: Money
    planned_for: date | None
    planned_amount: Money | None
    #: Linked as a whole (posted from the schedule, or matched in Review).
    whole: bool
    #: What it allocates, account by account, in the splits' signs.
    allocated: tuple[tuple[str, Money], ...] = ()

    @property
    def splits(self) -> tuple[tuple[str, Money], ...]:
        """The transaction's legs, each scaled to this share."""
        return tuple((split.account, split.value * self.share) for split in self.transaction.splits)


def account_classes(db: DbSQLite) -> ClassOf:
    """Each account's class, read once per account."""
    seen: dict[str, AccountClass | None] = {}

    def class_of(handle: str) -> AccountClass | None:
        if handle not in seen:
            account = db.get_account(handle)
            seen[handle] = account.account_class if account is not None else None
        return seen[handle]

    return class_of


def plan_legs(transaction: Transaction, class_of: ClassOf) -> list[Split]:
    """The splits that say what ``transaction`` was for (see the module notes)."""
    legs = [
        split
        for split in transaction.splits
        if split.value and (split.planning_flow is not None or class_of(split.account) in _CATEGORY)
    ]
    return legs or [split for split in transaction.splits if split.value > 0]


def linked_key(transaction: Transaction) -> str | None:
    """The occurrence a whole-transaction link names, if any."""
    if transaction.planned_occurrence is not None:
        return transaction.planned_occurrence
    if transaction.scheduled_from is not None:
        return ScheduledTransaction.occurrence_key_for(
            transaction.scheduled_from, transaction.planned_for or transaction.post_date
        )
    return None


def clear_allocations(transaction: Transaction) -> None:
    for split in transaction.splits:
        split.plan_allocations.clear()


def allocate_whole(
    transaction: Transaction,
    key: str,
    planned_for: date | None,
    planned_amount: Money | None,
    class_of: ClassOf,
) -> Transaction:
    """Allocate every plan leg of ``transaction`` in full to occurrence ``key``.

    Whatever the transaction counted toward before is replaced, as matching a
    whole transaction always has.
    """
    clear_allocations(transaction)
    for split in plan_legs(transaction, class_of):
        split.plan_allocations.append(PlanAllocation(key, split.value, planned_for, planned_amount))
    return transaction


def allocate_posted(transaction: Transaction, class_of: ClassOf) -> Transaction:
    """Allocate a transaction just posted from a schedule to its occurrence in full."""
    key = linked_key(transaction)
    if key is None:
        return transaction
    return allocate_whole(
        transaction, key, transaction.planned_for, transaction.planned_amount, class_of
    )


def _gross(transaction: Transaction) -> Money:
    total = Money(0)
    for split in transaction.splits:
        if split.value > 0:
            total = total + split.value
    return total


def _parts(transaction: Transaction, class_of: ClassOf) -> Iterable[tuple[str, OccurrenceActual]]:
    allocated: dict[str, Money] = defaultdict(lambda: Money(0))
    snapshot: dict[str, PlanAllocation] = {}
    by_account: dict[str, dict[str, Money]] = defaultdict(lambda: defaultdict(lambda: Money(0)))
    for split in transaction.splits:
        for item in split.plan_allocations:
            allocated[item.occurrence] += abs(item.amount)
            snapshot.setdefault(item.occurrence, item)
            by_account[item.occurrence][split.account] += item.amount
    gross = _gross(transaction)
    whole_key = linked_key(transaction)
    if not allocated:
        if whole_key is not None:
            yield (
                whole_key,
                OccurrenceActual(
                    transaction,
                    Fraction(1),
                    gross,
                    transaction.planned_for,
                    transaction.planned_amount,
                    True,
                    _by_account(plan_legs(transaction, class_of)),
                ),
            )
        return
    base = sum((abs(split.value) for split in plan_legs(transaction, class_of)), Money(0))
    for key, amount in allocated.items():
        share = Fraction(1) if not base else min(Fraction(1), amount / base)
        item = snapshot[key]
        yield (
            key,
            OccurrenceActual(
                transaction,
                share,
                gross * share,
                item.planned_for,
                item.planned_amount,
                key == whole_key and share == 1,
                tuple(by_account[key].items()),
            ),
        )


def _by_account(splits: Iterable[Split]) -> tuple[tuple[str, Money], ...]:
    found: dict[str, Money] = defaultdict(lambda: Money(0))
    for split in splits:
        found[split.account] += split.value
    return tuple(found.items())


def occurrence_actuals(
    db: DbSQLite, class_of: ClassOf | None = None
) -> dict[str, list[OccurrenceActual]]:
    """Occurrence key → every transaction counting toward it, in ledger order."""
    classes = class_of or account_classes(db)
    found: dict[str, list[OccurrenceActual]] = defaultdict(list)
    for transaction in db.iter_plan_linked_transactions():
        for key, part in _parts(transaction, classes):
            found[key].append(part)
    return dict(found)
