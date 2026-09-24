"""Loader and stable snapshots for independent, hand-calculated golden books."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from breadsched.gen.db.sqlite import DbSQLite
from breadsched.gen.engine.activity import PlanMeasure, ReportingPeriod
from breadsched.gen.lib import (
    Account,
    AccountType,
    AssumptionPeriod,
    Assumptions,
    Money,
    PeriodType,
    PlanningResolution,
    Recurrence,
    Scenario,
    ScenarioSchedule,
    ScheduledSplit,
    ScheduledTransaction,
    Split,
    Transaction,
)
from breadsched.gen.services.assumptions import BASE_ASSUMPTIONS_KEY
from breadsched.gen.services.plan import PlanQuery, query_plan

ROOT = Path(__file__).with_name("goldens")


def read_book(name: str, *, milestone: str = "0226") -> dict[str, Any]:
    return json.loads((ROOT / milestone / name / "book.json").read_text())


def read_expected(name: str, report: str, *, milestone: str = "0226") -> dict[str, Any]:
    return json.loads((ROOT / milestone / name / f"expected-{report}.json").read_text())


def _when(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def _split(item: dict[str, Any], *, handle: str | None = None) -> Split:
    return Split(
        item["account"],
        item["amount"],
        handle=handle,
        planning_flow=item.get("planning_flow"),
        investment_activity=item.get("investment_activity"),
    )


def _scheduled_split(item: dict[str, Any]) -> ScheduledSplit:
    return ScheduledSplit(
        item["account"],
        item.get("amount"),
        formula=item.get("formula", ""),
        planning_flow=item.get("planning_flow"),
        investment_activity=item.get("investment_activity"),
    )


def _recurrence(item: dict[str, Any]) -> Recurrence:
    return Recurrence(
        period=PeriodType(item.get("period", "month")),
        interval=item.get("interval", 1),
        start=date.fromisoformat(item["start"]),
        end=_when(item.get("end")),
        count=item.get("count"),
        day_of_month=item.get("day_of_month"),
    )


def _assumptions(item: dict[str, Any]) -> Assumptions:
    return Assumptions(
        income_growth=item.get("income_growth", "0"),
        expense_inflation=item.get("expense_inflation", "0"),
        investment_return=item.get("investment_return", "0"),
        cash_interest=item.get("cash_interest", "0"),
        liability_interest=item.get("liability_interest", "0"),
        per_account=item.get("per_account", {}),
    )


def build_and_reopen_book(name: str, path: Path, *, milestone: str = "0226") -> DbSQLite:
    """Materialize a declaration, close it, and return a fresh database handle."""
    data = read_book(name, milestone=milestone)
    db = DbSQLite()
    db.load(str(path))
    with db.transaction(f"Build golden book {name}") as txn:
        for item in data["accounts"]:
            account = Account(
                handle=item["id"],
                name=item["name"],
                atype=AccountType.parse(item["type"]),
                parent=item.get("parent"),
                placeholder=item.get("placeholder", False),
            )
            if "annual_return" in item:
                account.annual_return = item["annual_return"]
            if "annual_interest" in item:
                account.annual_interest = item["annual_interest"]
            account.linked_asset = item.get("linked_asset")
            db.add_account(account, txn)
        for item in data.get("transactions", []):
            transaction = Transaction(
                handle=item["id"],
                post_date=date.fromisoformat(item["date"]),
                description=item["description"],
                splits=[
                    _split(split, handle=f"{item['id']}:split:{index}")
                    for index, split in enumerate(item["splits"], 1)
                ],
            )
            transaction.scheduled_from = item.get("scheduled_from")
            transaction.planned_occurrence = item.get("planned_occurrence")
            transaction.planned_for = _when(item.get("planned_for"))
            if item.get("planned_amount") is not None:
                transaction.planned_amount = Money(item["planned_amount"])
            transaction.planning_resolution = PlanningResolution(
                item.get("planning_resolution", "unresolved")
            )
            db.add_transaction(transaction, txn)
        for item in data.get("schedules", []):
            schedule = ScheduledTransaction(
                handle=item["id"],
                name=item["name"],
                description=item.get("description", item["name"]),
                recurrence=_recurrence(item["recurrence"]),
                splits=[_scheduled_split(split) for split in item["splits"]],
                growth_policy=item.get("growth_policy", "none"),
            )
            schedule.placeholder = item.get("placeholder", False)
            db.add_scheduled(schedule, txn)
        for item in data.get("scenarios", []):
            scenario = Scenario(
                handle=item["id"],
                name=item["name"],
                start=date.fromisoformat(item["start"]),
                years=item.get("years", 1),
                assumptions=_assumptions(item.get("assumptions", {})),
                assumption_periods=[
                    AssumptionPeriod(
                        date.fromisoformat(period["start"]),
                        end=_when(period.get("end")),
                        cash_interest=period.get("cash_interest"),
                        investment_return=period.get("investment_return"),
                        liability_interest=period.get("liability_interest"),
                        per_account=period.get("per_account"),
                        description=period.get("description", ""),
                    )
                    for period in item.get("assumption_periods", [])
                ],
                inherits_base_assumptions=item.get("inherits_base_assumptions", False),
                assumption_overrides=set(item.get("assumption_overrides", [])),
                account_assumption_overrides=set(item.get("account_assumption_overrides", [])),
            )
            for override in item.get("schedule_overrides", []):
                scenario.schedule_overrides.append(
                    ScenarioSchedule(
                        handle=override["id"],
                        name=override["name"],
                        source_schedule=override.get("source_schedule"),
                        enabled=override.get("enabled", True),
                        recurrence=_recurrence(override["recurrence"]),
                        splits=[_scheduled_split(split) for split in override.get("splits", [])],
                        placeholder=override.get("placeholder", False),
                        growth_policy=override.get("growth_policy", "none"),
                    )
                )
            for one_off in item.get("one_offs", []):
                scenario.add_one_off(
                    date.fromisoformat(one_off["date"]),
                    one_off["account"],
                    one_off["amount"],
                    one_off.get("description", ""),
                )
            db.add_scenario(scenario, txn)
    if "base_assumptions" in data:
        db.set_metadata(BASE_ASSUMPTIONS_KEY, _assumptions(data["base_assumptions"]).serialize())
    db.close()
    reopened = DbSQLite()
    reopened.load(str(path))
    return reopened


def money(value: Money | None) -> str | None:
    return None if value is None else f"{value.to_decimal():.2f}"


def plan_snapshot(db: DbSQLite, spec: dict[str, Any]) -> dict[str, Any]:
    result = query_plan(
        db,
        PlanQuery(
            start=date.fromisoformat(spec["start"]),
            end=date.fromisoformat(spec["end"]),
            period=ReportingPeriod(spec.get("period", "month")),
            scenario=spec.get("scenario"),
            compare=spec.get("compare"),
            today=date.fromisoformat(spec["today"]),
        ),
    )
    assert result.ok
    value = result.value
    assert value is not None
    report = value.report
    return {
        "periods": [period.label for period in report.activity.periods],
        "planned_cash": [money(period.planned_cash_change) for period in report.activity.periods],
        "actual_cash": [money(period.actual_cash_change) for period in report.activity.periods],
        "operating_net_planned": [
            money(item) for item in report.operating_net_totals(PlanMeasure.PLANNED)
        ],
        "operating_net_actual": [
            money(item) for item in report.operating_net_totals(PlanMeasure.ACTUAL)
        ],
        "cash_bridge_planned": [
            money(item) for item in report.cash_bridge_totals(PlanMeasure.PLANNED)
        ],
        "cash_bridge_actual": [
            money(item) for item in report.cash_bridge_totals(PlanMeasure.ACTUAL)
        ],
        "cash_position": {
            "opening": money(report.cash_position.opening),
            "closing": money(report.cash_position.closing),
            "minimum": money(report.cash_position.minimum),
            "minimum_date": report.cash_position.minimum_date.isoformat(),
        },
        "categories": {
            row.full_name: {
                "planned": [money(item) for item in row.planned],
                "actual": [money(item) for item in row.actual],
                "variance": [money(item) for item in row.variance],
            }
            for row in report.categories
            if any(row.planned) or any(row.actual)
        },
        "planning_flows": {
            f"{row.kind.value}:{row.full_name}": {
                "planned": [money(item) for item in row.planned],
                "actual": [money(item) for item in row.actual],
            }
            for row in report.planning_flows
        },
        "mortgage_payments": {
            row.full_name: [money(item) for item in row.planned] for row in report.mortgage_payments
        },
        "activity": {
            "unresolved": report.activity.unresolved_count,
            "unexpected": report.activity.unexpected_count,
            "actuals": [
                {
                    "id": actual.transaction,
                    "date": actual.post_date.isoformat(),
                    "resolution": actual.planning_resolution.value,
                    "planned_for": actual.planned_for.isoformat() if actual.planned_for else None,
                    "planned_amount": money(actual.planned_amount),
                    "variance": money(actual.variance),
                    "date_variance_days": actual.date_variance_days,
                }
                for period in report.activity.periods
                for actual in period.actual_transactions
            ],
        },
        "scenario": value.scenario.name,
        "assumption_sources": value.assumption_sources,
        "comparison": value.comparison.scenario.name if value.comparison else None,
    }


def projection_snapshot(db: DbSQLite, scenario: Scenario, month_count: int = 2) -> dict[str, Any]:
    from breadsched.gen.engine import projection

    result = projection.project(db, scenario)
    rows = result.rows[:month_count]
    return {
        "scenario": scenario.name,
        "months": [
            {
                "month": row.month.isoformat(),
                "cash_open": money(row.cash_open),
                "income": money(row.income),
                "expense": money(row.expense),
                "contributions": money(row.contributions),
                "retirement_distributions": money(row.retirement_distributions),
                "debt_payments": money(row.debt_payments),
                "interest_earned": money(row.interest_earned),
                "investment_growth": money(row.investment_growth),
                "interest_charged": money(row.interest_charged),
                "cash_close": money(row.cash_close),
                "holdings": money(row.holdings),
                "liabilities": money(row.liabilities),
                "net_worth": money(row.net_worth),
                "reconciles": row.ledger.reconciles(),
            }
            for row in rows
        ],
        "ending_cash": money(result.ending_cash),
        "minimum_cash": money(result.minimum_cash),
        "warnings": result.warnings,
    }
