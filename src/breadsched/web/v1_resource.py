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
* ``POST /undo`` and ``POST /redo``.
* ``GET /events`` (``since``) – server-sent change events, served by the transport.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any

from ..api.account_codec import save_account_from_json
from ..api.schedule_codec import fixed_request_from_json, formula_request_from_json
from ..api.transaction_codec import save_transaction_from_json, transaction_input_json
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
    register_json,
    resolution_json,
    rule_json,
    saved_account_json,
    saved_schedule_json,
    saved_transaction_json,
    tag_counts_json,
)
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
from ..gen.services.categorization import AddRule
from .context import Api

if TYPE_CHECKING:
    from .resources import QueryParams

__all__ = [
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
