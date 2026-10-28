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
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import date
from typing import Any
from urllib.parse import quote, urlencode, urlparse

from ..gen.db.verification import BookVerification
from ..gen.engine.activity import PlanSettings, ReportingPeriod
from ..gen.engine.category_report import CategoryActivity
from ..gen.engine.dashboard import Dashboard
from ..gen.engine.entry_input import TradeFill
from ..gen.engine.fsa_claim_proposals import FsaClaimProposal
from ..gen.engine.fsa_claim_report import ClaimReport
from ..gen.engine.loans import LoanTerms
from ..gen.engine.payroll import PayChangePlan, PaycheckBreakdown, PayrollTemplate
from ..gen.engine.planning import MatchCandidate, PlannedEvent
from ..gen.engine.projection import Projection, ProjectionMonthDetail, ProjectionProgress
from ..gen.engine.receivables import ReimbursementProposal
from ..gen.engine.reconciliation import ReconciliationSummary
from ..gen.engine.schedule import Occurrence
from ..gen.engine.transaction_query import TransactionQuery
from ..gen.lib import Scenario, ScheduledTransaction, Transaction
from ..gen.lib.commodity import Commodity, CommodityPrice
from ..gen.lib.fsa_claim import FsaClaim
from ..gen.lib.money import Money
from ..gen.lib.receivable import Receivable
from ..gen.lib.reconciliation import Reconciliation
from ..gen.lib.savings_goal import SavingsGoal
from ..gen.lib.scenario import AssumptionPeriod, Assumptions, Drawdown, ScenarioSchedule
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
from ..gen.services.account_inference import (
    AccountSuggestion,
    AppliedSuggestions,
    ApplyAccountSuggestions,
)
from ..gen.services.activity import ActivityView
from ..gen.services.assumptions import SaveBaseAssumptions, SaveScenarioAssumptions
from ..gen.services.attachments import AttachmentReport
from ..gen.services.autocomplete import EntryProposal, SuggestEntry
from ..gen.services.book_info import BookCounts, BookSummary
from ..gen.services.budget_jars import JarsView
from ..gen.services.categorization import (
    AddRule,
    AppliedCategories,
    CategorizationRules,
    CategoryRule,
)
from ..gen.services.claims import (
    AcceptedClaimLinks,
    ClaimReportQuery,
    ClaimStanding,
    CloseClaim,
    FsaClaimsView,
    FsaYearsView,
    ReopenClaim,
    SaveClaim,
    SavedClaim,
)
from ..gen.services.csv_import import (
    CsvImported,
    CsvImportRequest,
    CsvInspection,
    CsvPreview,
    InspectCsv,
)
from ..gen.services.dashboard_config import DashboardSettings, SaveDashboardConfig
from ..gen.services.entry_input import EntryAccount, EntryAmount, ReadEntryTrade
from ..gen.services.exchange_rates import CurrencyChoice, SaveExchangeRate
from ..gen.services.expense_explorer import ExpenseExplorer
from ..gen.services.gnucash_writeback import ApplyWriteback, WritebackApplied, WritebackPlan
from ..gen.services.historical_estimates import (
    AcceptedEstimate,
    AcceptHistoricalEstimate,
    HistoricalEstimatesView,
)
from ..gen.services.import_review import (
    HeldImportChange,
    HeldImportResolution,
    ResolveHeldImports,
)
from ..gen.services.imports import ImportBook, ImportedBook
from ..gen.services.investment_reports import HoldingsView, RealizedGainsView, TaxYearView
from ..gen.services.loans import LoanChoices, LoanPreview, SavedLoan, SaveLoan
from ..gen.services.lots import SaleLots, SetSaleLots
from ..gen.services.net_worth import NetWorthChange, NetWorthHistory
from ..gen.services.payroll import CreatePaycheck, PayChange, PayrollView, SavePayrollTemplate
from ..gen.services.plan import PlanBounds, PlanQuery, PlanQueryResult
from ..gen.services.plan_cells import PlanCellDetail, PlanCellQuery
from ..gen.services.quotes import QuotePlan, QuoteUpdate, SetQuoteSource, StoreQuotes
from ..gen.services.receivables import (
    AcceptedReimbursements,
    CandidateLine,
    ReceivablesView,
    RecordWriteOff,
    SaveReceivable,
)
from ..gen.services.reconciliations import (
    AddStatementEntry,
    ReconciliationView,
    StartReconciliation,
    ToggleCleared,
    UpdateReconciliation,
)
from ..gen.services.register_view import RegisterChanges, RegisterView
from ..gen.services.review import (
    PendingActual,
    ReviewClaimAttachment,
    ReviewDetail,
    ReviewMutation,
    ReviewOccurrence,
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
from ..gen.services.schedule_editing import (
    ScenarioEventsView,
    ScheduledPage,
    ScheduleEditorView,
    SchedulesView,
)
from ..gen.services.schedules import (
    FormulaScenarioScheduleInput,
    SavedScenarioSchedule,
    SaveFixedScenarioSchedule,
)
from ..gen.services.security_prices import SavedSecurityPrice, SaveSecurityPrice, SecurityQuote
from ..gen.services.tax import SetTaxMarks, TaxMarks
from ..gen.services.transaction_editing import TransactionEditorView
from ..gen.services.transaction_search import FindResult
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

    def due_occurrences(
        self, as_of: date | None = None, days: int | None = None
    ) -> tuple[Occurrence, ...]:
        found = self._call("GET", "/schedules/due", {"as_of": as_of, "days": days})
        return from_data(tuple[Occurrence, ...], found["occurrences"], "occurrences")

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

    def scenario_events(self, scenario: str) -> ScenarioEventsView:
        path = f"/scenarios/{quote(scenario, safe='')}/events"
        return from_data(ScenarioEventsView, self._call("GET", path))

    def save_fixed_scenario_schedule(
        self, request: SaveFixedScenarioSchedule
    ) -> SavedScenarioSchedule:
        found = self._call("POST", "/scenario-schedules/fixed", body=to_data(request))
        return from_data(SavedScenarioSchedule, found)

    def save_formula_scenario_schedule(
        self, request: FormulaScenarioScheduleInput
    ) -> SavedScenarioSchedule:
        found = self._call("POST", "/scenario-schedules/formula", body=to_data(request))
        return from_data(SavedScenarioSchedule, found)

    def draft_fixed_scenario_schedule(self, request: SaveFixedScenarioSchedule) -> ScenarioSchedule:
        found = self._call("POST", "/scenario-schedules/fixed/draft", body=to_data(request))
        return ScenarioSchedule.from_dict(found["schedule"])

    def draft_formula_scenario_schedule(
        self, request: FormulaScenarioScheduleInput
    ) -> ScenarioSchedule:
        found = self._call("POST", "/scenario-schedules/formula/draft", body=to_data(request))
        return ScenarioSchedule.from_dict(found["schedule"])

    # ------------------------------------------------------------ transactions

    def save_transaction(self, request: SaveTransaction) -> SavedTransaction:
        found = self._call("POST", "/transactions", body=save_transaction_json(request))
        return saved_transaction_from_json(found)

    def delete_transaction(self, transaction: str) -> SavedTransaction:
        path = f"/transactions/{quote(transaction, safe='')}"
        return saved_transaction_from_json(self._call("DELETE", path))

    # ------------------------------------------------------------ entry input

    def suggest_entry(self, request: SuggestEntry) -> EntryProposal:
        params = {
            "description": request.description,
            "account": request.account,
            "currency": request.currency,
            "exclude": request.exclude,
        }
        return from_data(EntryProposal, self._call("GET", "/entry/suggestion", params))

    def entry_amount(
        self, text: str, currency: str | None = None, number_format: str = "auto"
    ) -> EntryAmount:
        params = {"text": text, "currency": currency, "number_format": number_format}
        return from_data(EntryAmount, self._call("GET", "/entry/amount", params))

    def entry_trade(self, request: ReadEntryTrade) -> TradeFill:
        params = {
            "account": request.account,
            "shares": request.shares,
            "price": request.price,
            "amount": request.amount,
            "changed": request.changed,
            "currency": request.currency,
            "number_format": request.number_format,
        }
        return from_data(TradeFill, self._call("GET", "/entry/trade", params))

    def entry_accounts(
        self, text: str, exclude: str | None = None, limit: int = 20
    ) -> tuple[EntryAccount, ...]:
        found = self._call(
            "GET", "/entry/accounts", {"text": text, "exclude": exclude, "limit": limit}
        )
        return from_data(tuple[EntryAccount, ...], found["accounts"], "accounts")

    def next_num(self, account: str, text: str = "", step: int = 1) -> str:
        found = self._call("GET", "/entry/num", {"account": account, "text": text, "step": step})
        return str(found["num"])

    # -------------------------------------------------------------------- book

    def book_counts(self) -> BookCounts:
        return from_data(BookCounts, self._call("GET", "/book/counts"))

    def book_summary(self) -> BookSummary:
        return from_data(BookSummary, self._call("GET", "/book/summary"))

    def verify_book(self) -> BookVerification:
        return from_data(BookVerification, self._call("GET", "/book/verification"))

    # ------------------------------------------------------------ ledger reads

    def transaction_currency(self) -> str:
        return str(self._call("GET", "/currency")["currency"])

    def find_account(self, reference: str) -> AccountRef:
        found = self._call("GET", "/accounts/lookup", {"reference": reference})
        return account_ref_from_json(found["account"])

    def find_transaction(self, reference: str) -> Transaction:
        found = self._call("GET", "/transactions/lookup", {"reference": reference})
        return Transaction.from_dict(found["transaction"])

    def transaction_editor(self, transaction: str | None = None) -> TransactionEditorView:
        found = self._call("GET", "/transaction-editor", {"transaction": transaction})
        return from_data(TransactionEditorView, found)

    def editable_transaction(self, transaction: str) -> TransactionInput:
        path = f"/transactions/{quote(transaction, safe='')}/editable"
        return transaction_input_from_json(self._call("GET", path)["definition"])

    def register(self, query: RegisterQuery) -> RegisterPage:
        path = f"/accounts/{quote(query.account, safe='')}/register"
        params = {"start": query.start, "end": query.end, "tag": query.tag, "limit": query.limit}
        return register_from_json(self._call("GET", path, params))

    def register_view(
        self, account: str, limit: int | None = 250, filter_text: str = ""
    ) -> RegisterView:
        path = f"/accounts/{quote(account, safe='')}/register-view"
        params = {"limit": "all" if limit is None else limit, "filter": filter_text or None}
        return from_data(RegisterView, self._call("GET", path, params))

    def register_changes(self, account: str, changed: Sequence[str]) -> RegisterChanges:
        path = f"/accounts/{quote(account, safe='')}/register-changes"
        found = self._call("POST", path, body={"changed": list(changed)})
        return from_data(RegisterChanges, found)

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

    def commodities(self) -> tuple[Commodity, ...]:
        found = self._call("GET", "/commodities")["commodities"]
        return tuple(Commodity.from_dict(item) for item in found)

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

    def reconciled_balance(self, account: str, through: date) -> Money:
        path = f"/accounts/{quote(account, safe='')}/reconciled-balance"
        return Money(self._call("GET", path, {"through": through})["balance"])

    def add_statement_entry(self, request: AddStatementEntry) -> SavedTransaction:
        path = f"/accounts/{quote(request.account, safe='')}/statement-entries"
        body = {
            "other_account": request.other_account,
            "post_date": request.post_date,
            "description": request.description,
            "amount": request.amount,
        }
        return saved_transaction_from_json(self._call("POST", path, body=body))

    # ----------------------------------------------------- historical estimates

    def schedules_view(self, days: int = 60) -> SchedulesView:
        return from_data(SchedulesView, self._call("GET", "/schedules/view", {"days": days}))

    def schedule_editor(self, schedule: str) -> ScheduleEditorView:
        path = f"/schedules/{quote(schedule, safe='')}/editor"
        return from_data(ScheduleEditorView, self._call("GET", path))

    def scheduled_page(self, days: int | None = None, as_of: date | None = None) -> ScheduledPage:
        found = self._call("GET", "/schedules/page", {"days": days, "as_of": as_of})
        return from_data(ScheduledPage, found)

    def schedule_layout(self, definition: ScheduledTransaction) -> ScheduleEditorView:
        body = {"definition": definition.serialize()}
        return from_data(ScheduleEditorView, self._call("POST", "/schedules/layout", body=body))

    def schedule_draft(self, transaction: str) -> ScheduleEditorView:
        path = f"/transactions/{quote(transaction, safe='')}/schedule-draft"
        return from_data(ScheduleEditorView, self._call("GET", path))

    def historical_estimates(
        self,
        months: int = 12,
        min_active_months: int = 3,
        scenario: str | None = None,
        as_of: date | None = None,
    ) -> HistoricalEstimatesView:
        params = {
            "months": months,
            "min_active_months": min_active_months,
            "scenario": scenario,
            "as_of": as_of,
        }
        return from_data(
            HistoricalEstimatesView, self._call("GET", "/historical-estimates", params)
        )

    def accept_historical_estimate(self, request: AcceptHistoricalEstimate) -> AcceptedEstimate:
        found = self._call("POST", "/historical-estimates", body=to_data(request))
        return from_data(AcceptedEstimate, found)

    # ---------------------------------------------------------------- activity

    def activity(
        self,
        start: date,
        end: date,
        period: ReportingPeriod = ReportingPeriod.MONTH,
        as_of: date | None = None,
    ) -> ActivityView:
        params = {"start": start, "end": end, "period": ReportingPeriod(period).value}
        return from_data(ActivityView, self._call("GET", "/activity", {**params, "as_of": as_of}))

    # ------------------------------------------------------------- budget jars

    def budget_jars(
        self,
        start: date,
        end: date,
        period: ReportingPeriod = ReportingPeriod.MONTH,
        today: date | None = None,
    ) -> JarsView:
        params = {"start": start, "end": end, "period": ReportingPeriod(period).value}
        return from_data(JarsView, self._call("GET", "/budget-jars", {**params, "today": today}))

    # ------------------------------------------------------- investment reports

    def holdings(self, as_of: date | None = None) -> HoldingsView:
        return from_data(HoldingsView, self._call("GET", "/holdings", {"as_of": as_of}))

    def realized_gains(
        self, year: int | None = None, account: str | None = None
    ) -> RealizedGainsView:
        found = self._call("GET", "/realized-gains", {"year": year, "account": account})
        return from_data(RealizedGainsView, found)

    def tax_year(self, year: int | None = None) -> TaxYearView:
        return from_data(TaxYearView, self._call("GET", "/tax-year", {"year": year}))

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

    def reimbursable_costs(self, start: date, end: date) -> tuple[CategoryActivity, ...]:
        found = self._call("GET", "/receivables/costs", {"start": start, "end": end})
        return from_data(tuple[CategoryActivity, ...], found["categories"], "categories")

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

    # ---------------------------------------------------------------- scenarios

    def scenarios(self) -> list[Scenario]:
        return from_data(list[Scenario], self._call("GET", "/scenarios")["scenarios"])

    def find_scenario(self, reference: str) -> Scenario:
        found = self._call("GET", "/scenarios/lookup", {"reference": reference})
        return from_data(Scenario, found["scenario"], "scenario")

    def save_scenario(self, request: SaveScenario) -> SavedScenario:
        return from_data(SavedScenario, self._call("POST", "/scenarios", body=to_data(request)))

    def save_scenario_assumptions(self, request: SaveScenarioAssumptions) -> SavedScenario:
        found = self._call("POST", "/scenario-assumptions", body=to_data(request))
        return from_data(SavedScenario, found)

    def duplicate_scenario(self, request: DuplicateScenario) -> SavedScenario:
        found = self._call("POST", "/scenarios/duplicate", body=to_data(request))
        return from_data(SavedScenario, found)

    def delete_scenario(self, scenario: str) -> SavedScenario:
        found = self._call("DELETE", f"/scenarios/{quote(scenario, safe='')}")
        return from_data(SavedScenario, found)

    def suppress_scenario_schedule(self, request: SuppressScenarioSchedule) -> SavedScenario:
        path = f"/scenarios/{quote(request.scenario, safe='')}/suppressed-schedules"
        found = self._call("POST", path, body={"source_schedule": request.source_schedule})
        return from_data(SavedScenario, found)

    def drawdown_accounts(self) -> tuple[list[str], list[str]]:
        found = self._call("GET", "/drawdown-accounts")
        return list(found["sources"]), list(found["targets"])

    def save_drawdown(self, request: SaveDrawdown) -> Drawdown:
        found = self._call("POST", "/drawdowns", body=to_data(request))
        return from_data(Drawdown, found["drawdown"], "drawdown")

    def remove_drawdown(self, scenario: str, drawdown: str) -> Drawdown:
        path = f"/scenarios/{quote(scenario, safe='')}/drawdowns/{quote(drawdown, safe='')}"
        return from_data(Drawdown, self._call("DELETE", path)["drawdown"], "drawdown")

    def base_assumptions(self) -> Assumptions:
        found = self._call("GET", "/assumptions")
        return from_data(Assumptions, found["assumptions"], "assumptions")

    def save_base_assumptions(self, request: SaveBaseAssumptions) -> Assumptions:
        found = self._call("POST", "/assumptions", body=to_data(request))
        return from_data(Assumptions, found["assumptions"], "assumptions")

    def save_assumption_period(
        self, scenario: str, period: AssumptionPeriod, index: int | None = None
    ) -> SavedScenario:
        path = f"/scenarios/{quote(scenario, safe='')}/periods"
        found = self._call("POST", path, body={"period": to_data(period), "index": index})
        return from_data(SavedScenario, found)

    def delete_assumption_period(self, scenario: str, index: int) -> SavedScenario:
        path = f"/scenarios/{quote(scenario, safe='')}/periods/{index}"
        return from_data(SavedScenario, self._call("DELETE", path))

    # ------------------------------------------------------------------ payroll

    def payroll(self, as_of: date | None = None) -> PayrollView:
        return from_data(PayrollView, self._call("GET", "/payroll", {"as_of": as_of}))

    def paycheck(self, schedule: str, as_of: date | None = None) -> PaycheckBreakdown:
        path = f"/schedules/{quote(schedule, safe='')}/paycheck"
        found = self._call("GET", path, {"as_of": as_of})
        return from_data(PaycheckBreakdown, found["paycheck"], "paycheck")

    def _template(self, method: str, path: str, body: dict[str, Any]) -> PayrollTemplate:
        found = self._call(method, path, body=body)
        return from_data(PayrollTemplate, found["template"], "template")

    def template_from_schedule(
        self, schedule: str, name: str, as_of: date | None = None
    ) -> PayrollTemplate:
        body = {"schedule": schedule, "name": name, "as_of": as_of}
        return self._template("POST", "/payroll/templates/draft", body)

    def save_payroll_template(self, request: SavePayrollTemplate) -> PayrollTemplate:
        return self._template("POST", "/payroll/templates", to_data(request))

    def delete_payroll_template(self, name: str) -> PayrollTemplate:
        return self._template("DELETE", f"/payroll/templates/{quote(name, safe='')}", {})

    def create_paycheck(self, request: CreatePaycheck) -> SavedSchedule:
        found = self._call("POST", "/payroll/paychecks", body=to_data(request))
        return from_data(SavedSchedule, found)

    def preview_pay_change(self, request: PayChange) -> PayChangePlan:
        found = self._call("POST", "/payroll/pay-changes/preview", body=to_data(request))
        return from_data(PayChangePlan, found["plan"], "plan")

    def apply_pay_change(self, request: PayChange) -> PayChangePlan:
        found = self._call("POST", "/payroll/pay-changes", body=to_data(request))
        return from_data(PayChangePlan, found["plan"], "plan")

    # --------------------------------------------------------------- FSA claims

    def fsa_claims(self, as_of: date | None = None) -> FsaClaimsView:
        return from_data(FsaClaimsView, self._call("GET", "/fsa-claims", {"as_of": as_of}))

    def fsa_claim_standing(self, claim: str) -> ClaimStanding:
        path = f"/fsa-claims/{quote(claim, safe='')}/standing"
        return from_data(ClaimStanding, self._call("GET", path))

    def find_fsa_claim(self, reference: str) -> FsaClaim:
        found = self._call("GET", "/fsa-claims/lookup", {"reference": reference})
        return from_data(FsaClaim, found["claim"], "claim")

    def save_fsa_claim(self, request: SaveClaim) -> SavedClaim:
        return from_data(SavedClaim, self._call("POST", "/fsa-claims", body=to_data(request)))

    def delete_fsa_claim(self, claim: str) -> SavedClaim:
        found = self._call("DELETE", f"/fsa-claims/{quote(claim, safe='')}")
        return from_data(SavedClaim, found)

    def close_fsa_claim(self, request: CloseClaim) -> SavedClaim:
        body = {"on": request.on, "reason": request.reason}
        path = f"/fsa-claims/{quote(request.handle, safe='')}/close"
        return from_data(SavedClaim, self._call("POST", path, body=body))

    def reopen_fsa_claim(self, request: ReopenClaim) -> SavedClaim:
        body = {"on": request.on, "note": request.note}
        path = f"/fsa-claims/{quote(request.handle, safe='')}/reopen"
        return from_data(SavedClaim, self._call("POST", path, body=body))

    def fsa_claim_report(self, query: ClaimReportQuery) -> ClaimReport:
        params = {
            "as_of": query.as_of,
            "by": query.by,
            "account": query.account,
            "funding_year": query.funding_year,
            "provider": query.provider,
            "status": query.status.value if query.status is not None else None,
            "attention": "true" if query.attention_only else None,
        }
        return from_data(ClaimReport, self._call("GET", "/fsa-claims/report", params))

    def benefit_years(self, as_of: date | None = None) -> FsaYearsView:
        return from_data(FsaYearsView, self._call("GET", "/fsa-years", {"as_of": as_of}))

    def claim_link_proposals(self, account: str | None = None) -> tuple[FsaClaimProposal, ...]:
        found = self._call("GET", "/fsa-claim-proposals", {"account": account})
        return from_data(tuple[FsaClaimProposal, ...], found["proposals"], "proposals")

    def accept_claim_links(self, chosen: tuple[tuple[str, str, str], ...]) -> AcceptedClaimLinks:
        body = {"chosen": [list(item) for item in chosen]}
        found = self._call("POST", "/fsa-claim-proposals/accept", body=body)
        return from_data(AcceptedClaimLinks, found)

    # ------------------------------------------------------------- plan review

    def review_queue(self) -> tuple[PendingActual, ...]:
        found = self._call("GET", "/review")
        return from_data(tuple[PendingActual, ...], found["actuals"], "actuals")

    def review_detail(self, transaction: str) -> ReviewDetail:
        return from_data(ReviewDetail, self._call("GET", f"/review/{quote(transaction, safe='')}"))

    def _review(self, transaction: str, action: str, body: dict[str, Any]) -> ReviewMutation:
        path = f"/review/{quote(transaction, safe='')}/{action}"
        return from_data(ReviewMutation, self._call("POST", path, body=body))

    def match_review(self, request: ReviewOccurrence) -> ReviewMutation:
        return self._review(request.transaction, "match", {"occurrence": request.occurrence})

    def reject_review(self, request: ReviewOccurrence) -> ReviewMutation:
        return self._review(request.transaction, "reject", {"occurrence": request.occurrence})

    def skip_review(self, request: ReviewOccurrence) -> ReviewMutation:
        return self._review(request.transaction, "skip", {"occurrence": request.occurrence})

    def mark_review_unexpected(self, transaction: str) -> ReviewMutation:
        return self._review(transaction, "unexpected", {})

    def attach_review_claim(self, request: ReviewClaimAttachment) -> ReviewMutation:
        body = {
            "claim": request.claim,
            "role": request.role,
            "split": request.split,
            "funding_year_start": request.funding_year_start,
        }
        return self._review(request.transaction, "claim", body)

    def plan_occurrence(self, key: str) -> PlannedEvent:
        found = self._call("GET", "/plan/occurrences/lookup", {"key": key})
        return from_data(PlannedEvent, found["occurrence"], "occurrence")

    def unresolved_occurrences(self, start: date, end: date) -> tuple[PlannedEvent, ...]:
        found = self._call("GET", "/plan/occurrences", {"start": start, "end": end})
        return from_data(tuple[PlannedEvent, ...], found["occurrences"], "occurrences")

    def match_candidates(
        self, transaction: str, window_days: int = 7
    ) -> tuple[MatchCandidate, ...]:
        path = f"/transactions/{quote(transaction, safe='')}/plan-matches"
        found = self._call("GET", path, {"window_days": window_days})
        return from_data(tuple[MatchCandidate, ...], found["candidates"], "candidates")

    # ------------------------------------------------------------ import review

    def held_imports(self) -> tuple[HeldImportChange, ...]:
        found = self._call("GET", "/held-imports")
        return from_data(tuple[HeldImportChange, ...], found["changes"], "changes")

    def account_suggestions(self) -> tuple[AccountSuggestion, ...]:
        found = self._call("GET", "/account-suggestions")
        return from_data(tuple[AccountSuggestion, ...], found["suggestions"], "suggestions")

    def apply_account_suggestions(self, request: ApplyAccountSuggestions) -> AppliedSuggestions:
        found = self._call("POST", "/account-suggestions/apply", body=to_data(request))
        return from_data(AppliedSuggestions, found)

    def resolve_held_imports(self, request: ResolveHeldImports) -> HeldImportResolution:
        found = self._call("POST", "/held-imports/resolve", body=to_data(request))
        return from_data(HeldImportResolution, found)

    # ------------------------------------------------------------ lots and tax

    def _lots_path(self, transaction: str, split: str) -> str:
        return f"/transactions/{quote(transaction, safe='')}/splits/{quote(split, safe='')}/lots"

    def sale_lots(self, transaction: str, split: str) -> SaleLots:
        return from_data(SaleLots, self._call("GET", self._lots_path(transaction, split)))

    def set_sale_lots(self, request: SetSaleLots) -> SaleLots:
        path = self._lots_path(request.transaction, request.split)
        body = {"picks": [list(pick) for pick in request.picks]}
        return from_data(SaleLots, self._call("POST", path, body=body))

    def tax_marks(self) -> TaxMarks:
        return from_data(TaxMarks, self._call("GET", "/tax-marks"))

    def set_tax_marks(self, request: SetTaxMarks) -> TaxMarks:
        return from_data(TaxMarks, self._call("PATCH", "/tax-marks", body=to_data(request)))

    # ------------------------------------------------------------------ quotes

    def quote_plan(self) -> QuotePlan:
        return from_data(QuotePlan, self._call("GET", "/quotes"))

    def store_quotes(self, request: StoreQuotes) -> QuoteUpdate:
        return from_data(QuoteUpdate, self._call("POST", "/quotes", body=to_data(request)))

    def set_quote_source(self, request: SetQuoteSource) -> str:
        path = f"/commodities/{quote(request.commodity, safe='')}/quote-source"
        found = self._call("PATCH", path, body={"source": request.source})
        return str(found["source"])

    # ------------------------------------------------------------------- loans

    def loan_choices(self) -> LoanChoices:
        return from_data(LoanChoices, self._call("GET", "/loans/choices"))

    def preview_loan(self, terms: LoanTerms) -> LoanPreview:
        found = self._call("POST", "/loans/preview", body=to_data(terms))
        return from_data(LoanPreview, found)

    def save_loan(self, request: SaveLoan) -> SavedLoan:
        return from_data(SavedLoan, self._call("POST", "/loans", body=to_data(request)))

    # ---------------------------------------------------------- exchange rates

    def currencies(self, book_order: bool = False) -> tuple[CurrencyChoice, ...]:
        found = self._call("GET", "/currencies", {"order": "book" if book_order else None})
        return from_data(tuple[CurrencyChoice, ...], found["currencies"], "currencies")

    def find_currency(self, reference: str) -> CurrencyChoice:
        found = self._call("GET", "/currencies/lookup", {"reference": reference})
        return from_data(CurrencyChoice, found["currency"], "currency")

    def save_exchange_rate(self, request: SaveExchangeRate) -> CommodityPrice:
        found = self._call("POST", "/exchange-rates", body=to_data(request))
        return from_data(CommodityPrice, found["rate"], "rate")

    def securities(self) -> tuple[SecurityQuote, ...]:
        found = self._call("GET", "/securities")
        return from_data(tuple[SecurityQuote, ...], found["securities"], "securities")

    def save_security_price(self, request: SaveSecurityPrice) -> SavedSecurityPrice:
        found = self._call("POST", "/security-prices", body=to_data(request))
        return from_data(SavedSecurityPrice, found, "saved")

    # ------------------------------------------------------ GnuCash write-back

    def writeback_preview(self) -> WritebackPlan:
        return from_data(WritebackPlan, self._call("GET", "/gnucash/writeback"))

    def writeback_keep_backups(self) -> int:
        return int(self._call("GET", "/gnucash/writeback/settings")["keep_backups"])

    def set_writeback_keep_backups(self, keep: int) -> int:
        found = self._call("PATCH", "/gnucash/writeback/settings", body={"keep_backups": keep})
        return int(found["keep_backups"])

    def apply_writeback(self, request: ApplyWriteback) -> WritebackApplied:
        found = self._call("POST", "/gnucash/writeback", body=to_data(request))
        return from_data(WritebackApplied, found)

    # ------------------------------------------------------- dashboard settings

    def dashboard(
        self,
        as_of: date | None = None,
        liquidity_days: int | None = None,
        emergency_months: int | None = None,
    ) -> Dashboard:
        params = {
            "as_of": as_of,
            "liquidity_days": liquidity_days,
            "emergency_months": emergency_months,
        }
        return from_data(Dashboard, self._call("GET", "/dashboard", params))

    def dashboard_settings(self) -> DashboardSettings:
        return from_data(DashboardSettings, self._call("GET", "/dashboard/settings"))

    def default_dashboard_settings(self) -> DashboardSettings:
        found = self._call("GET", "/dashboard/settings/defaults")
        return from_data(DashboardSettings, found)

    def save_dashboard_settings(self, request: SaveDashboardConfig) -> DashboardSettings:
        found = self._call("PATCH", "/dashboard/settings", body=to_data(request))
        return from_data(DashboardSettings, found)

    def explain_plan_cell(self, query: PlanCellQuery) -> PlanCellDetail:
        return from_data(PlanCellDetail, self._call("POST", "/plan/cell", body=to_data(query)))

    # ---------------------------------------------------------------- imports

    def inspect_csv(self, request: InspectCsv) -> CsvInspection:
        found = self._call("POST", "/imports/csv/inspect", body=to_data(request))
        return from_data(CsvInspection, found)

    def preview_csv(self, request: CsvImportRequest) -> CsvPreview:
        found = self._call("POST", "/imports/csv/preview", body=to_data(request))
        return from_data(CsvPreview, found)

    def import_csv(self, request: CsvImportRequest) -> CsvImported:
        return from_data(CsvImported, self._call("POST", "/imports/csv", body=to_data(request)))

    def import_book(self, request: ImportBook) -> ImportedBook:
        # Progress is heard in process only; the server reports the outcome.
        body = to_data(replace(request, progress=None))
        found = self._call("POST", "/imports", body=body)
        # The service names the importers' result only for type checking (it does not
        # import the plugins), so its field is decoded with the plugins' own class.
        from ..plugins.importer.gnucash_common import ImportResult

        return ImportedBook(
            found["source"],
            found["format_id"],
            found["format_name"],
            from_data(ImportResult, found["result"], "result"),
        )

    def import_source(self) -> str | None:
        return self._call("GET", "/imports/source")["source"]

    # ------------------------------------------------------------------- find

    def find(self, query: TransactionQuery) -> FindResult:
        return from_data(FindResult, self._call("POST", "/find", body=to_data(query)))

    # ------------------------------------------------------------- attachments

    def attachment_report(
        self, missing_only: bool = False, transaction: str | None = None
    ) -> AttachmentReport:
        params = {"missing": "true" if missing_only else None, "transaction": transaction}
        return from_data(AttachmentReport, self._call("GET", "/attachments", params))

    def _attachments(self, transaction: str, action: str, body: dict[str, Any]) -> Transaction:
        path = f"/transactions/{quote(transaction, safe='')}/attachments{action}"
        found = self._call("POST", path, body=body)
        return from_data(Transaction, found["transaction"], "transaction")

    def link_attachment(self, transaction: str, location: str) -> Transaction:
        return self._attachments(transaction, "", {"location": location})

    def attach_file(self, transaction: str, path: str, copy: bool = True) -> Transaction:
        return self._attachments(transaction, "/files", {"path": path, "copy": copy})

    def unlink_attachment(self, transaction: str, location: str) -> Transaction:
        return self._attachments(transaction, "/remove", {"location": location})

    def relink_attachment(self, transaction: str, location: str, new: str) -> Transaction:
        return self._attachments(transaction, "/relink", {"location": location, "to": new})

    def set_attachment_folder(self, folder: str) -> str:
        found = self._call("PATCH", "/attachment-folders", body={"folder": folder})
        return str(found["folder"])

    def set_source_link_folder(self, folder: str) -> str:
        found = self._call("PATCH", "/attachment-folders", body={"gnucash_folder": folder})
        return str(found["gnucash_folder"])

    # ------------------------------------------------------- plan and spending

    def reporting_currency(self) -> str:
        return str(self._call("GET", "/reporting-currency")["currency"])

    def plan_settings(self, default_start: date, default_end: date) -> PlanSettings:
        params = {"default_start": default_start, "default_end": default_end}
        return from_data(PlanSettings, self._call("GET", "/plan/settings", params))

    def plan_bounds(self, today: date | None = None) -> PlanBounds:
        return from_data(PlanBounds, self._call("GET", "/plan/bounds", {"today": today}))

    def save_plan_settings(self, settings: PlanSettings) -> PlanSettings:
        found = self._call("PATCH", "/plan/settings", body=to_data(settings))
        return from_data(PlanSettings, found)

    def plan(self, query: PlanQuery) -> PlanQueryResult:
        return from_data(PlanQueryResult, self._call("POST", "/plan", body=to_data(query)))

    def expense_explorer(
        self,
        query: PlanQuery,
        account: str | None = None,
        period_index: int | None = None,
        rollover: bool = False,
    ) -> ExpenseExplorer:
        body = {
            "query": to_data(query),
            "account": account,
            "period_index": period_index,
            "rollover": rollover,
        }
        return from_data(ExpenseExplorer, self._call("POST", "/expense-explorer", body=body))

    # -------------------------------------------------------------- projection

    def project(
        self,
        scenario: Scenario,
        progress: Callable[[ProjectionProgress], None] | None = None,
    ) -> Projection:
        found = self._call("POST", "/projection", body={"scenario": to_data(scenario)})
        return from_data(Projection, found)

    def projection_month(
        self,
        scenario: Scenario,
        month_index: int,
        projected: Projection | None = None,
    ) -> ProjectionMonthDetail:
        body = {"scenario": to_data(scenario), "month_index": month_index}
        return from_data(ProjectionMonthDetail, self._call("POST", "/projection/month", body=body))

    # --------------------------------------------------------------- net worth

    def net_worth_history(
        self,
        start: date,
        end: date,
        period: ReportingPeriod = ReportingPeriod.MONTH,
        today: date | None = None,
    ) -> NetWorthHistory:
        params = {"start": start, "end": end, "period": ReportingPeriod(period).value}
        found = self._call("GET", "/net-worth", {**params, "today": today})
        return from_data(NetWorthHistory, found)

    def net_worth_change(self, start: date, end: date, today: date | None = None) -> NetWorthChange:
        params = {"start": start, "end": end, "today": today}
        return from_data(NetWorthChange, self._call("GET", "/net-worth/change", params))
