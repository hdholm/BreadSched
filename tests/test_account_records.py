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
    "imported_notes_digest": "",
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


def test_no_code_uses_the_flat_names():
    """Setting a removed flat name only adds a stray attribute, silently (the demo's
    growth rates were lost that way), so no source, script, or example may use one."""
    import re
    from pathlib import Path

    names = (
        "pays_in_full|usual_payment|payment_day|card_payment_account|linked_asset|"
        "annual_interest|fsa_years|fsa_dependent_care|annual_return|cost_basis_method|"
        "exclude_from_projection|emergency_fund_override|tax_relevant_override"
    )
    # A bare receiver (not a record such as ``account.card``, nor text such as an
    # error code) other than ``self`` or ``args``, whose widgets and options keep
    # the names.
    pattern = re.compile(rf"(?<![\w.\"'])(?!self\b|args\b)[a-z_]\w*\.({names})\b")
    root = Path(__file__).resolve().parents[1]
    found = []
    for folder in ("src", "scripts", "examples"):
        for path in (root / folder).rglob("*.py"):
            if path.name == "account.py" and path.parent.name == "lib":
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if pattern.search(line):
                    found.append(f"{path.relative_to(root)}:{number}: {line.strip()}")
    assert not found, "\n".join(found)
