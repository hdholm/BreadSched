"""Read-only projection month explanation for the web presentation."""

from __future__ import annotations

from ..gen.db.sqlite import DbSQLite
from ..gen.engine import projection
from ..gen.lib import Scenario


def projection_month_report(db: DbSQLite, scenario: Scenario, month_index: int) -> dict:
    """Calculate and serialize one month of a detached projection draft."""
    result = projection.project(db, scenario)
    detail = projection.explain_month(db, result, month_index)

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


def projection_report(
    db: DbSQLite,
    scenario: Scenario,
    *,
    base: bool = False,
    result: projection.Projection | None = None,
) -> dict:
    """Calculate and serialize a projection with saved scenario choices."""
    if result is None:
        result = projection.project(db, scenario)
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
                *[{"handle": item.handle, "name": item.name} for item in db.iter_scenarios()],
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


def projection_comparison_report(
    db: DbSQLite,
    primary: Scenario,
    comparison: Scenario,
    *,
    primary_base: bool,
    comparison_base: bool,
) -> dict:
    """Calculate and serialize two aligned projection drafts and their differences."""
    primary_result = projection.project(db, primary)
    comparison_result = projection.project(db, comparison)
    if len(primary_result.rows) != len(comparison_result.rows):
        raise ValueError("projection comparison horizons do not align")

    def difference(left, right):
        return left - right

    primary_summary = primary_result.summary()
    comparison_summary = comparison_result.summary()
    return {
        "primary": projection_report(db, primary, base=primary_base, result=primary_result),
        "comparison": {
            "scenario": {
                "handle": None if comparison_base else comparison.handle,
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
