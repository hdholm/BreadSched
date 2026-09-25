"""Read-only scenario management response over stored scenarios."""

from __future__ import annotations

from ..gen.db.sqlite import DbSQLite
from ..gen.lib import AccountClass, Scenario


def scenario_payload(scenario: Scenario, *, base: bool = False) -> dict:
    """Serialize effective assumptions and their inheritance evidence."""
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


def scenarios_report(db: DbSQLite, base: Scenario) -> dict:
    """Project Base, saved scenarios, and eligible rate accounts."""
    projection_accounts = []
    for account in db.iter_accounts():
        if not (
            account.account_class is AccountClass.LIABILITY
            or (account.account_class is AccountClass.ASSET and account.atype.is_investment)
        ):
            continue
        projection_accounts.append(
            {
                "handle": account.handle,
                "name": db.full_name(account),
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
            scenario_payload(base, base=True),
            *(scenario_payload(item) for item in db.iter_scenarios()),
        ],
        "projection_accounts": projection_accounts,
    }
