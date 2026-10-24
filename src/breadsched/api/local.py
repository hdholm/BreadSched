"""The in-process book server (#356).

:class:`LocalBookServer` wraps one open :class:`~breadsched.gen.db.sqlite.DbSQLite`
and implements :class:`~breadsched.api.protocol.BookApi` over the shared services.
It observes the book's own storage signals, so a change made through it, by undo or
redo, or by any other code holding the same book in this process, raises the
revision and reaches every subscriber.

The revision is the book's own change counter (:meth:`DbSQLite.revision`), stored
with the book, so every process that opens it agrees on it (#357).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date

from ..gen.db.sqlite import DbSQLite
from ..gen.engine.receivables import ReimbursementProposal
from ..gen.engine.reconciliation import ReconciliationSummary
from ..gen.lib import Scenario, ScheduledTransaction, Transaction
from ..gen.lib.receivable import Receivable
from ..gen.lib.reconciliation import Reconciliation
from ..gen.lib.savings_goal import SavingsGoal
from ..gen.lib.scenario import AssumptionPeriod, Assumptions, Drawdown
from ..gen.lib.transaction import ReconcileState
from ..gen.services import (
    AccountBalance,
    AccountRecord,
    AccountRef,
    AdvanceDays,
    BalanceQuery,
    BalanceSummary,
    ChartLine,
    DeleteAccount,
    DeleteSchedule,
    DeleteTransaction,
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
    ServiceError,
    SetAdvanceDays,
    TagCount,
    TransactionInput,
    account_balance,
    account_record,
    account_records,
    balance_summary,
    categorization,
    chart_of_accounts,
    delete_account,
    delete_schedule,
    delete_transaction,
    duplicate_schedule,
    editable_transaction,
    find_account,
    find_schedule,
    find_transaction,
    next_occurrence,
    pending_due_review,
    post_due,
    reconciliations,
    register_page,
    resolve_due,
    resolve_next,
    save_account,
    save_fixed_schedule,
    save_formula_schedule,
    save_transaction,
    savings_goals,
    set_advance_days,
    set_tags,
    tag_counts,
    transaction_currency,
    transactions_with_tag,
)
from ..gen.services import advance_days as stored_advance_days
from ..gen.services import assumptions as assumption_service
from ..gen.services import receivables as receivable_service
from ..gen.services import scenarios as scenario_service
from ..gen.services.assumptions import SaveBaseAssumptions, SaveScenarioAssumptions
from ..gen.services.categorization import (
    AddRule,
    AppliedCategories,
    CategorizationRules,
    CategoryRule,
)
from ..gen.services.receivables import (
    AcceptedReimbursements,
    CandidateLine,
    ReceivablesView,
    RecordWriteOff,
    SaveReceivable,
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
from ..gen.services.scenarios import (
    DuplicateScenario,
    SaveDrawdown,
    SavedScenario,
    SaveScenario,
    SetReimbursementOverride,
    SuppressScenarioSchedule,
)
from .contract import ApiError, unwrap
from .protocol import Change, Listener

__all__ = ["LocalBookServer", "book_server", "lookup_schedule", "open_book_server"]

#: Storage signal stems whose add, update, and delete signals name a change's kinds.
_STEMS = (
    "account",
    "transaction",
    "scheduled",
    "scenario",
    "commodity",
    "price",
    "fsa-claim",
    "reconciliation",
    "receivable",
    "savings-goal",
)


class LocalBookServer:
    """A book server in this process, over one open book."""

    def __init__(self, db: DbSQLite) -> None:
        self.db = db
        self._pending: set[str] = set()
        self._listeners: list[Listener] = []
        known = db._known_signals()
        self._connections: list[int] = []
        for stem in _STEMS:
            for action in ("add", "update", "delete"):
                signal = f"{stem}-{action}"
                if signal in known:
                    self._connections.append(
                        db.connect(signal, lambda _handles, kind=stem: self._pending.add(kind))
                    )
        self._connections.append(db.connect("database-changed", self._on_changed))

    # ------------------------------------------------------------- changes

    @property
    def revision(self) -> int:
        return self.db.revision()

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        self._listeners.append(listener)

        def stop() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return stop

    def _on_changed(self, _db: DbSQLite) -> None:
        change = Change(self.db.revision(), frozenset(self._pending), self.db.undo_message())
        self._pending.clear()
        for listener in list(self._listeners):
            listener(change)

    def close(self) -> None:
        """Stop observing the book (the book itself stays open)."""
        for handle in self._connections:
            self.db.disconnect(handle)
        self._connections.clear()
        self._listeners.clear()

    def undo(self) -> bool:
        return self.db.undo()

    def redo(self) -> bool:
        return self.db.redo()

    # ------------------------------------------------- scheduled transactions

    def find_schedule(self, reference: str) -> ScheduledTransaction:
        return lookup_schedule(self.db, reference)

    def due_review(self, as_of: date | None = None) -> list[DueScheduleReview]:
        return pending_due_review(self.db, as_of)

    def resolve_due(self, request: ResolveDue) -> DueResolution:
        return unwrap(resolve_due(self.db, request))

    def next_occurrence(self, schedule: str, as_of: date | None = None) -> NextOccurrence | None:
        return next_occurrence(self.db, schedule, as_of=as_of)

    def resolve_next(self, request: ResolveNext) -> DueResolution:
        return unwrap(resolve_next(self.db, request))

    def advance_days(self, schedule: str) -> AdvanceDays:
        found = stored_advance_days(self.db, schedule)
        if found is None:
            raise ApiError.from_service(ServiceError("schedule.not_found", ("schedule",)))
        return found

    def set_advance_days(self, request: SetAdvanceDays) -> AdvanceDays:
        return unwrap(set_advance_days(self.db, request))

    def delete_schedule(self, schedule: str) -> SavedSchedule:
        return unwrap(delete_schedule(self.db, DeleteSchedule(schedule)))

    def duplicate_schedule(self, request: DuplicateSchedule) -> SavedSchedule:
        return unwrap(duplicate_schedule(self.db, request))

    def post_due(self, request: PostDue) -> PostedDue:
        return unwrap(post_due(self.db, request))

    def save_fixed_schedule(self, request: SaveFixedSchedule) -> SavedSchedule:
        return unwrap(save_fixed_schedule(self.db, request))

    def save_formula_schedule(self, request: FormulaScheduleInput) -> SavedSchedule:
        return unwrap(save_formula_schedule(self.db, request))

    # ------------------------------------------------------------ transactions

    def save_transaction(self, request: SaveTransaction) -> SavedTransaction:
        return unwrap(save_transaction(self.db, request))

    def delete_transaction(self, transaction: str) -> SavedTransaction:
        return unwrap(delete_transaction(self.db, DeleteTransaction(transaction)))

    # ------------------------------------------------------------ ledger reads

    def transaction_currency(self) -> str:
        return transaction_currency(self.db)

    def find_account(self, reference: str) -> AccountRef:
        return _named(find_account(self.db, reference), reference, "account")

    def find_transaction(self, reference: str) -> Transaction:
        return _named(find_transaction(self.db, reference), reference, "transaction")

    def editable_transaction(self, transaction: str) -> TransactionInput:
        return unwrap(editable_transaction(self.db, transaction))

    def register(self, query: RegisterQuery) -> RegisterPage:
        return unwrap(register_page(self.db, query))

    def account_balance(self, query: BalanceQuery) -> AccountBalance:
        return unwrap(account_balance(self.db, query))

    def balance_summary(self, as_of: date | None = None) -> BalanceSummary:
        return balance_summary(self.db, as_of)

    # ---------------------------------------------------------------- accounts

    def accounts(self) -> list[AccountRecord]:
        return list(account_records(self.db))

    def account(self, account: str) -> AccountRecord:
        return unwrap(account_record(self.db, account))

    def chart(self, as_of: date | None = None, include_hidden: bool = False) -> list[ChartLine]:
        return list(chart_of_accounts(self.db, as_of, include_hidden))

    def save_account(self, request: SaveAccount) -> SavedAccount:
        return unwrap(save_account(self.db, request))

    def delete_account(self, account: str) -> SavedAccount:
        return unwrap(delete_account(self.db, DeleteAccount(account)))

    # -------------------------------------------------------------------- tags

    def tag_counts(self) -> list[TagCount]:
        return tag_counts(self.db)

    def transactions_with_tag(self, tag: str) -> list[Transaction]:
        return transactions_with_tag(self.db, tag)

    def set_tags(self, transaction: str, tags: list[str]) -> Transaction:
        return unwrap(set_tags(self.db, transaction, tags))

    # ---------------------------------------------------------------- scenarios

    def scenarios(self) -> list[Scenario]:
        return list(self.db.iter_scenarios())

    def find_scenario(self, reference: str) -> Scenario:
        found = self.db.get_scenario(reference) or self.db.get_scenario_by_name(reference)
        if found is None:
            raise ApiError(
                404, "scenario.not_found", ("scenario",), f"no scenario matches {reference!r}"
            )
        return found

    def save_scenario(self, request: SaveScenario) -> SavedScenario:
        return unwrap(scenario_service.save_scenario(self.db, request))

    def save_scenario_assumptions(self, request: SaveScenarioAssumptions) -> SavedScenario:
        return unwrap(assumption_service.save_scenario_assumptions(self.db, request))

    def duplicate_scenario(self, request: DuplicateScenario) -> SavedScenario:
        return unwrap(scenario_service.duplicate_scenario(self.db, request))

    def delete_scenario(self, scenario: str) -> SavedScenario:
        request = scenario_service.DeleteScenario(scenario)
        return unwrap(scenario_service.delete_scenario(self.db, request))

    def suppress_scenario_schedule(self, request: SuppressScenarioSchedule) -> SavedScenario:
        return unwrap(scenario_service.suppress_scenario_schedule(self.db, request))

    def drawdown_accounts(self) -> tuple[list[str], list[str]]:
        return scenario_service.drawdown_accounts(self.db)

    def save_drawdown(self, request: SaveDrawdown) -> Drawdown:
        return unwrap(scenario_service.save_drawdown(self.db, request))

    def remove_drawdown(self, scenario: str, drawdown: str) -> Drawdown:
        return unwrap(scenario_service.remove_drawdown(self.db, scenario, drawdown))

    def base_assumptions(self) -> Assumptions:
        return assumption_service.base_assumptions(self.db)

    def save_base_assumptions(self, request: SaveBaseAssumptions) -> Assumptions:
        with self._changing("base-assumptions"):
            return unwrap(assumption_service.save_base_assumptions(self.db, request))

    def save_assumption_period(
        self, scenario: str, period: AssumptionPeriod, index: int | None = None
    ) -> SavedScenario:
        request = assumption_service.SaveAssumptionPeriod(scenario, period, index)
        return unwrap(assumption_service.save_assumption_period(self.db, request))

    def delete_assumption_period(self, scenario: str, index: int) -> SavedScenario:
        request = assumption_service.DeleteAssumptionPeriod(scenario, index)
        return unwrap(assumption_service.delete_assumption_period(self.db, request))

    # -------------------------------------------------------------- receivables

    def receivables_view(self, as_of: date | None = None) -> ReceivablesView:
        return receivable_service.receivables_view(self.db, as_of)

    def receivable_candidates(
        self, receivable: str | None = None
    ) -> tuple[tuple[CandidateLine, ...], tuple[CandidateLine, ...]]:
        return unwrap(receivable_service.receivable_candidate_lines(self.db, receivable))

    def find_receivable(self, reference: str) -> Receivable:
        found = receivable_service.find_receivable(self.db, reference)
        return _named(found, reference, "receivable")

    def save_receivable(self, request: SaveReceivable) -> Receivable:
        return unwrap(receivable_service.save_receivable(self.db, request))

    def link_receivable_split(
        self, receivable: str, transaction: str, split: str, role: str
    ) -> Receivable:
        if role not in ("expense", "reimbursement"):
            raise ApiError(
                400, "receivable.role.invalid", ("role",), "choose expense or reimbursement"
            )
        link = (
            receivable_service.attach_expense_split
            if role == "expense"
            else receivable_service.attach_reimbursement_split
        )
        return unwrap(link(self.db, receivable, transaction, split))

    def detach_receivable_split(self, receivable: str, transaction: str, split: str) -> Receivable:
        return unwrap(receivable_service.detach_split(self.db, receivable, transaction, split))

    def mark_disputed(self, receivable: str, disputed_on: date, note: str = "") -> Receivable:
        return unwrap(receivable_service.mark_disputed(self.db, receivable, disputed_on, note))

    def clear_dispute(self, receivable: str) -> Receivable:
        return unwrap(receivable_service.clear_dispute(self.db, receivable))

    def record_write_off(self, request: RecordWriteOff) -> Receivable:
        return unwrap(receivable_service.record_write_off(self.db, request))

    def delete_receivable(self, receivable: str) -> str:
        return unwrap(receivable_service.delete_receivable(self.db, receivable))

    def reimbursement_proposals(
        self, as_of: date | None = None, account: str | None = None
    ) -> tuple[ReimbursementProposal, ...]:
        found = receivable_service.reimbursement_proposals(self.db, as_of=as_of, account=account)
        return unwrap(found)

    def accept_reimbursements(
        self, chosen: tuple[tuple[str, str, str], ...]
    ) -> AcceptedReimbursements:
        return unwrap(receivable_service.accept_reimbursements(self.db, chosen))

    def set_reimbursement_override(self, request: SetReimbursementOverride) -> Scenario:
        return unwrap(scenario_service.set_reimbursement_override(self.db, request))

    # ---------------------------------------------------------- reconciliation

    def reconciliation(self, account: str) -> ReconciliationView:
        return unwrap(reconciliations.reconciliation_view(self.db, account))

    def start_reconciliation(self, request: StartReconciliation) -> ReconciliationSummary:
        return unwrap(reconciliations.start_reconciliation(self.db, request))

    def update_reconciliation(self, request: UpdateReconciliation) -> ReconciliationSummary:
        return unwrap(reconciliations.update_reconciliation(self.db, request))

    def complete_reconciliation(self, reconciliation: str) -> Reconciliation:
        action = reconciliations.ReconciliationAction(reconciliation)
        return unwrap(reconciliations.complete_reconciliation(self.db, action))

    def cancel_reconciliation(self, reconciliation: str) -> Reconciliation:
        action = reconciliations.ReconciliationAction(reconciliation)
        return unwrap(reconciliations.cancel_reconciliation(self.db, action))

    def reopen_reconciliation(self, reconciliation: str) -> Reconciliation:
        action = reconciliations.ReconciliationAction(reconciliation)
        return unwrap(reconciliations.reopen_reconciliation(self.db, action))

    def toggle_cleared(self, request: ToggleCleared) -> ReconcileState:
        return unwrap(reconciliations.toggle_cleared(self.db, request))

    # ------------------------------------------------------------ savings goals

    def savings_goals(self, as_of: date, include_closed: bool = False) -> SavingsGoalsView:
        return savings_goals.savings_goals_view(self.db, as_of, include_closed=include_closed)

    def find_savings_goal(self, reference: str) -> SavingsGoal:
        return _named(
            savings_goals.find_savings_goal(self.db, reference), reference, "savings goal"
        )

    def save_savings_goal(self, request: SaveSavingsGoal) -> SavingsGoal:
        return unwrap(savings_goals.save_savings_goal(self.db, request))

    def allocate_to_goal(self, request: AllocateToGoal) -> SavingsGoal:
        return unwrap(savings_goals.allocate_to_goal(self.db, request))

    def close_savings_goal(self, goal: str, closed_on: date) -> SavingsGoal:
        return unwrap(savings_goals.close_savings_goal(self.db, goal, closed_on))

    def reopen_savings_goal(self, goal: str) -> SavingsGoal:
        return unwrap(savings_goals.reopen_savings_goal(self.db, goal))

    def delete_savings_goal(self, goal: str) -> str:
        return unwrap(savings_goals.delete_savings_goal(self.db, goal))

    def set_goal_override(self, request: SetGoalOverride) -> Scenario:
        return unwrap(savings_goals.set_goal_override(self.db, request))

    # ------------------------------------------------- categorization rules

    @contextmanager
    def _changing(self, kind: str) -> Iterator[None]:
        """Name ``kind`` in the change a write inside the block makes.

        Rules are kept in the book's metadata, which raises no storage signal.
        """
        self._pending.add(kind)
        try:
            yield
        finally:
            self._pending.discard(kind)

    def categorization_rules(self) -> CategorizationRules:
        return categorization.categorization_rules(self.db)

    def add_rule(self, request: AddRule) -> CategoryRule:
        with self._changing("categorization-rule"):
            return unwrap(categorization.add_rule(self.db, request))

    def move_rule(self, rule: str, position: int) -> CategoryRule:
        with self._changing("categorization-rule"):
            return unwrap(categorization.move_rule(self.db, rule, position))

    def delete_rule(self, rule: str) -> CategoryRule:
        with self._changing("categorization-rule"):
            return unwrap(categorization.delete_rule(self.db, rule))

    def apply_category_proposals(
        self, transactions: tuple[str, ...] | None = None
    ) -> AppliedCategories:
        return unwrap(categorization.apply_category_proposals(self.db, transactions))


def book_server(db: DbSQLite) -> LocalBookServer:
    """The one in-process server over ``db``, for code that holds the book itself.

    GTK holds its book directly; each view and dialog moved onto the book API asks
    for the server here, so one server (and one set of storage-signal connections)
    serves the whole application.
    """
    # Kept on the book itself (a cycle the collector frees with it), not in a cache
    # whose values would keep every book ever opened alive.
    server = getattr(db, "_book_server", None)
    if not isinstance(server, LocalBookServer):
        server = LocalBookServer(db)
        db._book_server = server  # type: ignore[attr-defined]
    return server


def _named(found, reference: str, kind: str):
    """``found``'s value, or its failure worded with the reference that was asked for."""
    if found.value is not None:
        return found.value
    error = found.errors[0]
    if error.code.endswith(".not_found"):
        raise ApiError(404, error.code, error.fields, f"no {kind} matches {reference!r}")
    raise ApiError(
        400,
        error.code,
        error.fields,
        f"{reference!r} matches more than one {kind}; use more characters",
    )


def lookup_schedule(db: DbSQLite, reference: str) -> ScheduledTransaction:
    """A schedule by handle, unique handle prefix, or exact name, or an ApiError."""
    found = find_schedule(db, reference)
    if found.value is not None:
        return found.value
    error = found.errors[0]
    if error.code == "schedule.not_found":
        raise ApiError(404, error.code, error.fields, f"no schedule matches {reference!r}")
    raise ApiError(400, error.code, error.fields, f"{reference!r} matches more than one schedule")


@contextmanager
def open_book_server(path: str, mode: str = "w") -> Iterator[LocalBookServer]:
    """Open the book at ``path`` and serve it in process until the block ends."""
    db = DbSQLite()
    db.load(path, mode=mode)
    server = LocalBookServer(db)
    try:
        yield server
    finally:
        server.close()
        db.close()
