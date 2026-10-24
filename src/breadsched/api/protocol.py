"""What a book server offers, whichever process it runs in (#355).

The protocol grows area by area as interfaces move onto it; scheduled transactions
come first. Requests and results are the services' own typed dataclasses, so a
caller written against an in-process server works unchanged against a remote one.
Writes either succeed or raise :class:`~breadsched.api.contract.ApiError` without
changing the book.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from ..gen.engine.reconciliation import ReconciliationSummary
from ..gen.lib import Scenario, ScheduledTransaction, Transaction
from ..gen.lib.reconciliation import Reconciliation
from ..gen.lib.savings_goal import SavingsGoal
from ..gen.lib.transaction import ReconcileState
from ..gen.services import (
    AccountBalance,
    AccountRecord,
    AccountRef,
    AdvanceDays,
    BalanceQuery,
    BalanceSummary,
    ChartLine,
    DueResolution,
    DueScheduleReview,
    DuplicateSchedule,
    FormulaScheduleInput,
    NextOccurrence,
    PostDue,
    PostedDue,
    RegisterPage,
    RegisterQuery,
    ResolveDue,
    ResolveNext,
    SaveAccount,
    SavedAccount,
    SavedSchedule,
    SavedTransaction,
    SaveFixedSchedule,
    SaveTransaction,
    SetAdvanceDays,
    TagCount,
    TransactionInput,
)
from ..gen.services.categorization import (
    AddRule,
    AppliedCategories,
    CategorizationRules,
    CategoryRule,
)
from ..gen.services.reconciliations import (
    ReconciliationView,
    StartReconciliation,
    ToggleCleared,
    UpdateReconciliation,
)
from ..gen.services.savings_goals import (
    AllocateToGoal,
    SaveSavingsGoal,
    SavingsGoalsView,
    SetGoalOverride,
)

__all__ = ["BookApi", "Change", "Listener"]


@dataclass(frozen=True, slots=True)
class Change:
    """One committed change to the book, as every subscriber sees it.

    ``revision`` is the book's change counter after the change; it rises with each
    recorded change, undo, and redo, whichever process made it. ``kinds`` names what
    changed (``transaction``, ``scheduled``, and so on, as the storage signals call
    them) and ``label`` is the undo step's wording, when there is one. An empty
    ``kinds`` means only that the book moved on, as when a client reconnects after
    missing changes.
    """

    revision: int
    kinds: frozenset[str]
    label: str | None = None


Listener = Callable[[Change], None]


class BookApi(Protocol):
    """The operations every interface reaches a book through."""

    @property
    def revision(self) -> int:
        """The book's change counter: it rises with every recorded change."""
        ...

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        """Call ``listener`` after each change; returns a function that stops it."""
        ...

    def undo(self) -> bool: ...

    def redo(self) -> bool: ...

    # ------------------------------------------------- scheduled transactions

    def find_schedule(self, reference: str) -> ScheduledTransaction:
        """A schedule by handle, unique handle prefix, or exact name."""
        ...

    def due_review(self, as_of: date | None = None) -> list[DueScheduleReview]: ...

    def resolve_due(self, request: ResolveDue) -> DueResolution: ...

    def next_occurrence(
        self, schedule: str, as_of: date | None = None
    ) -> NextOccurrence | None: ...

    def resolve_next(self, request: ResolveNext) -> DueResolution: ...

    def advance_days(self, schedule: str) -> AdvanceDays: ...

    def set_advance_days(self, request: SetAdvanceDays) -> AdvanceDays: ...

    def delete_schedule(self, schedule: str) -> SavedSchedule:
        """Remove a definition; what it already posted stays in the ledger."""
        ...

    def duplicate_schedule(self, request: DuplicateSchedule) -> SavedSchedule:
        """Save an independent exact copy, including editor-protected fields."""
        ...

    def post_due(self, request: PostDue) -> PostedDue:
        """Post what is due without a review (automatic schedules, or all)."""
        ...

    def save_fixed_schedule(self, request: SaveFixedSchedule) -> SavedSchedule:
        """Create or replace a fixed schedule from the editor's typed input."""
        ...

    def save_formula_schedule(self, request: FormulaScheduleInput) -> SavedSchedule:
        """Replace a formula schedule's editable inputs, keeping its structure."""
        ...

    # ------------------------------------------------------------ transactions

    def save_transaction(self, request: SaveTransaction) -> SavedTransaction:
        """Add a transaction, or replace one, with an optional claim link."""
        ...

    def delete_transaction(self, transaction: str) -> SavedTransaction:
        """Remove a transaction (never a receivable's own reclassification)."""
        ...

    # ------------------------------------------------------------ ledger reads

    def transaction_currency(self) -> str:
        """The currency a new transaction is entered in."""
        ...

    def find_account(self, reference: str) -> AccountRef:
        """An account by handle, full name, or unique bare name."""
        ...

    def find_transaction(self, reference: str) -> Transaction:
        """A transaction, as stored, by handle or unique handle prefix."""
        ...

    def editable_transaction(self, transaction: str) -> TransactionInput:
        """The stored transaction in the editors' input form."""
        ...

    def register(self, query: RegisterQuery) -> RegisterPage: ...

    def account_balance(self, query: BalanceQuery) -> AccountBalance: ...

    def balance_summary(self, as_of: date | None = None) -> BalanceSummary: ...

    # ---------------------------------------------------------------- accounts

    def accounts(self) -> list[AccountRecord]:
        """Every account as stored, the root included, by full name."""
        ...

    def account(self, account: str) -> AccountRecord:
        """One account as stored, by handle."""
        ...

    def chart(self, as_of: date | None = None, include_hidden: bool = False) -> list[ChartLine]:
        """The account tree, depth first, each valued with its quote evidence."""
        ...

    def save_account(self, request: SaveAccount) -> SavedAccount:
        """Add or change an account (and post an opening balance, if given)."""
        ...

    def delete_account(self, account: str) -> SavedAccount:
        """Remove an account nothing uses."""
        ...

    # ------------------------------------------------- categorization rules

    def categorization_rules(self) -> CategorizationRules:
        """The rules in priority order, their current proposals, and the choices."""
        ...

    def add_rule(self, request: AddRule) -> CategoryRule:
        """Insert a rule (last, unless a 1-based position is given)."""
        ...

    def move_rule(self, rule: str, position: int) -> CategoryRule:
        """Move a rule to a 1-based position; earlier rules win."""
        ...

    def delete_rule(self, rule: str) -> CategoryRule: ...

    def apply_category_proposals(
        self, transactions: tuple[str, ...] | None = None
    ) -> AppliedCategories:
        """Accept the current proposals: all of them, or only these transactions'."""
        ...

    # -------------------------------------------------------------------- tags

    def tag_counts(self) -> list[TagCount]:
        """Every tag in the book with how many transactions carry it."""
        ...

    def transactions_with_tag(self, tag: str) -> list[Transaction]:
        """The transactions carrying ``tag`` (any case), as stored."""
        ...

    def set_tags(self, transaction: str, tags: list[str]) -> Transaction:
        """Replace a transaction's tags; returns it as stored."""
        ...

    # ------------------------------------------------------------ savings goals

    def savings_goals(self, as_of: date, include_closed: bool = False) -> SavingsGoalsView:
        """Every goal's earmark on ``as_of``, its scenario changes, and the choices."""
        ...

    def find_savings_goal(self, reference: str) -> SavingsGoal:
        """A goal, as stored, by handle, unique handle prefix, or name."""
        ...

    def save_savings_goal(self, request: SaveSavingsGoal) -> SavingsGoal: ...

    def allocate_to_goal(self, request: AllocateToGoal) -> SavingsGoal: ...

    def close_savings_goal(self, goal: str, closed_on: date) -> SavingsGoal: ...

    def reopen_savings_goal(self, goal: str) -> SavingsGoal: ...

    def delete_savings_goal(self, goal: str) -> str:
        """Delete a goal and its scenario changes; returns its handle."""
        ...

    def set_goal_override(self, request: SetGoalOverride) -> Scenario:
        """Change a goal in one scenario; returns the scenario as stored."""
        ...

    # ---------------------------------------------------------- reconciliation

    def reconciliation(self, account: str) -> ReconciliationView:
        """An account's open statement session, its history, and waiting proposals."""
        ...

    def start_reconciliation(self, request: StartReconciliation) -> ReconciliationSummary: ...

    def update_reconciliation(self, request: UpdateReconciliation) -> ReconciliationSummary:
        """Replace the checked entries and/or the ending balance."""
        ...

    def complete_reconciliation(self, reconciliation: str) -> Reconciliation: ...

    def cancel_reconciliation(self, reconciliation: str) -> Reconciliation: ...

    def reopen_reconciliation(self, reconciliation: str) -> Reconciliation: ...

    def toggle_cleared(self, request: ToggleCleared) -> ReconcileState:
        """Cycle one split between not cleared and cleared."""
        ...
