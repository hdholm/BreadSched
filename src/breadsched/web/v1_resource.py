"""Versioned book-server resources: ``/api/v1`` (#357).

**Experimental.** Each handler serves one operation of the book API
(:class:`~breadsched.api.protocol.BookApi`) as a resource, through the request's
book server (``Api.book``), and returns its JSON form from
:mod:`breadsched.api.views`. ``web.resources.V1_ROUTES`` maps methods and paths to
them; ``web.transport`` adds each response's revision (``ETag``), refuses a write
whose ``If-Match`` names an older revision, and streams change events.

Resources so far, all under ``/api/v1``:

* ``GET /book`` – the book's revision.
* ``GET /due-review`` (``as_of``) and ``POST /due-review`` (``decisions``, ``as_of``).
* ``GET /schedules/lookup`` (``reference``) – one schedule, as it is stored.
* ``GET`` and ``POST /schedules/{handle}/next`` (``decision``, ``date``, ``as_of``).
* ``GET`` and ``PATCH /schedules/{handle}/advance-days``.
* ``DELETE /schedules/{handle}`` and ``POST /schedules/{handle}/duplicate`` (``name``).
* ``POST /post-due`` (``as_of``, ``all``) – post what is due without a review.
* ``POST /schedules/fixed`` and ``POST /schedules/formula`` – save from an editor;
  the body is ``api.schedule_codec``'s form of the typed request.
* ``POST /transactions`` (``api.transaction_codec``'s form of ``SaveTransaction``)
  and ``DELETE /transactions/{handle}``.
* ``GET /currency`` – the currency a new transaction is entered in.
* ``GET /accounts/lookup`` and ``GET /transactions/lookup`` (``reference``) – an
  account, or a transaction as it is stored, by handle, name, or unique prefix.
* ``GET /transactions/{handle}/editable`` – the transaction in the editors' form.
* ``GET /accounts/{handle}/register`` (``start``, ``end``, ``tag``, ``limit``).
* ``GET /accounts/{handle}/balance`` (``as_of``, ``recursive``) and ``GET /balances``
  (``as_of``) – one account's balance, or the book's cash and net worth.
* ``GET /accounts`` and ``GET /accounts/{handle}`` – accounts as stored, with full
  names; ``GET /chart`` (``as_of``, ``hidden``) – the valued account tree.
* ``POST /accounts`` (``api.account_codec``'s form of ``SaveAccount``) and
  ``DELETE /accounts/{handle}``.
* ``GET`` and ``POST /rules`` (``category``, ``description``, ``position``) – the
  categorization rules with their proposals, and adding one; ``PATCH /rules/{handle}``
  (``position``) moves one and ``DELETE /rules/{handle}`` removes it.
  ``POST /rules/accept`` (``transactions``, or all) accepts proposals.
* ``GET /tags`` – every tag with its count; ``GET /tags/transactions`` (``tag``) –
  the transactions carrying one, as stored; ``PATCH /transactions/{handle}/tags``
  (``tags``) replaces a transaction's tags.
* ``GET /goals`` (``as_of``, ``closed``) – savings goals with their scenario changes
  and the forms' choices; ``GET /goals/lookup`` (``reference``); ``POST /goals``
  saves one (``handle`` to edit); ``POST /goals/{handle}/allocations``,
  ``/close``, ``/reopen``, and ``/override``; ``DELETE /goals/{handle}``.
* ``GET /accounts/{handle}/reconciliation`` – the open statement session, the
  history, and waiting proposals; ``POST /reconciliations`` (``account``,
  ``statement_date``, ``ending_balance``) starts one; ``PATCH /reconciliations/{handle}``
  (``selected_splits``, ``ending_balance``); ``POST /reconciliations/{handle}/complete``,
  ``/cancel``, and ``/reopen``; ``POST /transactions/{handle}/splits/{split}/cleared``.
* ``GET /receivables`` (``as_of``) – receivables with their links and the forms'
  choices; ``GET /receivables/lookup`` (``reference``);
  ``GET /receivables/candidates`` (``receivable``) – splits it could take;
  ``POST /receivables`` saves one; ``POST /receivables/{handle}/splits`` links a split and
  ``DELETE /receivables/{handle}/splits/{transaction}/{split}`` unlinks it;
  ``POST`` and ``DELETE /receivables/{handle}/dispute``;
  ``POST /receivables/{handle}/write-offs``; ``DELETE /receivables/{handle}``;
  ``GET /reimbursement-proposals`` (``as_of``, ``account``) and
  ``POST /reimbursement-proposals/accept``; ``POST /scenarios/{handle}/reimbursements``.
  These read and write the generic typed form (``api.typed_codec``).
* ``GET /scenarios`` (stored) and ``GET /scenarios/lookup`` (``reference``: handle or
  name); ``POST /scenarios`` saves (``SaveScenario``) and ``POST /scenario-assumptions``
  saves after checking assumptions; ``POST /scenarios/duplicate``;
  ``DELETE /scenarios/{handle}``; ``POST /scenarios/{handle}/suppressed-schedules``;
  ``GET /drawdown-accounts``, ``POST /drawdowns``, and
  ``DELETE /scenarios/{handle}/drawdowns/{drawdown}``; ``GET`` and ``POST /assumptions``;
  ``POST /scenarios/{handle}/periods`` and ``DELETE /scenarios/{handle}/periods/{index}``.
* ``POST /undo`` and ``POST /redo``.
* ``GET /events`` (``since``) – server-sent change events, served by the transport.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any, Literal

from ..api.account_codec import save_account_from_json
from ..api.schedule_codec import fixed_request_from_json, formula_request_from_json
from ..api.transaction_codec import save_transaction_from_json, transaction_input_json
from ..api.typed_codec import from_data, to_data
from ..api.views import (
    account_balance_json,
    account_record_json,
    account_ref_json,
    advance_days_json,
    applied_categories_json,
    balance_summary_json,
    categorization_rules_json,
    chart_json,
    due_review_json,
    next_occurrence_json,
    posted_due_json,
    reconciliation_summary_json,
    reconciliation_view_json,
    register_json,
    resolution_json,
    rule_json,
    saved_account_json,
    saved_schedule_json,
    saved_transaction_json,
    savings_goals_json,
    tag_counts_json,
)
from ..gen.lib.money import Money
from ..gen.lib.scenario import AssumptionPeriod
from ..gen.services import (
    BalanceQuery,
    DueDecision,
    DuplicateSchedule,
    PostDue,
    RegisterQuery,
    ResolveDue,
    ResolveNext,
    SetAdvanceDays,
)
from ..gen.services.assumptions import SaveBaseAssumptions, SaveScenarioAssumptions
from ..gen.services.categorization import AddRule
from ..gen.services.receivables import RecordWriteOff, SaveReceivable
from ..gen.services.reconciliations import (
    StartReconciliation,
    ToggleCleared,
    UpdateReconciliation,
)
from ..gen.services.savings_goals import AllocateToGoal, SaveSavingsGoal, SetGoalOverride
from ..gen.services.scenarios import (
    DuplicateScenario,
    SaveDrawdown,
    SaveScenario,
    SetReimbursementOverride,
    SuppressScenarioSchedule,
)
from .context import Api

if TYPE_CHECKING:
    from .resources import QueryParams

__all__ = [
    "assumption_period_delete",
    "assumption_period_save",
    "base_assumptions_get",
    "base_assumptions_save",
    "drawdown_accounts_get",
    "drawdown_remove",
    "drawdown_save",
    "scenario_assumptions_save",
    "scenario_delete_v1",
    "scenario_duplicate_v1",
    "scenario_lookup",
    "scenario_save_v1",
    "scenario_suppress",
    "scenarios_get",
    "receivable_candidates_get",
    "receivable_clear_dispute",
    "receivable_delete",
    "receivable_detach",
    "receivable_dispute",
    "receivable_link",
    "receivable_lookup",
    "receivable_save",
    "receivable_write_off",
    "receivables_get",
    "reimbursement_override",
    "reimbursement_proposals_accept",
    "reimbursement_proposals_get",
    "cleared_toggle",
    "reconciliation_cancel",
    "reconciliation_complete",
    "reconciliation_get",
    "reconciliation_reopen",
    "reconciliation_start",
    "reconciliation_update",
    "goal_allocate",
    "goal_close",
    "goal_delete",
    "goal_lookup",
    "goal_override",
    "goal_reopen",
    "goal_save",
    "goals_get",
    "tags_get",
    "tags_patch",
    "tagged_get",
    "rule_add",
    "rule_delete",
    "rule_move",
    "rules_accept",
    "rules_get",
    "account_delete",
    "account_get",
    "account_save",
    "accounts_get",
    "chart_get",
    "account_balance_get",
    "account_lookup",
    "balances_get",
    "currency_get",
    "register_get",
    "transaction_editable",
    "transaction_lookup",
    "advance_days_get",
    "advance_days_patch",
    "book_get",
    "due_review_get",
    "due_review_post",
    "next_get",
    "next_post",
    "post_due_post",
    "schedule_delete",
    "schedule_duplicate",
    "schedule_save_fixed",
    "schedule_save_formula",
    "transaction_delete",
    "transaction_save",
    "redo_post",
    "schedule_lookup",
    "undo_post",
]

Path = dict[str, str]


def _optional_date(body: dict[str, Any], key: str) -> date | None:
    raw = body.get(key)
    if raw in (None, ""):
        return None
    if not isinstance(raw, str):
        raise ValueError(f"{key} must be a date (YYYY-MM-DD)")
    return date.fromisoformat(raw)


def _optional_days(body: dict[str, Any], key: str) -> int | None:
    raw = body.get(key)
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError(f"{key} must be a whole number of days")
    return raw


def _only(body: dict[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise ValueError(f"unknown field: {', '.join(unknown)}")


def book_get(api: Api, _path: Path, query: QueryParams) -> dict:
    query.finish()
    return {"revision": api.book.revision}


def due_review_get(api: Api, _path: Path, query: QueryParams) -> dict:
    as_of = query.iso_date("as_of")
    query.finish()
    return due_review_json(api.book.due_review(as_of))


def due_review_post(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    _only(body, {"decisions", "as_of"})
    raw = body.get("decisions")
    if not isinstance(raw, list):
        raise ValueError("decisions must be a list")
    decisions: list[tuple[str, date, DueDecision]] = []
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"schedule", "date", "decision"}:
            raise ValueError("each decision needs exactly schedule, date, and decision")
        decisions.append(
            (
                str(item["schedule"]),
                date.fromisoformat(str(item["date"])),
                DueDecision(item["decision"]),
            )
        )
    request = ResolveDue(tuple(decisions), as_of=_optional_date(body, "as_of"))
    return resolution_json(api.book.resolve_due(request))


def schedule_lookup(api: Api, _path: Path, query: QueryParams) -> dict:
    reference = query.text("reference", required=True) or ""
    query.finish()
    return {"schedule": api.book.find_schedule(reference).serialize()}


def next_get(api: Api, path: Path, query: QueryParams) -> dict:
    as_of = query.iso_date("as_of")
    query.finish()
    return {"next": next_occurrence_json(api.book.next_occurrence(path["handle"], as_of))}


def next_post(api: Api, path: Path, body: dict[str, Any]) -> dict:
    _only(body, {"decision", "date", "as_of"})
    decision = body.get("decision")
    if decision not in ("post", "skip"):
        raise ValueError("decision must be post or skip")
    request = ResolveNext(
        path["handle"],
        DueDecision(decision),
        when=_optional_date(body, "date"),
        as_of=_optional_date(body, "as_of"),
    )
    return resolution_json(api.book.resolve_next(request))


def advance_days_get(api: Api, path: Path, query: QueryParams) -> dict:
    query.finish()
    return advance_days_json(api.book.advance_days(path["handle"]))


def advance_days_patch(api: Api, path: Path, body: dict[str, Any]) -> dict:
    _only(body, {"advance_days", "remind_days"})
    request = SetAdvanceDays(
        path["handle"],
        advance_days=_optional_days(body, "advance_days"),
        remind_days=_optional_days(body, "remind_days"),
    )
    return advance_days_json(api.book.set_advance_days(request))


def undo_post(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    _only(body, set())
    return {"done": api.book.undo(), "revision": api.book.revision}


def redo_post(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    _only(body, set())
    return {"done": api.book.redo(), "revision": api.book.revision}


def schedule_delete(api: Api, path: Path, body: dict[str, Any]) -> dict:
    _only(body, set())
    return saved_schedule_json(api.book.delete_schedule(path["handle"]))


def schedule_duplicate(api: Api, path: Path, body: dict[str, Any]) -> dict:
    _only(body, {"name"})
    name = body.get("name")
    if name is not None and not isinstance(name, str):
        raise ValueError("name must be text")
    request = DuplicateSchedule(path["handle"], name=name or None)
    return saved_schedule_json(api.book.duplicate_schedule(request))


def post_due_post(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    _only(body, {"as_of", "all"})
    everything = body.get("all", False)
    if not isinstance(everything, bool):
        raise ValueError("all must be true or false")
    request = PostDue(as_of=_optional_date(body, "as_of"), only_automatic=not everything)
    return posted_due_json(api.book.post_due(request))


def schedule_save_fixed(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    return saved_schedule_json(api.book.save_fixed_schedule(fixed_request_from_json(body)))


def schedule_save_formula(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    return saved_schedule_json(api.book.save_formula_schedule(formula_request_from_json(body)))


def transaction_save(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    return saved_transaction_json(api.book.save_transaction(save_transaction_from_json(body)))


def transaction_delete(api: Api, path: Path, body: dict[str, Any]) -> dict:
    _only(body, set())
    return saved_transaction_json(api.book.delete_transaction(path["handle"]))


def currency_get(api: Api, _path: Path, query: QueryParams) -> dict:
    query.finish()
    return {"currency": api.book.transaction_currency()}


def account_lookup(api: Api, _path: Path, query: QueryParams) -> dict:
    reference = query.text("reference", required=True) or ""
    query.finish()
    return {"account": account_ref_json(api.book.find_account(reference))}


def transaction_lookup(api: Api, _path: Path, query: QueryParams) -> dict:
    reference = query.text("reference", required=True) or ""
    query.finish()
    return {"transaction": api.book.find_transaction(reference).serialize()}


def transaction_editable(api: Api, path: Path, query: QueryParams) -> dict:
    query.finish()
    return {"definition": transaction_input_json(api.book.editable_transaction(path["handle"]))}


def register_get(api: Api, path: Path, query: QueryParams) -> dict:
    request = RegisterQuery(
        path["handle"],
        start=query.iso_date("start"),
        end=query.iso_date("end"),
        tag=query.text("tag"),
        limit=query.integer("limit", minimum=0),
    )
    query.finish()
    return register_json(api.book.register(request))


def account_balance_get(api: Api, path: Path, query: QueryParams) -> dict:
    request = BalanceQuery(
        path["handle"],
        as_of=query.iso_date("as_of"),
        recursive=query.choice("recursive", ("true", "false"), "false") == "true",
    )
    query.finish()
    return account_balance_json(api.book.account_balance(request))


def balances_get(api: Api, _path: Path, query: QueryParams) -> dict:
    as_of = query.iso_date("as_of")
    query.finish()
    return balance_summary_json(api.book.balance_summary(as_of))


def accounts_get(api: Api, _path: Path, query: QueryParams) -> dict:
    query.finish()
    return {"accounts": [account_record_json(record) for record in api.book.accounts()]}


def account_get(api: Api, path: Path, query: QueryParams) -> dict:
    query.finish()
    return account_record_json(api.book.account(path["handle"]))


def chart_get(api: Api, _path: Path, query: QueryParams) -> dict:
    as_of = query.iso_date("as_of")
    hidden = query.choice("hidden", ("true", "false"), "false") == "true"
    query.finish()
    return chart_json(api.book.chart(as_of, hidden))


def account_save(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    return saved_account_json(api.book.save_account(save_account_from_json(body)))


def account_delete(api: Api, path: Path, body: dict[str, Any]) -> dict:
    _only(body, set())
    return saved_account_json(api.book.delete_account(path["handle"]))


def _required_text(body: dict[str, Any], key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be text")
    return value


def _position(body: dict[str, Any], key: str) -> int | None:
    value = body.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be a whole number")
    return value


def rules_get(api: Api, _path: Path, query: QueryParams) -> dict:
    query.finish()
    return categorization_rules_json(api.book.categorization_rules())


def rule_add(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    _only(body, {"category", "description", "position"})
    request = AddRule(
        category=_required_text(body, "category"),
        description=_required_text(body, "description"),
        position=_position(body, "position"),
    )
    return rule_json(api.book.add_rule(request))


def rule_move(api: Api, path: Path, body: dict[str, Any]) -> dict:
    _only(body, {"position"})
    position = _position(body, "position")
    if position is None:
        raise ValueError("position must be a whole number")
    return rule_json(api.book.move_rule(path["handle"], position))


def rule_delete(api: Api, path: Path, body: dict[str, Any]) -> dict:
    _only(body, set())
    return rule_json(api.book.delete_rule(path["handle"]))


def rules_accept(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    _only(body, {"transactions"})
    raw = body.get("transactions")
    if raw is not None and (
        not isinstance(raw, list) or not all(isinstance(item, str) for item in raw)
    ):
        raise ValueError("transactions must be a list of text")
    chosen = None if raw is None else tuple(raw)
    return applied_categories_json(api.book.apply_category_proposals(chosen))


def tags_get(api: Api, _path: Path, query: QueryParams) -> dict:
    query.finish()
    return tag_counts_json(api.book.tag_counts())


def tagged_get(api: Api, _path: Path, query: QueryParams) -> dict:
    tag = query.text("tag", required=True) or ""
    query.finish()
    return {"transactions": [item.serialize() for item in api.book.transactions_with_tag(tag)]}


def tags_patch(api: Api, path: Path, body: dict[str, Any]) -> dict:
    _only(body, {"tags"})
    tags = body.get("tags")
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise ValueError("tags must be a list of text")
    return {"transaction": api.book.set_tags(path["handle"], tags).serialize()}


def _money(body: dict[str, Any], key: str, *, optional: bool = False) -> Money | None:
    raw = body.get(key)
    if raw is None and optional:
        return None
    if not isinstance(raw, str):
        raise ValueError(f"{key} must be a decimal amount in text")
    try:
        return Money(raw)
    except (ArithmeticError, TypeError, ValueError):
        raise ValueError(f"{key} is not an amount") from None


def _required_date(body: dict[str, Any], key: str) -> date:
    found = _optional_date(body, key)
    if found is None:
        raise ValueError(f"{key} must be a date (YYYY-MM-DD)")
    return found


def _optional_text(body: dict[str, Any], key: str) -> str | None:
    value = body.get(key)
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{key} must be text")
    return value


def _goal_json(goal) -> dict:
    return {"goal": goal.serialize()}


def goals_get(api: Api, _path: Path, query: QueryParams) -> dict:
    as_of = query.iso_date("as_of") or date.today()
    closed = query.choice("closed", ("true", "false"), "false") == "true"
    query.finish()
    return savings_goals_json(api.book.savings_goals(as_of, closed))


def goal_lookup(api: Api, _path: Path, query: QueryParams) -> dict:
    reference = query.text("reference", required=True) or ""
    query.finish()
    return _goal_json(api.book.find_savings_goal(reference))


def goal_save(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    _only(
        body,
        {"handle", "name", "account", "target_amount", "target_date", "start_date", "description"},
    )
    target = _money(body, "target_amount")
    assert target is not None
    request = SaveSavingsGoal(
        name=_required_text(body, "name"),
        account=_required_text(body, "account"),
        target_amount=target,
        target_date=_required_date(body, "target_date"),
        start_date=_required_date(body, "start_date"),
        description=_optional_text(body, "description") or "",
        handle=_optional_text(body, "handle"),
    )
    return _goal_json(api.book.save_savings_goal(request))


def goal_allocate(api: Api, path: Path, body: dict[str, Any]) -> dict:
    _only(body, {"amount", "date", "memo"})
    amount = _money(body, "amount")
    assert amount is not None
    request = AllocateToGoal(
        path["handle"], amount, _required_date(body, "date"), _optional_text(body, "memo") or ""
    )
    return _goal_json(api.book.allocate_to_goal(request))


def goal_close(api: Api, path: Path, body: dict[str, Any]) -> dict:
    _only(body, {"date"})
    return _goal_json(api.book.close_savings_goal(path["handle"], _required_date(body, "date")))


def goal_reopen(api: Api, path: Path, body: dict[str, Any]) -> dict:
    _only(body, set())
    return _goal_json(api.book.reopen_savings_goal(path["handle"]))


def goal_delete(api: Api, path: Path, body: dict[str, Any]) -> dict:
    _only(body, set())
    return {"deleted": api.book.delete_savings_goal(path["handle"])}


def goal_override(api: Api, path: Path, body: dict[str, Any]) -> dict:
    _only(
        body,
        {"scenario", "target_amount", "target_date", "excluded", "purchase_on", "purchase_account"},
    )
    excluded = body.get("excluded", False)
    if not isinstance(excluded, bool):
        raise ValueError("excluded must be true or false")
    request = SetGoalOverride(
        scenario=_required_text(body, "scenario"),
        goal=path["handle"],
        target_amount=_money(body, "target_amount", optional=True),
        target_date=_optional_date(body, "target_date"),
        excluded=excluded,
        purchase_on=_optional_date(body, "purchase_on"),
        purchase_account=_optional_text(body, "purchase_account"),
    )
    return {"scenario": api.book.set_goal_override(request).serialize()}


def reconciliation_get(api: Api, path: Path, query: QueryParams) -> dict:
    query.finish()
    return reconciliation_view_json(api.book.reconciliation(path["handle"]))


def reconciliation_start(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    _only(body, {"account", "statement_date", "ending_balance"})
    ending = _money(body, "ending_balance")
    assert ending is not None
    request = StartReconciliation(
        _required_text(body, "account"), _required_date(body, "statement_date"), ending
    )
    return reconciliation_summary_json(api.book.start_reconciliation(request))


def reconciliation_update(api: Api, path: Path, body: dict[str, Any]) -> dict:
    _only(body, {"selected_splits", "ending_balance"})
    raw = body.get("selected_splits")
    if raw is not None and (
        not isinstance(raw, list) or not all(isinstance(item, str) for item in raw)
    ):
        raise ValueError("selected_splits must be a list of split handles")
    request = UpdateReconciliation(
        path["handle"],
        selected_splits=None if raw is None else tuple(raw),
        ending_balance=_money(body, "ending_balance", optional=True),
    )
    return reconciliation_summary_json(api.book.update_reconciliation(request))


def reconciliation_complete(api: Api, path: Path, body: dict[str, Any]) -> dict:
    _only(body, set())
    return {"reconciliation": api.book.complete_reconciliation(path["handle"]).serialize()}


def reconciliation_cancel(api: Api, path: Path, body: dict[str, Any]) -> dict:
    _only(body, set())
    return {"reconciliation": api.book.cancel_reconciliation(path["handle"]).serialize()}


def reconciliation_reopen(api: Api, path: Path, body: dict[str, Any]) -> dict:
    _only(body, set())
    return {"reconciliation": api.book.reopen_reconciliation(path["handle"]).serialize()}


def cleared_toggle(api: Api, path: Path, body: dict[str, Any]) -> dict:
    _only(body, set())
    state = api.book.toggle_cleared(ToggleCleared(path["handle"], path["split"]))
    return {"state": state.value}


@dataclass(frozen=True, slots=True)
class _SplitLink:
    transaction: str
    split: str
    role: Literal["expense", "reimbursement"]


@dataclass(frozen=True, slots=True)
class _Dispute:
    on: date
    note: str = ""


@dataclass(frozen=True, slots=True)
class _WriteOff:
    amount: Money
    written_off_on: date
    reason: str = ""


@dataclass(frozen=True, slots=True)
class _Accept:
    chosen: tuple[tuple[str, str, str], ...]


@dataclass(frozen=True, slots=True)
class _Expectation:
    receivable: str
    amount: Money | None = None
    on: date | None = None


def receivables_get(api: Api, _path: Path, query: QueryParams) -> dict:
    as_of = query.iso_date("as_of")
    query.finish()
    return to_data(api.book.receivables_view(as_of))


def receivable_candidates_get(api: Api, _path: Path, query: QueryParams) -> dict:
    receivable = query.text("receivable")
    query.finish()
    costs, credits = api.book.receivable_candidates(receivable)
    return {"costs": to_data(costs), "credits": to_data(credits)}


def receivable_lookup(api: Api, _path: Path, query: QueryParams) -> dict:
    reference = query.text("reference", required=True) or ""
    query.finish()
    return {"receivable": to_data(api.book.find_receivable(reference))}


def receivable_save(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    request = from_data(SaveReceivable, body, "request")
    return {"receivable": to_data(api.book.save_receivable(request))}


def receivable_link(api: Api, path: Path, body: dict[str, Any]) -> dict:
    link = from_data(_SplitLink, body, "request")
    saved = api.book.link_receivable_split(path["handle"], link.transaction, link.split, link.role)
    return {"receivable": to_data(saved)}


def receivable_detach(api: Api, path: Path, body: dict[str, Any]) -> dict:
    _only(body, set())
    saved = api.book.detach_receivable_split(path["handle"], path["transaction"], path["split"])
    return {"receivable": to_data(saved)}


def receivable_dispute(api: Api, path: Path, body: dict[str, Any]) -> dict:
    dispute = from_data(_Dispute, body, "request")
    return {"receivable": to_data(api.book.mark_disputed(path["handle"], dispute.on, dispute.note))}


def receivable_clear_dispute(api: Api, path: Path, body: dict[str, Any]) -> dict:
    _only(body, set())
    return {"receivable": to_data(api.book.clear_dispute(path["handle"]))}


def receivable_write_off(api: Api, path: Path, body: dict[str, Any]) -> dict:
    write_off = from_data(_WriteOff, body, "request")
    request = RecordWriteOff(
        path["handle"], write_off.amount, write_off.written_off_on, write_off.reason
    )
    return {"receivable": to_data(api.book.record_write_off(request))}


def receivable_delete(api: Api, path: Path, body: dict[str, Any]) -> dict:
    _only(body, set())
    return {"deleted": api.book.delete_receivable(path["handle"])}


def reimbursement_proposals_get(api: Api, _path: Path, query: QueryParams) -> dict:
    as_of = query.iso_date("as_of")
    account = query.text("account")
    query.finish()
    return {"proposals": to_data(api.book.reimbursement_proposals(as_of, account))}


def reimbursement_proposals_accept(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    chosen = from_data(_Accept, body, "request").chosen
    return to_data(api.book.accept_reimbursements(chosen))


def reimbursement_override(api: Api, path: Path, body: dict[str, Any]) -> dict:
    change = from_data(_Expectation, body, "request")
    request = SetReimbursementOverride(path["handle"], change.receivable, change.amount, change.on)
    return {"scenario": to_data(api.book.set_reimbursement_override(request))}


@dataclass(frozen=True, slots=True)
class _Suppress:
    source_schedule: str


@dataclass(frozen=True, slots=True)
class _Period:
    period: AssumptionPeriod
    index: int | None = None


def scenarios_get(api: Api, _path: Path, query: QueryParams) -> dict:
    query.finish()
    return {"scenarios": to_data(api.book.scenarios())}


def scenario_lookup(api: Api, _path: Path, query: QueryParams) -> dict:
    reference = query.text("reference", required=True) or ""
    query.finish()
    return {"scenario": to_data(api.book.find_scenario(reference))}


def scenario_save_v1(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    return to_data(api.book.save_scenario(from_data(SaveScenario, body, "request")))


def scenario_assumptions_save(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    request = from_data(SaveScenarioAssumptions, body, "request")
    return to_data(api.book.save_scenario_assumptions(request))


def scenario_duplicate_v1(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    return to_data(api.book.duplicate_scenario(from_data(DuplicateScenario, body, "request")))


def scenario_delete_v1(api: Api, path: Path, body: dict[str, Any]) -> dict:
    _only(body, set())
    return to_data(api.book.delete_scenario(path["handle"]))


def scenario_suppress(api: Api, path: Path, body: dict[str, Any]) -> dict:
    source = from_data(_Suppress, body, "request").source_schedule
    return to_data(
        api.book.suppress_scenario_schedule(SuppressScenarioSchedule(path["handle"], source))
    )


def drawdown_accounts_get(api: Api, _path: Path, query: QueryParams) -> dict:
    query.finish()
    sources, targets = api.book.drawdown_accounts()
    return {"sources": sources, "targets": targets}


def drawdown_save(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    return {"drawdown": to_data(api.book.save_drawdown(from_data(SaveDrawdown, body, "request")))}


def drawdown_remove(api: Api, path: Path, body: dict[str, Any]) -> dict:
    _only(body, set())
    return {"drawdown": to_data(api.book.remove_drawdown(path["handle"], path["drawdown"]))}


def base_assumptions_get(api: Api, _path: Path, query: QueryParams) -> dict:
    query.finish()
    return {"assumptions": to_data(api.book.base_assumptions())}


def base_assumptions_save(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    request = from_data(SaveBaseAssumptions, body, "request")
    return {"assumptions": to_data(api.book.save_base_assumptions(request))}


def assumption_period_save(api: Api, path: Path, body: dict[str, Any]) -> dict:
    change = from_data(_Period, body, "request")
    return to_data(api.book.save_assumption_period(path["handle"], change.period, change.index))


def assumption_period_delete(api: Api, path: Path, body: dict[str, Any]) -> dict:
    _only(body, set())
    try:
        index = int(path["index"])
    except ValueError:
        raise ValueError("index must be a whole number") from None
    return to_data(api.book.delete_assumption_period(path["handle"], index))
