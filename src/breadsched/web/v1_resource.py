"""Versioned book-server resources: ``/api/v1`` (#357).

**Experimental.** Each handler serves one operation of the book API
(:class:`~breadsched.api.protocol.BookApi`) as a resource, through the request's
book server (``Api.book``), and returns its JSON form from
:mod:`breadsched.api.views`. ``web.resources.V1_ROUTES`` maps methods and paths to
them; ``web.transport`` adds each response's revision (``ETag``), refuses a write
whose ``If-Match`` names an older revision, and streams change events.

Resources so far, all under ``/api/v1``:

* ``GET /book`` – the book's revision.
* ``GET /due-review`` (``as_of``) and ``POST /due-review`` (``decisions``, ``as_of``);
  ``GET /schedules/due`` (``as_of``, ``days``) – the occurrences already due.
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
* ``GET /accounts/{handle}/register-view`` (``limit``) – the newest rows with every name,
  document, and share detail the browser's register shows.
* ``POST /accounts/{handle}/register-changes`` (``changed``, transaction handles) – the
  rows of those transactions in the register, for a client holding every row to merge;
  it writes nothing.
* ``GET /accounts/{handle}/balance`` (``as_of``, ``recursive``) and ``GET /balances``
  (``as_of``) – one account's balance, or the book's cash and net worth.
* ``GET /accounts`` and ``GET /accounts/{handle}`` – accounts as stored, with full
  names; ``GET /chart`` (``as_of``, ``hidden``) – the valued account tree;
  ``GET /account-outline`` (``hidden``) – the same tree with depths and no values.
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
  ``/cancel``, and ``/reopen``; ``POST /transactions/{handle}/splits/{split}/cleared``;
  ``GET /accounts/{handle}/reconciled-balance`` (``through``) – the starting balance a
  statement on that date begins from; ``POST /accounts/{handle}/statement-entries``
  (``other_account``, ``post_date``, ``description``, ``amount``) adds a statement
  line such as interest, cleared and checked.
* ``GET /receivables`` (``as_of``) – receivables with their links and the forms'
  choices; ``GET /receivables/lookup`` (``reference``); ``GET /receivables/costs``
  (``start``, ``end``) – each category's gross and net cost, as the Plan shows it;
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
* ``GET /payroll`` (``as_of``) – paychecks, templates, and form choices;
  ``GET /schedules/{handle}/paycheck`` (``as_of``); ``POST /payroll/templates/draft``
  describes a paycheck as a template without saving it; ``POST /payroll/templates``
  (``SavePayrollTemplate``) and ``DELETE /payroll/templates/{name}``;
  ``POST /payroll/paychecks`` (``CreatePaycheck``); ``POST /payroll/pay-changes/preview``
  and ``POST /payroll/pay-changes`` (``PayChange``).
* ``GET /fsa-claims`` (``as_of``) – claims with their standing and what they could
  link; ``GET /fsa-claims/lookup`` (``reference``); ``POST /fsa-claims``
  (``SaveClaim``); ``DELETE /fsa-claims/{handle}``; ``POST /fsa-claims/{handle}/close``
  and ``/reopen``; ``GET /fsa-claims/report`` (``as_of``, ``by``, ``account``,
  ``funding_year``, ``provider``, ``status``, ``attention``); ``GET /fsa-years``
  (``as_of``); ``GET /fsa-claim-proposals`` (``account``) and
  ``POST /fsa-claim-proposals/accept``.
* ``GET /review`` – actuals awaiting Review; ``GET /review/{transaction}`` – what
  Review offers for one; ``POST /review/{transaction}/match``, ``/reject``, ``/skip``
  (``occurrence``), ``/unexpected``, and ``/claim``; ``GET /plan/occurrences``
  (``start``, ``end``) and ``/plan/occurrences/lookup`` (``key``);
  ``GET /transactions/{handle}/plan-matches`` (``window_days``).
* ``GET /held-imports`` – GnuCash changes held back from reconciled transactions;
  ``POST /held-imports/resolve`` (``ResolveHeldImports``).
* ``GET /account-suggestions`` – loan/asset links and card settings the book suggests;
  ``POST /account-suggestions/apply`` (``ApplyAccountSuggestions``) applies those at
  least ``min_confidence`` sure.
* ``GET`` and ``POST /transactions/{handle}/splits/{split}/lots`` – the lots a sale
  could name, and naming them (``picks``); ``GET`` and ``PATCH /tax-marks``.
* ``GET /quotes`` – commodities, their quote sources, and what a fetch would ask
  for; ``POST /quotes`` stores quotes the caller fetched (``StoreQuotes``);
  ``PATCH /commodities/{handle}/quote-source`` (``source``).
* ``GET /loans/choices``; ``POST /loans/preview`` (``LoanTerms``) and ``POST /loans``
  (``SaveLoan``).
* ``GET /currencies`` and ``/currencies/lookup`` (``reference``); ``POST /exchange-rates``
  (``SaveExchangeRate``).
* ``GET /securities`` – each security with its latest price; ``POST /security-prices``
  (``SaveSecurityPrice``) – one dated manual price, creating the security when needed.
* ``GET`` and ``POST /gnucash/writeback`` (``ApplyWriteback``) – the write-back
  preview and the write; ``GET`` and ``PATCH /gnucash/writeback/settings``
  (``keep_backups``).
* ``GET /dashboard`` (``as_of``, ``liquidity_days``, ``emergency_months``) – the
  Dashboard computed once, over other horizons without saving them.
* ``GET`` and ``PATCH /dashboard/settings`` (``SaveDashboardConfig``) – the
  Dashboard's groups, with their members, and its horizons;
  ``GET /dashboard/settings/defaults`` – the ones a new book starts with.
* ``GET /schedules/view`` (``days``) – every saved definition with its editor view and
  next occurrence, card payments, upcoming occurrences, and account choices;
  ``GET /schedules/{handle}/editor`` and ``/transactions/{handle}/schedule-draft`` –
  one definition's editor view, or a one-time draft made from a transaction.
* ``GET /scenarios/{handle}/events`` – a scenario's own estimates beside the baseline
  schedules; ``POST /scenario-schedules/fixed`` (``SaveFixedScenarioSchedule``) and
  ``/scenario-schedules/formula`` (``FormulaScenarioScheduleInput``) save one.
* ``POST /imports/csv/inspect`` (``InspectCsv``), ``/imports/csv/preview`` and
  ``/imports/csv`` (``CsvImportRequest``), and ``/imports`` (``ImportBook``) – a statement
  or book file, by a path the server can read; ``GET /imports/source`` – the file last
  imported from.
* ``POST /plan/cell`` (``PlanCellQuery``) – one Plan cell's planned occurrences beside
  the actual transactions; it reads only.
* ``POST /find`` (``TransactionQuery``) – splits across the book that match, grouped,
  with per-currency totals; it reads only (#382).
* ``GET /attachments`` (``missing``, ``transaction``) – linked documents checked
  against the file system; ``POST /transactions/{handle}/attachments`` (``location``),
  ``/attachments/files`` (``path`` as the server sees it, ``copy``), ``/remove``, and
  ``/relink``; ``PATCH /attachment-folders`` (``folder`` or ``gnucash_folder``).
* ``GET /net-worth`` (``start``, ``end``, ``period``, ``today``) and
  ``GET /net-worth/change`` (``start``, ``end``, ``today``).
* ``GET /book/counts`` – how many accounts and transactions the book holds;
  ``GET /book/id`` – its permanent id (decision 0004).
* ``GET /book/summary`` – the book's counts, cash, net worth, and scenarios;
  ``GET /book/verification`` – its physical and logical checks.
* ``GET /entry/suggestion`` (``description``, ``account``, ``currency``, ``exclude``),
  ``/entry/amount`` (``text``, ``currency``, ``number_format``), ``/entry/trade``
  (``account``, ``shares``, ``price``, ``amount``, ``changed``, ``currency``,
  ``number_format``), ``/entry/accounts`` (``text``, ``exclude``, ``limit``), and
  ``/entry/num`` (``account``, ``text``, ``step``) – reading what is typed into a
  register; none writes.
* ``GET /historical-estimates`` (``months``, ``min_active_months``, ``scenario``,
  ``as_of``) – proposals from closed history with their drafts; ``POST
  /historical-estimates`` (``AcceptHistoricalEstimate``) – accept one.
* ``GET /activity`` (``start``, ``end``, ``period``, ``as_of``) – plan versus
  actuals with the notes that disclose its quotes and exclusions.
* ``GET /budget-jars`` (``start``, ``end``, ``period``, ``today``).
* ``GET /holdings`` (``as_of``), ``GET /realized-gains`` (``year``, ``account``), and
  ``GET /tax-year`` (``year``; the latest year with a transaction when absent).
* ``GET`` and ``PATCH /plan/settings`` (``default_start``, ``default_end``;
  ``PlanSettings``) – the Plan presentation every interface restores.
* ``GET /plan/bounds`` (``today``) – the first and last months a Plan may show.
* ``GET /reporting-currency`` – the reporting currency's code; ``POST /plan``
  (``PlanQuery``) and ``POST /expense-explorer`` (``query``, ``account``,
  ``period_index``, ``rollover``) – reads whose request is a typed query.
* ``POST /projection`` (``scenario``, saved or a draft) and ``POST /projection/month``
  (``scenario``, ``month_index``).
* ``POST /undo`` and ``POST /redo``.
* ``GET /events`` (``since``) – server-sent change events, served by the transport.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any, Literal, cast

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
from ..gen.engine.activity import PlanSettings, ReportingPeriod
from ..gen.engine.fsa_claim_report import GROUPINGS
from ..gen.engine.fsa_claims import FsaClaimStatus
from ..gen.engine.loans import LoanTerms
from ..gen.engine.transaction_query import TransactionQuery
from ..gen.lib import Scenario, ScheduledTransaction
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
from ..gen.services.account_inference import ApplyAccountSuggestions
from ..gen.services.assumptions import SaveBaseAssumptions, SaveScenarioAssumptions
from ..gen.services.autocomplete import SuggestEntry
from ..gen.services.categorization import AddRule
from ..gen.services.claims import ClaimReportQuery, CloseClaim, ReopenClaim, SaveClaim
from ..gen.services.csv_import import CsvImportRequest, InspectCsv
from ..gen.services.dashboard_config import SaveDashboardConfig
from ..gen.services.entry_input import ReadEntryTrade
from ..gen.services.exchange_rates import SaveExchangeRate
from ..gen.services.gnucash_writeback import ApplyWriteback
from ..gen.services.historical_estimates import AcceptHistoricalEstimate
from ..gen.services.import_review import ResolveHeldImports
from ..gen.services.imports import ImportBook
from ..gen.services.loans import SaveLoan
from ..gen.services.lots import SetSaleLots
from ..gen.services.payroll import CreatePaycheck, PayChange, SavePayrollTemplate
from ..gen.services.plan import PlanQuery
from ..gen.services.plan_cells import PlanCellQuery
from ..gen.services.quotes import SetQuoteSource, StoreQuotes
from ..gen.services.receivables import RecordWriteOff, SaveReceivable
from ..gen.services.reconciliations import (
    AddStatementEntry,
    StartReconciliation,
    ToggleCleared,
    UpdateReconciliation,
)
from ..gen.services.review import ReviewClaimAttachment, ReviewOccurrence
from ..gen.services.savings_goals import AllocateToGoal, SaveSavingsGoal, SetGoalOverride
from ..gen.services.scenarios import (
    DuplicateScenario,
    SaveDrawdown,
    SaveScenario,
    SetReimbursementOverride,
    SuppressScenarioSchedule,
)
from ..gen.services.schedules import FormulaScenarioScheduleInput, SaveFixedScenarioSchedule
from ..gen.services.security_prices import SaveSecurityPrice
from ..gen.services.tax import SetTaxMarks
from .context import Api

if TYPE_CHECKING:
    from .resources import QueryParams

__all__ = [
    "projection_month_v1",
    "projection_v1",
    "reporting_currency_get",
    "expense_explorer_v1",
    "plan_bounds_get",
    "plan_settings_get",
    "plan_settings_patch",
    "plan_v1",
    "activity_get",
    "historical_estimate_accept_v1",
    "historical_estimates_get",
    "book_counts_get",
    "book_id_get",
    "book_summary_get",
    "book_verification_get",
    "entry_accounts_get",
    "entry_amount_get",
    "entry_num_get",
    "entry_suggestion_get",
    "entry_trade_get",
    "budget_jars_get",
    "holdings_get",
    "net_worth_change_get",
    "net_worth_history_get",
    "realized_gains_get",
    "tax_year_get",
    "attachment_file_v1",
    "attachment_folders_patch",
    "attachment_link_v1",
    "attachment_relink_v1",
    "attachment_remove_v1",
    "attachments_get",
    "dashboard_get",
    "writeback_apply",
    "writeback_get",
    "writeback_settings_get",
    "writeback_settings_patch",
    "dashboard_settings_get",
    "dashboard_defaults_get",
    "dashboard_settings_patch",
    "find_post",
    "fsa_claim_standing_get",
    "commodities_get",
    "transaction_editor_get",
    "scenario_schedule_fixed_draft",
    "scenario_schedule_formula_draft",
    "plan_cell_v1",
    "csv_inspect_v1",
    "csv_preview_v1",
    "csv_import_v1",
    "import_book_v1",
    "import_source_get",
    "scenario_events_get",
    "scenario_schedule_fixed_post",
    "scenario_schedule_formula_post",
    "schedule_draft_get",
    "schedule_layout_post",
    "scheduled_page_get",
    "schedule_editor_get",
    "schedules_view_get",
    "currencies_get",
    "currency_lookup",
    "exchange_rate_save",
    "securities_get",
    "security_price_save",
    "loan_choices_get",
    "loan_preview_v1",
    "loan_save_v1",
    "quote_plan_get",
    "quote_source_patch",
    "quotes_store",
    "sale_lots_get",
    "sale_lots_post",
    "tax_marks_get",
    "tax_marks_patch",
    "held_imports_get",
    "account_suggestions_get",
    "account_suggestions_apply",
    "held_imports_resolve",
    "plan_matches_get",
    "plan_occurrence_lookup",
    "plan_occurrences_get",
    "review_claim_v1",
    "review_detail_get",
    "review_match_v1",
    "review_queue_get",
    "review_reject_v1",
    "review_skip_v1",
    "review_unexpected_v1",
    "claim_links_accept",
    "claim_proposals_get",
    "fsa_claim_close_v1",
    "fsa_claim_lookup",
    "fsa_claim_remove",
    "fsa_claim_reopen_v1",
    "fsa_claim_report_get",
    "fsa_claim_store",
    "fsa_claims_get",
    "benefit_years_get",
    "pay_change_apply",
    "pay_change_preview",
    "paycheck_create",
    "paycheck_get",
    "payroll_get",
    "payroll_template_draft",
    "payroll_template_remove",
    "payroll_template_store",
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
    "receivable_costs_get",
    "reimbursement_override",
    "reimbursement_proposals_accept",
    "reimbursement_proposals_get",
    "cleared_toggle",
    "reconciliation_cancel",
    "statement_entry_add",
    "reconciliation_complete",
    "reconciled_balance_get",
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
    "account_outline_get",
    "chart_get",
    "account_balance_get",
    "account_lookup",
    "balances_get",
    "currency_get",
    "register_get",
    "register_view_get",
    "register_changes_v1",
    "transaction_editable",
    "transaction_lookup",
    "advance_days_get",
    "advance_days_patch",
    "book_get",
    "due_review_get",
    "due_occurrences_get",
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


def due_occurrences_get(api: Api, _path: Path, query: QueryParams) -> dict:
    as_of = query.iso_date("as_of")
    days = query.integer("days")
    query.finish()
    return {"occurrences": to_data(api.book.due_occurrences(as_of, days))}


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


def register_view_get(api: Api, path: Path, query: QueryParams) -> dict:
    # ``limit=all`` is every row, as the desktop register shows them.
    every = query.text("limit") == "all"
    limit = None if every else query.integer("limit", default=250, minimum=1, maximum=10000)
    needle = query.text("filter") or ""
    query.finish()
    return to_data(api.book.register_view(path["handle"], limit, needle))


@dataclass(frozen=True, slots=True)
class _RegisterChanged:
    changed: tuple[str, ...]


def register_changes_v1(api: Api, path: Path, body: dict[str, Any]) -> dict:
    request = from_data(_RegisterChanged, body, "request")
    return to_data(api.book.register_changes(path["handle"], request.changed))


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


def account_outline_get(api: Api, _path: Path, query: QueryParams) -> dict:
    hidden = query.choice("hidden", ("true", "false"), "false") == "true"
    query.finish()
    return {
        "lines": [
            {
                **account_record_json(line.record),
                "depth": line.depth,
                "class": line.record.account.account_class.value,
            }
            for line in api.book.account_outline(hidden)
        ]
    }


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


def reconciled_balance_get(api: Api, path: Path, query: QueryParams) -> dict:
    through = query.iso_date("through", required=True)
    query.finish()
    assert through is not None
    return {"balance": api.book.reconciled_balance(path["handle"], through)}


def statement_entry_add(api: Api, path: Path, body: dict[str, Any]) -> dict:
    _only(body, {"other_account", "post_date", "description", "amount"})
    amount = _money(body, "amount")
    assert amount is not None
    request = AddStatementEntry(
        path["handle"],
        _required_text(body, "other_account"),
        _required_date(body, "post_date"),
        str(body.get("description") or ""),
        amount,
    )
    return saved_transaction_json(api.book.add_statement_entry(request))


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


def receivable_costs_get(api: Api, _path: Path, query: QueryParams) -> dict:
    start = query.iso_date("start", required=True)
    end = query.iso_date("end", required=True)
    query.finish()
    assert start is not None and end is not None
    return {"categories": to_data(api.book.reimbursable_costs(start, end))}


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


@dataclass(frozen=True, slots=True)
class _TemplateDraft:
    schedule: str
    name: str
    as_of: date | None = None


def payroll_get(api: Api, _path: Path, query: QueryParams) -> dict:
    as_of = query.iso_date("as_of")
    query.finish()
    return to_data(api.book.payroll(as_of))


def paycheck_get(api: Api, path: Path, query: QueryParams) -> dict:
    as_of = query.iso_date("as_of")
    query.finish()
    return {"paycheck": to_data(api.book.paycheck(path["handle"], as_of))}


def payroll_template_draft(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    draft = from_data(_TemplateDraft, body, "request")
    template = api.book.template_from_schedule(draft.schedule, draft.name, draft.as_of)
    return {"template": to_data(template)}


def payroll_template_store(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    request = from_data(SavePayrollTemplate, body, "request")
    return {"template": to_data(api.book.save_payroll_template(request))}


def payroll_template_remove(api: Api, path: Path, body: dict[str, Any]) -> dict:
    _only(body, set())
    return {"template": to_data(api.book.delete_payroll_template(path["name"]))}


def paycheck_create(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    return to_data(api.book.create_paycheck(from_data(CreatePaycheck, body, "request")))


def pay_change_preview(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    request = from_data(PayChange, body, "request")
    return {"plan": to_data(api.book.preview_pay_change(request))}


def pay_change_apply(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    request = from_data(PayChange, body, "request")
    return {"plan": to_data(api.book.apply_pay_change(request))}


@dataclass(frozen=True, slots=True)
class _Closing:
    on: date
    reason: str = ""


@dataclass(frozen=True, slots=True)
class _Reopening:
    on: date
    note: str = ""


@dataclass(frozen=True, slots=True)
class _ChosenLinks:
    chosen: tuple[tuple[str, str, str], ...]


def fsa_claims_get(api: Api, _path: Path, query: QueryParams) -> dict:
    as_of = query.iso_date("as_of")
    query.finish()
    return to_data(api.book.fsa_claims(as_of))


def fsa_claim_lookup(api: Api, _path: Path, query: QueryParams) -> dict:
    reference = query.text("reference", required=True) or ""
    query.finish()
    return {"claim": to_data(api.book.find_fsa_claim(reference))}


def fsa_claim_store(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    return to_data(api.book.save_fsa_claim(from_data(SaveClaim, body, "request")))


def fsa_claim_remove(api: Api, path: Path, body: dict[str, Any]) -> dict:
    _only(body, set())
    return to_data(api.book.delete_fsa_claim(path["handle"]))


def fsa_claim_close_v1(api: Api, path: Path, body: dict[str, Any]) -> dict:
    closing = from_data(_Closing, body, "request")
    return to_data(api.book.close_fsa_claim(CloseClaim(path["handle"], closing.on, closing.reason)))


def fsa_claim_reopen_v1(api: Api, path: Path, body: dict[str, Any]) -> dict:
    change = from_data(_Reopening, body, "request")
    return to_data(api.book.reopen_fsa_claim(ReopenClaim(path["handle"], change.on, change.note)))


def fsa_claim_report_get(api: Api, _path: Path, query: QueryParams) -> dict:
    status = query.choice("status", tuple(item.value for item in FsaClaimStatus), "")
    request = ClaimReportQuery(
        as_of=query.iso_date("as_of"),
        by=query.choice("by", GROUPINGS, "status"),
        account=query.text("account"),
        funding_year=query.iso_date("funding_year"),
        provider=query.text("provider"),
        status=FsaClaimStatus(status) if status else None,
        attention_only=query.choice("attention", ("true", "false"), "false") == "true",
    )
    query.finish()
    return to_data(api.book.fsa_claim_report(request))


def benefit_years_get(api: Api, _path: Path, query: QueryParams) -> dict:
    as_of = query.iso_date("as_of")
    query.finish()
    return to_data(api.book.benefit_years(as_of))


def claim_proposals_get(api: Api, _path: Path, query: QueryParams) -> dict:
    account = query.text("account")
    query.finish()
    return {"proposals": to_data(api.book.claim_link_proposals(account))}


def claim_links_accept(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    chosen = from_data(_ChosenLinks, body, "request").chosen
    return to_data(api.book.accept_claim_links(chosen))


@dataclass(frozen=True, slots=True)
class _Occurrence:
    occurrence: str


@dataclass(frozen=True, slots=True)
class _ClaimAttachment:
    claim: str
    role: str
    split: str | None = None
    funding_year_start: date | None = None


def review_queue_get(api: Api, _path: Path, query: QueryParams) -> dict:
    query.finish()
    return {"actuals": to_data(api.book.review_queue())}


def review_detail_get(api: Api, path: Path, query: QueryParams) -> dict:
    query.finish()
    return to_data(api.book.review_detail(path["transaction"]))


def _occurrence(path: Path, body: dict[str, Any]) -> ReviewOccurrence:
    chosen = from_data(_Occurrence, body, "request")
    return ReviewOccurrence(path["transaction"], chosen.occurrence)


def review_match_v1(api: Api, path: Path, body: dict[str, Any]) -> dict:
    return to_data(api.book.match_review(_occurrence(path, body)))


def review_reject_v1(api: Api, path: Path, body: dict[str, Any]) -> dict:
    return to_data(api.book.reject_review(_occurrence(path, body)))


def review_skip_v1(api: Api, path: Path, body: dict[str, Any]) -> dict:
    return to_data(api.book.skip_review(_occurrence(path, body)))


def review_unexpected_v1(api: Api, path: Path, body: dict[str, Any]) -> dict:
    _only(body, set())
    return to_data(api.book.mark_review_unexpected(path["transaction"]))


def review_claim_v1(api: Api, path: Path, body: dict[str, Any]) -> dict:
    chosen = from_data(_ClaimAttachment, body, "request")
    request = ReviewClaimAttachment(
        path["transaction"], chosen.claim, chosen.role, chosen.split, chosen.funding_year_start
    )
    return to_data(api.book.attach_review_claim(request))


def plan_occurrence_lookup(api: Api, _path: Path, query: QueryParams) -> dict:
    key = query.text("key", required=True) or ""
    query.finish()
    return {"occurrence": to_data(api.book.plan_occurrence(key))}


def plan_occurrences_get(api: Api, _path: Path, query: QueryParams) -> dict:
    start = query.iso_date("start", required=True)
    end = query.iso_date("end", required=True)
    query.finish()
    assert start is not None and end is not None
    return {"occurrences": to_data(api.book.unresolved_occurrences(start, end))}


def plan_matches_get(api: Api, path: Path, query: QueryParams) -> dict:
    window = query.integer("window_days", default=7, minimum=0, maximum=366)
    query.finish()
    assert window is not None
    return {"candidates": to_data(api.book.match_candidates(path["handle"], window))}


def held_imports_get(api: Api, _path: Path, query: QueryParams) -> dict:
    query.finish()
    return {"changes": to_data(api.book.held_imports())}


def account_suggestions_get(api: Api, _path: Path, query: QueryParams) -> dict:
    query.finish()
    return {"suggestions": to_data(api.book.account_suggestions())}


def account_suggestions_apply(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    request = from_data(ApplyAccountSuggestions, body, "request")
    return to_data(api.book.apply_account_suggestions(request))


def held_imports_resolve(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    request = from_data(ResolveHeldImports, body, "request")
    return to_data(api.book.resolve_held_imports(request))


@dataclass(frozen=True, slots=True)
class _Picks:
    picks: tuple[tuple[str, str], ...] = ()


def sale_lots_get(api: Api, path: Path, query: QueryParams) -> dict:
    query.finish()
    return to_data(api.book.sale_lots(path["handle"], path["split"]))


def sale_lots_post(api: Api, path: Path, body: dict[str, Any]) -> dict:
    picks = from_data(_Picks, body, "request").picks
    return to_data(api.book.set_sale_lots(SetSaleLots(path["handle"], path["split"], picks)))


def tax_marks_get(api: Api, _path: Path, query: QueryParams) -> dict:
    query.finish()
    return to_data(api.book.tax_marks())


def tax_marks_patch(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    return to_data(api.book.set_tax_marks(from_data(SetTaxMarks, body, "request")))


@dataclass(frozen=True, slots=True)
class _Source:
    source: str


def quote_plan_get(api: Api, _path: Path, query: QueryParams) -> dict:
    query.finish()
    return to_data(api.book.quote_plan())


def quotes_store(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    return to_data(api.book.store_quotes(from_data(StoreQuotes, body, "request")))


def quote_source_patch(api: Api, path: Path, body: dict[str, Any]) -> dict:
    source = from_data(_Source, body, "request").source
    return {"source": api.book.set_quote_source(SetQuoteSource(path["handle"], source))}


def loan_choices_get(api: Api, _path: Path, query: QueryParams) -> dict:
    hidden = query.choice("hidden", ("true", "false"), "false") == "true"
    query.finish()
    return to_data(api.book.loan_choices(hidden))


def loan_preview_v1(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    return to_data(api.book.preview_loan(from_data(LoanTerms, body, "terms")))


def loan_save_v1(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    return to_data(api.book.save_loan(from_data(SaveLoan, body, "request")))


def currencies_get(api: Api, _path: Path, query: QueryParams) -> dict:
    book_order = query.choice("order", ("code", "book"), "code") == "book"
    query.finish()
    return {"currencies": to_data(api.book.currencies(book_order))}


def currency_lookup(api: Api, _path: Path, query: QueryParams) -> dict:
    reference = query.text("reference", required=True) or ""
    query.finish()
    return {"currency": to_data(api.book.find_currency(reference))}


def exchange_rate_save(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    request = from_data(SaveExchangeRate, body, "request")
    return {"rate": to_data(api.book.save_exchange_rate(request))}


def securities_get(api: Api, _path: Path, query: QueryParams) -> dict:
    query.finish()
    return {"securities": to_data(api.book.securities())}


def security_price_save(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    request = from_data(SaveSecurityPrice, body, "request")
    return to_data(api.book.save_security_price(request))


@dataclass(frozen=True, slots=True)
class _KeepBackups:
    keep_backups: int


def writeback_get(api: Api, _path: Path, query: QueryParams) -> dict:
    query.finish()
    return to_data(api.book.writeback_preview())


def writeback_apply(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    return to_data(api.book.apply_writeback(from_data(ApplyWriteback, body, "request")))


def writeback_settings_get(api: Api, _path: Path, query: QueryParams) -> dict:
    query.finish()
    return {"keep_backups": api.book.writeback_keep_backups()}


def writeback_settings_patch(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    keep = from_data(_KeepBackups, body, "request").keep_backups
    return {"keep_backups": api.book.set_writeback_keep_backups(keep)}


def dashboard_get(api: Api, _path: Path, query: QueryParams) -> dict:
    as_of = query.iso_date("as_of")
    liquidity_days = query.integer("liquidity_days")
    emergency_months = query.integer("emergency_months")
    query.finish()
    return to_data(api.book.dashboard(as_of, liquidity_days, emergency_months))


def dashboard_settings_get(api: Api, _path: Path, query: QueryParams) -> dict:
    query.finish()
    return to_data(api.book.dashboard_settings())


def dashboard_defaults_get(api: Api, _path: Path, query: QueryParams) -> dict:
    query.finish()
    return to_data(api.book.default_dashboard_settings())


def dashboard_settings_patch(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    request = from_data(SaveDashboardConfig, body, "request")
    return to_data(api.book.save_dashboard_settings(request))


def find_post(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    return to_data(api.book.find(from_data(TransactionQuery, body, "request")))


@dataclass(frozen=True, slots=True)
class _Location:
    location: str


@dataclass(frozen=True, slots=True)
class _File:
    path: str
    copy: bool = True


@dataclass(frozen=True, slots=True)
class _Relink:
    location: str
    to: str


@dataclass(frozen=True, slots=True)
class _Folders:
    folder: str | None = None
    gnucash_folder: str | None = None


def attachments_get(api: Api, _path: Path, query: QueryParams) -> dict:
    missing = query.choice("missing", ("true", "false"), "false") == "true"
    transaction = query.text("transaction")
    query.finish()
    return to_data(api.book.attachment_report(missing, transaction))


def attachment_link_v1(api: Api, path: Path, body: dict[str, Any]) -> dict:
    location = from_data(_Location, body, "request").location
    return {"transaction": to_data(api.book.link_attachment(path["handle"], location))}


def attachment_file_v1(api: Api, path: Path, body: dict[str, Any]) -> dict:
    chosen = from_data(_File, body, "request")
    changed = api.book.attach_file(path["handle"], chosen.path, chosen.copy)
    return {"transaction": to_data(changed)}


def attachment_remove_v1(api: Api, path: Path, body: dict[str, Any]) -> dict:
    location = from_data(_Location, body, "request").location
    return {"transaction": to_data(api.book.unlink_attachment(path["handle"], location))}


def attachment_relink_v1(api: Api, path: Path, body: dict[str, Any]) -> dict:
    change = from_data(_Relink, body, "request")
    changed = api.book.relink_attachment(path["handle"], change.location, change.to)
    return {"transaction": to_data(changed)}


def attachment_folders_patch(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    change = from_data(_Folders, body, "request")
    if (change.folder is None) == (change.gnucash_folder is None):
        raise ValueError("give folder or gnucash_folder")
    if change.folder is not None:
        return {"folder": api.book.set_attachment_folder(change.folder)}
    assert change.gnucash_folder is not None
    return {"gnucash_folder": api.book.set_source_link_folder(change.gnucash_folder)}


def book_id_get(api: Api, _path: Path, query: QueryParams) -> dict:
    query.finish()
    return {"id": api.book.book_id()}


def book_counts_get(api: Api, _path: Path, query: QueryParams) -> dict:
    query.finish()
    return to_data(api.book.book_counts())


def book_summary_get(api: Api, _path: Path, query: QueryParams) -> dict:
    query.finish()
    return to_data(api.book.book_summary())


def book_verification_get(api: Api, _path: Path, query: QueryParams) -> dict:
    query.finish()
    return to_data(api.book.verify_book())


_NUMBER_FORMATS = ("auto", "dot", "comma")


def entry_suggestion_get(api: Api, _path: Path, query: QueryParams) -> dict:
    request = SuggestEntry(
        description=query.text("description") or "",
        account=query.text("account"),
        currency=query.text("currency"),
        exclude=query.text("exclude"),
    )
    query.finish()
    return to_data(api.book.suggest_entry(request))


def entry_amount_get(api: Api, _path: Path, query: QueryParams) -> dict:
    text = query.text("text") or ""
    currency = query.text("currency")
    number_format = query.choice("number_format", _NUMBER_FORMATS, "auto")
    query.finish()
    return to_data(api.book.entry_amount(text, currency, number_format))


def entry_trade_get(api: Api, _path: Path, query: QueryParams) -> dict:
    request = ReadEntryTrade(
        account=query.text("account", required=True) or "",
        shares=query.text("shares") or "",
        price=query.text("price") or "",
        amount=query.text("amount") or "",
        changed=cast(
            'Literal["shares", "price", "amount"]',
            query.choice("changed", ("shares", "price", "amount"), "amount"),
        ),
        currency=query.text("currency"),
        number_format=cast(
            'Literal["auto", "dot", "comma"]',
            query.choice("number_format", _NUMBER_FORMATS, "auto"),
        ),
    )
    query.finish()
    return to_data(api.book.entry_trade(request))


def entry_accounts_get(api: Api, _path: Path, query: QueryParams) -> dict:
    text = query.text("text") or ""
    exclude = query.text("exclude")
    limit = query.integer("limit", default=20, minimum=1, maximum=200)
    query.finish()
    assert limit is not None
    return {"accounts": to_data(api.book.entry_accounts(text, exclude, limit))}


def entry_num_get(api: Api, _path: Path, query: QueryParams) -> dict:
    account = query.text("account", required=True) or ""
    text = query.text("text") or ""
    step = query.integer("step", default=1, minimum=-1000, maximum=1000)
    query.finish()
    assert step is not None
    return {"num": api.book.next_num(account, text, step)}


def schedules_view_get(api: Api, _path: Path, query: QueryParams) -> dict:
    days = query.integer("days", default=60, minimum=0, maximum=3660)
    query.finish()
    assert days is not None
    return to_data(api.book.schedules_view(days))


def schedule_editor_get(api: Api, path: Path, query: QueryParams) -> dict:
    query.finish()
    return to_data(api.book.schedule_editor(path["handle"]))


def scheduled_page_get(api: Api, _path: Path, query: QueryParams) -> dict:
    days = query.integer("days", minimum=0, maximum=3660)
    as_of = query.iso_date("as_of")
    query.finish()
    return to_data(api.book.scheduled_page(days, as_of))


def schedule_layout_post(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    raw = body.get("definition")
    if not isinstance(raw, dict):
        raise ValueError("definition must be a schedule")
    return to_data(api.book.schedule_layout(ScheduledTransaction.from_dict(raw)))


def schedule_draft_get(api: Api, path: Path, query: QueryParams) -> dict:
    query.finish()
    return to_data(api.book.schedule_draft(path["handle"]))


def scenario_events_get(api: Api, path: Path, query: QueryParams) -> dict:
    query.finish()
    return to_data(api.book.scenario_events(path["handle"]))


def scenario_schedule_fixed_post(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    request = from_data(SaveFixedScenarioSchedule, body, "request")
    return to_data(api.book.save_fixed_scenario_schedule(request))


def scenario_schedule_formula_post(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    request = from_data(FormulaScenarioScheduleInput, body, "request")
    return to_data(api.book.save_formula_scenario_schedule(request))


def csv_inspect_v1(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    return to_data(api.book.inspect_csv(from_data(InspectCsv, body, "request")))


def csv_preview_v1(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    return to_data(api.book.preview_csv(from_data(CsvImportRequest, body, "request")))


def csv_import_v1(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    return to_data(api.book.import_csv(from_data(CsvImportRequest, body, "request")))


def import_book_v1(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    request = from_data(ImportBook, body, "request")
    if request.progress is not None:
        raise ValueError("progress is heard in process only")
    return to_data(api.book.import_book(request))


def import_source_get(api: Api, _path: Path, query: QueryParams) -> dict:
    query.finish()
    return {"source": api.book.import_source()}


def plan_cell_v1(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    return to_data(api.book.explain_plan_cell(from_data(PlanCellQuery, body, "request")))


def scenario_schedule_fixed_draft(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    request = from_data(SaveFixedScenarioSchedule, body, "request")
    return {"schedule": api.book.draft_fixed_scenario_schedule(request).serialize()}


def scenario_schedule_formula_draft(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    request = from_data(FormulaScenarioScheduleInput, body, "request")
    return {"schedule": api.book.draft_formula_scenario_schedule(request).serialize()}


def transaction_editor_get(api: Api, _path: Path, query: QueryParams) -> dict:
    transaction = query.text("transaction")
    query.finish()
    return to_data(api.book.transaction_editor(transaction))


def commodities_get(api: Api, _path: Path, query: QueryParams) -> dict:
    query.finish()
    return {"commodities": [item.serialize() for item in api.book.commodities()]}


def fsa_claim_standing_get(api: Api, path: Path, query: QueryParams) -> dict:
    query.finish()
    return to_data(api.book.fsa_claim_standing(path["handle"]))


def historical_estimates_get(api: Api, _path: Path, query: QueryParams) -> dict:
    months = query.integer("months", default=12)
    minimum = query.integer("min_active_months", default=3)
    scenario = query.text("scenario")
    as_of = query.iso_date("as_of")
    query.finish()
    assert months is not None and minimum is not None
    return to_data(api.book.historical_estimates(months, minimum, scenario, as_of))


def historical_estimate_accept_v1(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    request = from_data(AcceptHistoricalEstimate, body, "request")
    return to_data(api.book.accept_historical_estimate(request))


def activity_get(api: Api, _path: Path, query: QueryParams) -> dict:
    start = query.iso_date("start", required=True)
    end = query.iso_date("end", required=True)
    period = query.choice("period", tuple(item.value for item in ReportingPeriod), "month")
    as_of = query.iso_date("as_of")
    query.finish()
    assert start is not None and end is not None
    return to_data(api.book.activity(start, end, ReportingPeriod(period), as_of))


def budget_jars_get(api: Api, _path: Path, query: QueryParams) -> dict:
    start = query.iso_date("start", required=True)
    end = query.iso_date("end", required=True)
    period = query.choice("period", tuple(item.value for item in ReportingPeriod), "month")
    today = query.iso_date("today")
    query.finish()
    assert start is not None and end is not None
    return to_data(api.book.budget_jars(start, end, ReportingPeriod(period), today))


def holdings_get(api: Api, _path: Path, query: QueryParams) -> dict:
    as_of = query.iso_date("as_of")
    query.finish()
    return to_data(api.book.holdings(as_of))


def realized_gains_get(api: Api, _path: Path, query: QueryParams) -> dict:
    year = query.integer("year", minimum=1900, maximum=9999)
    account = query.text("account")
    query.finish()
    return to_data(api.book.realized_gains(year, account))


def tax_year_get(api: Api, _path: Path, query: QueryParams) -> dict:
    year = query.integer("year", minimum=1900, maximum=9999)
    query.finish()
    return to_data(api.book.tax_year(year))


def net_worth_history_get(api: Api, _path: Path, query: QueryParams) -> dict:
    start = query.iso_date("start", required=True)
    end = query.iso_date("end", required=True)
    period = query.choice("period", tuple(item.value for item in ReportingPeriod), "month")
    today = query.iso_date("today")
    query.finish()
    assert start is not None and end is not None
    return to_data(api.book.net_worth_history(start, end, ReportingPeriod(period), today))


def net_worth_change_get(api: Api, _path: Path, query: QueryParams) -> dict:
    start = query.iso_date("start", required=True)
    end = query.iso_date("end", required=True)
    today = query.iso_date("today")
    query.finish()
    assert start is not None and end is not None
    return to_data(api.book.net_worth_change(start, end, today))


@dataclass(frozen=True, slots=True)
class _Explore:
    query: PlanQuery = PlanQuery()
    account: str | None = None
    period_index: int | None = None
    rollover: bool = False


def plan_bounds_get(api: Api, _path: Path, query: QueryParams) -> dict:
    today = query.iso_date("today")
    query.finish()
    return to_data(api.book.plan_bounds(today))


def plan_settings_get(api: Api, _path: Path, query: QueryParams) -> dict:
    default_start = query.iso_date("default_start", required=True)
    default_end = query.iso_date("default_end", required=True)
    query.finish()
    assert default_start is not None and default_end is not None
    return to_data(api.book.plan_settings(default_start, default_end))


def plan_settings_patch(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    return to_data(api.book.save_plan_settings(from_data(PlanSettings, body, "request")))


def plan_v1(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    return to_data(api.book.plan(from_data(PlanQuery, body, "query")))


def expense_explorer_v1(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    request = from_data(_Explore, body, "request")
    found = api.book.expense_explorer(
        request.query, request.account, request.period_index, request.rollover
    )
    return to_data(found)


def reporting_currency_get(api: Api, _path: Path, query: QueryParams) -> dict:
    query.finish()
    return {"currency": api.book.reporting_currency()}


@dataclass(frozen=True, slots=True)
class _Projected:
    scenario: Scenario


@dataclass(frozen=True, slots=True)
class _ProjectedMonth:
    scenario: Scenario
    month_index: int


def projection_v1(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    return to_data(api.book.project(from_data(_Projected, body, "request").scenario))


def projection_month_v1(api: Api, _path: Path, body: dict[str, Any]) -> dict:
    request = from_data(_ProjectedMonth, body, "request")
    return to_data(api.book.projection_month(request.scenario, request.month_index))
