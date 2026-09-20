"""A locally hosted web interface.

Built on :mod:`http.server` with no framework, for the same reason the core has no
third-party dependencies: a finance tool that people run on their own machine for
years should not rot because a web framework moved on. The whole surface is a small
JSON API plus one static page.

It binds to loopback only and still treats browser requests as untrusted input.
Every API request carries an unguessable per-server token, writes must be JSON, and
Host/Origin checks reject cross-site and DNS-rebinding requests.
"""

from __future__ import annotations

from calendar import monthrange
from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from typing import Literal, cast

from ..gen.db.sqlite import DbSQLite
from ..gen.engine import (
    activity,
    estimates,
    fsa_claims,
    ledger,
    loans,
    planning,
    projection,
    reconciliation,
    schedule,
    valuation,
)
from ..gen.engine.activity import PlanMeasure, PlanSettings
from ..gen.engine.currency import reporting_fraction
from ..gen.lib import (
    Account,
    AccountClass,
    AccountType,
    Amount,
    AssumptionPeriod,
    Assumptions,
    FsaFundingYear,
    InvestmentActivityKind,
    Money,
    PeriodType,
    PlanningFlowKind,
    PlanningResolution,
    Rate,
    Recurrence,
    Scenario,
    ScenarioSchedule,
    ScheduledAmountChange,
    ScheduledMonthAmount,
    ScheduledOccurrenceAdjustment,
    ScheduledSplitAmountChange,
    ScheduledTransaction,
    ScheduleGrowthPolicy,
    Transaction,
    WeekendAdjust,
    scheduled_occurrence_preview,
)
from ..gen.plug import (
    remembered_import_source,
)
from ..gen.services import (
    ClaimAllocationInput,
    ClaimAttachment,
    ClaimInput,
    ClaimLinkInput,
    ClaimRejectionInput,
    DeleteAssumptionPeriod,
    DeleteClaim,
    DeleteScenario,
    DeleteSchedule,
    DuplicateScenario,
    DuplicateSchedule,
    FixedScheduleInput,
    FixedSplitInput,
    FormulaScheduleInput,
    ImportBook,
    PlanQuery,
    ReconciliationAction,
    ReviewClaimAttachment,
    ReviewOccurrence,
    ReviewTransaction,
    SaveAccount,
    SaveAssumptionPeriod,
    SaveBaseAssumptions,
    SaveClaim,
    SaveFixedScenarioSchedule,
    SaveFixedSchedule,
    SaveLoan,
    SaveScenarioAssumptions,
    SaveTransaction,
    ServiceError,
    StartReconciliation,
    SuppressScenarioSchedule,
    TransactionInput,
    TransactionSplitInput,
    UpdateReconciliation,
    attach_review_claim,
    cancel_reconciliation,
    complete_reconciliation,
    delete_assumption_period,
    delete_claim,
    delete_scenario,
    delete_schedule,
    duplicate_scenario,
    duplicate_schedule,
    import_book,
    mark_review_unexpected,
    match_review,
    query_plan,
    reject_review,
    reopen_reconciliation,
    save_account,
    save_assumption_period,
    save_base_assumptions,
    save_claim,
    save_fixed_scenario_schedule,
    save_fixed_schedule,
    save_formula_schedule,
    save_loan,
    save_scenario_assumptions,
    save_transaction,
    skip_review,
    start_reconciliation,
    suppress_scenario_schedule,
    transaction_currency,
    update_reconciliation,
    validate_loan,
)
from ..gen.utils.amount_input import NumberFormat, parse_user_amount
from ..presentation import service_error_message
from ..versioning import version_details
from .resources import ResourceError

__all__ = ["serve", "build_handler", "api"]


class Api:
    """The JSON surface. Every method returns plain data, never a response object."""

    @staticmethod
    def _service_resource_error(error: ServiceError) -> ResourceError:
        status = 404 if error.code.endswith(".not_found") else 400
        return ResourceError(
            status,
            error.code,
            error.fields,
            service_error_message(error),
        )

    @staticmethod
    def _input_money(payload: dict, raw: object) -> Money:
        """Parse a browser-entered amount using the browser decimal convention."""
        if isinstance(raw, (list, tuple)) and len(raw) == 2:
            return Money(int(raw[0]), int(raw[1]))
        number_format = str(payload.get("number_format") or "auto")
        if number_format not in {"auto", "dot", "comma"}:
            raise ValueError("invalid number format")
        selected = cast(NumberFormat | Literal["auto"], number_format)
        return Money(parse_user_amount(str(raw).strip(), selected))

    def __init__(self, db: DbSQLite) -> None:
        self.db = db

    # ---------------------------------------------------------------- reading

    def dashboard(self, liquidity_days: int | None, emergency_months: int | None) -> dict:
        """The overview: groups, liquidity, pending bills, and expected income.

        Computed by the same engine the desktop dashboard uses, so the two cannot
        disagree about a household's position.
        """
        from ..gen.engine import dashboard as engine

        config = engine.DashboardConfig.load(self.db)
        if liquidity_days:
            config.liquidity_days = liquidity_days
        if emergency_months:
            config.emergency_months = emergency_months
        board = engine.build(self.db, config)

        return {
            "summary": _plain(board.summary()),
            "config": {
                "liquidity_days": config.liquidity_days,
                "emergency_months": config.emergency_months,
                "groups": [group.serialize() for group in config.groups],
                "accounts": [
                    {"handle": account.handle, "name": self.db.full_name(account)}
                    for account in self.db.iter_accounts()
                    if not account.is_root
                ],
            },
            "groups": [
                {
                    "name": group.name,
                    "path": group.path,
                    "depth": group.depth,
                    "heading": group.heading,
                    "note": group.note,
                    "kind": group.kind,
                    "total": str(group.total.to_decimal()),
                    "value": (str(group.value.to_decimal()) if group.value is not None else None),
                    "debt": str(group.debt.to_decimal()) if group.debt is not None else None,
                    "equity": (
                        str(group.equity.to_decimal()) if group.equity is not None else None
                    ),
                    "loan_to_value": (
                        float(group.loan_to_value) if group.loan_to_value is not None else None
                    ),
                    "loan_end": group.loan_end.isoformat() if group.loan_end is not None else None,
                    "accounts": [
                        {
                            "name": account.name,
                            "balance": (
                                str(account.total.to_decimal())
                                if account.total is not None
                                else None
                            ),
                            "source": account.source,
                            "note": account.note,
                        }
                        for account in group.accounts
                    ],
                }
                for group in board.groups
            ],
            "bills": [
                {
                    "name": item.name,
                    "next_due": item.next_due.isoformat(),
                    "days_until": item.days_until(board.as_of),
                    "cycle_months": float(item.cycle_months),
                    "amount": str(item.amount.to_decimal()),
                    "monthly": None if item.generated else str(item.monthly.to_decimal()),
                    "annual": None if item.generated else str(item.annual.to_decimal()),
                    "hold": None if item.income else str(item.held.to_decimal()),
                    "reserve_for": item.reserve_for.isoformat() if item.reserve_for else None,
                    "estimate": item.estimate,
                    "generated": item.generated,
                    "schedule": item.schedule.handle if item.schedule is not None else None,
                    "account": item.account,
                }
                for item in board.bills
            ],
            "income": [
                {
                    "name": item.name,
                    "next_due": item.next_due.isoformat(),
                    "days_until": item.days_until(board.as_of),
                    "cycle_months": float(item.cycle_months),
                    "amount": str(item.amount.to_decimal()),
                    "monthly": str(item.monthly.to_decimal()),
                    "annual": str(item.annual.to_decimal()),
                    "schedule": item.schedule.handle if item.schedule is not None else None,
                }
                for item in board.incomes
            ],
        }

    def fsa_dashboard(self) -> dict:
        """FSA benefit-year availability and open healthcare claims."""
        from ..gen.engine import fsa

        return {
            "claims": [
                {
                    "handle": summary.claim.handle,
                    "service_date": summary.claim.service_date.isoformat(),
                    "provider": summary.claim.provider,
                    "status": summary.status.label,
                    "paid": str(summary.net_paid.to_decimal()),
                    "reimbursed": str(summary.reimbursed.to_decimal()),
                    "rejected": str(summary.rejected.to_decimal()),
                    "remaining": str(summary.remaining_reimbursable.to_decimal()),
                }
                for claim in fsa_claims.iter_claims(self.db)
                for summary in [fsa_claims.claim_summary(self.db, claim)]
                if summary.status is not fsa_claims.FsaClaimStatus.FULLY_REIMBURSED
            ],
            "years": [
                {
                    "account": self.db.full_name(status.account),
                    "account_handle": status.account.handle,
                    "start": status.year.start.isoformat(),
                    "through": status.year.through.isoformat(),
                    "runout_through": (
                        status.year.runout_through.isoformat()
                        if status.year.runout_through
                        else None
                    ),
                    "election": str(status.year.election.to_decimal()),
                    "funded": str(status.funded.to_decimal()),
                    "used": str(status.used.to_decimal()),
                    "remaining": str(status.remaining.to_decimal()),
                    "overage": str(status.overage.to_decimal()),
                    "forfeited": str(status.forfeited.to_decimal()),
                    "phase": status.phase,
                }
                for status in fsa.dashboard_statuses(self.db)
            ],
        }

    def dashboard_config_save(self, payload: dict) -> dict:
        """Persist the same group paths and account selections edited by GTK."""
        from ..gen.engine import dashboard as engine

        allowed_kinds = {"liquid", "retirement", "asset", "property", "liability"}
        groups: list[engine.GroupConfig] = []
        raw_groups = payload.get("groups", [])
        if not isinstance(raw_groups, list):
            raise ValueError("dashboard groups must be a list")
        for raw in raw_groups:
            if not isinstance(raw, dict):
                raise ValueError("each dashboard group must be an object")
            name = str(raw.get("name", "")).strip()
            if not name:
                raise ValueError("dashboard group name cannot be empty")
            kind = str(raw.get("kind", "asset"))
            if kind not in allowed_kinds:
                raise ValueError("choose a valid dashboard group kind")
            handles: list[str] = []
            raw_handles = raw.get("accounts", [])
            if not isinstance(raw_handles, list):
                raise ValueError("dashboard group accounts must be a list")
            for raw_handle in raw_handles:
                handle = str(raw_handle)
                account = self.db.get_account(handle)
                if account is None or account.is_root:
                    raise ValueError("dashboard group references an unknown account")
                if handle not in handles:
                    handles.append(handle)
            groups.append(engine.GroupConfig(name, handles, kind))

        config = engine.DashboardConfig.load(self.db)
        config.groups = groups
        if "liquidity_days" in payload:
            config.liquidity_days = min(365, max(1, int(payload["liquidity_days"])))
        if "emergency_months" in payload:
            config.emergency_months = min(36, max(1, int(payload["emergency_months"])))
        config.save(self.db)
        return {
            "groups": [group.serialize() for group in config.groups],
            "liquidity_days": config.liquidity_days,
            "emergency_months": config.emergency_months,
        }

    def summary(self) -> dict:
        counts = self.db.summary()
        return {
            "book": self.db.path,
            "accounts": counts["account"],
            "transactions": counts["txn"],
            "cash": ledger.cash_on_hand(self.db),
            "net_worth": valuation.net_worth(self.db),
            "scenarios": [s.name for s in self.db.iter_scenarios()],
        }

    def verify(self) -> dict[str, object]:
        """Run physical and logical checks without modifying the open book."""
        report = self.db.verification_report()
        payload = report.as_dict()
        payload.update(version_details(native_schema_version=report.native_schema_version))
        return payload

    def accounts(self) -> list[dict]:
        """The chart of accounts as a flat list carrying its own depth."""
        rows: list[dict] = []

        def walk(parent: str | None, depth: int) -> None:
            for account in self.db.child_accounts(parent):
                valued = valuation.account_value(self.db, account)
                recursive = valuation.value_recursive(self.db, account)
                rows.append(
                    {
                        "handle": account.handle,
                        "name": account.name,
                        "full_name": self.db.full_name(account),
                        "type": account.atype.value,
                        "class": account.account_class.value,
                        "placeholder": account.placeholder,
                        "hidden": account.hidden,
                        "code": account.code,
                        "description": account.description,
                        "notes": account.notes,
                        "source_notes": account.source_notes,
                        "commodity_scu": account.commodity_scu,
                        "emergency_fund_eligible": account.emergency_fund_eligible,
                        "emergency_fund_included": account.emergency_fund_included,
                        "pays_in_full": account.pays_in_full,
                        "usual_payment": (
                            str(account.usual_payment.to_decimal())
                            if account.usual_payment is not None
                            else None
                        ),
                        "payment_day": account.payment_day,
                        "card_payment_account": account.card_payment_account,
                        "source_type": (
                            account.source_type
                            or (account.source_atype.value if account.source_atype else None)
                        ),
                        "source_guid": account.source_guid,
                        "source_fields": [field.serialize() for field in account.source_fields],
                        "fsa_years": [
                            {
                                "start": year.start.isoformat(),
                                "through": year.through.isoformat(),
                                "election": str(year.election.to_decimal()),
                                "runout_through": (
                                    year.runout_through.isoformat() if year.runout_through else None
                                ),
                            }
                            for year in account.fsa_years
                        ],
                        "depth": depth,
                        "balance": recursive,
                        "book_balance": ledger.balance_recursive(self.db, account.handle),
                        "own_balance": ledger.balance(self.db, account.handle),
                        "valuation_source": valued.source,
                        "quantity": valued.quantity,
                        "price": valued.price,
                        "price_date": valued.price_date,
                        "commodity": (
                            valued.commodity.mnemonic if valued.commodity is not None else None
                        ),
                        "currency": (
                            valued.currency.mnemonic if valued.currency is not None else None
                        ),
                    }
                )
                walk(account.handle, depth + 1)

        root = self.db.root_account()
        walk(root.handle if root else None, 0)
        return rows

    def commodities(self) -> dict:
        """Securities, currencies, and their latest exact dated prices."""
        currencies = [item for item in self.db.iter_commodities() if item.is_currency]
        securities = []
        for item in self.db.iter_commodities():
            if item.is_currency:
                continue
            latest = valuation.latest_price(self.db, item)
            securities.append(
                {
                    "handle": item.handle,
                    "namespace": item.namespace,
                    "mnemonic": item.mnemonic,
                    "fullname": item.fullname,
                    "fraction": item.fraction,
                    "price": latest.value if latest is not None else None,
                    "price_date": latest.quote_date if latest is not None else None,
                    "currency": latest.currency if latest is not None else None,
                }
            )
        return {
            "currencies": [
                {"handle": item.handle, "mnemonic": item.mnemonic, "fullname": item.fullname}
                for item in currencies
            ],
            "securities": securities,
        }

    def commodity_price_save(self, payload: dict) -> dict:
        """Create a security when needed and add or replace one dated quote."""
        security_handle = str(payload.get("commodity") or "").strip() or None
        currency_handle = str(payload.get("currency") or "").strip()
        try:
            quote_date = date.fromisoformat(str(payload.get("date") or ""))
        except ValueError as exc:
            raise ValueError("quote date is invalid") from exc
        try:
            value = self._input_money(payload, payload.get("price") or "0")
        except (ValueError, ArithmeticError) as exc:
            raise ValueError("price must be a valid number") from exc
        if value <= 0:
            raise ValueError("price must be greater than zero")
        try:
            fraction = int(payload.get("fraction") or 10000)
        except (TypeError, ValueError) as exc:
            raise ValueError("security fraction must be a whole number") from exc
        security, price = valuation.save_security_price(
            self.db,
            security_handle=security_handle,
            currency_handle=currency_handle,
            quote_date=quote_date,
            value=value,
            mnemonic=str(payload.get("mnemonic") or ""),
            fullname=str(payload.get("fullname") or ""),
            namespace=str(payload.get("namespace") or "FUND"),
            fraction=fraction,
        )
        currency = self.db.get_commodity(price.currency)
        assert currency is not None
        return {
            "commodity": security.handle,
            "mnemonic": security.mnemonic,
            "currency": currency.mnemonic,
            "date": quote_date,
            "price": value,
        }

    def account_type_save(self, payload: dict) -> dict:
        handle = str(payload.get("handle", ""))
        account = self.db.get_account(handle)
        if account is None:
            raise KeyError(handle)
        source = Account.from_dict(account.serialize())
        raw_type = str(payload.get("type", ""))
        try:
            account_type = AccountType(raw_type.strip().upper())
        except ValueError:
            raise ValueError("choose a valid account type") from None
        if account_type in {AccountType.ROOT, AccountType.TECHNICAL}:
            raise ValueError("choose a user account type")
        account.atype = account_type
        if account_type is not AccountType.CREDIT:
            account.card_payment_account = None
        result = save_account(
            self.db,
            SaveAccount(account, existing_handle=account.handle, source=source),
        )
        if not result.ok:
            raise self._service_resource_error(result.errors[0])
        return {"handle": account.handle, "type": account.atype.value}

    def account_emergency_fund_save(self, payload: dict) -> dict:
        handle = str(payload.get("handle", ""))
        account = self.db.get_account(handle)
        if account is None:
            raise KeyError(handle)
        source = Account.from_dict(account.serialize())
        if not account.emergency_fund_eligible:
            raise ValueError("this account type is always excluded from the emergency fund")
        account.emergency_fund_override = bool(payload.get("included"))
        result = save_account(
            self.db,
            SaveAccount(account, existing_handle=account.handle, source=source),
        )
        if not result.ok:
            raise self._service_resource_error(result.errors[0])
        return {
            "handle": account.handle,
            "emergency_fund_included": account.emergency_fund_included,
        }

    def account_card_save(self, payload: dict) -> dict:
        """Persist the account-owned definition of a credit-card payment."""
        handle = str(payload.get("handle", ""))
        account = self.db.get_account(handle)
        if account is None:
            raise KeyError(handle)
        source = Account.from_dict(account.serialize())
        if account.atype is not AccountType.CREDIT:
            raise ValueError("card payment settings require a Credit card account")
        raw_full = payload.get("pays_in_full", True)
        if not isinstance(raw_full, bool):
            raise ValueError("pays_in_full must be true or false")
        raw_day = payload.get("payment_day")
        payment_day = int(raw_day) if raw_day is not None and raw_day != "" else None
        if payment_day is not None and not 1 <= payment_day <= 28:
            raise ValueError("payment day must be between 1 and 28")
        raw_usual = str(payload.get("usual_payment") or "").strip()
        usual_payment = self._input_money(payload, raw_usual) if raw_usual else None
        if usual_payment is not None and usual_payment <= 0:
            raise ValueError("usual payment must be positive")
        if not raw_full and usual_payment is None:
            raise ValueError("a card carrying a balance needs a usual payment")
        payment_handle = str(payload.get("payment_account") or "") or None
        if payment_handle is not None:
            payment = self.db.get_account(payment_handle)
            if payment is None or not payment.atype.is_cash_like or payment.placeholder:
                raise ValueError("paid from must be a Bank or Cash account")
            if payment.hidden and payment.handle != account.card_payment_account:
                raise ValueError("a hidden account cannot fund a new card payment")

        account.pays_in_full = raw_full
        account.usual_payment = usual_payment
        account.payment_day = payment_day
        account.card_payment_account = payment_handle
        result = save_account(
            self.db,
            SaveAccount(account, existing_handle=account.handle, source=source),
        )
        if not result.ok:
            raise self._service_resource_error(result.errors[0])
        return {
            "handle": account.handle,
            "pays_in_full": account.pays_in_full,
            "usual_payment": (
                str(account.usual_payment.to_decimal())
                if account.usual_payment is not None
                else None
            ),
            "payment_day": account.payment_day,
            "card_payment_account": account.card_payment_account,
        }

    def loan_options(self) -> dict:
        """Return the accounts accepted by the shared loan creator."""

        def choices(predicate) -> list[dict[str, str]]:
            return [
                {"handle": account.handle, "name": self.db.full_name(account)}
                for account in sorted(self.db.iter_accounts(), key=self.db.full_name)
                if predicate(account)
                and not account.is_root
                and not account.placeholder
                and not account.hidden
            ]

        return {
            "liabilities": choices(lambda account: account.account_class is AccountClass.LIABILITY),
            "expenses": choices(lambda account: account.account_class is AccountClass.EXPENSE),
            "payment_accounts": choices(lambda account: account.atype.is_cash_like),
        }

    def _loan_terms(self, payload: dict) -> loans.LoanTerms:
        """Parse untrusted web input into the shared typed loan contract."""
        name = str(payload.get("name") or "").strip()
        principal = self._input_money(payload, payload.get("principal", ""))
        try:
            annual_rate = Decimal(str(payload.get("annual_rate") or "0")) / Decimal(100)
            years = int(payload.get("years") or 0)
            start = date.fromisoformat(str(payload.get("start") or ""))
        except (ValueError, ArithmeticError) as exc:
            raise ValueError("enter a valid rate, term, and first-payment date") from exc
        return loans.LoanTerms(
            name=name,
            principal=principal,
            annual_rate=annual_rate,
            years=years,
            start=start,
            liability=str(payload.get("liability") or ""),
            interest_account=str(payload.get("interest_account") or ""),
            payment_account=str(payload.get("payment_account") or ""),
            fraction=reporting_fraction(self.db),
        )

    def loan_preview(self, payload: dict) -> dict:
        terms = self._loan_terms(payload)
        errors = validate_loan(self.db, terms)
        if errors:
            raise self._service_resource_error(errors[0])
        return {
            "payment": terms.payment(),
            "total_interest": terms.total_interest(),
            "rows": loans.schedule_preview(terms, rows=12),
        }

    def loan_save(self, payload: dict) -> dict:
        terms = self._loan_terms(payload)
        opening_balance = payload.get("opening_balance", True)
        if not isinstance(opening_balance, bool):
            raise ValueError("opening_balance must be true or false")
        result = save_loan(self.db, SaveLoan(terms, opening_balance=opening_balance))
        if not result.ok:
            raise self._service_resource_error(result.errors[0])
        saved = result.value
        assert saved is not None
        return {
            "handle": saved.handle,
            "name": saved.name,
            "payment": saved.payment,
        }

    def account_fsa_years_save(self, payload: dict) -> dict:
        handle = str(payload.get("handle", ""))
        account = self.db.get_account(handle)
        if account is None:
            raise KeyError(handle)
        source = Account.from_dict(account.serialize())
        if account.atype is not AccountType.FSA:
            raise ValueError("FSA funding years require an FSA account")
        years: list[FsaFundingYear] = []
        for raw in payload.get("years", []):
            runout = str(raw.get("runout_through", "")).strip()
            years.append(
                FsaFundingYear(
                    start=date.fromisoformat(str(raw["start"])),
                    through=date.fromisoformat(str(raw["through"])),
                    election=self._input_money(payload, raw["election"]),
                    runout_through=date.fromisoformat(runout) if runout else None,
                )
            )
        years.sort(key=lambda year: year.start)
        for earlier, later in zip(years, years[1:], strict=False):
            if later.start <= earlier.through:
                raise ValueError("FSA funding years cannot overlap")
        account.fsa_years = years
        result = save_account(
            self.db,
            SaveAccount(account, existing_handle=account.handle, source=source),
        )
        if not result.ok:
            raise self._service_resource_error(result.errors[0])
        return {"handle": account.handle, "years": [year.serialize() for year in years]}

    def fsa_claims(self) -> dict:
        rows = []
        for claim in fsa_claims.iter_claims(self.db):
            summary = fsa_claims.claim_summary(self.db, claim)
            rows.append(
                {
                    "handle": claim.handle,
                    "service_date": claim.service_date.isoformat(),
                    "provider": claim.provider,
                    "description": claim.description,
                    "eob_responsibility": (
                        str(claim.eob_responsibility.to_decimal())
                        if claim.eob_responsibility is not None
                        else None
                    ),
                    "paid": str(summary.paid.to_decimal()),
                    "provider_refunds": str(summary.refunds.to_decimal()),
                    "net_paid": str(summary.net_paid.to_decimal()),
                    "reimbursed": str(summary.reimbursed.to_decimal()),
                    "rejected": str(summary.rejected.to_decimal()),
                    "remaining": str(summary.remaining_reimbursable.to_decimal()),
                    "status": summary.status.value,
                    "status_label": summary.status.label,
                    "payments": [link.serialize() for link in claim.payments],
                    "refunds": [link.serialize() for link in claim.refunds],
                    "allocations": [
                        {
                            **allocation.serialize(),
                            "account_name": (
                                self.db.full_name(allocation.account)
                                if self.db.get_account(allocation.account)
                                else allocation.account
                            ),
                        }
                        for allocation in claim.allocations
                    ],
                }
            )
        return {"claims": rows, "candidates": self.fsa_claim_candidates()}

    def fsa_claim_candidates(self) -> dict:
        payments = []
        refunds = []
        reimbursements = []
        fsa_years = [
            year
            for account in self.db.iter_accounts()
            if account.atype is AccountType.FSA
            for year in account.fsa_years
        ]
        candidate_start = min((year.start for year in fsa_years), default=None)
        for transaction in self.db.iter_transactions():
            if candidate_start is not None and transaction.post_date < candidate_start:
                continue
            for split in transaction.splits:
                account = self.db.get_account(split.account)
                if account is None:
                    continue
                row = {
                    "transaction": transaction.handle,
                    "split": split.handle,
                    "date": transaction.post_date.isoformat(),
                    "description": transaction.description,
                    "account": account.handle,
                    "account_name": self.db.full_name(account),
                    "amount": str(abs(split.value).to_decimal()),
                }
                if account.account_class is AccountClass.EXPENSE and split.value > 0:
                    payments.append(row)
                if account.account_class is AccountClass.EXPENSE and split.value < 0:
                    refunds.append(row)
                if account.atype is AccountType.FSA and split.value < 0:
                    reimbursements.append(row)
        fsa_accounts = [
            {
                "handle": account.handle,
                "name": self.db.full_name(account),
                "years": [year.serialize() for year in account.fsa_years],
            }
            for account in self.db.iter_accounts()
            if account.atype is AccountType.FSA
        ]
        return {
            "payments": payments,
            "refunds": refunds,
            "reimbursements": reimbursements,
            "fsa_accounts": fsa_accounts,
        }

    def fsa_claim_save(self, payload: dict) -> dict:
        eob = str(payload.get("eob_responsibility", "")).strip()
        handle = str(payload.get("handle") or "").strip() or None

        def link(item: dict) -> ClaimLinkInput:
            return ClaimLinkInput(str(item["transaction"]), str(item["split"]))

        result = save_claim(
            self.db,
            SaveClaim(
                ClaimInput(
                    service_date=date.fromisoformat(str(payload["service_date"])),
                    provider=str(payload.get("provider", "")).strip(),
                    description=str(payload.get("description", "")).strip(),
                    eob_responsibility=self._input_money(payload, eob) if eob else None,
                    payments=tuple(link(item) for item in payload.get("payments", [])),
                    refunds=tuple(link(item) for item in payload.get("refunds", [])),
                    allocations=tuple(
                        ClaimAllocationInput(
                            account=str(item["account"]),
                            funding_year_start=date.fromisoformat(str(item["funding_year_start"])),
                            target=(
                                self._input_money(payload, item["target"])
                                if item.get("target") not in (None, "")
                                else None
                            ),
                            reimbursements=tuple(
                                link(raw_link) for raw_link in item.get("reimbursements", [])
                            ),
                            rejections=tuple(
                                ClaimRejectionInput(
                                    attempted_on=date.fromisoformat(str(rejection["attempted_on"])),
                                    amount=self._input_money(payload, rejection["amount"]),
                                    reason=str(rejection.get("reason", "")),
                                )
                                for rejection in item.get("rejections", [])
                            ),
                        )
                        for item in payload.get("allocations", [])
                    ),
                ),
                existing_handle=handle,
            ),
        )
        if result.value is None:
            raise self._service_resource_error(result.errors[0])
        return {"handle": result.value.handle}

    def fsa_claim_delete(self, payload: dict) -> dict:
        handle = str(payload.get("handle", ""))
        result = delete_claim(self.db, DeleteClaim(handle))
        if result.value is None:
            raise self._service_resource_error(result.errors[0])
        return {"handle": handle}

    def register(self, handle: str, limit: int = 250) -> dict:
        account = self.db.get_account(handle)
        if account is None:
            raise KeyError(handle)
        rows = ledger.register(self.db, handle)[-limit:]
        debit_label, credit_label = ledger.register_headings(account.atype)
        return {
            "account": self.db.full_name(account),
            "type": account.atype.value,
            "debit_label": debit_label,
            "credit_label": credit_label,
            "rows": [
                {
                    "handle": row.transaction.handle,
                    "date": row.post_date,
                    "num": row.transaction.num,
                    "description": row.description,
                    "notes": row.transaction.notes,
                    "source_notes": row.transaction.source_notes,
                    "transfer": row.transfer_label(self.db),
                    "amount": row.amount,
                    "balance": row.running,
                }
                for row in rows
            ],
        }

    def reconciliation(self, account_handle: str) -> dict:
        """Return the current statement workflow and its auditable history."""
        account = self.db.get_account(account_handle)
        if account is None:
            raise KeyError(account_handle)
        current = reconciliation.open_for_account(self.db, account_handle)
        current_state = reconciliation.summary(self.db, current) if current else None
        return {
            "account": {"handle": account.handle, "name": self.db.full_name(account)},
            "open": (
                {
                    "handle": current.handle,
                    "statement_date": current.statement_date,
                    "ending_balance": current.ending_balance,
                    "opening_balance": current_state.opening_balance,
                    "selected_balance": current_state.selected_balance,
                    "difference": current_state.difference,
                    "balanced": current_state.balanced,
                    "selected_splits": list(current.selected_splits),
                    "candidates": [
                        {
                            "transaction": item.transaction,
                            "split": item.split,
                            "date": item.post_date,
                            "description": item.description,
                            "amount": item.amount,
                            "state": item.state.value,
                            "selected": item.selected,
                        }
                        for item in current_state.candidates
                    ],
                }
                if current is not None and current_state is not None
                else None
            ),
            "history": [
                {
                    "handle": item.handle,
                    "statement_date": item.statement_date,
                    "ending_balance": item.ending_balance,
                    "status": item.status.value,
                    "completed_at": item.completed_at,
                    "cancelled_at": item.cancelled_at,
                    "events": [
                        {"action": event.action, "at": event.occurred_at}
                        for event in item.audit_events
                    ],
                }
                for item in reversed(list(self.db.iter_reconciliations(account_handle)))
            ],
        }

    def reconciliation_start(self, payload: dict) -> dict:
        account = str(payload.get("account") or "")
        try:
            statement_date = date.fromisoformat(str(payload.get("statement_date") or ""))
        except ValueError as exc:
            raise ValueError("enter a valid statement date") from exc
        ending = self._input_money(payload, payload.get("ending_balance", ""))
        result = start_reconciliation(
            self.db,
            StartReconciliation(account, statement_date, ending),
        )
        if result.value is None:
            raise self._service_resource_error(result.errors[0])
        started = result.value.reconciliation
        return {"handle": started.handle, "status": started.status.value}

    def reconciliation_update(self, payload: dict) -> dict:
        handle = str(payload.get("handle") or "")
        raw_splits = payload.get("selected_splits", [])
        if not isinstance(raw_splits, list) or not all(
            isinstance(item, str) for item in raw_splits
        ):
            raise ValueError("selected_splits must be a list of split handles")
        ending = (
            self._input_money(payload, payload["ending_balance"])
            if "ending_balance" in payload
            else None
        )
        result = update_reconciliation(
            self.db,
            UpdateReconciliation(
                handle,
                selected_splits=tuple(raw_splits),
                ending_balance=ending,
            ),
        )
        if result.value is None:
            raise self._service_resource_error(result.errors[0])
        state = result.value
        return {"handle": handle, "difference": state.difference, "balanced": state.balanced}

    def reconciliation_complete(self, payload: dict) -> dict:
        result = complete_reconciliation(
            self.db,
            ReconciliationAction(str(payload.get("handle") or "")),
        )
        if result.value is None:
            raise self._service_resource_error(result.errors[0])
        completed = result.value
        return {"handle": completed.handle, "status": completed.status.value}

    def reconciliation_cancel(self, payload: dict) -> dict:
        result = cancel_reconciliation(
            self.db,
            ReconciliationAction(str(payload.get("handle") or "")),
        )
        if result.value is None:
            raise self._service_resource_error(result.errors[0])
        cancelled = result.value
        return {"handle": cancelled.handle, "status": cancelled.status.value}

    def reconciliation_reopen(self, payload: dict) -> dict:
        result = reopen_reconciliation(
            self.db,
            ReconciliationAction(str(payload.get("handle") or "")),
        )
        if result.value is None:
            raise self._service_resource_error(result.errors[0])
        reopened = result.value
        return {"handle": reopened.handle, "status": reopened.status.value}

    def historical_estimates(
        self,
        months: int = 12,
        min_active_months: int = 3,
        scenario_handle: str | None = None,
    ) -> dict:
        """Return reviewable category estimates inferred from closed history."""
        proposals = estimates.propose_historical_estimates(
            self.db,
            months=months,
            min_active_months=min_active_months,
            scenario_handle=scenario_handle,
        )
        return {
            "months": months,
            "proposals": [
                {
                    "key": item.key,
                    "category": item.category,
                    "category_name": item.category_name,
                    "purpose_name": item.purpose_name,
                    "funding": item.funding,
                    "funding_name": item.funding_name,
                    "amount": item.display_amount,
                    "source_name": item.source_name,
                    "destination_name": item.destination_name,
                    "start": item.recurrence.start,
                    "frequency": item.recurrence.describe(),
                    "frequency_key": self._frequency_key(item.recurrence),
                    "seasonal_amounts": [
                        {"month": value.month, "amount": value.amount}
                        for value in item.seasonal_amounts
                    ],
                    "scheduled_amount": item.scheduled_amount,
                    "active_months": item.active_months,
                    "transaction_count": item.transaction_count,
                    "confidence": item.confidence,
                    "outlier_months": item.outlier_months,
                    "variability": item.variability,
                    "reason": item.reason,
                    "evidence": item.evidence.serialize(),
                    "draft": self._historical_estimate_draft_payload(item),
                    "planning_flow": (
                        item.planning_flow.value if item.planning_flow is not None else None
                    ),
                    "investment_activity": (
                        item.investment_activity.value
                        if item.investment_activity is not None
                        else None
                    ),
                }
                for item in proposals
            ],
            "targets": [
                {"handle": None, "name": "Base"},
                *[
                    {"handle": scenario.handle, "name": scenario.name}
                    for scenario in self.db.iter_scenarios()
                ],
            ],
        }

    def _historical_estimate_draft_payload(
        self, proposal: estimates.HistoricalEstimateProposal
    ) -> dict:
        """Translate the engine-owned draft once for every interactive client."""
        draft = estimates.draft_historical_estimate(self.db, proposal)
        parts = self._simple_schedule_parts(draft)
        if parts is None:
            raise ValueError("historical estimate draft is not safely editable")
        frequency = self._frequency_key(draft.recurrence)
        if frequency is None:
            raise ValueError("historical estimate cadence is not safely editable")
        return {
            "handle": None,
            "name": draft.name,
            "category": parts["category"],
            "funding": parts["funding"],
            "amount": parts["amount"],
            "category_planning_flow": parts["category_planning_flow"],
            "planning_flow": parts["planning_flow"],
            "investment_activity": parts["investment_activity"],
            "frequency": frequency,
            "frequency_key": frequency,
            "start": draft.recurrence.start.isoformat(),
            "end": draft.recurrence.end.isoformat() if draft.recurrence.end else None,
            "count": draft.recurrence.count,
            "weekend": self._weekend_key(draft.recurrence.weekend_adjust),
            "enabled": True,
            "placeholder": True,
            "auto": False,
            "growth_policy": draft.growth_policy.value,
            "category_memo": parts["category_memo"],
            "funding_memo": parts["funding_memo"],
            "additional_splits": parts["additional_splits"],
            "amount_changes": [],
            "split_amount_changes": [],
            "seasonal_amounts": [
                {"month": item.month, "amount": item.amount} for item in draft.seasonal_amounts
            ],
            "skipped": [],
            "occurrence_adjustments": [],
            "account_handles": [parts["category"], parts["funding"]],
            "estimate_evidence": draft.estimate_evidence,
        }

    def historical_estimate_accept(self, payload: dict) -> dict:
        """Accept one historical proposal as a normal planning estimate."""
        months = int(payload.get("months") or 12)
        minimum = int(payload.get("min_active_months") or 3)
        proposal_key = str(payload.get("key") or "")
        category = str(payload.get("category") or "")
        scenario = str(payload.get("scenario") or "").strip() or None
        proposals = estimates.propose_historical_estimates(
            self.db,
            months=months,
            min_active_months=minimum,
            scenario_handle=scenario,
        )
        proposal = next(
            (
                item
                for item in proposals
                if item.key == proposal_key or (not proposal_key and item.category == category)
            ),
            None,
        )
        if proposal is None:
            raise ValueError("historical estimate proposal is no longer available")
        handle = estimates.accept_historical_estimate(self.db, proposal, scenario_handle=scenario)
        return {"handle": handle, "category": proposal.category_name}

    def scheduled(self, days: int = 60) -> dict:
        occurrences = schedule.upcoming_occurrences(self.db, horizon_days=days)
        accounts = sorted(
            (
                account
                for account in self.db.iter_accounts()
                if not account.is_root and not account.placeholder
            ),
            key=self.db.full_name,
        )
        definitions = []
        saved_schedules = list(self.db.iter_scheduled())
        for item in saved_schedules:
            projection = schedule.schedule_edit_projection(self.db, item)
            simple = self._simple_schedule_parts(item)
            frequency = self._frequency_key(item.recurrence)
            editability = projection.editability
            definitions.append(
                {
                    "handle": item.handle,
                    "account_linked": False,
                    "linked_account": None,
                    "name": item.name,
                    "frequency": item.recurrence.describe(),
                    "frequency_key": frequency,
                    "amount": item.amount(),
                    "enabled": item.enabled,
                    "placeholder": item.placeholder,
                    "auto": item.auto_create,
                    "growth_policy": item.growth_policy.value,
                    "simple": simple is not None and frequency is not None,
                    "editable": editability.editable,
                    "editor_mode": editability.mode.value,
                    "editability_reason": editability.reason,
                    "unsupported_reason": (editability.reason if not editability.editable else ""),
                    "source_recurrence": item.source_recurrence,
                    "category": simple["category"] if simple else None,
                    "funding": simple["funding"] if simple else None,
                    "planning_flow": simple["planning_flow"] if simple else None,
                    "category_planning_flow": (
                        simple["category_planning_flow"] if simple else None
                    ),
                    "investment_activity": (simple["investment_activity"] if simple else None),
                    "category_memo": simple["category_memo"] if simple else "",
                    "funding_memo": simple["funding_memo"] if simple else "",
                    "additional_splits": (simple["additional_splits"] if simple else []),
                    "formula_splits": [
                        {
                            "index": index,
                            "account": item.splits[index].account,
                            "account_name": (
                                self.db.full_name(account)
                                if (account := self.db.get_account(item.splits[index].account))
                                is not None
                                else item.splits[index].account
                            ),
                            "formula": item.splits[index].formula,
                        }
                        for index in projection.formula_split_indices
                    ],
                    "variables": dict(item.variables),
                    "account_handles": [split.account for split in item.splits],
                    "start": item.recurrence.start.isoformat(),
                    "end": (
                        item.recurrence.end.isoformat() if item.recurrence.end is not None else None
                    ),
                    "count": item.recurrence.count,
                    "weekend": self._weekend_key(item.recurrence.weekend_adjust),
                    "amount_changes": [
                        {"start": change.start.isoformat(), "amount": change.amount}
                        for change in item.amount_changes
                    ],
                    "split_amount_changes": [
                        {
                            "account": split.account,
                            "start": change.start.isoformat(),
                            "amount": change.amount,
                        }
                        for split in item.splits
                        for change in split.amount_changes
                    ],
                    "seasonal_amounts": [
                        {"month": value.month, "amount": value.amount}
                        for value in item.seasonal_amounts
                    ],
                    "skipped": [when.isoformat() for when in item.skipped],
                    "occurrence_adjustments": [
                        {"when": change.when.isoformat(), "amount": change.amount}
                        for change in item.occurrence_adjustments
                    ],
                    "estimate_evidence": item.estimate_evidence,
                }
            )
        for payment_definition in schedule.account_payment_definitions(
            self.db, schedules=saved_schedules
        ):
            definitions.append(
                {
                    "handle": payment_definition.handle,
                    "account_linked": True,
                    "linked_account": payment_definition.account,
                    "name": payment_definition.name,
                    "frequency": payment_definition.recurrence.describe(),
                    "frequency_key": "monthly",
                    "amount": payment_definition.amount_due,
                    "enabled": True,
                    "placeholder": False,
                    "auto": False,
                    "growth_policy": "none",
                    "simple": False,
                    "editable": True,
                    "editor_mode": "account",
                    "editability_reason": "",
                    "unsupported_reason": "",
                    "source_recurrence": None,
                    "category": None,
                    "funding": payment_definition.payment_account,
                    "planning_flow": None,
                    "investment_activity": None,
                    "category_memo": "",
                    "funding_memo": "",
                    "additional_splits": [],
                    "account_handles": [split.account for split in payment_definition.splits],
                    "start": payment_definition.next_due.isoformat(),
                    "end": None,
                    "count": None,
                    "weekend": "none",
                    "amount_changes": [],
                    "seasonal_amounts": [],
                    "skipped": [],
                    "occurrence_adjustments": [],
                }
            )
        return {
            "definitions": definitions,
            "accounts": [
                {
                    "handle": account.handle,
                    "name": self.db.full_name(account),
                    "class": account.account_class.value,
                    "hidden": account.hidden,
                }
                for account in accounts
            ],
            "upcoming": [
                {
                    "date": occurrence.when,
                    "name": occurrence.name,
                    "amount": occurrence.amount,
                    "schedule": occurrence.schedule.handle,
                    "account_linked": isinstance(
                        occurrence.schedule, schedule.AccountPaymentDefinition
                    ),
                    "linked_account": getattr(occurrence.schedule, "account", None),
                }
                for occurrence in occurrences
            ],
        }

    def scheduled_draft(self, payload: dict) -> dict:
        """Represent an actual as a reviewable, unsaved fixed-schedule draft."""
        transaction = self.db.get_transaction(str(payload.get("transaction") or ""))
        if transaction is None:
            raise KeyError(str(payload.get("transaction") or ""))
        draft = schedule.from_transaction(transaction)
        simple = self._simple_schedule_parts(draft)
        if simple is None:
            raise ValueError("this transaction's split structure needs the desktop schedule editor")
        return {
            "handle": None,
            "name": draft.name,
            "frequency": draft.recurrence.describe(),
            "frequency_key": "once",
            "amount": draft.amount(),
            "enabled": True,
            "placeholder": False,
            "auto": False,
            "growth_policy": draft.growth_policy.value,
            "simple": True,
            "category": simple["category"],
            "funding": simple["funding"],
            "planning_flow": simple["planning_flow"],
            "investment_activity": simple["investment_activity"],
            "category_memo": simple["category_memo"],
            "funding_memo": simple["funding_memo"],
            "additional_splits": simple["additional_splits"],
            "account_handles": [split.account for split in draft.splits],
            "start": draft.recurrence.start.isoformat(),
            "end": None,
            "count": None,
            "weekend": "none",
            "amount_changes": [],
            "split_amount_changes": [],
            "skipped": [],
            "occurrence_adjustments": [],
        }

    @staticmethod
    def _month_end(year: int, month: int) -> date:
        return date(year, month, monthrange(year, month)[1])

    @staticmethod
    def _previous_month(when: date) -> date:
        if when.month == 1:
            return date(when.year - 1, 12, 1)
        return date(when.year, when.month - 1, 1)

    def _plan_earliest_data_date(self) -> date:
        today = date.today()
        dates: list[date] = []
        dates.extend(transaction.post_date for transaction in self.db.iter_transactions())
        dates.extend(schedule.recurrence.start for schedule in self.db.iter_scheduled())
        for scenario in self.db.iter_scenarios():
            dates.extend(item.recurrence.start for item in scenario.schedule_overrides)
            dates.extend(item.when for item in scenario.one_offs)
        return min(dates, default=date(today.year, 1, 1))

    def _plan_maximum_through_month(self) -> date:
        today = date.today()
        anniversary_year = today.year + 150
        anniversary = date(
            anniversary_year,
            today.month,
            min(today.day, monthrange(anniversary_year, today.month)[1]),
        )
        candidate = date(anniversary.year, anniversary.month, 1)
        if self._month_end(candidate.year, candidate.month) > anniversary:
            candidate = self._previous_month(candidate)
        return candidate

    def _base_scenario(self, start: date, end: date) -> Scenario:
        scenario = Scenario(
            name="Base scenario",
            start=start,
            years=max(1, end.year - start.year + 1),
        )
        stored = self.db.get_metadata("planning.base_assumptions", None)
        if isinstance(stored, dict):
            scenario.assumptions = Assumptions.from_dict(stored)
        return scenario

    def _management_base_scenario(self) -> Scenario:
        today = date.today()
        return self._base_scenario(
            date(today.year, 1, 1),
            date(today.year + 9, 12, 31),
        )

    @staticmethod
    def _scenario_payload(scenario: Scenario, *, base: bool = False) -> dict:
        assumptions = scenario.effective_assumptions()
        return {
            "handle": None if base else scenario.handle,
            "base": base,
            "name": "Base scenario" if base else scenario.name,
            "description": "" if base else scenario.description,
            "parent_handle": None if base else scenario.parent_handle,
            "assumptions": assumptions.serialize(),
            "assumption_sources": scenario.assumption_sources(),
            "account_assumption_sources": scenario.account_assumption_sources(),
            "assumption_overrides": sorted(scenario.assumption_overrides),
            "periods": [
                {"index": index, **period.serialize()}
                for index, period in enumerate(scenario.assumption_periods)
            ],
            "schedule_changes": 0 if base else len(scenario.schedule_overrides),
        }

    def scenarios(self) -> dict:
        """Base and saved planning scenarios for the management surface."""
        base = self._management_base_scenario()
        projection_accounts = []
        for account in self.db.iter_accounts():
            if not (
                account.account_class is AccountClass.LIABILITY
                or (account.account_class is AccountClass.ASSET and account.atype.is_investment)
            ):
                continue
            projection_accounts.append(
                {
                    "handle": account.handle,
                    "name": self.db.full_name(account),
                    "class": account.account_class.value,
                    "account_rate": (
                        account.annual_interest
                        if account.account_class is AccountClass.LIABILITY
                        else account.annual_return
                    ),
                }
            )
        projection_accounts.sort(key=lambda item: item["name"].casefold())
        return {
            "scenarios": [
                self._scenario_payload(base, base=True),
                *(self._scenario_payload(item) for item in self.db.iter_scenarios()),
            ],
            "projection_accounts": projection_accounts,
        }

    def _per_account_rates(
        self,
        payload: object,
        existing: Mapping[str, Decimal | Rate] | None = None,
    ) -> dict[str, Decimal]:
        if payload is None:
            return {
                handle: value.decimal if isinstance(value, Rate) else value
                for handle, value in (existing or {}).items()
            }
        if not isinstance(payload, dict):
            raise ValueError("per_account assumptions must be an object")
        per_account: dict[str, Decimal] = {}
        for handle, raw_rate in payload.items():
            account = self.db.get_account(str(handle))
            if account is None:
                raise ValueError(f"unknown account assumption: {handle}")
            if not (
                account.account_class is AccountClass.LIABILITY
                or (account.account_class is AccountClass.ASSET and account.atype.is_investment)
            ):
                raise ValueError(
                    "account-specific projection rate is not valid for "
                    f"{self.db.full_name(account)}"
                )
            if raw_rate is None or str(raw_rate).strip() == "":
                continue
            rate = Decimal(str(raw_rate))
            if rate < Decimal("-1") or rate > Decimal("1"):
                raise ValueError("account-specific rates must be between -1 and 1")
            per_account[account.handle] = rate
        return per_account

    def _assumptions_from_payload(
        self, payload: object, existing: Assumptions | None = None
    ) -> Assumptions:
        if not isinstance(payload, dict):
            raise ValueError("assumptions must be an object")
        fields = (
            "income_growth",
            "expense_inflation",
            "investment_return",
            "cash_interest",
            "liability_interest",
        )
        values: dict[str, Decimal] = {}
        for field in fields:
            if field not in payload:
                raise ValueError(f"missing assumption: {field}")
            value = Decimal(str(payload[field]))
            if value < Decimal("-1") or value > Decimal("1"):
                raise ValueError(f"{field} must be between -1 and 1")
            values[field] = value
        per_account = self._per_account_rates(
            payload.get("per_account") if "per_account" in payload else None,
            existing.per_account if existing is not None else None,
        )
        return Assumptions(
            income_growth=values["income_growth"],
            expense_inflation=values["expense_inflation"],
            investment_return=values["investment_return"],
            cash_interest=values["cash_interest"],
            liability_interest=values["liability_interest"],
            per_account=per_account,
        )

    def scenario_save(self, payload: dict) -> dict:
        handle = payload.get("handle")
        if not handle:
            base = self._management_base_scenario()
            assumptions = self._assumptions_from_payload(
                payload.get("assumptions"), base.assumptions
            )
            base_result = save_base_assumptions(self.db, SaveBaseAssumptions(assumptions))
            if not base_result.ok:
                raise self._service_resource_error(base_result.errors[0])
            return self._scenario_payload(self._management_base_scenario(), base=True)

        scenario = self.db.get_scenario(str(handle))
        if scenario is None:
            raise KeyError(str(handle))
        name = str(payload.get("name", "")).strip()
        if not name:
            raise ValueError("give the scenario a name first")
        duplicate = self.db.get_scenario_by_name(name)
        if duplicate is not None and duplicate.handle != scenario.handle:
            raise ValueError(f'a scenario named "{name}" already exists')
        scenario.name = name
        scenario.description = str(payload.get("description", "")).strip()
        previous = scenario.effective_assumptions()
        updated = self._assumptions_from_payload(payload.get("assumptions"), previous)
        if "parent_handle" in payload:
            requested_parent = payload.get("parent_handle")
            scenario.parent_handle = str(requested_parent).strip() if requested_parent else None
            scenario.inherits_base_assumptions = True
        if scenario.inherits_base_assumptions:
            requested = payload.get("assumption_overrides")
            if requested is not None:
                if not isinstance(requested, list):
                    raise ValueError("assumption_overrides must be a list")
                fields = {
                    "income_growth",
                    "expense_inflation",
                    "investment_return",
                    "cash_interest",
                    "liability_interest",
                }
                overrides = {str(item) for item in requested}
                if not overrides <= fields:
                    raise ValueError("unknown assumption override")
                scenario.assumption_overrides = overrides
            else:
                for field in scenario.assumption_sources():
                    if getattr(updated, field) != getattr(previous, field):
                        scenario.assumption_overrides.add(field)
            changed_accounts = set(updated.per_account) | set(previous.per_account)
            for account_handle in changed_accounts:
                if updated.per_account.get(account_handle) == previous.per_account.get(
                    account_handle
                ):
                    continue
                if account_handle in updated.per_account:
                    scenario.set_account_assumption_override(
                        account_handle, updated.per_account[account_handle]
                    )
                else:
                    scenario.inherit_account_assumption(account_handle)
        scenario.assumptions = updated
        result = save_scenario_assumptions(
            self.db,
            SaveScenarioAssumptions(scenario, existing_handle=scenario.handle),
        )
        if not result.ok:
            raise self._service_resource_error(result.errors[0])
        reloaded = self.db.get_scenario(scenario.handle)
        if reloaded is None:  # pragma: no cover - guarded by the successful commit
            raise KeyError(scenario.handle)
        return self._scenario_payload(reloaded)

    def scenario_duplicate(self, payload: dict) -> dict:
        handle = payload.get("handle")
        if handle:
            source = self.db.get_scenario(str(handle))
            if source is None:
                raise KeyError(str(handle))
        else:
            source = self._management_base_scenario()
        result = duplicate_scenario(
            self.db,
            DuplicateScenario(source, from_base=not bool(handle)),
        )
        if not result.ok:
            raise self._service_resource_error(result.errors[0])
        saved = result.value
        assert saved is not None
        clone = self.db.get_scenario(saved.handle)
        assert clone is not None
        return self._scenario_payload(clone)

    def scenario_delete(self, payload: dict) -> dict:
        handle = str(payload.get("handle") or "").strip()
        if not handle:
            raise ValueError("Base scenario cannot be deleted")
        result = delete_scenario(self.db, DeleteScenario(handle))
        if not result.ok:
            raise self._service_resource_error(result.errors[0])
        return {"deleted": handle}

    @staticmethod
    def _optional_rate(value: object) -> Decimal | None:
        if value is None or str(value).strip() == "":
            return None
        rate = Decimal(str(value))
        if rate < Decimal("-1") or rate > Decimal("1"):
            raise ValueError("dated assumption rates must be between -1 and 1")
        return rate

    def scenario_period_save(self, payload: dict) -> dict:
        handle = str(payload.get("handle", "")).strip()
        scenario = self.db.get_scenario(handle) if handle else None
        if scenario is None:
            raise ValueError("dated assumptions belong to a saved scenario")
        start = date.fromisoformat(str(payload.get("start", "")))
        end_value = str(payload.get("end", "")).strip()
        end = date.fromisoformat(end_value) if end_value else None
        index_value = payload.get("index")
        existing_period = None
        if index_value is not None and index_value != "":
            index = int(index_value)
            if index < 0 or index >= len(scenario.assumption_periods):
                raise ValueError("dated assumption period no longer exists")
            existing_period = scenario.assumption_periods[index]
        period = AssumptionPeriod(
            start=start,
            end=end,
            description=str(payload.get("description", "")).strip(),
            income_growth=self._optional_rate(payload.get("income_growth")),
            expense_inflation=self._optional_rate(payload.get("expense_inflation")),
            investment_return=self._optional_rate(payload.get("investment_return")),
            cash_interest=self._optional_rate(payload.get("cash_interest")),
            liability_interest=self._optional_rate(payload.get("liability_interest")),
            per_account=self._per_account_rates(
                payload.get("per_account") if "per_account" in payload else None,
                existing_period.per_account if existing_period is not None else None,
            ),
        )
        result = save_assumption_period(
            self.db,
            SaveAssumptionPeriod(
                scenario.handle,
                period,
                index=None if existing_period is None else index,
            ),
        )
        if not result.ok:
            raise self._service_resource_error(result.errors[0])
        saved = self.db.get_scenario(scenario.handle)
        assert saved is not None
        return self._scenario_payload(saved)

    def scenario_period_delete(self, payload: dict) -> dict:
        handle = str(payload.get("handle", "")).strip()
        scenario = self.db.get_scenario(handle) if handle else None
        if scenario is None:
            raise ValueError("dated assumptions belong to a saved scenario")
        index = int(payload.get("index", -1))
        if index < 0 or index >= len(scenario.assumption_periods):
            raise ValueError("dated assumption period no longer exists")
        result = delete_assumption_period(self.db, DeleteAssumptionPeriod(scenario.handle, index))
        if not result.ok:
            raise self._service_resource_error(result.errors[0])
        saved = self.db.get_scenario(scenario.handle)
        assert saved is not None
        return self._scenario_payload(saved)

    _SCENARIO_FREQUENCIES = {
        "weekly": (PeriodType.WEEK, 1),
        "biweekly": (PeriodType.WEEK, 2),
        "semimonthly": (PeriodType.SEMI_MONTH, 1),
        "monthly": (PeriodType.MONTH, 1),
        "nth_weekday": (PeriodType.NTH_WEEKDAY, 1),
        "last_weekday": (PeriodType.LAST_WEEKDAY, 1),
        "quarterly": (PeriodType.MONTH, 3),
        "semiannual": (PeriodType.MONTH, 6),
        "annual": (PeriodType.YEAR, 1),
        "once": (PeriodType.ONCE, 1),
    }
    _SCENARIO_WEEKENDS = {
        "none": WeekendAdjust.NONE,
        "previous": WeekendAdjust.PREVIOUS,
        "next": WeekendAdjust.NEXT,
    }

    def _scenario_for_events(self, handle: object) -> Scenario:
        value = str(handle or "").strip()
        if not value:
            raise ValueError("scenario event changes require a saved scenario")
        scenario = self.db.get_scenario(value)
        if scenario is None:
            raise KeyError(value)
        return scenario

    def _simple_schedule_parts(self, scheduled) -> dict | None:
        projection = schedule.schedule_edit_projection(self.db, scheduled)
        if (
            projection.editability.mode is not schedule.ScheduleEditorMode.FIXED
            or projection.primary is None
            or projection.funding is None
        ):
            return None
        primary_projection = projection.primary
        funding_projection = projection.funding
        flow = scheduled.splits[primary_projection.index]
        funding = scheduled.splits[funding_projection.index]
        flow_account = self.db.get_account(flow.account)
        assert flow_account is not None
        additional = []
        for item in projection.additional:
            split = scheduled.splits[item.index]
            row = {
                "account": split.account,
                "amount": str(item.amount.to_decimal()),
                "planning_flow": (
                    split.planning_flow.value if split.planning_flow is not None else None
                ),
            }
            if split.investment_activity is not None:
                row["investment_activity"] = split.investment_activity.value
            if item.opposite_direction:
                row["direction"] = "opposite"
            if split.memo:
                row["memo"] = split.memo
            additional.append(row)
        return {
            "category": flow.account,
            "funding": funding.account,
            "amount": str(primary_projection.amount.to_decimal()),
            "category_memo": flow.memo,
            "funding_memo": funding.memo,
            "category_planning_flow": (
                flow.planning_flow.value if flow.planning_flow is not None else None
            ),
            "category_ledger_direction": (
                primary_projection.ledger_direction
                if flow_account.account_class not in {AccountClass.INCOME, AccountClass.EXPENSE}
                else None
            ),
            "planning_flow": (
                funding.planning_flow.value if funding.planning_flow is not None else None
            ),
            "investment_activity": (
                flow.investment_activity.value if flow.investment_activity is not None else None
            ),
            "additional_splits": additional,
        }

    @staticmethod
    def _frequency_key(recurrence: Recurrence) -> str | None:
        for key, (period, interval) in Api._SCENARIO_FREQUENCIES.items():
            if recurrence.period is period and recurrence.interval == interval:
                return key
        return None

    def _schedule_recurrence_from_payload(
        self,
        payload: dict,
        existing: ScheduledTransaction | None = None,
    ) -> Recurrence:
        """Parse web recurrence controls while retaining unexposed source details."""
        frequency = str(payload.get("frequency") or "monthly")
        if frequency not in self._SCENARIO_FREQUENCIES:
            raise ValueError("unsupported schedule frequency")
        period, interval = self._SCENARIO_FREQUENCIES[frequency]
        try:
            start = date.fromisoformat(str(payload.get("start") or ""))
        except ValueError as exc:
            raise ValueError("first due date is invalid") from exc
        end = None
        raw_end = str(payload.get("end") or "").strip()
        if raw_end:
            try:
                end = date.fromisoformat(raw_end)
            except ValueError as exc:
                raise ValueError("end date is invalid") from exc
            if end < start:
                raise ValueError("end date cannot precede first due date")
        count = None
        raw_count = payload.get("count")
        if raw_count is not None and raw_count != "":
            try:
                count = int(raw_count)
            except (TypeError, ValueError) as exc:
                raise ValueError("occurrence count must be a whole number") from exc
            if count < 1:
                raise ValueError("occurrence count must be positive")
        if end is not None and count is not None:
            raise ValueError("choose an end date or occurrence count, not both")
        if period is PeriodType.ONCE:
            end = None
            count = None
        weekend_key = str(payload.get("weekend") or "none")
        if weekend_key not in self._SCENARIO_WEEKENDS:
            raise ValueError("unsupported weekend adjustment")
        preserve_shape = (
            existing is not None
            and existing.recurrence.period is period
            and existing.recurrence.interval == interval
        )
        day_of_month = existing.recurrence.day_of_month if existing and preserve_shape else None
        second_day_of_month = (
            existing.recurrence.second_day_of_month if existing and preserve_shape else None
        )
        return Recurrence(
            period=period,
            interval=interval,
            start=start,
            end=end,
            count=count,
            day_of_month=day_of_month,
            second_day_of_month=second_day_of_month,
            weekend_adjust=self._SCENARIO_WEEKENDS[weekend_key],
        )

    @staticmethod
    def _weekend_key(adjust: WeekendAdjust) -> str:
        for key, value in Api._SCENARIO_WEEKENDS.items():
            if adjust is value:
                return key
        return "none"

    def _scenario_event_payload(self, item: ScenarioSchedule) -> dict:
        source = self.db.get_scheduled(item.source_schedule) if item.source_schedule else None
        simple = self._simple_schedule_parts(item)
        return {
            "handle": item.handle,
            "name": item.name,
            "source_schedule": item.source_schedule,
            "source_name": source.name if source is not None else None,
            "enabled": item.enabled,
            "growth_policy": item.growth_policy.value,
            "simple": (simple is not None and self._frequency_key(item.recurrence) is not None),
            "category": simple["category"] if simple else None,
            "funding": simple["funding"] if simple else None,
            "amount": simple["amount"] if simple else None,
            "planning_flow": simple["planning_flow"] if simple else None,
            "category_planning_flow": (simple["category_planning_flow"] if simple else None),
            "investment_activity": simple["investment_activity"] if simple else None,
            "additional_splits": simple["additional_splits"] if simple else [],
            "frequency": self._frequency_key(item.recurrence),
            "start": item.recurrence.start.isoformat(),
            "end": item.recurrence.end.isoformat() if item.recurrence.end else None,
            "count": item.recurrence.count,
            "weekend": self._weekend_key(item.recurrence.weekend_adjust),
            "amount_changes": [
                {"start": change.start.isoformat(), "amount": change.amount}
                for change in item.amount_changes
            ],
            "seasonal_amounts": [
                {"month": value.month, "amount": value.amount} for value in item.seasonal_amounts
            ],
            "skipped": [when.isoformat() for when in item.skipped],
            "occurrence_adjustments": [
                {"when": change.when.isoformat(), "amount": change.amount}
                for change in item.occurrence_adjustments
            ],
            "estimate_evidence": item.estimate_evidence,
        }

    def scenario_events(self, handle: str | None) -> dict:
        scenario = self._scenario_for_events(handle)
        accounts = sorted(
            (
                account
                for account in self.db.iter_accounts()
                if not account.is_root and not account.placeholder
            ),
            key=self.db.full_name,
        )
        schedules = list(self.db.iter_scheduled())
        return {
            "scenario": {"handle": scenario.handle, "name": scenario.name},
            "accounts": [
                {
                    "handle": account.handle,
                    "name": self.db.full_name(account),
                    "class": account.account_class.value,
                }
                for account in accounts
            ],
            "baseline": [
                {
                    "handle": item.handle,
                    "name": item.name,
                    "growth_policy": item.growth_policy.value,
                    "simple": (parts := self._simple_schedule_parts(item)) is not None
                    and (frequency := self._frequency_key(item.recurrence)) is not None,
                    "category": parts["category"] if parts else None,
                    "funding": parts["funding"] if parts else None,
                    "amount": parts["amount"] if parts else None,
                    "category_planning_flow": (parts["category_planning_flow"] if parts else None),
                    "frequency": frequency if parts else None,
                    "start": item.recurrence.start.isoformat(),
                    "end": item.recurrence.end.isoformat() if item.recurrence.end else None,
                    "count": item.recurrence.count,
                    "weekend": self._weekend_key(item.recurrence.weekend_adjust),
                    "amount_changes": [
                        {"start": change.start.isoformat(), "amount": change.amount}
                        for change in item.amount_changes
                    ],
                    "seasonal_amounts": [
                        {"month": value.month, "amount": value.amount}
                        for value in item.seasonal_amounts
                    ],
                    "skipped": [when.isoformat() for when in item.skipped],
                    "occurrence_adjustments": [
                        {"when": change.when.isoformat(), "amount": change.amount}
                        for change in item.occurrence_adjustments
                    ],
                    "estimate_evidence": item.estimate_evidence,
                }
                for item in schedules
            ],
            "changes": [self._scenario_event_payload(item) for item in scenario.schedule_overrides],
        }

    @staticmethod
    def _parse_amount_changes(payload: dict, schedule_start: date) -> list[ScheduledAmountChange]:
        raw_changes = payload.get("amount_changes") or []
        if not isinstance(raw_changes, list):
            raise ValueError("future amounts must be a list")
        changes = []
        seen = set()
        for raw in raw_changes:
            if not isinstance(raw, dict):
                raise ValueError("future amount entry is invalid")
            try:
                when = date.fromisoformat(str(raw.get("start") or ""))
                amount = abs(Api._input_money(payload, raw.get("amount") or ""))
            except (ValueError, ArithmeticError):
                raise ValueError(
                    "future amounts require YYYY-MM-DD dates and valid amounts"
                ) from None
            if when < schedule_start:
                raise ValueError("future amount date cannot precede first occurrence")
            if not amount:
                raise ValueError("future amount must be greater than zero")
            if when in seen:
                raise ValueError("future amount dates must be unique")
            seen.add(when)
            changes.append(ScheduledAmountChange(when, amount))
        return sorted(changes, key=lambda item: item.start)

    @staticmethod
    def _parse_seasonal_amounts(payload: dict) -> list[ScheduledMonthAmount]:
        raw_amounts = payload.get("seasonal_amounts") or []
        if not isinstance(raw_amounts, list):
            raise ValueError("seasonal amounts must be a list")
        amounts = []
        seen = set()
        for raw in raw_amounts:
            if not isinstance(raw, dict):
                raise ValueError("seasonal amount entry is invalid")
            try:
                raw_month = raw.get("month")
                if raw_month is None:
                    raise ValueError
                month = int(raw_month)
                amount = abs(Api._input_money(payload, raw.get("amount") or ""))
                item = ScheduledMonthAmount(month, amount)
            except (TypeError, ValueError, ArithmeticError):
                raise ValueError(
                    "seasonal amounts require a month from 1 through 12 and a positive amount"
                ) from None
            if month in seen:
                raise ValueError("seasonal amount months must be unique")
            seen.add(month)
            amounts.append(item)
        return sorted(amounts, key=lambda item: item.month)

    @staticmethod
    def _parse_skipped(payload: dict, recurrence: Recurrence) -> list[date]:
        raw_skipped = payload.get("skipped") or []
        if not isinstance(raw_skipped, list):
            raise ValueError("skipped occurrences must be a list")
        skipped = []
        seen = set()
        for raw in raw_skipped:
            try:
                when = date.fromisoformat(str(raw))
            except ValueError:
                raise ValueError("skipped occurrences require YYYY-MM-DD dates") from None
            if when in seen:
                raise ValueError("skipped occurrence dates must be unique")
            if when not in recurrence.occurrences(when, since=when):
                raise ValueError(f"{when.isoformat()} is not an occurrence of this schedule")
            seen.add(when)
            skipped.append(when)
        return sorted(skipped)

    @staticmethod
    def _parse_occurrence_adjustments(
        payload: dict, recurrence: Recurrence
    ) -> list[ScheduledOccurrenceAdjustment]:
        raw_changes = payload.get("occurrence_adjustments") or []
        if not isinstance(raw_changes, list):
            raise ValueError("one-time amounts must be a list")
        changes = []
        seen = set()
        for raw in raw_changes:
            if not isinstance(raw, dict):
                raise ValueError("one-time amount entry is invalid")
            try:
                when = date.fromisoformat(str(raw.get("when") or ""))
                amount = abs(Api._input_money(payload, raw.get("amount") or ""))
            except (ValueError, ArithmeticError):
                raise ValueError(
                    "one-time amounts require YYYY-MM-DD dates and valid amounts"
                ) from None
            if not amount:
                raise ValueError("one-time amount must be greater than zero")
            if when in seen:
                raise ValueError("one-time amount dates must be unique")
            if when not in recurrence.occurrences(when, since=when):
                raise ValueError(f"{when.isoformat()} is not an occurrence of this schedule")
            seen.add(when)
            changes.append(ScheduledOccurrenceAdjustment(when, amount))
        return sorted(changes, key=lambda item: item.when)

    def _parse_additional_splits(
        self, payload: dict, _excluded: set[str]
    ) -> tuple[FixedSplitInput, ...]:
        raw_splits = payload.get("additional_splits") or []
        if not isinstance(raw_splits, list):
            raise ValueError("additional splits must be a list")
        splits: list[FixedSplitInput] = []
        for raw in raw_splits:
            if not isinstance(raw, dict):
                raise ValueError("additional split entry is invalid")
            handle = str(raw.get("account") or "").strip()
            try:
                amount = self._input_money(payload, raw.get("amount") or "0")
            except (ValueError, ArithmeticError) as exc:
                raise ValueError("additional split amount must be a valid number") from exc
            raw_purpose = str(raw.get("planning_flow") or "").strip()
            try:
                purpose = PlanningFlowKind(raw_purpose) if raw_purpose else None
            except ValueError:
                raise ValueError("choose a valid planning purpose") from None
            raw_activity = str(raw.get("investment_activity") or "").strip()
            try:
                investment_activity = InvestmentActivityKind(raw_activity) if raw_activity else None
            except ValueError:
                raise ValueError("choose a valid investment activity") from None
            direction = str(raw.get("direction") or "normal")
            if direction not in {"normal", "opposite"}:
                raise ValueError("choose a valid additional split direction")
            splits.append(
                FixedSplitInput(
                    account=handle,
                    amount=amount,
                    memo=str(raw.get("memo") or "").strip(),
                    planning_flow=purpose,
                    investment_activity=investment_activity,
                    opposite_direction=direction == "opposite",
                )
            )
        return tuple(splits)

    def _parse_split_amount_changes(
        self,
        payload: dict,
        schedule_start: date,
        allowed_accounts: set[str],
    ) -> dict[str, list[ScheduledSplitAmountChange]]:
        raw_changes = payload.get("split_amount_changes") or []
        if not isinstance(raw_changes, list):
            raise ValueError("per-leg amount changes must be a list")
        grouped: dict[str, list[ScheduledSplitAmountChange]] = {}
        seen: set[tuple[str, date]] = set()
        for raw in raw_changes:
            if not isinstance(raw, dict):
                raise ValueError("per-leg amount change entry is invalid")
            account = str(raw.get("account") or "").strip()
            if account not in allowed_accounts:
                raise ValueError("per-leg amount change must reference a selected split account")
            try:
                when = date.fromisoformat(str(raw.get("start") or ""))
                amount = self._input_money(payload, raw.get("amount") or "0")
            except (ValueError, ArithmeticError):
                raise ValueError(
                    "per-leg amounts require YYYY-MM-DD dates and valid signed amounts"
                ) from None
            if when < schedule_start:
                raise ValueError("per-leg amount changes cannot precede the first occurrence")
            key = (account, when)
            if key in seen:
                raise ValueError("per-leg amount dates must be unique for each account")
            seen.add(key)
            grouped.setdefault(account, []).append(ScheduledSplitAmountChange(when, amount))
        for changes in grouped.values():
            changes.sort(key=lambda item: item.start)
        return grouped

    def scenario_event_save(self, payload: dict) -> dict:
        scenario = self._scenario_for_events(payload.get("handle"))
        source_handle = str(payload.get("source_schedule") or "").strip() or None
        source = self.db.get_scheduled(source_handle) if source_handle else None
        if source_handle and source is None:
            raise KeyError(source_handle)
        if source is not None and self._simple_schedule_parts(source) is None:
            raise ValueError("complex schedules can only be suppressed for now")

        name = str(payload.get("name", "")).strip()
        if not name:
            raise ValueError("give the scenario estimate a name")
        category_handle = str(payload.get("category", "")).strip()
        funding_handle = str(payload.get("funding", "")).strip()
        investment_activity_raw = str(payload.get("investment_activity") or "").strip()
        try:
            investment_activity = (
                InvestmentActivityKind(investment_activity_raw) if investment_activity_raw else None
            )
        except ValueError:
            raise ValueError("choose a valid investment activity") from None
        category_planning_flow_raw = str(payload.get("category_planning_flow") or "").strip()
        try:
            category_planning_flow = (
                PlanningFlowKind(category_planning_flow_raw) if category_planning_flow_raw else None
            )
        except ValueError:
            raise ValueError("choose a valid category planning purpose") from None
        try:
            amount = abs(self._input_money(payload, payload.get("amount", "")))
        except (ValueError, ArithmeticError):
            raise ValueError("enter a valid amount") from None
        frequency = str(payload.get("frequency", "monthly"))
        if frequency not in self._SCENARIO_FREQUENCIES:
            raise ValueError("choose a supported frequency")
        weekend = str(payload.get("weekend", "none"))
        if weekend not in self._SCENARIO_WEEKENDS:
            raise ValueError("choose a supported weekend adjustment")
        try:
            start = date.fromisoformat(str(payload.get("start", "")))
        except ValueError:
            raise ValueError("first occurrence must be YYYY-MM-DD") from None
        period, interval = self._SCENARIO_FREQUENCIES[frequency]
        end = None
        count = None
        end_text = str(payload.get("end") or "").strip()
        count_text = str(payload.get("count") or "").strip()
        if period is not PeriodType.ONCE:
            if end_text and count_text:
                raise ValueError("choose either an end date or an occurrence count")
            if end_text:
                try:
                    end = date.fromisoformat(end_text)
                except ValueError:
                    raise ValueError("end date must be YYYY-MM-DD") from None
                if end < start:
                    raise ValueError("end date cannot be before the first occurrence")
            elif count_text:
                try:
                    count = int(count_text)
                except ValueError:
                    raise ValueError("occurrence count must be a whole number") from None
                if count < 1:
                    raise ValueError("occurrence count must be at least 1")
        recurrence = Recurrence(
            period=period,
            interval=interval,
            start=start,
            end=end,
            count=count,
            weekend_adjust=self._SCENARIO_WEEKENDS[weekend],
        )
        skipped = self._parse_skipped(payload, recurrence)
        adjustments = self._parse_occurrence_adjustments(payload, recurrence)
        if set(skipped) & {item.when for item in adjustments}:
            raise ValueError("an occurrence cannot be both skipped and overridden")
        planning_flow_raw = str(payload.get("planning_flow") or "").strip()
        try:
            planning_flow = PlanningFlowKind(planning_flow_raw) if planning_flow_raw else None
        except ValueError:
            raise ValueError("choose a valid planning purpose") from None
        additional_splits = self._parse_additional_splits(
            payload, {category_handle, funding_handle}
        )
        existing_change = (
            next(
                (
                    item
                    for item in scenario.schedule_overrides
                    if item.source_schedule == source_handle and item.enabled
                ),
                None,
            )
            if source_handle is not None
            else None
        )
        default_growth_policy = (
            existing_change.growth_policy
            if existing_change is not None
            else source.growth_policy
            if source is not None
            else ScheduleGrowthPolicy.AUTO
        )
        try:
            growth_policy = ScheduleGrowthPolicy(
                str(payload.get("growth_policy") or default_growth_policy.value)
            )
        except ValueError:
            raise ValueError("choose a valid projection growth policy") from None
        result = save_fixed_scenario_schedule(
            self.db,
            SaveFixedScenarioSchedule(
                scenario_handle=scenario.handle,
                source_schedule=source_handle,
                definition=FixedScheduleInput(
                    name=name,
                    recurrence=recurrence,
                    category=category_handle,
                    funding=funding_handle,
                    amount=amount,
                    category_planning_flow=category_planning_flow,
                    funding_planning_flow=planning_flow,
                    investment_activity=investment_activity,
                    additional_splits=additional_splits,
                    enabled=True,
                    placeholder=source.placeholder if source is not None else True,
                    growth_policy=growth_policy,
                    amount_changes=tuple(self._parse_amount_changes(payload, start)),
                    seasonal_amounts=tuple(
                        self._parse_seasonal_amounts(payload)
                        if "seasonal_amounts" in payload
                        else source.seasonal_amounts
                        if source is not None
                        else ()
                    ),
                    skipped=tuple(skipped),
                    occurrence_adjustments=tuple(adjustments),
                    estimate_evidence=(
                        payload.get("estimate_evidence")
                        if isinstance(payload.get("estimate_evidence"), dict)
                        else existing_change.estimate_evidence
                        if existing_change is not None
                        else source.estimate_evidence
                        if source is not None
                        else None
                    ),
                ),
            ),
        )
        if result.value is None:
            raise self._service_resource_error(result.errors[0])
        return self.scenario_events(scenario.handle)

    def scenario_event_suppress(self, payload: dict) -> dict:
        scenario = self._scenario_for_events(payload.get("handle"))
        result = suppress_scenario_schedule(
            self.db,
            SuppressScenarioSchedule(
                scenario.handle, str(payload.get("source_schedule", "")).strip()
            ),
        )
        if not result.ok:
            raise self._service_resource_error(result.errors[0])
        return self.scenario_events(scenario.handle)

    def plan(
        self,
        start_month: str | None = None,
        through_month: str | None = None,
        period: str | None = None,
        scenario_handle: str | None = None,
        compare_handle: str | None = None,
        measure: str | None = None,
    ) -> dict:
        """Derived category Plan using the same event stream as the GTK view."""
        use_saved = all(
            value is None
            for value in (
                start_month,
                through_month,
                period,
                scenario_handle,
                compare_handle,
                measure,
            )
        )
        start = date.fromisoformat(f"{start_month}-01") if start_month is not None else None
        through = date.fromisoformat(f"{through_month}-01") if through_month is not None else None
        end = self._month_end(through.year, through.month) if through is not None else None
        service_result = query_plan(
            self.db,
            PlanQuery(
                start=start,
                end=end,
                period=activity.ReportingPeriod(period) if period is not None else None,
                measure=PlanMeasure(measure) if measure is not None else None,
                scenario=scenario_handle,
                compare=compare_handle,
                use_saved=use_saved,
            ),
        )
        if service_result.value is None:
            error = service_result.errors[0]
            messages = {
                "plan.scenario.not_found": "Scenario was not found.",
                "plan.comparison.not_found": "Comparison scenario was not found.",
                "plan.start.before_book_data": "From cannot be earlier than the first book data.",
                "plan.end.before_start": "Through must be the same month as From or later.",
                "plan.end.after_maximum": "Through exceeds the supported planning horizon.",
                "plan.comparison.same": "Plan comparison must use a different scenario.",
            }
            status = 404 if error.code.endswith(".not_found") else 400
            raise ResourceError(
                status,
                error.code,
                error.fields,
                messages.get(error.code, error.code),
            )
        plan = service_result.value
        start = plan.start
        end = plan.end
        minimum = plan.minimum
        maximum_month = plan.maximum
        grouping = plan.period
        selected_measure = plan.measure
        scenario_handle = plan.scenario.handle
        compare_handle = plan.compare_handle
        report = plan.report
        totals = report.activity

        comparison: dict[str, object] | None = None
        if plan.comparison is not None:
            compare_identity = plan.comparison.scenario.handle
            compare_name = plan.comparison.scenario.name
            compare_report = plan.comparison.report
            compare_rows = {row.account: row for row in compare_report.categories}
            compare_bridges = {row.kind: row for row in compare_report.cash_bridge}
            compare_flows = {(row.kind, row.account): row for row in compare_report.planning_flows}
            compare_mortgages = {row.account: row for row in compare_report.mortgage_payments}
            comparison_categories: list[dict[str, object]] = []
            comparison_bridges: list[dict[str, object]] = []
            comparison_flows: list[dict[str, object]] = []
            comparison_mortgages: list[dict[str, object]] = []
            comparison = {
                "handle": compare_identity,
                "name": compare_name,
                "assumption_sources": plan.comparison.assumption_sources,
                "summary": {
                    "planned_cash": compare_report.activity.planned_cash_change,
                    "actual_cash": compare_report.actual_cash_through_as_of,
                    "variance": compare_report.cash_variance_through_as_of,
                    "opening_cash": compare_report.cash_position.opening,
                    "ending_cash": compare_report.cash_position.closing,
                    "minimum_cash": compare_report.cash_position.minimum,
                    "minimum_cash_date": compare_report.cash_position.minimum_date,
                    "planned_cash_delta": (
                        totals.planned_cash_change - compare_report.activity.planned_cash_change
                    ),
                    "actual_cash_delta": (
                        report.actual_cash_through_as_of - compare_report.actual_cash_through_as_of
                        if report.actual_cash_through_as_of is not None
                        and compare_report.actual_cash_through_as_of is not None
                        else None
                    ),
                    "variance_delta": (
                        report.cash_variance_through_as_of
                        - compare_report.cash_variance_through_as_of
                        if report.cash_variance_through_as_of is not None
                        and compare_report.cash_variance_through_as_of is not None
                        else None
                    ),
                },
                "categories": comparison_categories,
                "cash_bridge": comparison_bridges,
                "mortgage_payments": comparison_mortgages,
                "planning_flows": comparison_flows,
            }
            for bridge_row in report.cash_bridge:
                other_bridge = compare_bridges.get(bridge_row.kind)
                zeroes = [Money(0) for _ in bridge_row.planned]
                bridge_planned = other_bridge.planned if other_bridge is not None else zeroes
                bridge_actual = other_bridge.actual if other_bridge is not None else zeroes
                bridge_variance: list[Money | None] = (
                    other_bridge.variance if other_bridge is not None else list(zeroes)
                )
                comparison_bridges.append(
                    {
                        "kind": bridge_row.kind.value,
                        "planned": bridge_planned,
                        "actual": bridge_actual,
                        "variance": bridge_variance,
                        "planned_delta": [
                            value - alternate
                            for value, alternate in zip(
                                bridge_row.planned, bridge_planned, strict=True
                            )
                        ],
                        "actual_delta": [
                            value - alternate
                            for value, alternate in zip(
                                bridge_row.actual, bridge_actual, strict=True
                            )
                        ],
                        "variance_delta": [
                            (
                                value - alternate
                                if value is not None and alternate is not None
                                else None
                            )
                            for value, alternate in zip(
                                bridge_row.variance, bridge_variance, strict=True
                            )
                        ],
                    }
                )
            for row in report.categories:
                other = compare_rows.get(row.account)
                zeroes = [Money(0) for _ in row.planned]
                other_planned = other.planned if other is not None else zeroes
                other_actual = other.actual if other is not None else zeroes
                other_variance: list[Money | None] = (
                    other.variance if other is not None else list(zeroes)
                )
                comparison_categories.append(
                    {
                        "account": row.account,
                        "planned": other_planned,
                        "actual": other_actual,
                        "variance": other_variance,
                        "planned_delta": [
                            value - alternate
                            for value, alternate in zip(row.planned, other_planned, strict=True)
                        ],
                        "actual_delta": [
                            value - alternate
                            for value, alternate in zip(row.actual, other_actual, strict=True)
                        ],
                        "variance_delta": [
                            (
                                value - alternate
                                if value is not None and alternate is not None
                                else None
                            )
                            for value, alternate in zip(row.variance, other_variance, strict=True)
                        ],
                    }
                )
            for payment in report.mortgage_payments:
                other_payment = compare_mortgages.get(payment.account)
                zeroes = [Money(0) for _ in payment.planned]
                other_planned = other_payment.planned if other_payment is not None else zeroes
                other_actual = other_payment.actual if other_payment is not None else zeroes
                payment_variance: list[Money | None] = (
                    other_payment.variance if other_payment is not None else list(zeroes)
                )
                comparison_mortgages.append(
                    {
                        "account": payment.account,
                        "planned": other_planned,
                        "actual": other_actual,
                        "variance": payment_variance,
                        "planned_delta": [
                            value - alternate
                            for value, alternate in zip(payment.planned, other_planned, strict=True)
                        ],
                        "actual_delta": [
                            value - alternate
                            for value, alternate in zip(payment.actual, other_actual, strict=True)
                        ],
                        "variance_delta": [
                            (
                                value - alternate
                                if value is not None and alternate is not None
                                else None
                            )
                            for value, alternate in zip(
                                payment.variance, payment_variance, strict=True
                            )
                        ],
                    }
                )
            for flow_row in report.planning_flows:
                other_flow = compare_flows.get((flow_row.kind, flow_row.account))
                flow_zeroes = [Money(0) for _ in flow_row.planned]
                other_flow_planned = other_flow.planned if other_flow is not None else flow_zeroes
                other_flow_actual = other_flow.actual if other_flow is not None else flow_zeroes
                other_flow_variance: list[Money | None] = (
                    other_flow.variance if other_flow is not None else list(flow_zeroes)
                )
                comparison_flows.append(
                    {
                        "kind": flow_row.kind.value,
                        "account": flow_row.account,
                        "planned": other_flow_planned,
                        "actual": other_flow_actual,
                        "variance": other_flow_variance,
                        "planned_delta": [
                            value - alternate
                            for value, alternate in zip(
                                flow_row.planned, other_flow_planned, strict=True
                            )
                        ],
                        "actual_delta": [
                            value - alternate
                            for value, alternate in zip(
                                flow_row.actual, other_flow_actual, strict=True
                            )
                        ],
                        "variance_delta": [
                            (
                                value - alternate
                                if value is not None and alternate is not None
                                else None
                            )
                            for value, alternate in zip(
                                flow_row.variance, other_flow_variance, strict=True
                            )
                        ],
                    }
                )

        return {
            "controls": {
                "from": start.strftime("%Y-%m"),
                "through": end.strftime("%Y-%m"),
                "minimum": minimum.strftime("%Y-%m"),
                "maximum": maximum_month.strftime("%Y-%m"),
                "period": grouping.value,
                "measure": selected_measure.value,
                "scenario": scenario_handle,
                "compare": compare_handle,
                "scenarios": [
                    {"handle": item.handle, "name": item.name} for item in plan.scenarios
                ],
                "assumption_sources": plan.assumption_sources,
            },
            "periods": [
                {
                    "label": item.label,
                    "start": item.start,
                    "end": item.end,
                }
                for item in totals.periods
            ],
            "summary": {
                "planned_cash": totals.planned_cash_change,
                "actual_cash": report.actual_cash_through_as_of,
                "variance": report.cash_variance_through_as_of,
                "opening_cash": report.cash_position.opening,
                "ending_cash": report.cash_position.closing,
                "minimum_cash": report.cash_position.minimum,
                "minimum_cash_date": report.cash_position.minimum_date,
                "unresolved_expected": totals.unresolved_count,
                "unresolved_actuals": totals.unresolved_actual_count,
            },
            "comparison": comparison,
            "cash_bridge": [
                {
                    "kind": row.kind.value,
                    "name": row.name,
                    "planned": row.planned,
                    "actual": row.actual,
                    "variance": row.variance,
                    "totals": {item.value: row.total(item) for item in PlanMeasure},
                }
                for row in report.cash_bridge
            ],
            "categories": [
                {
                    "account": row.account,
                    "name": row.name,
                    "full_name": row.full_name,
                    "class": row.account_class.value,
                    "depth": row.depth,
                    "planned": row.planned,
                    "actual": row.actual,
                    "variance": row.variance,
                    "totals": {item.value: row.total(item) for item in PlanMeasure},
                }
                for row in report.categories
            ],
            "mortgage_payments": [
                {
                    "account": row.account,
                    "account_name": row.account_name,
                    "full_name": row.full_name,
                    "name": row.name,
                    "planned": row.planned,
                    "actual": row.actual,
                    "variance": row.variance,
                    "totals": {item.value: row.total(item) for item in PlanMeasure},
                }
                for row in report.mortgage_payments
            ],
            "planning_flows": [
                {
                    "kind": row.kind.value,
                    "account": row.account,
                    "account_name": row.account_name,
                    "full_name": row.full_name,
                    "name": row.name,
                    "planned": row.planned,
                    "actual": row.actual,
                    "variance": row.variance,
                    "totals": {item.value: row.total(item) for item in PlanMeasure},
                }
                for row in report.planning_flows
            ],
            "column_totals": {
                "income": {
                    item.value: {
                        "periods": report.category_totals(AccountClass.INCOME, item),
                        "total": report.category_grand_total(AccountClass.INCOME, item),
                    }
                    for item in PlanMeasure
                },
                "expense": {
                    item.value: {
                        "periods": report.category_totals(AccountClass.EXPENSE, item),
                        "total": report.category_grand_total(AccountClass.EXPENSE, item),
                    }
                    for item in PlanMeasure
                },
                "operating_net": {
                    item.value: {
                        "periods": report.operating_net_totals(item),
                        "total": report.operating_net_grand_total(item),
                    }
                    for item in PlanMeasure
                },
                "cash_bridge": {
                    item.value: {
                        "periods": report.cash_bridge_totals(item),
                        "total": report.cash_bridge_grand_total(item),
                    }
                    for item in PlanMeasure
                },
                "planning_flows": {
                    item.value: {
                        "periods": report.planning_flow_totals(item),
                        "total": report.planning_flow_grand_total(item),
                    }
                    for item in PlanMeasure
                },
                "mortgage_payments": {
                    item.value: {
                        "periods": report.mortgage_payment_totals(item),
                        "total": report.mortgage_payment_grand_total(item),
                    }
                    for item in PlanMeasure
                },
                "net_cash": {
                    item.value: {
                        "periods": report.cash_totals(item),
                        "total": report.grand_total(item),
                    }
                    for item in PlanMeasure
                },
            },
        }

    def plan_settings_save(self, payload: dict) -> dict:
        """Validate and persist the shared per-book Plan presentation."""
        result = self.plan(
            str(payload.get("from", "")) or None,
            str(payload.get("through", "")) or None,
            str(payload.get("period", "")) or None,
            str(payload["scenario"]) if payload.get("scenario") else None,
            str(payload["compare"]) if payload.get("compare") else None,
            str(payload.get("measure", "")) or None,
        )
        controls = result["controls"]
        start = date.fromisoformat(f"{controls['from']}-01")
        through = date.fromisoformat(f"{controls['through']}-01")
        PlanSettings(
            start=start,
            end=self._month_end(through.year, through.month),
            period=activity.ReportingPeriod(str(controls["period"])),
            measure=PlanMeasure(str(controls["measure"])),
            scenario=controls["scenario"],
            compare=(str(payload.get("compare")) if payload.get("compare") else None),
        ).save(self.db)
        return controls

    def plan_detail(
        self,
        account_handle: str,
        start_value: str,
        end_value: str,
        scenario_handle: str | None = None,
        flow_kind: str | None = None,
        requirement_kind: str | None = None,
    ) -> dict:
        """Explain one Plan category, planning-flow, or cash-requirement cell."""
        start = date.fromisoformat(start_value)
        end = date.fromisoformat(end_value)
        scenarios = list(self.db.iter_scenarios())
        if scenario_handle:
            scenario = next((item for item in scenarios if item.handle == scenario_handle), None)
            if scenario is None:
                raise KeyError(scenario_handle)
            scenario_name = scenario.name
        else:
            scenario = self._base_scenario(start, end)
            scenario_name = "Base scenario"

        detail: (
            activity.CategoryPeriodDetail
            | activity.PlanningFlowPeriodDetail
            | activity.MortgagePaymentPeriodDetail
        )
        if requirement_kind:
            if requirement_kind != "mortgage":
                raise ValueError("unknown Plan cash-requirement kind")
            mortgage_detail = activity.explain_mortgage_payment_period(
                self.db, account_handle, start, end, scenario=scenario
            )
            detail = mortgage_detail
            category = {
                "account": mortgage_detail.account,
                "name": mortgage_detail.name,
                "full_name": mortgage_detail.full_name,
                "class": "cash_requirement",
                "kind": "mortgage",
            }
        elif flow_kind:
            kind = PlanningFlowKind(flow_kind)
            flow_detail = activity.explain_planning_flow_period(
                self.db, kind, account_handle, start, end, scenario=scenario
            )
            detail = flow_detail
            category = {
                "account": flow_detail.account,
                "name": flow_detail.name,
                "full_name": flow_detail.full_name,
                "class": "planning_flow",
                "kind": kind.value,
            }
        else:
            category_detail = activity.explain_category_period(
                self.db, account_handle, start, end, scenario=scenario
            )
            detail = category_detail
            category = {
                "account": category_detail.account,
                "name": category_detail.name,
                "full_name": category_detail.full_name,
                "class": category_detail.account_class.value,
            }
        return {
            "category": category,
            "period": {"start": detail.start, "end": detail.end},
            "scenario": {"handle": scenario_handle, "name": scenario_name},
            "summary": {
                "planned": detail.planned,
                "actual": detail.actual,
                "variance": detail.variance,
            },
            "planned": [
                {
                    "occurrence": item.occurrence,
                    "date": item.planned_date,
                    "description": item.description,
                    "source": item.source,
                    "status": item.status,
                    "expected": item.expected,
                    "actual": item.actual,
                    "variance": item.variance,
                    "actual_transaction": item.actual_transaction,
                    "actual_date": item.actual_date,
                    "explanation": list(item.explanation),
                }
                for item in detail.planned_events
            ],
            "actuals": [
                {
                    "transaction": item.transaction,
                    "date": item.post_date,
                    "description": item.description,
                    "amount": item.amount,
                    "resolution": item.resolution.value,
                    "planned_occurrence": item.planned_occurrence,
                    "planned_for": item.planned_for,
                    "expected": item.expected,
                    "variance": item.variance,
                    "date_variance_days": item.date_variance_days,
                    "explanation": list(item.explanation),
                }
                for item in detail.actual_transactions
            ],
        }

    @staticmethod
    def _actual_amount(transaction: Transaction) -> Money:
        total = Money(0)
        for split in transaction.splits:
            if split.value > 0:
                total = total + split.value
        return total

    def _review_fsa_options(self, transaction: Transaction) -> dict:
        roles: list[dict[str, object]] = []
        for split in transaction.splits:
            account = self.db.get_account(split.account)
            if account is None:
                continue
            if account.account_class is AccountClass.EXPENSE and split.value > 0:
                roles.append(
                    {
                        "role": "payment",
                        "split": split.handle,
                        "account": self.db.full_name(account),
                    }
                )
            if account.account_class is AccountClass.EXPENSE and split.value < 0:
                roles.append(
                    {
                        "role": "refund",
                        "split": split.handle,
                        "account": self.db.full_name(account),
                    }
                )
            if account.atype is AccountType.FSA and split.value < 0:
                roles.append(
                    {
                        "role": "reimbursement",
                        "split": split.handle,
                        "account": self.db.full_name(account),
                        "years": [
                            year.start.isoformat()
                            for year in account.fsa_years
                            if transaction.post_date <= (year.runout_through or year.through)
                        ],
                    }
                )
        claims = []
        for suggestion in fsa_claims.suggest_claims_for_transaction(self.db, transaction):
            claim = suggestion.claim
            summary = fsa_claims.claim_summary(self.db, claim)
            claims.append(
                {
                    "handle": claim.handle,
                    "label": (
                        f"{claim.service_date.isoformat()} "
                        f"{claim.provider or claim.description or 'FSA claim'}"
                    ),
                    "remaining": summary.remaining_reimbursable,
                    "score": suggestion.score,
                    "reason": suggestion.reason,
                    "suggested_role": suggestion.role,
                    "suggested_split": suggestion.split_handle,
                }
            )
        return {"roles": roles, "claims": claims}

    def review(self, transaction_handle: str | None = None) -> dict:
        """Unresolved actuals and candidate plan occurrences for Review."""
        transactions = sorted(
            (
                transaction
                for transaction in self.db.iter_transactions()
                if transaction.planning_resolution is PlanningResolution.UNRESOLVED
            ),
            key=lambda transaction: (transaction.post_date, transaction.handle),
        )
        actuals = [
            {
                "handle": transaction.handle,
                "date": transaction.post_date,
                "description": transaction.description,
                "amount": self._actual_amount(transaction),
            }
            for transaction in transactions
        ]

        selected = None
        candidates: list[dict] = []
        if transaction_handle is not None:
            transaction = self.db.get_transaction(transaction_handle)
            if transaction is None:
                raise KeyError(transaction_handle)
            if transaction.planning_resolution is not PlanningResolution.UNRESOLVED:
                raise ValueError("transaction is no longer awaiting review")
            actual_amount = self._actual_amount(transaction)
            selected = {
                "handle": transaction.handle,
                "date": transaction.post_date,
                "description": transaction.description,
                "amount": actual_amount,
                "fsa": self._review_fsa_options(transaction),
            }
            for candidate in planning.match_candidates(self.db, transaction):
                event = candidate.event
                candidates.append(
                    {
                        "key": event.key,
                        "date": event.planned_date,
                        "description": event.description,
                        "expected_amount": event.expected_amount,
                        "date_distance_days": candidate.date_distance,
                        "date_variance_days": (transaction.post_date - event.planned_date).days,
                        "amount_difference": candidate.amount_difference,
                        "amount_variance": actual_amount - event.expected_amount,
                        "common_accounts": candidate.common_accounts,
                    }
                )

        return {
            "actuals": actuals,
            "selected": selected,
            "candidates": candidates,
        }

    def _projection_draft(
        self, scenario_handle: str | None = None, years: int | None = None
    ) -> Scenario:
        """Return a detached projection scenario safe for browser-side editing."""
        if scenario_handle:
            stored = self.db.get_scenario(scenario_handle)
            if stored is None:
                raise KeyError(scenario_handle)
            scenario = stored.clone()
        else:
            scenario = self._management_base_scenario()
        if years is not None:
            if years < 1 or years > 100:
                raise ValueError("projection years must be between 1 and 100")
            scenario.years = years
        return scenario

    def _apply_projection_payload(self, scenario: Scenario, payload: dict) -> Scenario:
        """Apply editable projection controls to a detached scenario."""
        years = int(payload.get("years", scenario.years))
        if years < 1 or years > 100:
            raise ValueError("projection years must be between 1 and 100")
        scenario.years = years
        previous = scenario.effective_assumptions()
        updated = self._assumptions_from_payload(
            payload.get("assumptions", previous.serialize()), previous
        )
        if scenario.inherits_base_assumptions:
            for field in scenario.assumption_sources():
                if getattr(updated, field) != getattr(previous, field):
                    scenario.set_assumption_override(field, getattr(updated, field))
            scenario.assumptions.per_account = dict(updated.per_account)
        else:
            scenario.assumptions = updated
        return scenario

    def _projection_payload(self, scenario: Scenario, *, base: bool = False, result=None) -> dict:
        if result is None:
            result = projection.project(self.db, scenario)
        assumptions = scenario.effective_assumptions()
        return {
            "scenario": {
                "handle": None if base else scenario.handle,
                "name": scenario.name,
                "parent_handle": None if base else scenario.parent_handle,
                "years": scenario.years,
                "assumptions": assumptions.serialize(),
                "assumption_sources": scenario.assumption_sources(scenario.start),
                "account_assumption_sources": scenario.account_assumption_sources(scenario.start),
                "assumption_overrides": sorted(scenario.assumption_overrides),
            },
            "controls": {
                "scenarios": [
                    {"handle": None, "name": "Base scenario"},
                    *[
                        {"handle": item.handle, "name": item.name}
                        for item in self.db.iter_scenarios()
                    ],
                ],
            },
            "summary": result.summary(),
            "warnings": list(result.warnings),
            "rows": [
                {
                    "label": row.label,
                    "income": row.income,
                    "expense": row.expense,
                    "cash": row.cash_close,
                    "holdings": row.holdings,
                    "liabilities": row.liabilities,
                    "net_worth": row.net_worth,
                }
                for row in result.rows
            ],
        }

    def projection_explain(self, payload: dict) -> dict:
        """Explain one month of the currently applied projection draft."""
        scenario = self._projection_draft(payload.get("handle"))
        scenario = self._apply_projection_payload(scenario, payload)
        result = projection.project(self.db, scenario)
        detail = projection.explain_month(self.db, result, int(payload["month_index"]))

        def account_row(item: projection.ProjectionAccountDetail) -> dict:
            return {
                "handle": item.handle,
                "name": item.name,
                "opening": item.opening,
                "movement": item.movement,
                "accrual": item.accrual,
                "closing": item.closing,
                "annual_rate": item.annual_rate,
                "annual_rate_source": item.annual_rate_source,
                "activities": dict(item.activities),
            }

        return {
            "index": detail.index,
            "month": detail.month,
            "label": detail.label,
            "cash": {
                "opening": detail.cash_open,
                "flow": detail.cash_flow,
                "interest": detail.cash_interest,
                "closing": detail.cash_close,
            },
            "income": detail.income,
            "expense": detail.expense,
            "holdings": {
                "opening": detail.holdings_open,
                "movement": detail.holding_movements,
                "contributions": detail.holding_contributions,
                "withdrawals": detail.holding_withdrawals,
                "retirement_distributions": detail.retirement_distributions,
                "investment_income": detail.investment_income,
                "fees": detail.investment_fees,
                "rollovers": detail.holding_rollovers,
                "growth": detail.investment_growth,
                "closing": detail.holdings_close,
                "accounts": [account_row(item) for item in detail.holdings],
            },
            "liabilities": {
                "opening": detail.liabilities_open,
                "movement": detail.liability_movements,
                "interest": detail.liability_interest,
                "closing": detail.liabilities_close,
                "accounts": [account_row(item) for item in detail.liabilities],
            },
            "net_worth": detail.net_worth,
            "assumptions": detail.assumptions.serialize(),
            "assumption_sources": detail.assumption_sources,
            "events": [event.as_dict() for event in detail.events],
            "escrow_explanations": list(detail.escrow_explanations),
        }

    def projection(self, scenario_handle: str | None = None, years: int | None = None) -> dict:
        """Calculate a persisted Base/saved scenario without mutating it."""
        return self._projection_payload(
            self._projection_draft(scenario_handle, years), base=scenario_handle is None
        )

    def projection_calculate(self, payload: dict) -> dict:
        """Calculate an edited projection draft without persisting the edits."""
        handle = str(payload.get("handle") or "").strip() or None
        scenario = self._projection_draft(handle)
        self._apply_projection_payload(scenario, payload)
        return self._projection_payload(scenario, base=handle is None)

    def projection_compare(self, payload: dict) -> dict:
        """Compare an edited projection draft with another persisted scenario."""
        handle = str(payload.get("handle") or "").strip() or None
        compare_handle = str(payload.get("compare_handle") or "").strip() or None
        if handle == compare_handle:
            raise ValueError("choose two different scenarios to compare")

        primary = self._projection_draft(handle)
        self._apply_projection_payload(primary, payload)
        comparison = self._projection_draft(compare_handle)
        comparison.years = primary.years

        primary_result = projection.project(self.db, primary)
        comparison_result = projection.project(self.db, comparison)
        if len(primary_result.rows) != len(comparison_result.rows):
            raise ValueError("projection comparison horizons do not align")

        def difference(left, right):
            return left - right

        primary_summary = primary_result.summary()
        comparison_summary = comparison_result.summary()
        return {
            "primary": self._projection_payload(
                primary, base=handle is None, result=primary_result
            ),
            "comparison": {
                "scenario": {
                    "handle": None if compare_handle is None else comparison.handle,
                    "name": comparison.name,
                    "assumptions": comparison.effective_assumptions().serialize(),
                    "assumption_sources": comparison.assumption_sources(comparison.start),
                },
                "summary": comparison_summary,
                "summary_delta": {
                    "ending_net_worth": difference(
                        primary_summary["ending_net_worth"],
                        comparison_summary["ending_net_worth"],
                    ),
                    "ending_cash": difference(
                        primary_summary["ending_cash"],
                        comparison_summary["ending_cash"],
                    ),
                    "minimum_cash": difference(
                        primary_summary["minimum_cash"],
                        comparison_summary["minimum_cash"],
                    ),
                },
                "rows": [
                    {
                        "label": left.label,
                        "cash": right.cash_close,
                        "net_worth": right.net_worth,
                        "cash_delta": difference(left.cash_close, right.cash_close),
                        "net_worth_delta": difference(left.net_worth, right.net_worth),
                    }
                    for left, right in zip(primary_result.rows, comparison_result.rows, strict=True)
                ],
            },
        }

    def projection_save(self, payload: dict) -> dict:
        """Persist projection controls explicitly, preserving hidden model fields."""
        handle = str(payload.get("handle") or "").strip() or None
        scenario = self._projection_draft(handle)
        self._apply_projection_payload(scenario, payload)
        if handle is None:
            base_result = save_base_assumptions(self.db, SaveBaseAssumptions(scenario.assumptions))
            if not base_result.ok:
                raise self._service_resource_error(base_result.errors[0])
            return self._projection_payload(scenario, base=True)
        result = save_scenario_assumptions(
            self.db, SaveScenarioAssumptions(scenario, existing_handle=handle)
        )
        if not result.ok:
            raise self._service_resource_error(result.errors[0])
        return self._projection_payload(scenario)

    # ---------------------------------------------------------------- writing

    def add_transaction(self, payload: dict) -> dict:
        """Post a two-split transaction. The only write the web interface allows."""
        debit = self.db.get_account_by_name(payload["to"])
        credit = self.db.get_account_by_name(payload["from"])
        if debit is None or credit is None:
            raise ResourceError(400, "transaction.account.not_found", ("from", "to"))
        when = date.fromisoformat(payload.get("date") or date.today().isoformat())
        amount = self._input_money(payload, payload["amount"])
        if amount <= 0:
            raise ResourceError(400, "transaction.amount.non_positive", ("amount",))
        description = str(payload.get("description") or "").strip()
        notes = str(payload.get("notes") or "").strip()
        memo = payload.get("memo", "")
        investment_raw = str(payload.get("investment_activity") or "").strip()
        try:
            investment_activity = InvestmentActivityKind(investment_raw) if investment_raw else None
        except ValueError:
            raise ValueError("choose a valid investment activity") from None
        claim_handle = str(payload.get("fsa_claim") or "").strip()
        claim_role = str(payload.get("fsa_role") or "").strip()
        funding_year = str(payload.get("fsa_year") or "").strip()
        attachment = (
            ClaimAttachment(
                claim_handle,
                claim_role,
                date.fromisoformat(funding_year) if funding_year else None,
            )
            if claim_handle and claim_role
            else None
        )
        currency = transaction_currency(self.db)
        result = save_transaction(
            self.db,
            SaveTransaction(
                TransactionInput(
                    post_date=when,
                    description=description,
                    notes=notes,
                    currency=currency,
                    splits=(
                        TransactionSplitInput(debit.handle, Amount(amount, currency), memo=memo),
                        TransactionSplitInput(credit.handle, Amount(-amount, currency), memo=memo),
                    ),
                    investment_activity=investment_activity,
                ),
                claim_attachment=attachment,
            ),
        )
        if result.value is None:
            raise self._service_resource_error(result.errors[0])
        return {"handle": result.value.handle, "date": when, "amount": amount}

    def review_match(self, payload: dict) -> dict:
        result = match_review(
            self.db,
            ReviewOccurrence(str(payload["transaction"]), str(payload["occurrence"])),
        )
        if not result.ok:
            raise self._service_resource_error(result.errors[0])
        mutation = result.value
        assert mutation is not None and mutation.resolution is not None
        return {
            "transaction": mutation.transaction,
            "resolution": mutation.resolution.value,
            "occurrence": mutation.occurrence,
        }

    def review_reject(self, payload: dict) -> dict:
        result = reject_review(
            self.db,
            ReviewOccurrence(str(payload["transaction"]), str(payload["occurrence"])),
        )
        if not result.ok:
            raise self._service_resource_error(result.errors[0])
        mutation = result.value
        assert mutation is not None
        return {
            "transaction": mutation.transaction,
            "rejected": mutation.occurrence,
        }

    def review_skip(self, payload: dict) -> dict:
        result = skip_review(
            self.db,
            ReviewOccurrence(str(payload["transaction"]), str(payload["occurrence"])),
        )
        if not result.ok:
            raise self._service_resource_error(result.errors[0])
        mutation = result.value
        assert mutation is not None
        return {"transaction": mutation.transaction, "skipped": mutation.occurrence}

    def review_fsa_attach(self, payload: dict) -> dict:
        funding_year = str(payload.get("funding_year") or "").strip()
        result = attach_review_claim(
            self.db,
            ReviewClaimAttachment(
                str(payload["transaction"]),
                str(payload["claim"]),
                str(payload["role"]),
                str(payload.get("split") or "") or None,
                date.fromisoformat(funding_year) if funding_year else None,
            ),
        )
        if not result.ok:
            raise self._service_resource_error(result.errors[0])
        mutation = result.value
        assert mutation is not None
        return {"claim": mutation.claim, "transaction": mutation.transaction}

    def review_unexpected(self, payload: dict) -> dict:
        result = mark_review_unexpected(self.db, ReviewTransaction(str(payload["transaction"])))
        if not result.ok:
            raise self._service_resource_error(result.errors[0])
        mutation = result.value
        assert mutation is not None and mutation.resolution is not None
        return {
            "transaction": mutation.transaction,
            "resolution": mutation.resolution.value,
        }

    def scheduled_occurrence_options(self, payload: dict) -> dict:
        """Return selectable occurrence dates for schedule exception editors."""
        frequency = str(payload.get("frequency") or "monthly")
        if frequency not in self._SCENARIO_FREQUENCIES:
            raise ValueError("unsupported schedule frequency")
        period, interval = self._SCENARIO_FREQUENCIES[frequency]
        try:
            start = date.fromisoformat(str(payload.get("start") or ""))
        except ValueError as exc:
            raise ValueError("first due date is invalid") from exc
        end = None
        raw_end = str(payload.get("end") or "").strip()
        raw_count = str(payload.get("count") or "").strip()
        if raw_end and raw_count:
            raise ValueError("choose an end date or occurrence count, not both")
        if period is not PeriodType.ONCE and raw_end:
            try:
                end = date.fromisoformat(raw_end)
            except ValueError as exc:
                raise ValueError("end date is invalid") from exc
            if end < start:
                raise ValueError("end date cannot precede first due date")
        count = None
        if period is not PeriodType.ONCE and raw_count:
            try:
                count = int(raw_count)
            except ValueError as exc:
                raise ValueError("occurrence count must be a whole number") from exc
            if count < 1:
                raise ValueError("occurrence count must be positive")
        weekend_key = str(payload.get("weekend") or "none")
        if weekend_key not in self._SCENARIO_WEEKENDS:
            raise ValueError("unsupported weekend adjustment")
        recurrence = Recurrence(
            period=period,
            interval=interval,
            start=start,
            end=end,
            count=count,
            weekend_adjust=self._SCENARIO_WEEKENDS[weekend_key],
        )
        horizon = date(min(start.year + 10, 9999), 12, 31)
        result: dict[str, object] = {
            "occurrences": [item.isoformat() for item in recurrence.occurrences(horizon)[:500]]
        }
        raw_amount = str(payload.get("amount") or "").strip()
        if raw_amount:
            try:
                amount = abs(self._input_money(payload, raw_amount))
            except (ValueError, ArithmeticError) as exc:
                raise ValueError("amount must be a valid number") from exc
            if not amount:
                raise ValueError("amount must be greater than zero")
            amount_changes = self._parse_amount_changes(payload, start)
            skipped = self._parse_skipped(payload, recurrence)
            adjustments = self._parse_occurrence_adjustments(payload, recurrence)
            if set(skipped) & {item.when for item in adjustments}:
                raise ValueError("an occurrence cannot be both skipped and overridden")
            result["preview"] = [
                {"when": when, "amount": value, "status": status}
                for when, value, status in scheduled_occurrence_preview(
                    recurrence, amount, amount_changes, skipped, adjustments
                )
            ]
        return result

    def scheduled_save(self, payload: dict) -> dict:
        """Create or update a fixed-split baseline schedule."""
        handle = str(payload.get("handle") or "").strip()
        existing = self.db.get_scheduled(handle) if handle else None
        if handle and existing is None:
            raise KeyError(handle)
        existing_parts = self._simple_schedule_parts(existing) if existing is not None else None
        if existing is not None:
            editability = schedule.schedule_editability(self.db, existing)
            if not editability.editable:
                raise ValueError(editability.reason)
            if (
                editability.mode is not schedule.ScheduleEditorMode.FIXED
                or existing_parts is None
                or self._frequency_key(existing.recurrence) is None
            ):
                raise ValueError(
                    "this schedule needs an editor that preserves its complete structure"
                )

        name = str(payload.get("name") or "").strip()
        if not name:
            raise ValueError("schedule name is required")
        category_handle = str(payload.get("category") or "").strip()
        funding_handle = str(payload.get("funding") or "").strip()
        investment_activity_raw = str(payload.get("investment_activity") or "").strip()
        try:
            investment_activity = (
                InvestmentActivityKind(investment_activity_raw) if investment_activity_raw else None
            )
        except ValueError:
            raise ValueError("choose a valid investment activity") from None
        category_planning_flow = None
        category_ledger_direction = None
        if existing is not None and existing_parts is not None:
            raw_category_flow = existing_parts.get("category_planning_flow")
            category_planning_flow = (
                PlanningFlowKind(str(raw_category_flow)) if raw_category_flow else None
            )
            raw_direction = existing_parts.get("category_ledger_direction")
            category_ledger_direction = int(raw_direction) if raw_direction is not None else None
        if "category_planning_flow" in payload:
            raw_category_flow = str(payload.get("category_planning_flow") or "").strip()
            try:
                category_planning_flow = (
                    PlanningFlowKind(raw_category_flow) if raw_category_flow else None
                )
            except ValueError:
                raise ValueError("choose a valid category planning purpose") from None
            category_ledger_direction = None
        try:
            amount = self._input_money(payload, payload.get("amount") or "0")
        except (ValueError, ArithmeticError) as exc:
            raise ValueError("amount must be a valid number") from exc
        recurrence = self._schedule_recurrence_from_payload(payload, existing)
        amount_changes = self._parse_amount_changes(payload, recurrence.start)
        skipped = self._parse_skipped(payload, recurrence)
        adjustments = self._parse_occurrence_adjustments(payload, recurrence)
        if set(skipped) & {change.when for change in adjustments}:
            raise ValueError("an occurrence cannot be both skipped and overridden")

        default_growth_policy = (
            existing.growth_policy if existing is not None else ScheduleGrowthPolicy.AUTO
        )
        try:
            growth_policy = ScheduleGrowthPolicy(
                str(payload.get("growth_policy") or default_growth_policy.value)
            )
        except ValueError:
            raise ValueError("choose a valid projection growth policy") from None

        planning_flow_raw = str(payload.get("planning_flow") or "").strip()
        try:
            planning_flow = PlanningFlowKind(planning_flow_raw) if planning_flow_raw else None
        except ValueError:
            raise ValueError("choose a valid planning purpose") from None
        additional_splits = self._parse_additional_splits(
            payload, {category_handle, funding_handle}
        )
        category_memo = (
            str(payload.get("category_memo") or "").strip()
            if "category_memo" in payload
            else str(existing_parts["category_memo"])
            if existing_parts is not None
            else ""
        )
        funding_memo = (
            str(payload.get("funding_memo") or "").strip()
            if "funding_memo" in payload
            else str(existing_parts["funding_memo"])
            if existing_parts is not None
            else ""
        )
        selected_handles = {
            category_handle,
            funding_handle,
            *(split.account for split in additional_splits),
        }
        existing_split_changes = (
            {split.account: tuple(split.amount_changes) for split in existing.splits}
            if existing is not None
            else {}
        )
        split_changes = (
            self._parse_split_amount_changes(
                payload,
                recurrence.start,
                selected_handles,
            )
            if "split_amount_changes" in payload
            else existing_split_changes
        )
        placeholder = bool(payload.get("placeholder", False))
        evidence = existing.estimate_evidence if existing is not None else None
        if "estimate_evidence" in payload:
            raw_evidence = payload.get("estimate_evidence")
            if raw_evidence is not None and not isinstance(raw_evidence, dict):
                raise ValueError("estimate evidence must be an object")
            evidence = raw_evidence
        seasonal_amounts = (
            self._parse_seasonal_amounts(payload)
            if "seasonal_amounts" in payload
            else list(existing.seasonal_amounts)
            if existing is not None
            else []
        )
        enabled = existing.enabled if existing is not None else True
        if "enabled" in payload:
            if not isinstance(payload["enabled"], bool):
                raise ValueError("active status must be true or false")
            enabled = payload["enabled"]

        result = save_fixed_schedule(
            self.db,
            SaveFixedSchedule(
                definition=FixedScheduleInput(
                    name=name,
                    recurrence=recurrence,
                    category=category_handle,
                    funding=funding_handle,
                    amount=amount,
                    category_planning_flow=category_planning_flow,
                    funding_planning_flow=planning_flow,
                    investment_activity=investment_activity,
                    category_ledger_direction=category_ledger_direction,
                    additional_splits=additional_splits,
                    category_memo=category_memo,
                    funding_memo=funding_memo,
                    enabled=enabled,
                    auto_create=bool(payload.get("auto", False)),
                    placeholder=placeholder,
                    growth_policy=growth_policy,
                    amount_changes=tuple(amount_changes),
                    split_amount_changes={
                        account: tuple(changes) for account, changes in split_changes.items()
                    },
                    seasonal_amounts=tuple(seasonal_amounts),
                    skipped=tuple(skipped),
                    occurrence_adjustments=tuple(adjustments),
                    estimate_evidence=evidence,
                ),
                existing_handle=existing.handle if existing is not None else None,
            ),
        )
        if result.value is None:
            raise self._service_resource_error(result.errors[0])
        return {"handle": result.value.handle, "name": result.value.name}

    def scheduled_formula_save(self, payload: dict) -> dict:
        """Update formula inputs and ordinary metadata without exposing owned mechanics."""
        handle = str(payload.get("handle") or "").strip()
        existing = self.db.get_scheduled(handle)
        if existing is None:
            raise KeyError(handle)
        raw_formulas = payload.get("formulas")
        if not isinstance(raw_formulas, list):
            raise ValueError("formula expressions must be a list")
        formulas: dict[int, str] = {}
        for row in raw_formulas:
            if not isinstance(row, dict):
                raise ValueError("formula expressions must identify their split")
            raw_index = row.get("index")
            if not isinstance(raw_index, (int, str)):
                raise ValueError("formula split index is invalid")
            try:
                index = int(raw_index)
            except (TypeError, ValueError) as exc:
                raise ValueError("formula split index is invalid") from exc
            if index in formulas:
                raise ValueError("formula split indices cannot be repeated")
            formulas[index] = str(row.get("formula") or "")
        raw_variables = payload.get("variables")
        if not isinstance(raw_variables, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in raw_variables.items()
        ):
            raise ValueError("formula variables must be a name-to-value object")
        name = str(payload.get("name") or "").strip()
        if not name:
            raise ValueError("schedule name is required")
        recurrence = self._schedule_recurrence_from_payload(payload, existing)
        try:
            growth_policy = ScheduleGrowthPolicy(
                str(payload.get("growth_policy") or existing.growth_policy.value)
            )
        except ValueError:
            raise ValueError("choose a valid projection growth policy") from None
        enabled = existing.enabled
        if "enabled" in payload:
            if not isinstance(payload["enabled"], bool):
                raise ValueError("active status must be true or false")
            enabled = payload["enabled"]
        placeholder = existing.placeholder
        if "placeholder" in payload:
            if not isinstance(payload["placeholder"], bool):
                raise ValueError("schedule kind must be a boolean estimate flag")
            placeholder = payload["placeholder"]
        auto_create = existing.auto_create
        if "auto" in payload:
            if not isinstance(payload["auto"], bool):
                raise ValueError("automatic posting status must be true or false")
            auto_create = payload["auto"]

        result = save_formula_schedule(
            self.db,
            FormulaScheduleInput(
                existing_handle=existing.handle,
                name=name,
                recurrence=recurrence,
                formulas=formulas,
                variables=raw_variables,
                enabled=enabled,
                auto_create=auto_create,
                placeholder=placeholder,
                growth_policy=growth_policy,
                skipped=tuple(self._parse_skipped(payload, recurrence)),
            ),
        )
        if result.value is None:
            raise self._service_resource_error(result.errors[0])
        return {"handle": result.value.handle, "name": result.value.name}

    def scheduled_delete(self, payload: dict) -> dict:
        """Remove one definition while retaining its posted ledger history."""
        handle = str(payload.get("handle") or "").strip()
        result = delete_schedule(self.db, DeleteSchedule(handle))
        if not result.ok:
            raise self._service_resource_error(result.errors[0])
        deleted = result.value
        assert deleted is not None
        return {"handle": deleted.handle, "name": deleted.name}

    def scheduled_duplicate(self, payload: dict) -> dict:
        """Save an independent exact copy, including editor-protected fields."""
        result = duplicate_schedule(
            self.db,
            DuplicateSchedule(
                str(payload.get("handle") or ""),
                name=str(payload.get("name") or ""),
            ),
        )
        if not result.ok:
            raise self._service_resource_error(result.errors[0])
        copied = result.value
        assert copied is not None
        return {"handle": copied.handle, "name": copied.name}

    def import_local(self, payload: dict) -> dict:
        path = str(payload.get("path") or "").strip()
        include_scheduled = payload.get("include_scheduled", True)
        if not isinstance(include_scheduled, bool):
            raise ValueError("include_scheduled must be true or false")
        result = import_book(
            self.db,
            ImportBook(
                source=path,
                format=(str(payload["format"]) if payload.get("format") else None),
                include_scheduled=include_scheduled,
                number_format=str(payload.get("number_format") or "auto"),
                date_format=str(payload.get("date_format") or "auto"),
            ),
        )
        if not result.ok:
            raise self._service_resource_error(result.errors[0])
        imported = result.value
        assert imported is not None
        return {"format": imported.format_name, "detail": imported.result.detail(limit=50)}

    def import_defaults(self) -> dict:
        """Return per-book presentation state without initiating an import."""
        return {"path": remembered_import_source(self.db) or ""}

    def post_scheduled(self) -> dict:
        posted = schedule.post_due(self.db, only_auto=False)
        return {
            "posted": len(posted),
            "transactions": [{"date": t.post_date, "description": t.description} for t in posted],
        }


def api(db: DbSQLite) -> Api:
    return Api(db)


def _plain(values: Mapping[str, object]) -> dict[str, str | None]:
    """Convert Money and date values to strings the browser can read."""
    out: dict[str, str | None] = {}
    for key, value in values.items():
        if hasattr(value, "to_decimal"):
            out[key] = str(value.to_decimal())
        elif hasattr(value, "isoformat"):
            out[key] = value.isoformat()
        else:
            out[key] = str(value) if value is not None else None
    return out


from .transport import build_handler, serve  # noqa: E402  (compatibility re-export)
