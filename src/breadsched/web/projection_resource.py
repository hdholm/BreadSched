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
