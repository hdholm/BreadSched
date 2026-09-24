"""Independent acceptance evidence for versioned historical-estimate rules."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

from golden_books import build_and_reopen_book, read_book, read_expected

from breadsched.gen.engine import estimates
from breadsched.gen.engine.estimate_rules import DEFAULT_HISTORICAL_ESTIMATE_RULES


def _snapshot(proposals: list[estimates.HistoricalEstimateProposal]) -> dict[str, object]:
    assert proposals
    return {
        "rules": proposals[0].evidence.rules.serialize(),
        "proposals": {
            proposal.category: {
                "amount": str(proposal.amount.to_decimal()),
                "recurrence": {
                    "period": proposal.recurrence.period.value,
                    "interval": proposal.recurrence.interval,
                    "start": proposal.recurrence.start.isoformat(),
                },
                "confidence": f"{proposal.confidence:.6f}",
                "selected_months": len(proposal.evidence.selected_months),
                "outlier_months": [
                    item.month.isoformat()
                    for item in proposal.evidence.exclusions
                    if item.exclusion == "isolated amount outlier"
                ],
                "trend": proposal.evidence.trend.serialize() if proposal.evidence.trend else None,
                "seasonal_amounts": [
                    {"month": item.month, "amount": str(item.amount.to_decimal())}
                    for item in proposal.seasonal_amounts
                ],
                "funding": proposal.evidence.funding.serialize(),
            }
            for proposal in proposals
        },
    }


def test_default_rules_match_independent_golden(tmp_path):
    declaration = read_book("historical-rules", milestone="0227")
    db = build_and_reopen_book(
        "historical-rules",
        tmp_path / "historical-rules.breadsched",
        milestone="0227",
    )
    try:
        proposals = estimates.propose_historical_estimates(
            db,
            as_of=date.fromisoformat(declaration["as_of"]),
            months=declaration["history_months"],
        )
        assert _snapshot(proposals) == read_expected(
            "historical-rules", "estimates", milestone="0227"
        )

        draft = estimates.draft_historical_estimate(db, proposals[0])
        assert draft.estimate_evidence is not None
        assert draft.estimate_evidence["rules"] == DEFAULT_HISTORICAL_ESTIMATE_RULES.serialize()
        assert proposals[0].evidence.summary_lines()[-1] == "Rules: historical-estimates-v1."
    finally:
        db.close()


def test_changed_rules_are_versioned_and_rechecked_against_the_same_book(tmp_path):
    declaration = read_book("historical-rules", milestone="0227")
    strict_seasonality = replace(
        DEFAULT_HISTORICAL_ESTIMATE_RULES,
        version="historical-estimates-test-strict-seasonality",
        seasonality=replace(
            DEFAULT_HISTORICAL_ESTIMATE_RULES.seasonality,
            minimum_peak_to_median_ratio=Decimal("3"),
        ),
    )
    db = build_and_reopen_book(
        "historical-rules",
        tmp_path / "strict-historical-rules.breadsched",
        milestone="0227",
    )
    try:
        proposals = estimates.propose_historical_estimates(
            db,
            as_of=date.fromisoformat(declaration["as_of"]),
            months=declaration["history_months"],
            rules=strict_seasonality,
        )
        utilities = next(item for item in proposals if item.category == "utilities")
        assert utilities.seasonal is False
        assert utilities.evidence.rules.version == strict_seasonality.version
        assert utilities.evidence.rules.serialize() == strict_seasonality.serialize()
    finally:
        db.close()
