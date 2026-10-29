"""What each role in a book may do, and the guard that enforces it (decision 0004).

**Experimental.** Every :class:`~breadsched.api.protocol.BookApi` operation declares
one permission, and an architecture test keeps the table complete, so a new
operation cannot be added open:

- ``ANY``: every role (the book's revision and id).
- ``LOOKUP``: what entering a transaction needs: the account tree without values,
  entry suggestions, currencies.
- ``READ``: everything else that reads.
- ``ENTRY``: adding a transaction (``save_transaction`` of a new one only).
- ``WRITE``: every other change, undo and redo included.
- ``OWNER``: the attachment and GnuCash folders and writing back to GnuCash.

The roles: a *viewer* reads; an *entry* user looks up and enters; an *editor* does
both and writes; an *owner* may do anything. The computer account that runs the
host is the owner of every book it holds.

The web layer reaches the book only through :class:`GuardedBook`, which checks the
role the transport set for the request (:func:`acting_as`) before every operation.
In process (GTK, a command with no server) no role is set and nothing is checked:
the book file's own permissions decide.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from enum import StrEnum
from typing import Any

from ..gen.services import ServiceError
from ..presentation import service_error_message
from .contract import ApiError

__all__ = [
    "OPERATIONS",
    "ROLES",
    "ROLE_PERMISSIONS",
    "GuardedBook",
    "Permission",
    "acting_as",
    "current_role",
    "require",
    "require_permission",
]


class Permission(StrEnum):
    ANY = "any"
    LOOKUP = "lookup"
    READ = "read"
    ENTRY = "entry"
    WRITE = "write"
    OWNER = "owner"


#: The roles a user may hold in a book, most to least.
ROLES = ("owner", "editor", "viewer", "entry")

ROLE_PERMISSIONS: dict[str, frozenset[Permission]] = {
    "owner": frozenset(Permission),
    "editor": frozenset(
        {Permission.ANY, Permission.LOOKUP, Permission.READ, Permission.ENTRY, Permission.WRITE}
    ),
    "viewer": frozenset({Permission.ANY, Permission.LOOKUP, Permission.READ}),
    "entry": frozenset({Permission.ANY, Permission.LOOKUP, Permission.ENTRY}),
    #: A request from nobody known: nothing at all.
    "none": frozenset(),
}

_ANY = {"revision", "book_id"}
_LOOKUP = {
    "account_outline",
    "suggest_entry",
    "entry_amount",
    "entry_trade",
    "entry_accounts",
    "next_num",
    "transaction_currency",
    "find_account",
    "currencies",
    "find_currency",
    "reporting_currency",
}
_ENTRY = {"save_transaction"}
_OWNER = {
    "set_attachment_folder",
    "set_source_link_folder",
    "writeback_preview",
    "writeback_keep_backups",
    "set_writeback_keep_backups",
    "apply_writeback",
}
_WRITE = {
    "undo",
    "redo",
    "resolve_due",
    "resolve_next",
    "set_advance_days",
    "delete_schedule",
    "duplicate_schedule",
    "post_due",
    "save_fixed_schedule",
    "save_formula_schedule",
    "save_fixed_scenario_schedule",
    "save_formula_scenario_schedule",
    "delete_transaction",
    "save_account",
    "delete_account",
    "add_rule",
    "move_rule",
    "delete_rule",
    "apply_category_proposals",
    "set_tags",
    "save_savings_goal",
    "allocate_to_goal",
    "close_savings_goal",
    "reopen_savings_goal",
    "delete_savings_goal",
    "set_goal_override",
    "start_reconciliation",
    "update_reconciliation",
    "complete_reconciliation",
    "cancel_reconciliation",
    "reopen_reconciliation",
    "toggle_cleared",
    "add_statement_entry",
    "accept_historical_estimate",
    "save_receivable",
    "link_receivable_split",
    "detach_receivable_split",
    "mark_disputed",
    "clear_dispute",
    "record_write_off",
    "delete_receivable",
    "accept_reimbursements",
    "set_reimbursement_override",
    "save_scenario",
    "save_scenario_assumptions",
    "duplicate_scenario",
    "delete_scenario",
    "suppress_scenario_schedule",
    "save_drawdown",
    "remove_drawdown",
    "save_base_assumptions",
    "save_assumption_period",
    "delete_assumption_period",
    "save_payroll_template",
    "delete_payroll_template",
    "create_paycheck",
    "apply_pay_change",
    "save_fsa_claim",
    "delete_fsa_claim",
    "close_fsa_claim",
    "reopen_fsa_claim",
    "accept_claim_links",
    "match_review",
    "reject_review",
    "skip_review",
    "mark_review_unexpected",
    "attach_review_claim",
    "apply_account_suggestions",
    "resolve_held_imports",
    "set_sale_lots",
    "set_tax_marks",
    "store_quotes",
    "set_quote_source",
    "save_loan",
    "save_exchange_rate",
    "save_security_price",
    # Book-wide until decision 0004's phase 4 makes the Dashboard's choices personal.
    "save_dashboard_settings",
    "import_csv",
    "import_book",
    "upload_import_file",
    "link_attachment",
    "attach_file",
    "attach_content",
    "unlink_attachment",
    "relink_attachment",
}
_READ = {
    "subscribe",
    "find_schedule",
    "due_review",
    "due_occurrences",
    "next_occurrence",
    "advance_days",
    "scenario_events",
    "draft_fixed_scenario_schedule",
    "draft_formula_scenario_schedule",
    "book_counts",
    "book_summary",
    "verify_book",
    "find_transaction",
    "transaction_editor",
    "editable_transaction",
    "register",
    "register_view",
    "register_changes",
    "account_balance",
    "balance_summary",
    "accounts",
    "commodities",
    "account",
    "chart",
    "categorization_rules",
    "tag_counts",
    "transactions_with_tag",
    "savings_goals",
    "find_savings_goal",
    "reconciliation",
    "reconciled_balance",
    "schedules_view",
    "schedule_editor",
    "scheduled_page",
    "schedule_layout",
    "schedule_draft",
    "historical_estimates",
    "activity",
    "budget_jars",
    "holdings",
    "realized_gains",
    "tax_year",
    "receivables_view",
    "receivable_candidates",
    "find_receivable",
    "reimbursement_proposals",
    "reimbursable_costs",
    "scenarios",
    "find_scenario",
    "drawdown_accounts",
    "base_assumptions",
    "payroll",
    "paycheck",
    "template_from_schedule",
    "preview_pay_change",
    "fsa_claims",
    "fsa_claim_standing",
    "find_fsa_claim",
    "fsa_claim_report",
    "benefit_years",
    "claim_link_proposals",
    "review_queue",
    "review_detail",
    "plan_occurrence",
    "unresolved_occurrences",
    "match_candidates",
    "held_imports",
    "account_suggestions",
    "sale_lots",
    "tax_marks",
    "quote_plan",
    "loan_choices",
    "preview_loan",
    "securities",
    "dashboard",
    "dashboard_settings",
    "default_dashboard_settings",
    "explain_plan_cell",
    "inspect_csv",
    "preview_csv",
    "import_source",
    "find",
    "attachment_report",
    "plan_settings",
    # Each person's own Plan view (decision 0004): whoever reads may keep theirs.
    "save_plan_settings",
    "plan_bounds",
    "plan",
    "expense_explorer",
    "project",
    "projection_month",
    "net_worth_history",
    "net_worth_change",
}

#: Every operation's permission.
OPERATIONS: dict[str, Permission] = {
    **dict.fromkeys(_ANY, Permission.ANY),
    **dict.fromkeys(_LOOKUP, Permission.LOOKUP),
    **dict.fromkeys(_READ, Permission.READ),
    **dict.fromkeys(_ENTRY, Permission.ENTRY),
    **dict.fromkeys(_WRITE, Permission.WRITE),
    **dict.fromkeys(_OWNER, Permission.OWNER),
}

_ROLE: ContextVar[str | None] = ContextVar("breadsched_book_role", default=None)


@contextmanager
def acting_as(role: str | None) -> Iterator[None]:
    """Serve the calls inside the block (in this thread) for a user in ``role``;
    ``None`` checks nothing (in process)."""
    token = _ROLE.set(role)
    try:
        yield
    finally:
        _ROLE.reset(token)


def current_role() -> str | None:
    return _ROLE.get()


def _refuse(code: str) -> ApiError:
    return ApiError(403, code, (), service_error_message(ServiceError(code, ())))


def require(operation: str) -> None:
    """Refuse ``operation`` unless the current role may use it."""
    role = _ROLE.get()
    if role is None:
        return
    permission = OPERATIONS.get(operation)
    if permission is None or permission not in ROLE_PERMISSIONS.get(role, frozenset()):
        raise _refuse("book.permission.denied")


def require_permission(permission: Permission) -> None:
    """Refuse unless the current role holds ``permission``: for the few web routes that
    reach the book file itself rather than through :class:`GuardedBook`."""
    role = _ROLE.get()
    if role is not None and permission not in ROLE_PERMISSIONS.get(role, frozenset()):
        raise _refuse("book.permission.denied")


class GuardedBook:
    """A book server whose every operation is checked against the request's role."""

    __slots__ = ("_book",)

    def __init__(self, book: Any) -> None:
        self._book = book

    def __getattr__(self, name: str) -> Any:
        if _ROLE.get() is None:
            return getattr(self._book, name)
        require(name)
        value = getattr(self._book, name)
        if name == "save_transaction" and _ROLE.get() == "entry":
            return _new_only(value)
        return value


def _new_only(save: Callable[..., Any]) -> Callable[..., Any]:
    """An entry user adds transactions; changing one already saved needs an editor."""

    def guarded(request: Any, *args: Any, **kwargs: Any) -> Any:
        if getattr(request, "existing_handle", None) is not None:
            raise _refuse("book.permission.denied")
        return save(request, *args, **kwargs)

    return guarded
