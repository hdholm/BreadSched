"""Transaction editor requests travel as JSON without loss (#356, #357)."""

from __future__ import annotations

import json
from datetime import date

import pytest

from breadsched.api.transaction_codec import save_transaction_from_json, save_transaction_json
from breadsched.gen.lib import Transaction
from breadsched.gen.lib.amount import Amount
from breadsched.gen.lib.money import Money
from breadsched.gen.lib.transaction import InvestmentActivityKind, PlanningFlowKind
from breadsched.gen.services import (
    ClaimAttachment,
    SaveTransaction,
    TransactionInput,
    TransactionSplitInput,
)


def _everything() -> SaveTransaction:
    source = Transaction.simple(date(2026, 1, 2), "Buy", "brokerage", "bank", "1000.00")
    return SaveTransaction(
        TransactionInput(
            post_date=date(2026, 1, 3),
            description="Buy fund",
            splits=(
                TransactionSplitInput(
                    "brokerage",
                    Amount(Money("1000.00"), "USD"),
                    quantity=Amount(Money("12.3456"), "FUND"),
                    handle="split-1",
                    memo="lot A",
                    planning_flow=PlanningFlowKind.RETIREMENT_SAVING,
                    investment_activity=InvestmentActivityKind.CONTRIBUTION,
                ),
                TransactionSplitInput("bank", Amount(Money("-1000.00"), "USD")),
            ),
            num="1042",
            notes="monthly",
            currency="USD",
            investment_activity=InvestmentActivityKind.CONTRIBUTION,
            tags=("retirement", "auto"),
        ),
        existing_handle=source.handle,
        claim_attachment=ClaimAttachment("claim-1", "payment", date(2026, 1, 1)),
        source=source,
    )


def test_a_transaction_request_survives_json_unchanged():
    request = _everything()
    encoded = save_transaction_json(request)
    decoded = save_transaction_from_json(json.loads(json.dumps(encoded)))
    assert save_transaction_json(decoded) == encoded
    split = decoded.definition.splits[0]
    assert split.quantity == Amount(Money("12.3456"), "FUND")
    assert split.value == Amount(Money("1000.00"), "USD")
    assert decoded.definition.tags == ("retirement", "auto")
    assert decoded.source.serialize() == request.source.serialize()


def test_tags_left_out_keep_the_stored_ones():
    request = SaveTransaction(
        TransactionInput(
            date(2026, 1, 3),
            "x",
            (TransactionSplitInput("a", Amount(Money("1"), "USD")),),
        )
    )
    assert save_transaction_from_json(save_transaction_json(request)).definition.tags is None


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda d: d["definition"].update(post_date="soon"), "post_date"),
        (lambda d: d["definition"].update(splits={}), "splits"),
        (lambda d: d["definition"]["splits"][0].update(value=12), "value"),
        (lambda d: d["definition"]["splits"][0]["value"].update(value=12), "value"),
        (lambda d: d["definition"]["splits"][0].update(planning_flow="other"), "planning_flow"),
        (lambda d: d["definition"].update(tags="food"), "tags"),
        (lambda d: d.update(claim_attachment={"claim": 1, "role": "payment"}), "claim"),
        (lambda d: d.update(source=[]), "source"),
    ],
)
def test_a_malformed_transaction_request_is_refused(change, message):
    data = json.loads(json.dumps(save_transaction_json(_everything())))
    change(data)
    with pytest.raises(ValueError, match=message):
        save_transaction_from_json(data)
