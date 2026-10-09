"""Web projection: detached drafts, their reports and month explanations, and saving."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..gen.db.sqlite import DbSQLite
from ..gen.engine import projection
from ..gen.engine.completeness import combine
from ..gen.engine.currency import reporting_currency_handle
from ..gen.engine.projection_bridge import month_bridges, projection_bridges
from ..gen.engine.projection_result import projection_chart
from ..gen.lib import Scenario
from ..gen.services import (
    SaveBaseAssumptions,
    SaveScenarioAssumptions,
    save_base_assumptions,
    save_scenario_assumptions,
)
from ..presentation import (
    projection_goal_notes,
    reimbursement_outlook_text,
    runway_comparison_text,
    runway_lines,
)
from .controls import service_error
from .scenario_resource import assumptions_from_payload, management_base_scenario

if TYPE_CHECKING:
    from .context import Api


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
        "bridges": [item.as_dict() for item in month_bridges(result, month_index)],
    }


def _currency_label(db: DbSQLite) -> str:
    commodity = db.get_commodity(reporting_currency_handle(db))
    return commodity.mnemonic if commodity is not None else ""


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
        # Opening + planned events + assumption effects = closing, over the horizon.
        "bridges": [item.as_dict() for item in projection_bridges(result) or ()],
        # A labelled subtotal when a foreign balance or event lacks a rate (#236).
        "completeness": result.completeness.as_dict(),
        "warnings": list(result.warnings),
        "goal_notes": projection_goal_notes(result),
        "goal_milestones": [item.as_dict() for item in result.goal_milestones],
        "runway": result.runway().as_dict(),
        "chart": projection_chart(result, None, _currency_label(db)).as_dict(),
        "runway_notes": runway_lines(result.runway()),
        "reimbursements": [item.as_dict() for item in result.reimbursements],
        "reimbursement_notes": [reimbursement_outlook_text(item) for item in result.reimbursements],
        "rows": [
            {
                "label": row.label,
                "income": row.income,
                "expense": row.expense,
                "cash": row.cash_close,
                "holdings": row.holdings,
                "liabilities": row.liabilities,
                "net_worth": row.net_worth,
                "completeness": result.month_completeness(index).as_dict(),
            }
            for index, row in enumerate(result.rows)
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
    shown = projection_report(db, primary, base=primary_base, result=primary_result)
    # The chart overlays the compared scenario's net worth, as on the desktop.
    shown["chart"] = projection_chart(
        primary_result, comparison_result, _currency_label(db)
    ).as_dict()
    return {
        "primary": shown,
        "comparison": {
            "scenario": {
                "handle": None if comparison_base else comparison.handle,
                "name": comparison.name,
                "assumptions": comparison.effective_assumptions().serialize(),
                "assumption_sources": comparison.assumption_sources(comparison.start),
            },
            "summary": comparison_summary,
            "runway": comparison_result.runway().as_dict(),
            "runway_comparison": runway_comparison_text(
                primary.name,
                primary_result.runway(),
                comparison.name,
                comparison_result.runway(),
            ),
            "completeness": comparison_result.completeness.as_dict(),
            # A difference is only as complete as both of its inputs.
            "delta_completeness": combine(
                primary_result.completeness, comparison_result.completeness
            ).as_dict(),
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
                    "completeness": combine(
                        primary_result.month_completeness(index),
                        comparison_result.month_completeness(index),
                    ).as_dict(),
                }
                for index, (left, right) in enumerate(
                    zip(primary_result.rows, comparison_result.rows, strict=True)
                )
            ],
        },
    }


def projection_draft(
    db: DbSQLite, scenario_handle: str | None = None, years: int | None = None
) -> Scenario:
    """Return a detached projection scenario safe for browser-side editing."""
    if scenario_handle:
        stored = db.get_scenario(scenario_handle)
        if stored is None:
            raise KeyError(scenario_handle)
        scenario = stored.clone()
    else:
        scenario = management_base_scenario(db)
    if years is not None:
        if years < 1 or years > 100:
            raise ValueError("projection years must be between 1 and 100")
        scenario.years = years
    return scenario


def apply_projection_payload(db: DbSQLite, scenario: Scenario, payload: dict) -> Scenario:
    """Apply editable projection controls to a detached scenario."""
    years = int(payload.get("years", scenario.years))
    if years < 1 or years > 100:
        raise ValueError("projection years must be between 1 and 100")
    scenario.years = years
    previous = scenario.effective_assumptions()
    updated = assumptions_from_payload(
        db, payload.get("assumptions", previous.serialize()), previous
    )
    if scenario.inherits_base_assumptions:
        for field in scenario.assumption_sources():
            if getattr(updated, field) != getattr(previous, field):
                scenario.set_assumption_override(field, getattr(updated, field))
        scenario.assumptions.per_account = dict(updated.per_account)
    else:
        scenario.assumptions = updated
    return scenario


def projection_explain(api: Api, payload: dict) -> dict:
    """Explain one month of the currently applied projection draft."""
    scenario = projection_draft(api.db, payload.get("handle"))
    apply_projection_payload(api.db, scenario, payload)
    return projection_month_report(api.db, scenario, int(payload["month_index"]))


def scenario_projection(
    api: Api, scenario_handle: str | None = None, years: int | None = None
) -> dict:
    """Calculate a persisted Base/saved scenario without mutating it."""
    return projection_report(
        api.db, projection_draft(api.db, scenario_handle, years), base=scenario_handle is None
    )


def projection_calculate(api: Api, payload: dict) -> dict:
    """Calculate an edited projection draft without persisting the edits."""
    handle = str(payload.get("handle") or "").strip() or None
    scenario = projection_draft(api.db, handle)
    apply_projection_payload(api.db, scenario, payload)
    return projection_report(api.db, scenario, base=handle is None)


def projection_compare(api: Api, payload: dict) -> dict:
    """Compare an edited projection draft with another persisted scenario."""
    handle = str(payload.get("handle") or "").strip() or None
    compare_handle = str(payload.get("compare_handle") or "").strip() or None
    if handle == compare_handle:
        raise ValueError("choose two different scenarios to compare")

    primary = projection_draft(api.db, handle)
    apply_projection_payload(api.db, primary, payload)
    comparison = projection_draft(api.db, compare_handle)
    comparison.years = primary.years

    return projection_comparison_report(
        api.db,
        primary,
        comparison,
        primary_base=handle is None,
        comparison_base=compare_handle is None,
    )


def projection_save(api: Api, payload: dict) -> dict:
    """Persist projection controls explicitly, preserving hidden model fields."""
    handle = str(payload.get("handle") or "").strip() or None
    scenario = projection_draft(api.db, handle)
    apply_projection_payload(api.db, scenario, payload)
    if handle is None:
        base_result = save_base_assumptions(api.db, SaveBaseAssumptions(scenario.assumptions))
        if not base_result.ok:
            raise service_error(base_result.errors[0])
        return projection_report(api.db, scenario, base=True)
    result = save_scenario_assumptions(
        api.db, SaveScenarioAssumptions(scenario, existing_handle=handle)
    )
    if not result.ok:
        raise service_error(result.errors[0])
    return projection_report(api.db, scenario)
