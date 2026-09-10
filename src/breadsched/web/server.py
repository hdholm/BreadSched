"""A locally hosted web interface.

Built on :mod:`http.server` with no framework, for the same reason the core has no
third-party dependencies: a finance tool that people run on their own machine for
years should not rot because a web framework moved on. The whole surface is a small
JSON API plus one static page.

It binds to the loopback address only. There is no authentication, because there is
no network exposure to authenticate against; if that ever changes, this docstring
is wrong and the change needs more than a new bind address.
"""

from __future__ import annotations

import json
import threading
from calendar import monthrange
from datetime import date
from decimal import Decimal
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..gen.db.sqlite import DbSQLite
from ..gen.engine import (
    activity,
    budgeting,
    cashflow,
    ledger,
    planning,
    projection,
    schedule,
)
from ..gen.lib import (
    AccountClass,
    AssumptionPeriod,
    Assumptions,
    Money,
    PeriodType,
    PlanningResolution,
    ProjectionBasis,
    Recurrence,
    Scenario,
    ScenarioSchedule,
    ScheduledSplit,
    Split,
    Transaction,
    WeekendAdjust,
)
from ..gen.lib.base import create_handle
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

    def __init__(self, db: DbSQLite) -> None:
        self.db = db

    # ---------------------------------------------------------------- reading

    def dashboard(self, liquidity_days: int | None, emergency_months: int | None) -> dict:
        """The overview: groups, the liquidity verdict, and the pending bills.

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
            },
            "groups": [
                {
                    "name": group.name,
                    "kind": group.kind,
                    "total": str(group.total.to_decimal()),
                    "value": str(group.value.to_decimal()) if group.value else None,
                    "debt": str(group.debt.to_decimal()) if group.debt else None,
                    "equity": (
                        str(group.equity.to_decimal())
                        if group.equity is not None else None
                    ),
                    "loan_to_value": (
                        float(group.loan_to_value)
                        if group.loan_to_value is not None else None
                    ),
                    "accounts": [
                        {"name": name, "balance": str(balance.to_decimal())}
                        for name, balance in group.accounts
                    ],
                }
                for group in board.groups
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
                        "depth": depth,
                        "balance": ledger.balance_recursive(self.db, account.handle),
                        "own_balance": ledger.balance(self.db, account.handle),
                    }
                )
                walk(account.handle, depth + 1)

        root = self.db.root_account()
        walk(root.handle if root else None, 0)
        return rows

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

    def scheduled(self, days: int = 60) -> dict:
        occurrences = schedule.due_occurrences(self.db, horizon_days=days)
        return {
            "definitions": [
                {
                    "handle": s.handle,
                    "name": s.name,
                    "frequency": s.recurrence.describe(),
                    "amount": s.amount(),
                    "enabled": s.enabled,
                    "placeholder": s.placeholder,
                    "auto": s.auto_create,
                }
                for s in self.db.iter_scheduled()
            ],
            "upcoming": [
                {"date": o.when, "name": o.name, "amount": o.amount}
                for o in occurrences
            ],
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
        return {
            "scenarios": [
                self._scenario_payload(base, base=True),
                *(self._scenario_payload(item) for item in self.db.iter_scenarios()),
            ]
        }

    @staticmethod
    def _assumptions_from_payload(
        payload: object, existing: Assumptions | None = None
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
        values = {}
        for field in fields:
            if field not in payload:
                raise ValueError(f"missing assumption: {field}")
            value = Decimal(str(payload[field]))
            if value < Decimal("-1") or value > Decimal("1"):
                raise ValueError(f"{field} must be between -1 and 1")
            values[field] = value
        values["per_account"] = dict(existing.per_account) if existing is not None else {}
        return Assumptions(**values)

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
            per_account=dict(existing_period.per_account) if existing_period is not None else {},
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
        if len(scheduled.splits) != 2 or any(
            split.formula for split in scheduled.splits
        ):
            return None
        flow = None
        other = None
        for split in scheduled.splits:
            account = self.db.get_account(split.account)
            if account is not None and account.account_class in (
                AccountClass.INCOME, AccountClass.EXPENSE
            ):
                flow = split
                break
        if flow is None:
            return None
        other = next((split for split in scheduled.splits if split is not flow), None)
        if other is None:
            return None
        account = self.db.get_account(flow.account)
        assert account is not None
        amount = flow.resolve(scheduled.variables) * account.sign()
        return {
            "category": flow.account,
            "funding": other.account,
            "amount": str(abs(amount).to_decimal()),
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
        source = (
            self.db.get_scheduled(item.source_schedule)
            if item.source_schedule
            else None
        )
        simple = self._simple_schedule_parts(item)
        return {
            "handle": item.handle,
            "name": item.name,
            "source_schedule": item.source_schedule,
            "source_name": source.name if source is not None else None,
            "enabled": item.enabled,
            "simple": (
                simple is not None and self._frequency_key(item.recurrence) is not None
            ),
            "category": simple["category"] if simple else None,
            "funding": simple["funding"] if simple else None,
            "amount": simple["amount"] if simple else None,
            "frequency": self._frequency_key(item.recurrence),
            "start": item.recurrence.start.isoformat(),
            "weekend": self._weekend_key(item.recurrence.weekend_adjust),
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
                    "simple": (parts := self._simple_schedule_parts(item)) is not None
                    and (frequency := self._frequency_key(item.recurrence)) is not None,
                    "category": parts["category"] if parts else None,
                    "funding": parts["funding"] if parts else None,
                    "amount": parts["amount"] if parts else None,
                    "frequency": frequency if parts else None,
                    "start": item.recurrence.start.isoformat(),
                    "weekend": self._weekend_key(item.recurrence.weekend_adjust),
                }
                for item in schedules
            ],
            "changes": [
                self._scenario_event_payload(item) for item in scenario.schedule_overrides
            ],
        }

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
        if (
            not category_handle
            or not funding_handle
            or category_handle == funding_handle
        ):
            raise ValueError("choose two different accounts")
        category = self.db.get_account(category_handle)
        funding = self.db.get_account(funding_handle)
        if category is None or funding is None:
            raise ValueError("choose valid accounts")
        if category.account_class not in (AccountClass.INCOME, AccountClass.EXPENSE):
            raise ValueError("choose an income or expense category")
        try:
            amount = abs(Money(str(payload.get("amount", "")).strip()))
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
        signed = amount * category.sign()
        change = ScenarioSchedule(
            name=name,
            recurrence=Recurrence(
                period=period, interval=interval, start=start,
                weekend_adjust=self._SCENARIO_WEEKENDS[weekend],
            ),
            splits=[
                ScheduledSplit(category.handle, signed),
                ScheduledSplit(funding.handle, -signed),
            ],
            source_schedule=source_handle,
            enabled=True,
            placeholder=source.placeholder if source is not None else True,
        )
        if source_handle:
            scenario.schedule_overrides = [
                item for item in scenario.schedule_overrides
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
            item for item in scenario.schedule_overrides
            if item.source_schedule != source_handle
        ]
        scenario.schedule_overrides.append(
            ScenarioSchedule.from_scheduled(source, enabled=False)
        )
        with self.db.transaction(f"Update scenario {scenario.name}") as txn:
            self.db.commit_scenario(scenario, txn)
        return self.scenario_events(scenario.handle)

    def plan(
        self,
        start_month: str | None = None,
        through_month: str | None = None,
        period: str = "month",
        scenario_handle: str | None = None,
        compare_handle: str | None = None,
    ) -> dict:
        """Derived category Plan using the same event stream as the GTK view."""
        today = date.today()
        minimum = self._plan_earliest_data_date().replace(day=1)
        maximum_month = self._plan_maximum_through_month()
        maximum = self._month_end(maximum_month.year, maximum_month.month)

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

        if start < minimum:
            raise ValueError(
                f"From cannot be earlier than the first book data ({minimum:%b %Y})."
            )
        if end < start:
            raise ValueError("Through must be the same month as From or later.")
        if end > maximum:
            raise ValueError(f"Through cannot be later than {maximum_month:%b %Y}.")

        grouping = activity.ReportingPeriod(period)
        scenarios = list(self.db.iter_scenarios())
        if scenario_handle:
            selected = next(
                (item for item in scenarios if item.handle == scenario_handle), None
            )
            if selected is None:
                raise KeyError(scenario_handle)
            scenario = selected
        else:
            scenario = self._base_scenario(start, end)

        report = activity.build_category_report(
            self.db, start, end, period=grouping, scenario=scenario
        )
        totals = report.activity

        comparison = None
        if compare_handle is not None:
            if compare_handle == "__base__":
                compare_scenario = self._base_scenario(start, end)
                compare_identity = None
                compare_name = "Base scenario"
            else:
                compare_scenario = next(
                    (item for item in scenarios if item.handle == compare_handle), None
                )
                if compare_scenario is None:
                    raise KeyError(compare_handle)
                compare_identity = compare_scenario.handle
                compare_name = compare_scenario.name
            if compare_identity == scenario_handle:
                raise ValueError("Plan comparison must use a different scenario.")
            compare_report = activity.build_category_report(
                self.db, start, end, period=grouping, scenario=compare_scenario
            )
            compare_rows = {row.account: row for row in compare_report.categories}
            comparison = {
                "handle": compare_identity,
                "name": compare_name,
                "summary": {
                    "planned_cash": compare_report.activity.planned_cash_change,
                    "actual_cash": compare_report.activity.actual_cash_change,
                    "variance": compare_report.activity.cash_variance,
                    "planned_cash_delta": (
                        totals.planned_cash_change
                        - compare_report.activity.planned_cash_change
                    ),
                    "actual_cash_delta": (
                        totals.actual_cash_change
                        - compare_report.activity.actual_cash_change
                    ),
                    "variance_delta": (
                        totals.cash_variance - compare_report.activity.cash_variance
                    ),
                },
                "categories": [],
            }
            for row in report.categories:
                other = compare_rows.get(row.account)
                zeroes = [Money(0) for _ in row.planned]
                other_planned = other.planned if other is not None else zeroes
                other_actual = other.actual if other is not None else zeroes
                other_variance = other.variance if other is not None else zeroes
                comparison["categories"].append(
                    {
                        "account": row.account,
                        "planned": other_planned,
                        "actual": other_actual,
                        "variance": other_variance,
                        "planned_delta": [
                            value - alternate
                            for value, alternate in zip(
                                row.planned, other_planned, strict=True
                            )
                        ],
                        "actual_delta": [
                            value - alternate
                            for value, alternate in zip(
                                row.actual, other_actual, strict=True
                            )
                        ],
                        "variance_delta": [
                            value - alternate
                            for value, alternate in zip(
                                row.variance, other_variance, strict=True
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
                "scenario": scenario_handle,
                "scenarios": [
                    {"handle": None, "name": "Base scenario"},
                    *[
                        {"handle": item.handle, "name": item.name}
                        for item in scenarios
                    ],
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
                "variance": totals.cash_variance,
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
                }
                for row in report.categories
            ],
        }

    @staticmethod
    def _actual_amount(transaction: Transaction) -> Money:
        total = Money(0)
        for split in transaction.splits:
            if split.value > 0:
                total = total + split.value
        return total

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
                        "date_variance_days": (
                            transaction.post_date - event.planned_date
                        ).days,
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
                report.net_cash_flow(p, actual=True)
                for p in range(report.budget.periods)
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

    def _projection_payload(
        self, scenario: Scenario, *, base: bool = False, result=None
    ) -> dict:
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
                    {"handle": item.handle, "name": item.name}
                    for item in self.db.iter_budgets()
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

    def projection(
        self, scenario_handle: str | None = None, years: int | None = None
    ) -> dict:
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
                    for left, right in zip(
                        primary_result.rows, comparison_result.rows, strict=True
                    )
                ],
            },
        }

    def projection_save(self, payload: dict) -> dict:
        """Persist projection controls explicitly, preserving hidden model fields."""
        handle = str(payload.get("handle") or "").strip() or None
        scenario = self._projection_draft(handle)
        self._apply_projection_payload(scenario, payload)
        if handle is None:
            self.db.set_metadata(
                "planning.base_assumptions", scenario.assumptions.serialize()
            )
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
        amount = Money(str(payload["amount"]))
        txn = Transaction(
            post_date=when, description=payload.get("description", "").strip()
        )
        memo = payload.get("memo", "")
        txn.add_split(Split(debit.handle, amount, memo=memo))
        txn.add_split(Split(credit.handle, -amount, memo=memo))
        with self.db.transaction(f"Add {txn.description}") as batch:
            self.db.add_transaction(txn, batch)
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

    def post_scheduled(self) -> dict:
        posted = schedule.post_due(self.db, only_auto=False)
        return {
            "posted": len(posted),
            "transactions": [
                {"date": t.post_date, "description": t.description} for t in posted
            ],
        }


def api(db: DbSQLite) -> Api:
    return Api(db)


def _plain(values: dict) -> dict:
    """Convert Money and date values to strings the browser can read."""
    out = {}
    for key, value in values.items():
        if hasattr(value, "to_decimal"):
            out[key] = str(value.to_decimal())
        elif hasattr(value, "isoformat"):
            out[key] = value.isoformat()
        else:
            out[key] = str(value) if value is not None else None
    return out


def _plain(values: dict) -> dict:
    """Convert Money and date values to strings the browser can read."""
    out = {}
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
    "/api/register": lambda a, q: a.register(
        q.get("account", [""])[0], int(q.get("limit", ["250"])[0])
    ),
    "/api/scheduled": lambda a, q: a.scheduled(int(q.get("days", ["60"])[0])),
    "/api/plan": lambda a, q: a.plan(
        q.get("from", [None])[0],
        q.get("through", [None])[0],
        q.get("period", ["month"])[0],
        q.get("scenario", [None])[0],
        q.get("compare", [None])[0],
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
    "/api/transaction": lambda a, body: a.add_transaction(body),
    "/api/post-scheduled": lambda a, body: a.post_scheduled(),
    "/api/review/match": lambda a, body: a.review_match(body),
    "/api/review/reject": lambda a, body: a.review_reject(body),
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
    "/api/projection/save": lambda a, body: a.projection_save(body),
}


class Handler(BaseHTTPRequestHandler):
    """Serves the single page and the JSON API. One database, guarded by a lock."""

    server_version = "BreadSched"
    api_object: Api
    lock: threading.Lock

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

    # ---------------------------------------------------------------- routing

    def do_GET(self) -> None:  # noqa: N802 - required by the base class
        parsed = urlparse(self.path)
        route = ROUTES.get(parsed.path)
        if route is None:
            self._static("index.html" if parsed.path in ("/", "") else parsed.path[1:])
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


def build_handler(db: DbSQLite) -> type[Handler]:
    """A handler class bound to one database, with a lock around every request.

    SQLite connections are not safe to share across threads, and the server is
    threaded, so requests are serialised. For a single-user local tool that costs
    nothing and removes a whole category of intermittent corruption.
    """
    return type(
        "BoundHandler",
        (Handler,),
        {"api_object": Api(db), "lock": threading.Lock()},
    )


def serve(
    db: DbSQLite,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = False,
) -> ThreadingHTTPServer:
    """Start the server. Returns it without blocking; call ``serve_forever``."""
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError(
            "the web interface has no authentication and binds to loopback only"
        )
    server = ThreadingHTTPServer((host, port), build_handler(db))
    if open_browser:  # pragma: no cover - depends on a desktop session
        import webbrowser

        threading.Timer(
            0.5, partial(webbrowser.open, f"http://{host}:{server.server_port}/")
        ).start()
    return server
