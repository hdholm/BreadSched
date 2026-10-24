"""A book API client over HTTP (#357).

**Experimental.** :class:`HttpBookClient` implements
:class:`~breadsched.api.protocol.BookApi` against a running BreadSched web server's
``/api/v1`` resources, so a caller written for the in-process server works
unchanged against a remote one. Results are decoded back into the services' typed
dataclasses, and an error response is raised as the same
:class:`~breadsched.api.contract.ApiError` the in-process server raises.

Every response names the book's revision; the client remembers the last one it
saw (:attr:`HttpBookClient.seen_revision`). With :attr:`require_revision` set, a
write is refused (``412``, ``book.revision.stale``) if the book has moved on.
:meth:`HttpBookClient.subscribe` follows ``/api/v1/events`` on a background thread.
"""

from __future__ import annotations

import http.client
import json
import socket
import ssl
import threading
from collections.abc import Callable
from datetime import date
from typing import Any
from urllib.parse import quote, urlencode, urlparse

from ..gen.engine.receivables import ReimbursementProposal
from ..gen.engine.reconciliation import ReconciliationSummary
from ..gen.lib import Scenario, ScheduledTransaction, Transaction
from ..gen.lib.receivable import Receivable
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
from ..gen.services.scenarios import SetReimbursementOverride
from .account_codec import save_account_json
from .contract import ApiError, to_json
from .protocol import Change, Listener
from .schedule_codec import fixed_request_json, formula_request_json
from .transaction_codec import save_transaction_json, transaction_input_from_json
from .typed_codec import from_data, to_data
from .views import (
    account_balance_from_json,
    account_record_from_json,
    account_ref_from_json,
    advance_days_from_json,
    applied_categories_from_json,
    balance_summary_from_json,
    categorization_rules_from_json,
    change_from_json,
    chart_from_json,
    due_review_from_json,
    next_occurrence_from_json,
    posted_due_from_json,
    reconciliation_summary_from_json,
    reconciliation_view_from_json,
    register_from_json,
    resolution_from_json,
    rule_from_json,
    saved_account_from_json,
    saved_schedule_from_json,
    saved_transaction_from_json,
    savings_goals_from_json,
    tag_counts_from_json,
)

__all__ = ["HttpBookClient"]


class HttpBookClient:
    """The book API of a BreadSched server at ``base_url``.

    ``http://`` reaches a server on this host. ``https://`` reaches one through a
    TLS reverse proxy: the proxy's certificate is verified against the system's
    certificate authorities, or ``ca_file``, and ``client_cert`` (with
    ``client_key`` when the key is separate) answers a proxy that asks for a
    client certificate.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: float = 30.0,
        *,
        ca_file: str | None = None,
        client_cert: str | None = None,
        client_key: str | None = None,
    ) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("a book server URL is http://host:port or https://host[:port]")
        if parsed.scheme == "http" and (ca_file or client_cert or client_key):
            raise ValueError("certificates are only used with an https:// server")
        self.host = parsed.hostname
        self.port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self.token = token
        self.timeout = timeout
        self._tls: ssl.SSLContext | None = None
        if parsed.scheme == "https":
            self._tls = ssl.create_default_context(cafile=ca_file)
            self._tls.minimum_version = ssl.TLSVersion.TLSv1_2
            if client_cert:
                self._tls.load_cert_chain(client_cert, client_key)
        #: The revision named by the last response.
        self.seen_revision: int | None = None
        #: When set, writes send ``If-Match`` with this revision.
        self.require_revision: int | None = None

    # -------------------------------------------------------------- transport

    def _connection(self, timeout: float | None) -> http.client.HTTPConnection:
        if self._tls is not None:
            return http.client.HTTPSConnection(
                self.host, self.port, timeout=timeout, context=self._tls
            )
        return http.client.HTTPConnection(self.host, self.port, timeout=timeout)

    def _call(
        self,
        method: str,
        path: str,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        target = "/api/v1" + path
        present = {key: to_json(value) for key, value in (query or {}).items() if value is not None}
        if present:
            target += "?" + urlencode(present)
        headers = {"X-BreadSched-Token": self.token, "Accept": "application/json"}
        payload = None
        if method != "GET":
            payload = json.dumps(to_json(body or {})).encode("utf-8")
            headers["Content-Type"] = "application/json"
            if self.require_revision is not None:
                headers["If-Match"] = f'"{self.require_revision}"'
        connection = self._connection(self.timeout)
        try:
            connection.request(method, target, body=payload, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            etag = response.getheader("ETag")
            status = response.status
        except OSError as exc:
            # Refused, unreachable, timed out, or a certificate that does not verify.
            raise ApiError(
                503, "server.unreachable", (), f"could not reach {self.host}:{self.port}: {exc}"
            ) from exc
        finally:
            connection.close()
        if etag:
            self.seen_revision = int(etag.strip('"'))
        data = json.loads(raw or b"null")
        if status != 200:
            data = data if isinstance(data, dict) else {}
            raise ApiError(
                status,
                str(data.get("code", "request.failed")),
                tuple(data.get("fields", ())),
                data.get("error"),
            )
        return data

    # ---------------------------------------------------------------- changes

    @property
    def revision(self) -> int:
        return int(self._call("GET", "/book")["revision"])

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        """Follow the server's change stream until the returned function is called.

        The listener runs on the stream's thread. If the stream reports changes
        missed since :attr:`seen_revision`, the listener first receives a change
        with no kinds, meaning "reload".
        """
        since = self.seen_revision if self.seen_revision is not None else self.revision
        connection = self._connection(None)
        connection.request(
            "GET",
            "/api/v1/events?" + urlencode({"since": since}),
            headers={"X-BreadSched-Token": self.token, "Accept": "text/event-stream"},
        )
        response = connection.getresponse()
        if response.status != 200:
            connection.close()
            raise ApiError(response.status, "events.unavailable")
        stopped = threading.Event()

        def follow() -> None:
            name = data = ""
            try:
                while not stopped.is_set():
                    line = response.readline()
                    if not line:
                        return
                    text = line.decode("utf-8").rstrip("\r\n")
                    if text.startswith("event:"):
                        name = text[6:].strip()
                    elif text.startswith("data:"):
                        data = text[5:].strip()
                    elif not text and data:
                        payload = json.loads(data)
                        if name == "hello" and payload.get("missed"):
                            listener(Change(int(payload["revision"]), frozenset(), None))
                        elif name == "change":
                            change = change_from_json(payload)
                            self.seen_revision = change.revision
                            listener(change)
                        name = data = ""
            except (OSError, ValueError):
                return

        thread = threading.Thread(target=follow, name="breadsched-events", daemon=True)
        thread.start()

        def stop() -> None:
            stopped.set()
            sock = connection.sock
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            connection.close()
            thread.join(timeout=5)

        return stop

    def undo(self) -> bool:
        return bool(self._call("POST", "/undo")["done"])

    def redo(self) -> bool:
        return bool(self._call("POST", "/redo")["done"])

    # ------------------------------------------------- scheduled transactions

    def find_schedule(self, reference: str) -> ScheduledTransaction:
        found = self._call("GET", "/schedules/lookup", {"reference": reference})
        return ScheduledTransaction.from_dict(found["schedule"])

    def due_review(self, as_of: date | None = None) -> list[DueScheduleReview]:
        return due_review_from_json(self._call("GET", "/due-review", {"as_of": as_of}))

    def resolve_due(self, request: ResolveDue) -> DueResolution:
        body = {
            "decisions": [
                {"schedule": handle, "date": when, "decision": decision}
                for handle, when, decision in request.decisions
            ],
            "as_of": request.as_of,
        }
        return resolution_from_json(self._call("POST", "/due-review", body=body))

    def next_occurrence(self, schedule: str, as_of: date | None = None) -> NextOccurrence | None:
        found = self._call("GET", f"/schedules/{quote(schedule, safe='')}/next", {"as_of": as_of})
        return next_occurrence_from_json(found["next"])

    def resolve_next(self, request: ResolveNext) -> DueResolution:
        body = {"decision": request.decision, "date": request.when, "as_of": request.as_of}
        path = f"/schedules/{quote(request.schedule, safe='')}/next"
        return resolution_from_json(self._call("POST", path, body=body))

    def advance_days(self, schedule: str) -> AdvanceDays:
        path = f"/schedules/{quote(schedule, safe='')}/advance-days"
        return advance_days_from_json(self._call("GET", path))

    def set_advance_days(self, request: SetAdvanceDays) -> AdvanceDays:
        body = {"advance_days": request.advance_days, "remind_days": request.remind_days}
        path = f"/schedules/{quote(request.schedule, safe='')}/advance-days"
        return advance_days_from_json(self._call("PATCH", path, body=body))

    def delete_schedule(self, schedule: str) -> SavedSchedule:
        path = f"/schedules/{quote(schedule, safe='')}"
        return saved_schedule_from_json(self._call("DELETE", path))

    def duplicate_schedule(self, request: DuplicateSchedule) -> SavedSchedule:
        path = f"/schedules/{quote(request.handle, safe='')}/duplicate"
        return saved_schedule_from_json(self._call("POST", path, body={"name": request.name}))

    def post_due(self, request: PostDue) -> PostedDue:
        body = {"as_of": request.as_of, "all": not request.only_automatic}
        return posted_due_from_json(self._call("POST", "/post-due", body=body))

    def save_fixed_schedule(self, request: SaveFixedSchedule) -> SavedSchedule:
        found = self._call("POST", "/schedules/fixed", body=fixed_request_json(request))
        return saved_schedule_from_json(found)

    def save_formula_schedule(self, request: FormulaScheduleInput) -> SavedSchedule:
        found = self._call("POST", "/schedules/formula", body=formula_request_json(request))
        return saved_schedule_from_json(found)

    # ------------------------------------------------------------ transactions

    def save_transaction(self, request: SaveTransaction) -> SavedTransaction:
        found = self._call("POST", "/transactions", body=save_transaction_json(request))
        return saved_transaction_from_json(found)

    def delete_transaction(self, transaction: str) -> SavedTransaction:
        path = f"/transactions/{quote(transaction, safe='')}"
        return saved_transaction_from_json(self._call("DELETE", path))

    # ------------------------------------------------------------ ledger reads

    def transaction_currency(self) -> str:
        return str(self._call("GET", "/currency")["currency"])

    def find_account(self, reference: str) -> AccountRef:
        found = self._call("GET", "/accounts/lookup", {"reference": reference})
        return account_ref_from_json(found["account"])

    def find_transaction(self, reference: str) -> Transaction:
        found = self._call("GET", "/transactions/lookup", {"reference": reference})
        return Transaction.from_dict(found["transaction"])

    def editable_transaction(self, transaction: str) -> TransactionInput:
        path = f"/transactions/{quote(transaction, safe='')}/editable"
        return transaction_input_from_json(self._call("GET", path)["definition"])

    def register(self, query: RegisterQuery) -> RegisterPage:
        path = f"/accounts/{quote(query.account, safe='')}/register"
        params = {"start": query.start, "end": query.end, "tag": query.tag, "limit": query.limit}
        return register_from_json(self._call("GET", path, params))

    def account_balance(self, query: BalanceQuery) -> AccountBalance:
        path = f"/accounts/{quote(query.account, safe='')}/balance"
        params = {"as_of": query.as_of, "recursive": "true" if query.recursive else None}
        return account_balance_from_json(self._call("GET", path, params))

    def balance_summary(self, as_of: date | None = None) -> BalanceSummary:
        return balance_summary_from_json(self._call("GET", "/balances", {"as_of": as_of}))

    # ---------------------------------------------------------------- accounts

    def accounts(self) -> list[AccountRecord]:
        found = self._call("GET", "/accounts")
        return [account_record_from_json(row) for row in found["accounts"]]

    def account(self, account: str) -> AccountRecord:
        return account_record_from_json(self._call("GET", f"/accounts/{quote(account, safe='')}"))

    def chart(self, as_of: date | None = None, include_hidden: bool = False) -> list[ChartLine]:
        params = {"as_of": as_of, "hidden": "true" if include_hidden else None}
        return chart_from_json(self._call("GET", "/chart", params))

    def save_account(self, request: SaveAccount) -> SavedAccount:
        found = self._call("POST", "/accounts", body=save_account_json(request))
        return saved_account_from_json(found)

    def delete_account(self, account: str) -> SavedAccount:
        path = f"/accounts/{quote(account, safe='')}"
        return saved_account_from_json(self._call("DELETE", path))

    # ------------------------------------------------- categorization rules

    def categorization_rules(self) -> CategorizationRules:
        return categorization_rules_from_json(self._call("GET", "/rules"))

    def add_rule(self, request: AddRule) -> CategoryRule:
        body = {
            "category": request.category,
            "description": request.description,
            "position": request.position,
        }
        return rule_from_json(self._call("POST", "/rules", body=body))

    def move_rule(self, rule: str, position: int) -> CategoryRule:
        path = f"/rules/{quote(rule, safe='')}"
        return rule_from_json(self._call("PATCH", path, body={"position": position}))

    def delete_rule(self, rule: str) -> CategoryRule:
        return rule_from_json(self._call("DELETE", f"/rules/{quote(rule, safe='')}"))

    def apply_category_proposals(
        self, transactions: tuple[str, ...] | None = None
    ) -> AppliedCategories:
        body = {"transactions": None if transactions is None else list(transactions)}
        return applied_categories_from_json(self._call("POST", "/rules/accept", body=body))

    # -------------------------------------------------------------------- tags

    def tag_counts(self) -> list[TagCount]:
        return tag_counts_from_json(self._call("GET", "/tags"))

    def transactions_with_tag(self, tag: str) -> list[Transaction]:
        found = self._call("GET", "/tags/transactions", {"tag": tag})
        return [Transaction.from_dict(item) for item in found["transactions"]]

    def set_tags(self, transaction: str, tags: list[str]) -> Transaction:
        path = f"/transactions/{quote(transaction, safe='')}/tags"
        return Transaction.from_dict(
            self._call("PATCH", path, body={"tags": list(tags)})["transaction"]
        )

    # ------------------------------------------------------------ savings goals

    def _goal(self, method: str, path: str, body: dict[str, Any] | None = None) -> SavingsGoal:
        return SavingsGoal.from_dict(self._call(method, path, body=body)["goal"])

    def savings_goals(self, as_of: date, include_closed: bool = False) -> SavingsGoalsView:
        params = {"as_of": as_of, "closed": "true" if include_closed else None}
        return savings_goals_from_json(self._call("GET", "/goals", params))

    def find_savings_goal(self, reference: str) -> SavingsGoal:
        found = self._call("GET", "/goals/lookup", {"reference": reference})
        return SavingsGoal.from_dict(found["goal"])

    def save_savings_goal(self, request: SaveSavingsGoal) -> SavingsGoal:
        body = {
            "handle": request.handle,
            "name": request.name,
            "account": request.account,
            "target_amount": request.target_amount,
            "target_date": request.target_date,
            "start_date": request.start_date,
            "description": request.description,
        }
        return self._goal("POST", "/goals", body)

    def allocate_to_goal(self, request: AllocateToGoal) -> SavingsGoal:
        body = {"amount": request.amount, "date": request.allocated_on, "memo": request.memo}
        return self._goal("POST", f"/goals/{quote(request.goal, safe='')}/allocations", body)

    def close_savings_goal(self, goal: str, closed_on: date) -> SavingsGoal:
        return self._goal("POST", f"/goals/{quote(goal, safe='')}/close", {"date": closed_on})

    def reopen_savings_goal(self, goal: str) -> SavingsGoal:
        return self._goal("POST", f"/goals/{quote(goal, safe='')}/reopen")

    def delete_savings_goal(self, goal: str) -> str:
        return str(self._call("DELETE", f"/goals/{quote(goal, safe='')}")["deleted"])

    def set_goal_override(self, request: SetGoalOverride) -> Scenario:
        body = {
            "scenario": request.scenario,
            "target_amount": request.target_amount,
            "target_date": request.target_date,
            "excluded": request.excluded,
            "purchase_on": request.purchase_on,
            "purchase_account": request.purchase_account,
        }
        path = f"/goals/{quote(request.goal, safe='')}/override"
        return Scenario.from_dict(self._call("POST", path, body=body)["scenario"])

    # ---------------------------------------------------------- reconciliation

    def reconciliation(self, account: str) -> ReconciliationView:
        path = f"/accounts/{quote(account, safe='')}/reconciliation"
        return reconciliation_view_from_json(self._call("GET", path))

    def start_reconciliation(self, request: StartReconciliation) -> ReconciliationSummary:
        body = {
            "account": request.account,
            "statement_date": request.statement_date,
            "ending_balance": request.ending_balance,
        }
        return reconciliation_summary_from_json(self._call("POST", "/reconciliations", body=body))

    def update_reconciliation(self, request: UpdateReconciliation) -> ReconciliationSummary:
        body = {
            "selected_splits": (
                None if request.selected_splits is None else list(request.selected_splits)
            ),
            "ending_balance": request.ending_balance,
        }
        path = f"/reconciliations/{quote(request.handle, safe='')}"
        return reconciliation_summary_from_json(self._call("PATCH", path, body=body))

    def _session(self, reconciliation: str, action: str) -> Reconciliation:
        path = f"/reconciliations/{quote(reconciliation, safe='')}/{action}"
        return Reconciliation.from_dict(self._call("POST", path)["reconciliation"])

    def complete_reconciliation(self, reconciliation: str) -> Reconciliation:
        return self._session(reconciliation, "complete")

    def cancel_reconciliation(self, reconciliation: str) -> Reconciliation:
        return self._session(reconciliation, "cancel")

    def reopen_reconciliation(self, reconciliation: str) -> Reconciliation:
        return self._session(reconciliation, "reopen")

    def toggle_cleared(self, request: ToggleCleared) -> ReconcileState:
        path = (
            f"/transactions/{quote(request.transaction, safe='')}"
            f"/splits/{quote(request.split, safe='')}/cleared"
        )
        return ReconcileState(self._call("POST", path)["state"])

    # -------------------------------------------------------------- receivables

    def _receivable(self, method: str, path: str, body: Any = None) -> Receivable:
        found = self._call(method, path, body=body)
        return from_data(Receivable, found["receivable"], "receivable")

    def receivables_view(self, as_of: date | None = None) -> ReceivablesView:
        return from_data(ReceivablesView, self._call("GET", "/receivables", {"as_of": as_of}))

    def receivable_candidates(
        self, receivable: str | None = None
    ) -> tuple[tuple[CandidateLine, ...], tuple[CandidateLine, ...]]:
        found = self._call("GET", "/receivables/candidates", {"receivable": receivable})
        kind = tuple[tuple[CandidateLine, ...], tuple[CandidateLine, ...]]
        return from_data(kind, [found["costs"], found["credits"]], "candidates")

    def find_receivable(self, reference: str) -> Receivable:
        found = self._call("GET", "/receivables/lookup", {"reference": reference})
        return from_data(Receivable, found["receivable"], "receivable")

    def save_receivable(self, request: SaveReceivable) -> Receivable:
        return self._receivable("POST", "/receivables", to_data(request))

    def link_receivable_split(
        self, receivable: str, transaction: str, split: str, role: str
    ) -> Receivable:
        body = {"transaction": transaction, "split": split, "role": role}
        return self._receivable("POST", f"/receivables/{quote(receivable, safe='')}/splits", body)

    def detach_receivable_split(self, receivable: str, transaction: str, split: str) -> Receivable:
        path = "/".join(
            ("/receivables", quote(receivable, safe=""), "splits", quote(transaction, safe=""))
        )
        return self._receivable("DELETE", f"{path}/{quote(split, safe='')}")

    def mark_disputed(self, receivable: str, disputed_on: date, note: str = "") -> Receivable:
        path = f"/receivables/{quote(receivable, safe='')}/dispute"
        return self._receivable("POST", path, {"on": disputed_on, "note": note})

    def clear_dispute(self, receivable: str) -> Receivable:
        return self._receivable("DELETE", f"/receivables/{quote(receivable, safe='')}/dispute")

    def record_write_off(self, request: RecordWriteOff) -> Receivable:
        body = {
            "amount": request.amount,
            "written_off_on": request.written_off_on,
            "reason": request.reason,
        }
        path = f"/receivables/{quote(request.receivable, safe='')}/write-offs"
        return self._receivable("POST", path, body)

    def delete_receivable(self, receivable: str) -> str:
        return str(self._call("DELETE", f"/receivables/{quote(receivable, safe='')}")["deleted"])

    def reimbursement_proposals(
        self, as_of: date | None = None, account: str | None = None
    ) -> tuple[ReimbursementProposal, ...]:
        found = self._call("GET", "/reimbursement-proposals", {"as_of": as_of, "account": account})
        return from_data(tuple[ReimbursementProposal, ...], found["proposals"], "proposals")

    def accept_reimbursements(
        self, chosen: tuple[tuple[str, str, str], ...]
    ) -> AcceptedReimbursements:
        found = self._call("POST", "/reimbursement-proposals/accept", body={"chosen": chosen})
        return from_data(AcceptedReimbursements, found)

    def set_reimbursement_override(self, request: SetReimbursementOverride) -> Scenario:
        body = {"receivable": request.receivable, "amount": request.amount, "on": request.on}
        path = f"/scenarios/{quote(request.scenario, safe='')}/reimbursements"
        found = self._call("POST", path, body=body)
        return from_data(Scenario, found["scenario"], "scenario")
