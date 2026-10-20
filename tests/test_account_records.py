"""Account settings grouped into per-type records, stored as before (#339).

Card terms, liability terms, the FSA plan, investment settings, and planning
choices each live in one small record, so editors and validation follow the
account type; the stored form is the flat one earlier versions wrote, so books
open unchanged in both directions.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from breadsched.gen.engine.inference import Suggestion
from breadsched.gen.lib import Account, AccountType, FsaFundingYear, Money
from breadsched.gen.lib.account import (
    CardTerms,
    FsaPlan,
    InvestmentSettings,
    LiabilityTerms,
    PlanningChoices,
)

#: An account as 0.2.0a287 stored it, every per-type field set.
STORED = {
    "name": "Visa",
    "atype": "CREDIT CARD",
    "parent": "liabilities",
    "commodity": None,
    "code": "",
    "description": "",
    "placeholder": False,
    "hidden": False,
    "commodity_scu": None,
    "notes": "",
    "source_notes": "",
    "source_atype": None,
    "source_guid": None,
    "source_type": "",
    "source_fields": [],
    "fsa_years": [],
    "fsa_dependent_care": False,
    "annual_return": "0.05",
    "cost_basis_method": "average",
    "annual_interest": "0.1899",
    "exclude_from_projection": True,
    "group": "Debts",
    "linked_asset": "house",
    "pays_in_full": False,
    "usual_payment": [250, 1],
    "payment_day": 12,
    "card_payment_account": "checking",
    "emergency_fund": False,
    "tax_relevant": True,
}


def test_the_stored_form_is_unchanged():
    account = Account.from_dict({**STORED, "handle": "card-1"})
    assert account.card == CardTerms(False, Money("250.00"), 12, "checking")
    assert account.liability == LiabilityTerms(Decimal("0.1899"), "house")
    assert account.investment == InvestmentSettings(Decimal("0.05"), "average")
    assert account.planning == PlanningChoices(True, False, True)
    assert account.fsa == FsaPlan([], False)
    stored = account.serialize()
    common = set(Account(name="x").serialize()) - set(STORED)
    assert {key: value for key, value in stored.items() if key not in common} == STORED


def test_a_new_account_has_its_own_records_with_the_old_defaults():
    first, second = Account(name="A"), Account(name="B", atype=AccountType.FSA)
    first.fsa.years.append(FsaFundingYear(date(2026, 1, 1), date(2026, 12, 31), Money(100)))
    first.card.pays_in_full = False
    assert second.fsa.years == [] and second.card.pays_in_full is True
    assert second.liability == LiabilityTerms()
    assert second.investment.cost_basis_method == "fifo"
    assert second.planning == PlanningChoices()
    # An unset tax choice is still left out of the stored form.
    assert "tax_relevant" not in second.serialize()


def test_a_carried_card_and_the_emergency_fund_read_the_records():
    card = Account(name="Visa", atype=AccountType.CREDIT)
    assert card.carries_balance is False
    card.card.pays_in_full = False
    assert card.carries_balance is True and card.emergency_fund_included is True
    card.planning.emergency_fund = False
    assert card.emergency_fund_included is False


def test_suggestions_write_to_the_right_record():
    card = Account(name="Visa", atype=AccountType.CREDIT)
    for field, value in (
        ("linked_asset", "house"),
        ("card_payment_account", "checking"),
        ("payment_day", 9),
        ("pays_in_full", False),
        ("usual_payment", Money(40)),
    ):
        Suggestion(account=card.handle, field=field, value=value, reason="").apply(card)
    assert card.liability.linked_asset == "house"
    assert card.card == CardTerms(False, Money(40), 9, "checking")
