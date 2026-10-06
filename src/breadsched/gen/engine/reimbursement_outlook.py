"""Expected reimbursements as one scenario sees them, with gross and net cost.

``receivables.expected_receipt`` says what a payer is still expected to send. A
scenario may change that expectation (``Scenario.reimbursement_overrides``): pay
less, nothing, or on another date. Whatever the payer does not pay is projected as
written off on the receipt date, back into the expense, exactly as a recorded
write-off posts; so the receivable account always empties on that date and the
household's net cost rises by the shortfall.

``scenario_receipts`` feeds ``planning.receivable_receipt_events`` (Plan and
Projection share it), and ``reimbursement_outlook`` gives each receipt's gross
cost, what was already reimbursed or written off, what this scenario expects, and
the resulting net household cost.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..db.sqlite import DbSQLite
from ..lib.money import Money
from ..lib.receivable import Receivable
from ..lib.scenario import Scenario
from .receivables import ExpectedReceipt, expected_receipt

__all__ = ["ReimbursementOutlook", "ScenarioReceipt", "reimbursement_outlook", "scenario_receipts"]


@dataclass(frozen=True, slots=True)
class ScenarioReceipt:
    """One expected reimbursement after the scenario's change, if any."""

    receipt: ExpectedReceipt
    when: date
    #: What the payer sends in this scenario (zero for nothing).
    amount: Money
    #: What is still owed but not paid in this scenario: written off on ``when``.
    shortfall: Money
    #: Whether the scenario changed the amount or the date.
    changed: bool

    @property
    def receivable(self) -> Receivable:
        return self.receipt.receivable


@dataclass(frozen=True, slots=True)
class ReimbursementOutlook:
    """Gross and net cost of one reimbursable expense in one scenario."""

    receivable: Receivable
    #: The linked expense: what was spent before anything came back.
    gross: Money
    #: Already reimbursed and already written off before the projection.
    reimbursed: Money
    written_off: Money
    #: What this scenario expects back, and when.
    expected: Money
    expected_on: date
    #: Owed but not expected back in this scenario (projected as written off).
    shortfall: Money
    changed: bool

    @property
    def net_cost(self) -> Money:
        """What the household bears once this scenario's expectation is met."""
        return self.gross - self.reimbursed - self.expected

    def as_dict(self) -> dict[str, object]:
        return {
            "receivable": self.receivable.handle,
            "payer": self.receivable.payer,
            "description": self.receivable.description,
            "gross": self.gross,
            "reimbursed": self.reimbursed,
            "written_off": self.written_off,
            "expected": self.expected,
            "expected_on": self.expected_on,
            "shortfall": self.shortfall,
            "net_cost": self.net_cost,
            "changed": self.changed,
        }


def scenario_receipts(
    db: DbSQLite,
    scenario: Scenario | None,
    start: date,
    end: date,
    *,
    as_of: date | None = None,
) -> list[ScenarioReceipt]:
    """Every expected reimbursement dated in ``start``..``end`` in ``scenario``.

    A scenario date earlier than ``as_of`` (default today) is ignored, because a
    receipt cannot be expected in the past; an amount above what is still owed is
    capped at it.
    """
    today = as_of or date.today()
    overrides = scenario.reimbursement_overrides if scenario is not None else {}
    found: list[ScenarioReceipt] = []
    for receivable in db.iter_receivables():
        receipt = expected_receipt(db, receivable, as_of=today)
        if receipt is None:
            continue
        override = overrides.get(receivable.handle)
        when, amount = receipt.when, receipt.amount
        if override is not None:
            if override.on is not None and override.on >= today:
                when = override.on
            if override.amount is not None:
                amount = min(max(override.amount, Money(0)), receipt.amount)
        if not start <= when <= end:
            continue
        found.append(
            ScenarioReceipt(
                receipt,
                when,
                amount,
                receipt.amount - amount,
                changed=(when, amount) != (receipt.when, receipt.amount),
            )
        )
    return sorted(found, key=lambda item: (item.when, item.receivable.handle))


def reimbursement_outlook(
    db: DbSQLite,
    scenario: Scenario | None,
    start: date,
    end: date,
    *,
    as_of: date | None = None,
) -> list[ReimbursementOutlook]:
    """Gross, expected, and net cost of each reimbursement expected in the range."""
    return [
        ReimbursementOutlook(
            item.receivable,
            gross=item.receipt.summary.expense_total,
            reimbursed=item.receipt.summary.reimbursed,
            written_off=item.receipt.summary.written_off,
            expected=item.amount,
            expected_on=item.when,
            shortfall=item.shortfall,
            changed=item.changed,
        )
        for item in scenario_receipts(db, scenario, start, end, as_of=as_of)
    ]
