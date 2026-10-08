"""Wording for FSA accounts and claims, shared costs, and reimbursable expenses."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..gen.engine.fsa import FsaYearStatus
    from ..gen.engine.fsa_claims import SharedCost
    from ..gen.engine.reimbursement_outlook import ReimbursementOutlook
    from ..gen.lib.scenario import ReimbursementOverride


#: How each FSA claim attachment role reads in every interface.
CLAIM_ROLE_LABELS = {
    "direct_payment": "Paid from the FSA card",
    "direct_refund": "Refunded to the FSA card",
    "payment": "Healthcare payment",
    "refund": "Provider refund",
    "reimbursement": "FSA reimbursement",
    "repayment": "Repaid to the FSA",
}


def claim_role_label(role: str) -> str:
    return CLAIM_ROLE_LABELS.get(role, role.replace("_", " "))


def fsa_account_text(name: str, dependent_care: bool) -> str:
    """An FSA account's name, marked when it is a dependent care FSA."""
    return f"{name} (dependent care)" if dependent_care else name


def fsa_usage_text(status: FsaYearStatus) -> str:
    """How an FSA year's election was used, in the words every interface shows."""
    parts = [
        (status.direct_payments, "paid from the card"),
        (status.reimbursements, "reimbursed"),
        (status.provider_refunds, "refunded to the card"),
        (status.repaid, "repaid"),
    ]
    shown = [f"{amount.format()} {words}" for amount, words in parts if amount]
    return "; ".join(shown) or "—"


def reimbursement_outlook_text(item: ReimbursementOutlook) -> str:
    """One expected reimbursement's gross and net cost in a projection scenario."""
    receivable = item.receivable
    label = f"{receivable.payer}: {receivable.description}"
    earlier = item.reimbursed + item.written_off
    before = f", {earlier.format()} already reimbursed or written off" if earlier else ""
    if item.expected > 0:
        expected = f"{item.expected.format()} expected back on {item.expected_on.isoformat()}"
    else:
        expected = f"nothing expected back (written off on {item.expected_on.isoformat()})"
    shortfall = (
        f"; {item.shortfall.format()} of what is owed is not expected and is projected "
        "as written off"
        if item.shortfall > 0 and item.expected > 0
        else ""
    )
    changed = " (changed in this scenario)" if item.changed else ""
    return (
        f"Reimbursable {label}{changed}: gross cost {item.gross.format()}{before}; "
        f"{expected}{shortfall}; net household cost {item.net_cost.format()}."
    )


def reimbursement_override_text(scenario_name: str, override: ReimbursementOverride) -> str:
    """How one scenario changes an expected reimbursement."""
    parts = []
    if override.amount is not None:
        parts.append(
            f"{override.amount.format()} expected back" if override.amount else "nothing expected"
        )
    if override.on is not None:
        parts.append(f"on {override.on.isoformat()}")
    return f"{scenario_name}: " + ", ".join(parts)


def shared_cost_text(shared: SharedCost) -> str:
    """One line allocating an expense between a payer, the FSA, and you (issue #192)."""
    fsa = (
        f"{shared.fsa_share.format()} until the EOB is entered"
        if shared.waiting_eob
        else shared.fsa_share.format()
    )
    text = (
        f"Of {shared.expense.format()}: {shared.payer} pays {shared.payer_share.format()}, "
        f"the FSA {fsa}, you {shared.your_share.format(parens_negative=True)}"
    )
    if shared.needs_review:
        text += (
            f". Needs review: {shared.over_allocated.format()} more than was paid; "
            "adjust the expected amount, the EOB, or write off the difference"
        )
    return text
