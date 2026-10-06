"""HTTP output for the open book as a whole: its summary and verification report."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..gen.engine import valuation
from ..versioning import version_details

if TYPE_CHECKING:
    from .context import Api


def summary(api: Api) -> dict:
    counts = api.db.summary()
    net_worth = valuation.aggregate_value(api.db, net_worth=True)
    cash = valuation.aggregate_value(
        api.db,
        accounts=[a for a in api.db.iter_accounts() if a.atype.is_cash_like and not a.placeholder],
    )
    return {
        "book": api.db.path,
        "accounts": counts["account"],
        "transactions": counts["txn"],
        "cash": cash.amount.value if cash.amount is not None else None,
        "cash_missing_quotes": list(cash.missing_quotes),
        "net_worth": net_worth.amount.value if net_worth.amount is not None else None,
        "net_worth_missing_quotes": list(net_worth.missing_quotes),
        "net_worth_incompatible_accounts": list(net_worth.incompatible_accounts),
        "scenarios": [s.name for s in api.db.iter_scenarios()],
    }


def verify(api: Api) -> dict[str, object]:
    """Run physical and logical checks without modifying the open book."""
    report = api.db.verification_report()
    payload = report.as_dict()
    payload.update(version_details(native_schema_version=report.native_schema_version))
    return payload
