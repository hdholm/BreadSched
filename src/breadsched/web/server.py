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

import json
import secrets
import threading
from calendar import monthrange
from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Literal, cast
from urllib.parse import parse_qs, urlencode, urlparse

from ..gen.db.sqlite import DbSQLite
from ..gen.engine import (
    activity,
    budgeting,
    cashflow,
    estimates,
    fsa_claims,
    ledger,
    planning,
    projection,
    schedule,
)
from ..gen.engine.activity import PlanMeasure, PlanSettings
from ..gen.lib import (
    AccountClass,
    AccountType,
    AssumptionPeriod,
    Assumptions,
    FsaClaim,
    FsaClaimAllocation,
    FsaClaimRejection,
    FsaClaimSplitLink,
    FsaFundingYear,
    Money,
    PeriodType,
    PlanningFlowKind,
    PlanningResolution,
    ProjectionBasis,
    Recurrence,
    Scenario,
    ScenarioSchedule,
    ScheduledAmountChange,
    ScheduledOccurrenceAdjustment,
    ScheduledSplit,
    ScheduledTransaction,
    ScheduleGrowthPolicy,
    Split,
    Transaction,
    WeekendAdjust,
    scheduled_occurrence_preview,
)
from ..gen.lib.base import create_handle
from ..gen.plug import IMPORTER, PluginManager
from ..gen.utils.amount_input import NumberFormat, parse_user_amount
from ..gen.utils.logs import get_logger

__all__ = ["serve", "build_handler", "api"]

LOG = get_logger(__name__)
STATIC = Path(__file__).parent / "static"


def _encode(value: object) -> object:
    if isinstance(value, Money):
        return str(value.to_decimal())
    if isinstance(value, (date, Decimal)):
        return str(value)
    raise TypeError(f"cannot serialise {type(value).__name__}")


class Api:
    """The JSON surface. Every method returns plain data, never a response object."""

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
        """The overview: groups, the liquidity verdict, and the pending bills.

        Computed by the same engine the desktop dashboard uses, so the two cannot
        disagree about a household's position.
        """
        from ..gen.engine import dashboard as engine
        from ..gen.engine import fsa

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
            "fsa_claims": [
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
                if summary.status
                not in {
                    fsa_claims.FsaClaimStatus.FULLY_REIMBURSED,
                }
            ],
            "fsa": [
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
            "bills": [
                {
                    "name": bill.name,
                    "next_due": bill.next_due.isoformat(),
                    "days_until": bill.days_until(board.as_of),
                    "cycle_months": float(bill.cycle_months),
                    "amount": str(bill.amount.to_decimal()),
                    "monthly": str(bill.monthly.to_decimal()),
                    "annual": str(bill.annual.to_decimal()),
                    "hold": str(bill.hold(board.as_of).to_decimal()),
                    "estimate": bill.estimate,
                }
                for bill in board.bills
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
            "net_worth": ledger.net_worth(self.db),
            "budgets": [b.name for b in self.db.iter_budgets()],
            "scenarios": [s.name for s in self.db.iter_scenarios()],
        }

    def accounts(self) -> list[dict]:
        """The chart of accounts as a flat list carrying its own depth."""
        rows: list[dict] = []

        def walk(parent: str | None, depth: int) -> None:
            for account in self.db.child_accounts(parent):
                rows.append(
                    {
                        "handle": account.handle,
                        "name": account.name,
                        "full_name": self.db.full_name(account),
                        "type": account.atype.value,
                        "class": account.account_class.value,
                        "placeholder": account.placeholder,
                        "source_type": (
                            account.source_atype.value if account.source_atype else None
                        ),
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
                        "balance": ledger.balance_recursive(self.db, account.handle),
                        "own_balance": ledger.balance(self.db, account.handle),
                    }
                )
                walk(account.handle, depth + 1)

        root = self.db.root_account()
        walk(root.handle if root else None, 0)
        return rows

    def account_type_save(self, payload: dict) -> dict:
        handle = str(payload.get("handle", ""))
        account = self.db.get_account(handle)
        if account is None:
            raise KeyError(handle)
        raw_type = str(payload.get("type", ""))
        try:
            account_type = AccountType(raw_type.strip().upper())
        except ValueError:
            raise ValueError("choose a valid account type") from None
        if account_type in {AccountType.ROOT, AccountType.TECHNICAL}:
            raise ValueError("choose a user account type")
        account.atype = account_type
        with self.db.transaction(f"Set account type for {account.name}") as txn:
            self.db.commit_account(account, txn)
        return {"handle": account.handle, "type": account.atype.value}

    def account_kind_save(self, payload: dict) -> dict:
        """Compatibility endpoint for the former account-kind web client."""
        account = self.db.get_account(str(payload.get("handle", "")))
        if account is None:
            raise KeyError(str(payload.get("handle", "")))
        legacy = str(payload.get("kind", "ordinary"))
        mapping = {
            "retirement": AccountType.RETIREMENT,
            "fsa": AccountType.FSA,
            "investment": AccountType.INVESTMENT,
            "escrow": AccountType.ESCROW,
            "debt": (
                AccountType.CREDIT if account.atype is AccountType.CREDIT else AccountType.LOAN
            ),
        }
        account_type = mapping.get(legacy)
        if account_type is None:
            return {"handle": account.handle, "type": account.atype.value, "kind": legacy}
        result = self.account_type_save({"handle": account.handle, "type": account_type.value})
        return {**result, "kind": legacy}

    def account_planning_role_save(self, payload: dict) -> dict:
        """Compatibility endpoint for older web clients."""
        migrated = dict(payload)
        migrated["kind"] = payload.get("planning_role", "ordinary")
        result = self.account_kind_save(migrated)
        return {
            "handle": result["handle"],
            "kind": result["kind"],
            "planning_role": result["kind"],
        }

    def account_fsa_years_save(self, payload: dict) -> dict:
        handle = str(payload.get("handle", ""))
        account = self.db.get_account(handle)
        if account is None:
            raise KeyError(handle)
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
        with self.db.transaction(f"Set FSA funding years for {account.name}") as txn:
            self.db.commit_account(account, txn)
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
        claim = FsaClaim(
            handle=str(payload.get("handle") or create_handle()),
            service_date=date.fromisoformat(str(payload["service_date"])),
            provider=str(payload.get("provider", "")).strip(),
            description=str(payload.get("description", "")).strip(),
            eob_responsibility=self._input_money(payload, eob) if eob else None,
            payments=[FsaClaimSplitLink.from_dict(item) for item in payload.get("payments", [])],
            refunds=[FsaClaimSplitLink.from_dict(item) for item in payload.get("refunds", [])],
            allocations=[
                FsaClaimAllocation(
                    account=str(item["account"]),
                    funding_year_start=date.fromisoformat(str(item["funding_year_start"])),
                    target=(
                        self._input_money(payload, item["target"])
                        if item.get("target") not in (None, "")
                        else None
                    ),
                    reimbursements=[
                        FsaClaimSplitLink.from_dict(link) for link in item.get("reimbursements", [])
                    ],
                    rejections=[
                        FsaClaimRejection(
                            attempted_on=date.fromisoformat(str(rejection["attempted_on"])),
                            amount=self._input_money(payload, rejection["amount"]),
                            reason=str(rejection.get("reason", "")),
                        )
                        for rejection in item.get("rejections", [])
                    ],
                )
                for item in payload.get("allocations", [])
            ],
        )
        fsa_claims.save_claim(self.db, claim)
        return {"handle": claim.handle}

    def fsa_claim_delete(self, payload: dict) -> dict:
        handle = str(payload.get("handle", ""))
        fsa_claims.delete_claim(self.db, handle)
        return {"handle": handle}

    def register(self, handle: str, limit: int = 250) -> dict:
        account = self.db.get_account(handle)
        if account is None:
            raise KeyError(handle)
        rows = ledger.register(self.db, handle)[-limit:]
        return {
            "account": self.db.full_name(account),
            "type": account.atype.value,
            "rows": [
                {
                    "date": row.post_date,
                    "num": row.transaction.num,
                    "description": row.description,
                    "transfer": row.transfer_label(self.db),
                    "amount": row.amount,
                    "balance": row.running,
                }
                for row in rows
            ],
        }

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
                    "category": item.category,
                    "category_name": item.category_name,
                    "funding": item.funding,
                    "funding_name": item.funding_name,
                    "amount": item.display_amount,
                    "source_name": item.source_name,
                    "destination_name": item.destination_name,
                    "start": item.recurrence.start,
                    "frequency": item.recurrence.describe(),
                    "scheduled_amount": item.scheduled_amount,
                    "active_months": item.active_months,
                    "transaction_count": item.transaction_count,
                    "confidence": item.confidence,
                    "reason": item.reason,
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

    def historical_estimate_accept(self, payload: dict) -> dict:
        """Accept one historical proposal as a normal planning estimate."""
        months = int(payload.get("months") or 12)
        minimum = int(payload.get("min_active_months") or 3)
        category = str(payload.get("category") or "")
        scenario = str(payload.get("scenario") or "").strip() or None
        proposals = estimates.propose_historical_estimates(
            self.db,
            months=months,
            min_active_months=minimum,
            scenario_handle=scenario,
        )
        proposal = next((item for item in proposals if item.category == category), None)
        if proposal is None:
            raise ValueError("historical estimate proposal is no longer available")
        handle = estimates.accept_historical_estimate(self.db, proposal, scenario_handle=scenario)
        return {"handle": handle, "category": proposal.category_name}

    def scheduled(self, days: int = 60) -> dict:
        occurrences = schedule.due_occurrences(self.db, horizon_days=days)
        accounts = sorted(
            (
                account
                for account in self.db.iter_accounts()
                if not account.is_root and not account.placeholder
            ),
            key=self.db.full_name,
        )
        definitions = []
        for item in self.db.iter_scheduled():
            simple = self._simple_schedule_parts(item)
            frequency = self._frequency_key(item.recurrence)
            definitions.append(
                {
                    "handle": item.handle,
                    "name": item.name,
                    "frequency": item.recurrence.describe(),
                    "frequency_key": frequency,
                    "amount": item.amount(),
                    "enabled": item.enabled,
                    "placeholder": item.placeholder,
                    "auto": item.auto_create,
                    "growth_policy": item.growth_policy.value,
                    "simple": simple is not None and frequency is not None,
                    "category": simple["category"] if simple else None,
                    "funding": simple["funding"] if simple else None,
                    "planning_flow": simple["planning_flow"] if simple else None,
                    "additional_splits": (simple["additional_splits"] if simple else []),
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
                    "skipped": [when.isoformat() for when in item.skipped],
                    "occurrence_adjustments": [
                        {"when": change.when.isoformat(), "amount": change.amount}
                        for change in item.occurrence_adjustments
                    ],
                }
            )
        return {
            "definitions": definitions,
            "accounts": [
                {
                    "handle": account.handle,
                    "name": self.db.full_name(account),
                    "class": account.account_class.value,
                }
                for account in accounts
            ],
            "upcoming": [{"date": o.when, "name": o.name, "amount": o.amount} for o in occurrences],
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
            basis=ProjectionBasis.SCHEDULED,
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
        return {
            "handle": None if base else scenario.handle,
            "base": base,
            "name": "Base scenario" if base else scenario.name,
            "description": "" if base else scenario.description,
            "assumptions": scenario.assumptions.serialize(),
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
        self, payload: object, existing: Mapping[str, Decimal] | None = None
    ) -> dict[str, Decimal]:
        if payload is None:
            return dict(existing or {})
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
            self.db.set_metadata("planning.base_assumptions", assumptions.serialize())
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
        scenario.assumptions = self._assumptions_from_payload(
            payload.get("assumptions"), scenario.assumptions
        )
        with self.db.transaction(f"Update scenario {scenario.name}") as txn:
            self.db.commit_scenario(scenario, txn)
        return self._scenario_payload(scenario)

    def _unique_scenario_copy_name(self, name: str) -> str:
        base = f"{name} copy"
        candidate = base
        number = 2
        while self.db.get_scenario_by_name(candidate) is not None:
            candidate = f"{base} {number}"
            number += 1
        return candidate

    def scenario_duplicate(self, payload: dict) -> dict:
        handle = payload.get("handle")
        if handle:
            source = self.db.get_scenario(str(handle))
            if source is None:
                raise KeyError(str(handle))
        else:
            source = self._management_base_scenario()
        clone = Scenario.from_dict(source.serialize())
        clone.handle = create_handle()
        clone.gid = ""
        clone.change = 0
        clone.name = self._unique_scenario_copy_name(source.name)
        with self.db.transaction(f"Duplicate scenario {source.name}") as txn:
            self.db.add_scenario(clone, txn)
        return self._scenario_payload(clone)

    def scenario_delete(self, payload: dict) -> dict:
        handle = str(payload.get("handle", "")).strip()
        if not handle:
            raise ValueError("Base scenario cannot be deleted")
        scenario = self.db.get_scenario(handle)
        if scenario is None:
            raise KeyError(handle)
        with self.db.transaction(f"Delete scenario {scenario.name}") as txn:
            self.db.remove_scenario(handle, txn)
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
        if existing_period is None:
            scenario.assumption_periods.append(period)
        else:
            scenario.assumption_periods[index] = period
        with self.db.transaction(f"Update scenario {scenario.name}") as txn:
            self.db.commit_scenario(scenario, txn)
        return self._scenario_payload(scenario)

    def scenario_period_delete(self, payload: dict) -> dict:
        handle = str(payload.get("handle", "")).strip()
        scenario = self.db.get_scenario(handle) if handle else None
        if scenario is None:
            raise ValueError("dated assumptions belong to a saved scenario")
        index = int(payload.get("index", -1))
        if index < 0 or index >= len(scenario.assumption_periods):
            raise ValueError("dated assumption period no longer exists")
        del scenario.assumption_periods[index]
        with self.db.transaction(f"Update scenario {scenario.name}") as txn:
            self.db.commit_scenario(scenario, txn)
        return self._scenario_payload(scenario)

    _SCENARIO_FREQUENCIES = {
        "weekly": (PeriodType.WEEK, 1),
        "biweekly": (PeriodType.WEEK, 2),
        "semimonthly": (PeriodType.SEMI_MONTH, 1),
        "monthly": (PeriodType.MONTH, 1),
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
        if len(scheduled.splits) < 2 or any(split.formula for split in scheduled.splits):
            return None
        flow = None
        for split in scheduled.splits:
            account = self.db.get_account(split.account)
            if account is not None and account.account_class in (
                AccountClass.INCOME,
                AccountClass.EXPENSE,
            ):
                flow = split
                break
        if flow is None:
            return None
        others = [split for split in scheduled.splits if split is not flow]
        funding = next(
            (
                split
                for split in others
                if (account := self.db.get_account(split.account)) is not None
                and account.account_class not in (AccountClass.INCOME, AccountClass.EXPENSE)
                and split.planning_flow is None
            ),
            others[-1] if others else None,
        )
        if funding is None:
            return None
        account = self.db.get_account(flow.account)
        assert account is not None
        amount = flow.resolve(scheduled.variables) * account.sign()
        additional = []
        for split in others:
            if split is funding:
                continue
            extra_account = self.db.get_account(split.account)
            if extra_account is None:
                return None
            resolved = split.resolve(scheduled.variables)
            normal_amount = (
                split.planning_flow.plan_amount(resolved)
                if split.planning_flow is not None
                else resolved * extra_account.sign()
            )
            if normal_amount <= 0:
                return None
            additional.append(
                {
                    "account": split.account,
                    "amount": str(normal_amount.to_decimal()),
                    "planning_flow": (
                        split.planning_flow.value if split.planning_flow is not None else None
                    ),
                }
            )
        return {
            "category": flow.account,
            "funding": funding.account,
            "amount": str(abs(amount).to_decimal()),
            "planning_flow": (
                funding.planning_flow.value if funding.planning_flow is not None else None
            ),
            "additional_splits": additional,
        }

    @staticmethod
    def _frequency_key(recurrence: Recurrence) -> str | None:
        for key, (period, interval) in Api._SCENARIO_FREQUENCIES.items():
            if recurrence.period is period and recurrence.interval == interval:
                return key
        return None

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
            "skipped": [when.isoformat() for when in item.skipped],
            "occurrence_adjustments": [
                {"when": change.when.isoformat(), "amount": change.amount}
                for change in item.occurrence_adjustments
            ],
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
                    "frequency": frequency if parts else None,
                    "start": item.recurrence.start.isoformat(),
                    "end": item.recurrence.end.isoformat() if item.recurrence.end else None,
                    "count": item.recurrence.count,
                    "weekend": self._weekend_key(item.recurrence.weekend_adjust),
                    "amount_changes": [
                        {"start": change.start.isoformat(), "amount": change.amount}
                        for change in item.amount_changes
                    ],
                    "skipped": [when.isoformat() for when in item.skipped],
                    "occurrence_adjustments": [
                        {"when": change.when.isoformat(), "amount": change.amount}
                        for change in item.occurrence_adjustments
                    ],
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
        self, payload: dict, excluded: set[str]
    ) -> tuple[list[ScheduledSplit], Money]:
        raw_splits = payload.get("additional_splits") or []
        if not isinstance(raw_splits, list):
            raise ValueError("additional splits must be a list")
        splits: list[ScheduledSplit] = []
        total = Money(0)
        used = set(excluded)
        for raw in raw_splits:
            if not isinstance(raw, dict):
                raise ValueError("additional split entry is invalid")
            handle = str(raw.get("account") or "").strip()
            account = self.db.get_account(handle) if handle else None
            if account is None or handle in used:
                raise ValueError("each additional split needs a different account")
            try:
                amount = self._input_money(payload, raw.get("amount") or "0")
            except (ValueError, ArithmeticError) as exc:
                raise ValueError("additional split amount must be a valid number") from exc
            if amount <= 0:
                raise ValueError("additional split amount must be greater than zero")
            raw_purpose = str(raw.get("planning_flow") or "").strip()
            try:
                purpose = PlanningFlowKind(raw_purpose) if raw_purpose else None
            except ValueError:
                raise ValueError("choose a valid planning purpose") from None
            value = (
                purpose.ledger_amount(amount) if purpose is not None else amount * account.sign()
            )
            splits.append(ScheduledSplit(handle, value, planning_flow=purpose))
            total = total + value
            used.add(handle)
        return splits, total

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
        if not category_handle or not funding_handle or category_handle == funding_handle:
            raise ValueError("choose two different accounts")
        category = self.db.get_account(category_handle)
        funding = self.db.get_account(funding_handle)
        if category is None or funding is None:
            raise ValueError("choose valid accounts")
        if category.account_class not in (AccountClass.INCOME, AccountClass.EXPENSE):
            raise ValueError("choose an income or expense category")
        try:
            amount = abs(self._input_money(payload, payload.get("amount", "")))
        except (ValueError, ArithmeticError):
            raise ValueError("enter a valid amount") from None
        if not amount:
            raise ValueError("amount must be greater than zero")
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
        signed = amount * category.sign()
        planning_flow_raw = str(payload.get("planning_flow") or "").strip()
        try:
            planning_flow = PlanningFlowKind(planning_flow_raw) if planning_flow_raw else None
        except ValueError:
            raise ValueError("choose a valid planning purpose") from None
        additional_splits, additional_total = self._parse_additional_splits(
            payload, {category.handle, funding.handle}
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
        change = ScenarioSchedule(
            name=name,
            recurrence=recurrence,
            splits=[
                ScheduledSplit(category.handle, signed),
                *additional_splits,
                ScheduledSplit(
                    funding.handle,
                    -(signed + additional_total),
                    planning_flow=planning_flow,
                ),
            ],
            source_schedule=source_handle,
            enabled=True,
            placeholder=source.placeholder if source is not None else True,
            growth_policy=growth_policy,
            amount_changes=self._parse_amount_changes(payload, start),
            seasonal_amounts=list(source.seasonal_amounts) if source is not None else [],
            skipped=skipped,
            occurrence_adjustments=adjustments,
        )
        if source_handle:
            scenario.schedule_overrides = [
                item
                for item in scenario.schedule_overrides
                if item.source_schedule != source_handle
            ]
        scenario.schedule_overrides.append(change)
        with self.db.transaction(f"Update scenario {scenario.name}") as txn:
            self.db.commit_scenario(scenario, txn)
        return self.scenario_events(scenario.handle)

    def scenario_event_suppress(self, payload: dict) -> dict:
        scenario = self._scenario_for_events(payload.get("handle"))
        source_handle = str(payload.get("source_schedule", "")).strip()
        source = self.db.get_scheduled(source_handle) if source_handle else None
        if source is None:
            raise KeyError(source_handle)
        scenario.schedule_overrides = [
            item for item in scenario.schedule_overrides if item.source_schedule != source_handle
        ]
        scenario.schedule_overrides.append(ScenarioSchedule.from_scheduled(source, enabled=False))
        with self.db.transaction(f"Update scenario {scenario.name}") as txn:
            self.db.commit_scenario(scenario, txn)
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
        today = date.today()
        minimum = self._plan_earliest_data_date().replace(day=1)
        maximum_month = self._plan_maximum_through_month()
        maximum = self._month_end(maximum_month.year, maximum_month.month)
        defaults = PlanSettings.load(
            self.db,
            date(today.year, 1, 1),
            date(today.year + 1, 12, 31),
        )
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
        if use_saved:
            start_month = defaults.start.strftime("%Y-%m")
            through_month = defaults.end.strftime("%Y-%m")
            period = defaults.period.value
            scenario_handle = defaults.scenario
            compare_handle = defaults.compare
            measure = defaults.measure.value

        if start_month is None:
            start = date(today.year, 1, 1)
            if start < minimum:
                start = minimum
        else:
            start = date.fromisoformat(f"{start_month}-01")

        if through_month is None:
            end = date(today.year + 1, 12, 31)
            if end < start:
                end = self._month_end(start.year, start.month)
            if end > maximum:
                end = maximum
        else:
            through = date.fromisoformat(f"{through_month}-01")
            end = self._month_end(through.year, through.month)

        if use_saved:
            start = max(start, minimum)
            end = min(end, maximum)
            if end < start:
                end = self._month_end(start.year, start.month)

        if start < minimum:
            raise ValueError(f"From cannot be earlier than the first book data ({minimum:%b %Y}).")
        if end < start:
            raise ValueError("Through must be the same month as From or later.")
        if end > maximum:
            raise ValueError(f"Through cannot be later than {maximum_month:%b %Y}.")

        grouping = activity.ReportingPeriod(period or "month")
        selected_measure = PlanMeasure(measure or "planned")
        scenarios = list(self.db.iter_scenarios())
        if scenario_handle:
            selected = next((item for item in scenarios if item.handle == scenario_handle), None)
            if selected is None:
                if not use_saved:
                    raise KeyError(scenario_handle)
                scenario_handle = None
                scenario = self._base_scenario(start, end)
            else:
                scenario = selected
        else:
            scenario = self._base_scenario(start, end)

        report = activity.build_category_report(
            self.db, start, end, period=grouping, scenario=scenario
        )
        totals = report.activity

        comparison: dict[str, object] | None = None
        if (
            use_saved
            and compare_handle not in {None, "__base__"}
            and not any(item.handle == compare_handle for item in scenarios)
        ):
            compare_handle = None
        if compare_handle is not None:
            if compare_handle == "__base__":
                compare_scenario = self._base_scenario(start, end)
                compare_identity = None
                compare_name = "Base scenario"
            else:
                selected_compare = next(
                    (item for item in scenarios if item.handle == compare_handle), None
                )
                if selected_compare is None:
                    raise KeyError(compare_handle)
                compare_scenario = selected_compare
                compare_identity = selected_compare.handle
                compare_name = selected_compare.name
            if compare_identity == scenario_handle:
                raise ValueError("Plan comparison must use a different scenario.")
            compare_report = activity.build_category_report(
                self.db, start, end, period=grouping, scenario=compare_scenario
            )
            compare_rows = {row.account: row for row in compare_report.categories}
            compare_flows = {(row.kind, row.account): row for row in compare_report.planning_flows}
            comparison_categories: list[dict[str, object]] = []
            comparison_flows: list[dict[str, object]] = []
            comparison = {
                "handle": compare_identity,
                "name": compare_name,
                "summary": {
                    "planned_cash": compare_report.activity.planned_cash_change,
                    "actual_cash": compare_report.activity.actual_cash_change,
                    "variance": compare_report.cash_variance,
                    "planned_cash_delta": (
                        totals.planned_cash_change - compare_report.activity.planned_cash_change
                    ),
                    "actual_cash_delta": (
                        totals.actual_cash_change - compare_report.activity.actual_cash_change
                    ),
                    "variance_delta": (report.cash_variance - compare_report.cash_variance),
                },
                "categories": comparison_categories,
                "planning_flows": comparison_flows,
            }
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
                    {"handle": None, "name": "Base scenario"},
                    *[{"handle": item.handle, "name": item.name} for item in scenarios],
                ],
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
                "actual_cash": totals.actual_cash_change,
                "variance": report.cash_variance,
                "unresolved_expected": totals.unresolved_count,
                "unresolved_actuals": totals.unresolved_actual_count,
            },
            "comparison": comparison,
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
                "planning_flows": {
                    item.value: {
                        "periods": report.planning_flow_totals(item),
                        "total": report.planning_flow_grand_total(item),
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
    ) -> dict:
        """Explain one Plan category/planning-flow cell from exact-dated activity."""
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

        if flow_kind:
            kind = PlanningFlowKind(flow_kind)
            flow_detail = activity.explain_planning_flow_period(
                self.db, kind, account_handle, start, end, scenario=scenario
            )
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
            category = {
                "account": category_detail.account,
                "name": category_detail.name,
                "full_name": category_detail.full_name,
                "class": category_detail.account_class.value,
            }
        detail = flow_detail if flow_kind else category_detail
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

    def budget(self, name: str | None = None) -> dict:
        budgets = list(self.db.iter_budgets())
        if name:
            budgets = [b for b in budgets if b.name == name]
        if not budgets:
            return {"budget": None, "labels": [], "lines": []}
        report = cashflow.build_report(self.db, budgets[0])
        return {
            "budget": report.budget.name,
            "kind": report.budget.kind.value,
            "labels": report.labels,
            "shortfalls": report.shortfall_periods(),
            "net": [report.net_cash_flow(p) for p in range(report.budget.periods)],
            "net_actual": [
                report.net_cash_flow(p, actual=True) for p in range(report.budget.periods)
            ],
            "lines": [
                {
                    "account": line.name,
                    "class": line.account_class.value,
                    "budgeted": [p.budgeted for p in line.periods],
                    "actual": [p.actual for p in line.periods],
                    "total_budgeted": line.budgeted_total,
                    "total_actual": line.actual_total,
                    "variance": line.variance_total,
                }
                for line in report.lines
            ],
        }

    def coverage(self, name: str | None = None) -> list[dict]:
        budgets = list(self.db.iter_budgets())
        if name:
            budgets = [b for b in budgets if b.name == name]
        if not budgets:
            return []
        return [
            {
                "account": row.name,
                "budgeted": row.budgeted,
                "scheduled": row.scheduled,
                "unexplained": row.unexplained,
            }
            for row in budgeting.coverage(self.db, budgets[0])
        ]

    def _projection_draft(
        self, scenario_handle: str | None = None, years: int | None = None
    ) -> Scenario:
        """Return a detached projection scenario safe for browser-side editing."""
        if scenario_handle:
            stored = self.db.get_scenario(scenario_handle)
            if stored is None:
                raise KeyError(scenario_handle)
            scenario = Scenario.from_dict(stored.serialize())
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
        basis = str(payload.get("basis", scenario.basis.value))
        try:
            scenario.basis = ProjectionBasis(basis)
        except ValueError:
            raise ValueError("choose a valid projection basis") from None
        budget = str(payload.get("budget") or "").strip() or None
        if budget is not None and self.db.get_budget(budget) is None:
            raise ValueError("choose a valid budget")
        scenario.budget = budget
        scenario.assumptions = self._assumptions_from_payload(
            payload.get("assumptions", scenario.assumptions.serialize()),
            scenario.assumptions,
        )
        return scenario

    def _projection_payload(self, scenario: Scenario, *, base: bool = False, result=None) -> dict:
        if result is None:
            result = projection.project(self.db, scenario)
        return {
            "scenario": {
                "handle": None if base else scenario.handle,
                "name": scenario.name,
                "years": scenario.years,
                "basis": scenario.basis.value,
                "budget": scenario.budget,
                "assumptions": scenario.assumptions.serialize(),
            },
            "controls": {
                "scenarios": [
                    {"handle": None, "name": "Base scenario"},
                    *[
                        {"handle": item.handle, "name": item.name}
                        for item in self.db.iter_scenarios()
                    ],
                ],
                "budgets": [
                    {"handle": item.handle, "name": item.name} for item in self.db.iter_budgets()
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
                "movement": detail.holding_contributions,
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
            "events": [event.as_dict() for event in detail.events],
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
            self.db.set_metadata("planning.base_assumptions", scenario.assumptions.serialize())
            return self._projection_payload(scenario, base=True)
        with self.db.transaction(f"Update scenario {scenario.name}") as txn:
            self.db.commit_scenario(scenario, txn)
        return self._projection_payload(scenario)

    # ---------------------------------------------------------------- writing

    def add_transaction(self, payload: dict) -> dict:
        """Post a two-split transaction. The only write the web interface allows."""
        debit = self.db.get_account_by_name(payload["to"])
        credit = self.db.get_account_by_name(payload["from"])
        if debit is None or credit is None:
            raise KeyError("unknown account")
        when = date.fromisoformat(payload.get("date") or date.today().isoformat())
        amount = self._input_money(payload, payload["amount"])
        txn = Transaction(post_date=when, description=payload.get("description", "").strip())
        memo = payload.get("memo", "")
        txn.add_split(Split(debit.handle, amount, memo=memo))
        txn.add_split(Split(credit.handle, -amount, memo=memo))
        with self.db.transaction(f"Add {txn.description}") as batch:
            self.db.add_transaction(txn, batch)
        claim_handle = str(payload.get("fsa_claim") or "").strip()
        claim_role = str(payload.get("fsa_role") or "").strip()
        if claim_handle and claim_role:
            funding_year = str(payload.get("fsa_year") or "").strip()
            fsa_claims.attach_transaction_to_claim(
                self.db,
                claim_handle,
                txn.handle,
                role=claim_role,
                funding_year_start=(date.fromisoformat(funding_year) if funding_year else None),
            )
        return {"handle": txn.handle, "date": when, "amount": amount}

    def review_match(self, payload: dict) -> dict:
        transaction = self.db.get_transaction(str(payload["transaction"]))
        if transaction is None:
            raise KeyError(str(payload["transaction"]))
        event = planning.event_by_key(self.db, str(payload["occurrence"]))
        if event is None:
            raise ValueError("planned occurrence does not exist")
        if transaction.planning_resolution is not PlanningResolution.UNRESOLVED:
            raise ValueError("transaction is no longer awaiting review")
        planning.actualize_transaction(transaction, event)
        with self.db.transaction("Match transaction to planned occurrence") as txn:
            self.db.commit_transaction(transaction, txn)
        return {
            "transaction": transaction.handle,
            "resolution": transaction.planning_resolution.value,
            "occurrence": event.key,
        }

    def review_reject(self, payload: dict) -> dict:
        transaction = self.db.get_transaction(str(payload["transaction"]))
        if transaction is None:
            raise KeyError(str(payload["transaction"]))
        event = planning.event_by_key(self.db, str(payload["occurrence"]))
        if event is None:
            raise ValueError("planned occurrence does not exist")
        if transaction.planning_resolution is not PlanningResolution.UNRESOLVED:
            raise ValueError("transaction is no longer awaiting review")
        planning.reject_candidate(transaction, event)
        with self.db.transaction("Reject planned occurrence candidate") as txn:
            self.db.commit_transaction(transaction, txn)
        return {
            "transaction": transaction.handle,
            "rejected": event.key,
        }

    def review_skip(self, payload: dict) -> dict:
        transaction = self.db.get_transaction(str(payload["transaction"]))
        if transaction is None:
            raise KeyError(str(payload["transaction"]))
        if transaction.planning_resolution is not PlanningResolution.UNRESOLVED:
            raise ValueError("transaction is no longer awaiting review")
        event = planning.event_by_key(self.db, str(payload["occurrence"]))
        if event is None:
            raise ValueError("planned occurrence does not exist")
        planning.skip_occurrence(self.db, event)
        return {"transaction": transaction.handle, "skipped": event.key}

    def review_fsa_attach(self, payload: dict) -> dict:
        funding_year = str(payload.get("funding_year") or "").strip()
        claim = fsa_claims.attach_transaction_to_claim(
            self.db,
            str(payload["claim"]),
            str(payload["transaction"]),
            role=str(payload["role"]),
            split_handle=str(payload.get("split") or "") or None,
            funding_year_start=date.fromisoformat(funding_year) if funding_year else None,
        )
        return {"claim": claim.handle, "transaction": str(payload["transaction"])}

    def review_unexpected(self, payload: dict) -> dict:
        transaction = self.db.get_transaction(str(payload["transaction"]))
        if transaction is None:
            raise KeyError(str(payload["transaction"]))
        if transaction.planning_resolution is not PlanningResolution.UNRESOLVED:
            raise ValueError("transaction is no longer awaiting review")
        planning.mark_unexpected(transaction)
        with self.db.transaction("Mark transaction as unexpected") as txn:
            self.db.commit_transaction(transaction, txn)
        return {
            "transaction": transaction.handle,
            "resolution": transaction.planning_resolution.value,
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
        if existing is not None and (
            self._simple_schedule_parts(existing) is None
            or self._frequency_key(existing.recurrence) is None
        ):
            raise ValueError("formula schedules cannot be edited in the fixed-split editor")

        name = str(payload.get("name") or "").strip()
        if not name:
            raise ValueError("schedule name is required")
        category_handle = str(payload.get("category") or "").strip()
        funding_handle = str(payload.get("funding") or "").strip()
        if not category_handle or not funding_handle or category_handle == funding_handle:
            raise ValueError("choose two different accounts")
        category = self.db.get_account(category_handle)
        funding = self.db.get_account(funding_handle)
        if category is None or funding is None:
            raise ValueError("scheduled account no longer exists")
        if category.account_class not in (AccountClass.INCOME, AccountClass.EXPENSE):
            raise ValueError("category must be an income or expense account")

        try:
            amount = self._input_money(payload, payload.get("amount") or "0")
        except (ValueError, ArithmeticError) as exc:
            raise ValueError("amount must be a valid number") from exc
        if amount <= 0:
            raise ValueError("amount must be greater than zero")
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
        recurrence = Recurrence(
            period=period,
            interval=interval,
            start=start,
            end=end,
            count=count,
            weekend_adjust=self._SCENARIO_WEEKENDS[weekend_key],
        )
        amount_changes = self._parse_amount_changes(payload, start)
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

        item = (
            ScheduledTransaction.from_dict(existing.serialize())
            if existing is not None
            else ScheduledTransaction()
        )
        old_name = item.name
        item.name = name
        if existing is None or item.description == old_name:
            item.description = name
        signed = amount * category.sign()
        planning_flow_raw = str(payload.get("planning_flow") or "").strip()
        try:
            planning_flow = PlanningFlowKind(planning_flow_raw) if planning_flow_raw else None
        except ValueError:
            raise ValueError("choose a valid planning purpose") from None
        additional_splits, additional_total = self._parse_additional_splits(
            payload, {category.handle, funding.handle}
        )
        item.recurrence = recurrence
        item.splits = [
            ScheduledSplit(category.handle, signed),
            *additional_splits,
            ScheduledSplit(
                funding.handle,
                -(signed + additional_total),
                planning_flow=planning_flow,
            ),
        ]
        item.placeholder = bool(payload.get("placeholder", False))
        item.growth_policy = growth_policy
        item.amount_changes = amount_changes
        item.skipped = skipped
        item.occurrence_adjustments = adjustments
        if "enabled" in payload:
            if not isinstance(payload["enabled"], bool):
                raise ValueError("active status must be true or false")
            item.enabled = payload["enabled"]
        item.auto_create = bool(payload.get("auto", False))
        if item.placeholder:
            item.auto_create = False

        action = "Update" if existing is not None else "Add"
        with self.db.transaction(f"{action} scheduled {item.name}") as txn:
            if existing is None:
                self.db.add_scheduled(item, txn)
            else:
                self.db.commit_scheduled(item, txn)
        return {"handle": item.handle, "name": item.name}

    def import_local(self, payload: dict) -> dict:
        path = str(payload.get("path") or "").strip()
        if not path:
            raise ValueError("choose a file to import")
        plugin = PluginManager.instance().for_file(path, IMPORTER)
        if plugin is None:
            raise ValueError("file format is not recognised")
        kwargs: dict[str, object] = {
            "include_scheduled": bool(payload.get("include_scheduled", True))
        }
        if plugin.id in {"qif", "ofx"}:
            number_format = str(payload.get("number_format") or "auto")
            if number_format not in {"auto", "dot", "comma"}:
                raise ValueError("choose a valid number format")
            kwargs["number_format"] = number_format
        if plugin.id == "qif":
            date_format = str(payload.get("date_format") or "auto")
            if date_format not in {"auto", "month-first", "day-first"}:
                raise ValueError("choose a valid QIF date order")
            kwargs["date_format"] = date_format
        result = plugin.run(self.db, path, **kwargs)
        return {"format": plugin.name, "detail": result.detail(limit=50)}

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


ROUTES = {
    "/api/dashboard": lambda a, q: a.dashboard(
        int(q.get("liquidity_days", ["0"])[0] or 0),
        int(q.get("emergency_months", ["0"])[0] or 0),
    ),
    "/api/summary": lambda a, q: a.summary(),
    "/api/accounts": lambda a, q: a.accounts(),
    "/api/fsa/claims": lambda a, q: a.fsa_claims(),
    "/api/register": lambda a, q: a.register(
        q.get("account", [""])[0], int(q.get("limit", ["250"])[0])
    ),
    "/api/scheduled": lambda a, q: a.scheduled(int(q.get("days", ["60"])[0])),
    "/api/historical-estimates": lambda a, q: a.historical_estimates(
        int(q.get("months", ["12"])[0]),
        int(q.get("min_active_months", ["3"])[0]),
        q.get("scenario", [None])[0] or None,
    ),
    "/api/plan": lambda a, q: a.plan(
        q.get("from", [None])[0],
        q.get("through", [None])[0],
        q.get("period", [None])[0],
        q.get("scenario", [None])[0],
        q.get("compare", [None])[0],
        q.get("measure", [None])[0],
    ),
    "/api/plan/detail": lambda a, q: a.plan_detail(
        q.get("account", [""])[0],
        q.get("start", [""])[0],
        q.get("end", [""])[0],
        q.get("scenario", [None])[0],
        q.get("flow_kind", [None])[0],
    ),
    "/api/review": lambda a, q: a.review(q.get("transaction", [None])[0]),
    "/api/scenarios": lambda a, q: a.scenarios(),
    "/api/scenario/events": lambda a, q: a.scenario_events(q.get("handle", [None])[0]),
    "/api/budget": lambda a, q: a.budget(q.get("name", [None])[0]),
    "/api/coverage": lambda a, q: a.coverage(q.get("name", [None])[0]),
    "/api/projection": lambda a, q: a.projection(
        q.get("scenario", [None])[0],
        int(q["years"][0]) if q.get("years") else None,
    ),
}

POST_ROUTES = {
    "/api/dashboard/config": lambda a, body: a.dashboard_config_save(body),
    "/api/account/type": lambda a, body: a.account_type_save(body),
    "/api/plan/settings": lambda a, body: a.plan_settings_save(body),
    "/api/account/planning-role": lambda a, body: a.account_planning_role_save(body),
    "/api/account/kind": lambda a, body: a.account_kind_save(body),
    "/api/account/fsa-years": lambda a, body: a.account_fsa_years_save(body),
    "/api/fsa/claim/save": lambda a, body: a.fsa_claim_save(body),
    "/api/fsa/claim/delete": lambda a, body: a.fsa_claim_delete(body),
    "/api/transaction": lambda a, body: a.add_transaction(body),
    "/api/import": lambda a, body: a.import_local(body),
    "/api/post-scheduled": lambda a, body: a.post_scheduled(),
    "/api/scheduled/occurrences": lambda a, body: a.scheduled_occurrence_options(body),
    "/api/scheduled/save": lambda a, body: a.scheduled_save(body),
    "/api/historical-estimate/accept": lambda a, body: a.historical_estimate_accept(body),
    "/api/review/match": lambda a, body: a.review_match(body),
    "/api/review/reject": lambda a, body: a.review_reject(body),
    "/api/review/skip": lambda a, body: a.review_skip(body),
    "/api/review/fsa-attach": lambda a, body: a.review_fsa_attach(body),
    "/api/review/unexpected": lambda a, body: a.review_unexpected(body),
    "/api/scenario/save": lambda a, body: a.scenario_save(body),
    "/api/scenario/duplicate": lambda a, body: a.scenario_duplicate(body),
    "/api/scenario/delete": lambda a, body: a.scenario_delete(body),
    "/api/scenario/period/save": lambda a, body: a.scenario_period_save(body),
    "/api/scenario/period/delete": lambda a, body: a.scenario_period_delete(body),
    "/api/scenario/event/save": lambda a, body: a.scenario_event_save(body),
    "/api/scenario/event/suppress": lambda a, body: a.scenario_event_suppress(body),
    "/api/projection/calculate": lambda a, body: a.projection_calculate(body),
    "/api/projection/compare": lambda a, body: a.projection_compare(body),
    "/api/projection/explain": lambda a, body: a.projection_explain(body),
    "/api/projection/save": lambda a, body: a.projection_save(body),
}


class Handler(BaseHTTPRequestHandler):
    """Serves the single page and the JSON API. One database, guarded by a lock."""

    server_version = "BreadSched"
    api_object: Api
    lock: threading.Lock
    token: str

    _LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - base class name
        LOG.debug("%s %s", self.address_string(), fmt % args)

    # ------------------------------------------------------------- responses

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # The page never loads anything remote; say so.
        self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline'")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: object) -> None:
        body = json.dumps(payload, default=_encode).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _static(self, name: str) -> None:
        path = (STATIC / name).resolve()
        if not path.is_file() or STATIC.resolve() not in path.parents:
            self._json(404, {"error": "not found"})
            return
        kind = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
        }.get(path.suffix, "application/octet-stream")
        self._send(200, path.read_bytes(), kind)

    def _trusted_host(self) -> bool:
        host = urlparse(f"//{self.headers.get('Host', '')}").hostname
        return host in self._LOCAL_HOSTS

    def _trusted_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urlparse(origin)
        return parsed.scheme in {"http", "https"} and parsed.hostname in self._LOCAL_HOSTS

    def _trusted_api_request(self, *, write: bool = False) -> bool:
        if not self._trusted_host() or not self._trusted_origin():
            return False
        if write and self.headers.get_content_type() != "application/json":
            return False
        supplied = self.headers.get("X-BreadSched-Token", "")
        return secrets.compare_digest(supplied, self.token)

    # ---------------------------------------------------------------- routing

    def do_GET(self) -> None:  # noqa: N802 - required by the base class
        if not self._trusted_host():
            self._json(403, {"error": "untrusted host"})
            return
        parsed = urlparse(self.path)
        route = ROUTES.get(parsed.path)
        if route is None:
            self._static("index.html" if parsed.path in ("/", "") else parsed.path[1:])
            return
        if not self._trusted_api_request():
            self._json(403, {"error": "untrusted request"})
            return
        query = parse_qs(parsed.query)
        try:
            with self.lock:
                self._json(200, route(self.api_object, query))
        except KeyError as exc:
            self._json(404, {"error": str(exc)})
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - a bad request must not kill the server
            LOG.exception("request failed: %s", self.path)
            self._json(500, {"error": str(exc)})

    def do_POST(self) -> None:  # noqa: N802 - required by the base class
        if not self._trusted_api_request(write=True):
            self._json(403, {"error": "untrusted request"})
            return
        parsed = urlparse(self.path)
        route = POST_ROUTES.get(parsed.path)
        if route is None:
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "body was not valid JSON"})
            return
        try:
            with self.lock:
                self._json(200, route(self.api_object, body))
        except KeyError as exc:
            self._json(400, {"error": f"unknown account: {exc}"})
        except Exception as exc:  # noqa: BLE001
            LOG.exception("request failed: %s", self.path)
            self._json(400, {"error": str(exc)})


class BreadSchedHTTPServer(ThreadingHTTPServer):
    """Threaded local server carrying the API token exposed to the launcher."""

    token: str


def build_handler(db: DbSQLite, token: str) -> type[Handler]:
    """A handler class bound to one database, with a lock around every request.

    SQLite connections are not safe to share across threads, and the server is
    threaded, so requests are serialised. For a single-user local tool that costs
    nothing and removes a whole category of intermittent corruption.
    """
    return type(
        "BoundHandler",
        (Handler,),
        {"api_object": Api(db), "lock": threading.Lock(), "token": token},
    )


def serve(
    db: DbSQLite,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = False,
) -> BreadSchedHTTPServer:
    """Start the server. Returns it without blocking; call ``serve_forever``."""
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("the web interface binds to loopback only")
    token = secrets.token_urlsafe(32)
    server = BreadSchedHTTPServer((host, port), build_handler(db, token))
    server.token = token
    if open_browser:  # pragma: no cover - depends on a desktop session
        import webbrowser

        query = urlencode({"token": token})
        threading.Timer(
            0.5,
            partial(webbrowser.open, f"http://{host}:{server.server_port}/#{query}"),
        ).start()
    return server
