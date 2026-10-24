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

from ..gen.db.verification import BookVerification
from ..gen.engine.activity import ReportingPeriod
from ..gen.engine.category_report import CategoryActivity
from ..gen.engine.dashboard import Dashboard
from ..gen.engine.entry_input import TradeFill
from ..gen.engine.fsa_claim_proposals import FsaClaimProposal
from ..gen.engine.fsa_claim_report import ClaimReport
from ..gen.engine.loans import LoanTerms
from ..gen.engine.payroll import PayChangePlan, PaycheckBreakdown, PayrollTemplate
from ..gen.engine.planning import MatchCandidate, PlannedEvent
from ..gen.engine.projection import Projection, ProjectionMonthDetail
from ..gen.engine.receivables import ReimbursementProposal
from ..gen.engine.reconciliation import ReconciliationSummary
from ..gen.engine.schedule import Occurrence
from ..gen.lib import Scenario, ScheduledTransaction, Transaction
from ..gen.lib.commodity import CommodityPrice
from ..gen.lib.fsa_claim import FsaClaim
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
from ..gen.services.activity import ActivityView
from ..gen.services.assumptions import SaveBaseAssumptions, SaveScenarioAssumptions
from ..gen.services.attachments import AttachmentReport
from ..gen.services.autocomplete import EntryProposal, SuggestEntry
from ..gen.services.book_info import BookSummary
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
    CloseClaim,
    FsaClaimsView,
    FsaYearsView,
    ReopenClaim,
    SaveClaim,
    SavedClaim,
)
from ..gen.services.dashboard_config import DashboardSettings, SaveDashboardConfig
from ..gen.services.entry_input import EntryAccount, EntryAmount, ReadEntryTrade
from ..gen.services.exchange_rates import CurrencyChoice, SaveExchangeRate
from ..gen.services.expense_explorer import ExpenseExplorer
from ..gen.services.gnucash_writeback import ApplyWriteback, WritebackApplied, WritebackPlan
from ..gen.services.import_review import (
    HeldImportChange,
    HeldImportResolution,
    ResolveHeldImports,
)
from ..gen.services.investment_reports import HoldingsView, RealizedGainsView, TaxYearView
from ..gen.services.loans import LoanChoices, LoanPreview, SavedLoan, SaveLoan
from ..gen.services.lots import SaleLots, SetSaleLots
from ..gen.services.net_worth import NetWorthChange, NetWorthHistory
from ..gen.services.payroll import CreatePaycheck, PayChange, PayrollView, SavePayrollTemplate
from ..gen.services.plan import PlanQuery, PlanQueryResult
from ..gen.services.quotes import QuotePlan, QuoteUpdate, SetQuoteSource, StoreQuotes
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
from ..gen.services.register_view import RegisterView
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
from ..gen.services.security_prices import SavedSecurityPrice, SaveSecurityPrice, SecurityQuote
from ..gen.services.tax import SetTaxMarks, TaxMarks

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

    def due_occurrences(
        self, as_of: date | None = None, days: int | None = None
    ) -> tuple[Occurrence, ...]:
        """Occurrences already due, plus those within ``days`` ahead (or each
        schedule's own days in advance)."""
        ...

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

    # ------------------------------------------------------------ entry input

    def suggest_entry(self, request: SuggestEntry) -> EntryProposal:
        """The latest earlier transaction matching what is being typed; writes nothing."""
        ...

    def entry_amount(
        self, text: str, currency: str | None = None, number_format: str = "auto"
    ) -> EntryAmount:
        """Typed text as an amount, arithmetic evaluated, in the currency's smallest unit."""
        ...

    def entry_trade(self, request: ReadEntryTrade) -> TradeFill:
        """A trade's shares, price, and amount, with the third worked out."""
        ...

    def entry_accounts(
        self, text: str, exclude: str | None = None, limit: int = 20
    ) -> tuple[EntryAccount, ...]:
        """Visible, postable accounts that typed text completes to."""
        ...

    def next_num(self, account: str, text: str = "", step: int = 1) -> str:
        """A check number moved ``step`` on; an empty one continues the register."""
        ...

    # -------------------------------------------------------------------- book

    def book_summary(self) -> BookSummary:
        """The book's counts, cash, net worth, and scenarios."""
        ...

    def verify_book(self) -> BookVerification:
        """Physical and logical checks of the book; writes nothing."""
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

    def register_view(self, account: str, limit: int = 250) -> RegisterView:
        """The newest ``limit`` rows of a register with every name, document, and share
        detail they show."""
        ...

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

    # ---------------------------------------------------------------- activity

    def activity(
        self,
        start: date,
        end: date,
        period: ReportingPeriod = ReportingPeriod.MONTH,
        as_of: date | None = None,
    ) -> ActivityView:
        """Plan versus actuals from ``start`` through ``end``, grouped only for display."""
        ...

    # ------------------------------------------------------------- budget jars

    def budget_jars(
        self,
        start: date,
        end: date,
        period: ReportingPeriod = ReportingPeriod.MONTH,
        today: date | None = None,
    ) -> JarsView:
        """Every jar's fills, draws, and level from ``start`` through ``end``."""
        ...

    # ------------------------------------------------------- investment reports

    def holdings(self, as_of: date | None = None) -> HoldingsView:
        """Every security holding's open lots, sales, and cost basis on ``as_of``."""
        ...

    def realized_gains(
        self, year: int | None = None, account: str | None = None
    ) -> RealizedGainsView:
        """Every sale (of one account, or all) with the lots it took, and yearly totals."""
        ...

    def tax_year(self, year: int | None = None) -> TaxYearView:
        """One calendar year's gains, tax-relevant totals, and income by source.

        Without ``year``, the latest year with a transaction.
        """
        ...

    # -------------------------------------------------------------- receivables

    def receivables_view(self, as_of: date | None = None) -> ReceivablesView:
        """Every receivable's standing with its links, proposals, and form choices."""
        ...

    def receivable_candidates(
        self, receivable: str | None = None
    ) -> tuple[tuple[CandidateLine, ...], tuple[CandidateLine, ...]]:
        """Recent unlinked splits a receivable could take: (costs, credits)."""
        ...

    def find_receivable(self, reference: str) -> Receivable:
        """A receivable, as stored, by handle or unique handle prefix."""
        ...

    def save_receivable(self, request: SaveReceivable) -> Receivable: ...

    def link_receivable_split(
        self, receivable: str, transaction: str, split: str, role: str
    ) -> Receivable:
        """Link a split as the receivable's ``expense`` or its ``reimbursement``."""
        ...

    def detach_receivable_split(
        self, receivable: str, transaction: str, split: str
    ) -> Receivable: ...

    def mark_disputed(self, receivable: str, disputed_on: date, note: str = "") -> Receivable: ...

    def clear_dispute(self, receivable: str) -> Receivable: ...

    def record_write_off(self, request: RecordWriteOff) -> Receivable: ...

    def delete_receivable(self, receivable: str) -> str: ...

    def reimbursement_proposals(
        self, as_of: date | None = None, account: str | None = None
    ) -> tuple[ReimbursementProposal, ...]: ...

    def reimbursable_costs(self, start: date, end: date) -> tuple[CategoryActivity, ...]:
        """Each expense category's gross and net cost in a range, as the Plan shows it."""
        ...

    def accept_reimbursements(
        self, chosen: tuple[tuple[str, str, str], ...]
    ) -> AcceptedReimbursements:
        """Link the chosen (receivable, transaction, split) proposals still on offer."""
        ...

    def set_reimbursement_override(self, request: SetReimbursementOverride) -> Scenario:
        """Change, in one scenario, what a payer is expected to reimburse and when."""
        ...

    # ---------------------------------------------------------------- scenarios

    def scenarios(self) -> list[Scenario]:
        """Every scenario as stored."""
        ...

    def find_scenario(self, reference: str) -> Scenario:
        """A scenario, as stored, by handle or exact name."""
        ...

    def save_scenario(self, request: SaveScenario) -> SavedScenario: ...

    def save_scenario_assumptions(self, request: SaveScenarioAssumptions) -> SavedScenario:
        """Save a scenario after checking its projection assumptions."""
        ...

    def duplicate_scenario(self, request: DuplicateScenario) -> SavedScenario: ...

    def delete_scenario(self, scenario: str) -> SavedScenario: ...

    def suppress_scenario_schedule(self, request: SuppressScenarioSchedule) -> SavedScenario:
        """Leave one baseline schedule out of a scenario."""
        ...

    def drawdown_accounts(self) -> tuple[list[str], list[str]]:
        """Accounts a drawdown can withdraw from, and pay into."""
        ...

    def save_drawdown(self, request: SaveDrawdown) -> Drawdown: ...

    def remove_drawdown(self, scenario: str, drawdown: str) -> Drawdown: ...

    def base_assumptions(self) -> Assumptions:
        """The book's base projection assumptions."""
        ...

    def save_base_assumptions(self, request: SaveBaseAssumptions) -> Assumptions:
        """Save the book's base projection assumptions."""
        ...

    def save_assumption_period(
        self, scenario: str, period: AssumptionPeriod, index: int | None = None
    ) -> SavedScenario:
        """Add (or, at ``index``, replace) one dated assumption regime."""
        ...

    def delete_assumption_period(self, scenario: str, index: int) -> SavedScenario: ...

    # ------------------------------------------------------------------ payroll

    def payroll(self, as_of: date | None = None) -> PayrollView:
        """Paychecks as of ``as_of`` (default today), templates, and form choices."""
        ...

    def paycheck(self, schedule: str, as_of: date | None = None) -> PaycheckBreakdown:
        """One schedule read as a paycheck."""
        ...

    def template_from_schedule(
        self, schedule: str, name: str, as_of: date | None = None
    ) -> PayrollTemplate:
        """A template describing a paycheck; nothing is saved."""
        ...

    def save_payroll_template(self, request: SavePayrollTemplate) -> PayrollTemplate: ...

    def delete_payroll_template(self, name: str) -> PayrollTemplate: ...

    def create_paycheck(self, request: CreatePaycheck) -> SavedSchedule:
        """Add a paycheck schedule from a saved template."""
        ...

    def preview_pay_change(self, request: PayChange) -> PayChangePlan:
        """The paycheck after a dated pay change; writes nothing."""
        ...

    def apply_pay_change(self, request: PayChange) -> PayChangePlan: ...

    # --------------------------------------------------------------- FSA claims

    def fsa_claims(self, as_of: date | None = None) -> FsaClaimsView:
        """Every claim's links and standing, and the splits a claim could link."""
        ...

    def find_fsa_claim(self, reference: str) -> FsaClaim:
        """A claim, as stored, by handle or unique handle prefix."""
        ...

    def save_fsa_claim(self, request: SaveClaim) -> SavedClaim: ...

    def delete_fsa_claim(self, claim: str) -> SavedClaim: ...

    def close_fsa_claim(self, request: CloseClaim) -> SavedClaim: ...

    def reopen_fsa_claim(self, request: ReopenClaim) -> SavedClaim: ...

    def fsa_claim_report(self, query: ClaimReportQuery) -> ClaimReport:
        """Claims grouped and totalled, with the reasons any needs attention."""
        ...

    def benefit_years(self, as_of: date | None = None) -> FsaYearsView:
        """Open and recently closed FSA benefit years, with how each was used."""
        ...

    def claim_link_proposals(self, account: str | None = None) -> tuple[FsaClaimProposal, ...]:
        """FSA movements that clearly belong on one claim; writes nothing."""
        ...

    def accept_claim_links(self, chosen: tuple[tuple[str, str, str], ...]) -> AcceptedClaimLinks:
        """Link the chosen (claim, transaction, split) proposals still on offer."""
        ...

    # ------------------------------------------------------------- plan review

    def review_queue(self) -> tuple[PendingActual, ...]:
        """Actuals not yet resolved against the plan, oldest first."""
        ...

    def review_detail(self, transaction: str) -> ReviewDetail:
        """The planned items one pending actual could be, why, and its FSA choices."""
        ...

    def match_review(self, request: ReviewOccurrence) -> ReviewMutation: ...

    def reject_review(self, request: ReviewOccurrence) -> ReviewMutation: ...

    def skip_review(self, request: ReviewOccurrence) -> ReviewMutation: ...

    def mark_review_unexpected(self, transaction: str) -> ReviewMutation: ...

    def attach_review_claim(self, request: ReviewClaimAttachment) -> ReviewMutation: ...

    def plan_occurrence(self, key: str) -> PlannedEvent:
        """One planned occurrence by its stable key."""
        ...

    def unresolved_occurrences(self, start: date, end: date) -> tuple[PlannedEvent, ...]: ...

    def match_candidates(
        self, transaction: str, window_days: int = 7
    ) -> tuple[MatchCandidate, ...]:
        """Unresolved occurrences that could explain one actual, best first."""
        ...

    # ------------------------------------------------------------ import review

    def held_imports(self) -> tuple[HeldImportChange, ...]:
        """GnuCash changes held back from transactions reconciled here."""
        ...

    def resolve_held_imports(self, request: ResolveHeldImports) -> HeldImportResolution: ...

    # ------------------------------------------------------------ lots and tax

    def sale_lots(self, transaction: str, split: str) -> SaleLots:
        """A sale, the lots open just before it, and the lots it names."""
        ...

    def set_sale_lots(self, request: SetSaleLots) -> SaleLots: ...

    def tax_marks(self) -> TaxMarks:
        """Every account and tag with its tax mark."""
        ...

    def set_tax_marks(self, request: SetTaxMarks) -> TaxMarks: ...

    # ------------------------------------------------------------------ quotes

    def quote_plan(self) -> QuotePlan:
        """Every commodity's quote source, and what a fetch would ask for."""
        ...

    def store_quotes(self, request: StoreQuotes) -> QuoteUpdate:
        """Store quotes the caller fetched; the server never reaches the network."""
        ...

    def set_quote_source(self, request: SetQuoteSource) -> str: ...

    # ------------------------------------------------------------------- loans

    def loan_choices(self) -> LoanChoices:
        """The accounts a new loan may use, and the book's amount precision."""
        ...

    def preview_loan(self, terms: LoanTerms) -> LoanPreview:
        """A loan's payment and first payments after checking its terms; writes nothing."""
        ...

    def save_loan(self, request: SaveLoan) -> SavedLoan: ...

    # ---------------------------------------------------------- exchange rates

    def currencies(self) -> tuple[CurrencyChoice, ...]: ...

    def find_currency(self, reference: str) -> CurrencyChoice:
        """A currency by exact handle or unambiguous code."""
        ...

    def save_exchange_rate(self, request: SaveExchangeRate) -> CommodityPrice:
        """Save a dated manual quote; one for the same pair and date is replaced."""
        ...

    def securities(self) -> tuple[SecurityQuote, ...]:
        """Every security with its latest exact dated price."""
        ...

    def save_security_price(self, request: SaveSecurityPrice) -> SavedSecurityPrice:
        """Save a dated manual quote, creating the security when none is named."""
        ...

    # ------------------------------------------------------ GnuCash write-back

    def writeback_preview(self) -> WritebackPlan:
        """Every local change that could be written to GnuCash, and every one that
        cannot; writes nothing."""
        ...

    def writeback_keep_backups(self) -> int: ...

    def set_writeback_keep_backups(self, keep: int) -> int:
        """How many backups of the GnuCash book a write-back keeps (1 to 1000)."""
        ...

    def apply_writeback(self, request: ApplyWriteback) -> WritebackApplied:
        """Write the chosen previewed changes to GnuCash, after a backup, and prove
        they read back as BreadSched has them."""
        ...

    # ------------------------------------------------------- dashboard settings

    def dashboard(
        self,
        as_of: date | None = None,
        liquidity_days: int | None = None,
        emergency_months: int | None = None,
    ) -> Dashboard:
        """The Dashboard on ``as_of``, optionally over other (unsaved) horizons."""
        ...

    def dashboard_settings(self) -> DashboardSettings:
        """The Dashboard's groups, with their members, and its horizons."""
        ...

    def save_dashboard_settings(self, request: SaveDashboardConfig) -> DashboardSettings: ...

    # ------------------------------------------------------------- attachments

    def attachment_report(
        self, missing_only: bool = False, transaction: str | None = None
    ) -> AttachmentReport:
        """Linked documents (all, or one transaction's) checked against the file system."""
        ...

    def link_attachment(self, transaction: str, location: str) -> Transaction:
        """Link a web address, an absolute path, or a path inside the attachment folder."""
        ...

    def attach_file(self, transaction: str, path: str, copy: bool = True) -> Transaction:
        """Link a file as the server sees it, copying it into the folder by default."""
        ...

    def unlink_attachment(self, transaction: str, location: str) -> Transaction: ...

    def relink_attachment(self, transaction: str, location: str, new: str) -> Transaction: ...

    def set_attachment_folder(self, folder: str) -> str:
        """Where relative locations resolve; empty restores the default. Returns it."""
        ...

    def set_source_link_folder(self, folder: str) -> str:
        """GnuCash's path head for linked files; empty restores the home folder."""
        ...

    # ------------------------------------------------------- plan and spending

    def reporting_currency(self) -> str:
        """The reporting currency's code for a chart or heading, or "" when it has none."""
        ...

    def plan(self, query: PlanQuery) -> PlanQueryResult:
        """One complete Plan calculation."""
        ...

    def expense_explorer(
        self,
        query: PlanQuery,
        account: str | None = None,
        period_index: int | None = None,
        rollover: bool = False,
    ) -> ExpenseExplorer:
        """The Plan's categories over time, optionally explaining one category cell."""
        ...

    # -------------------------------------------------------------- projection

    def project(self, scenario: Scenario) -> Projection:
        """Project ``scenario`` (saved or a draft) month by month; writes nothing."""
        ...

    def projection_month(self, scenario: Scenario, month_index: int) -> ProjectionMonthDetail:
        """Explain one month of ``scenario``'s projection."""
        ...

    # --------------------------------------------------------------- net worth

    def net_worth_history(
        self,
        start: date,
        end: date,
        period: ReportingPeriod = ReportingPeriod.MONTH,
        today: date | None = None,
    ) -> NetWorthHistory:
        """Market-valued assets and debts at each period end through ``today``."""
        ...

    def net_worth_change(self, start: date, end: date, today: date | None = None) -> NetWorthChange:
        """The postings behind a net worth change, and the market movement beside them."""
        ...
