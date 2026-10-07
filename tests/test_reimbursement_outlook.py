"""A scenario can expect less, nothing, or later from a payer; Projection shows the cost.

What a scenario does not expect back is projected as written off on the receipt date,
back into the expense, so the receivable account still empties and the shortfall is
a projected expense. Projection lists each expected reimbursement's gross cost, what
the scenario expects, and the resulting net household cost.
"""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal

import pytest
from test_receivable_receipts import DUE, TODAY, _post, _receivable

from breadsched.gen.engine import planning, projection
from breadsched.gen.engine.reimbursement_outlook import reimbursement_outlook
from breadsched.gen.lib import Assumptions, Money, Scenario, Transaction
from breadsched.gen.services import SetReimbursementOverride, set_reimbursement_override
from breadsched.gen.services.receivables import (
    attach_reimbursement_split,
    delete_receivable,
)
from breadsched.presentation import projection_notes, reimbursement_outlook_text

LATER = DUE + timedelta(days=40)


@pytest.fixture
def db(tmp_path):
    """A book on disk, so the command-line test can open the same file."""
    from breadsched.gen.db.sqlite import DbSQLite

    database = DbSQLite()
    database.load(str(tmp_path / "book.breadsched"))
    yield database
    database.close()


def _scenario(db, name="Careful"):
    scenario = Scenario(
        name=name,
        start=TODAY.replace(day=1),
        years=1,
        assumptions=Assumptions(
            cash_interest=Decimal(0), expense_inflation=Decimal(0), income_growth=Decimal(0)
        ),
    )
    with db.transaction("Add scenario") as txn:
        db.add_scenario(scenario, txn)
    return scenario


def _change(db, scenario, receivable, **fields):
    return set_reimbursement_override(
        db, SetReimbursementOverride(scenario.handle, receivable.handle, **fields)
    )


def _receipt_event(db, scenario):
    end = TODAY + timedelta(days=365)
    events = planning.scenario_events(db, db.get_scenario(scenario.handle), TODAY, end)
    [event] = [item for item in events if item.key.startswith("receivable:")]
    return event


def test_a_partial_payment_writes_the_rest_back_into_the_expense(db, book):
    receivable = _receivable(db, book)
    scenario = _scenario(db)
    assert _change(db, scenario, receivable, amount=Money("90.00")).ok

    event = _receipt_event(db, scenario)
    assert event.planned_date == DUE
    assert {(s.account, s.amount) for s in event.splits} == {
        (book.checking, Money("90.00")),
        (receivable.account, Money("-150.00")),
        (book.groceries, Money("60.00")),
    }
    [outlook] = reimbursement_outlook(db, db.get_scenario(scenario.handle), TODAY, LATER)
    assert (outlook.gross, outlook.expected, outlook.shortfall, outlook.net_cost) == (
        Money("150.00"),
        Money("90.00"),
        Money("60.00"),
        Money("60.00"),
    )
    assert outlook.changed
    assert reimbursement_outlook_text(outlook) == (
        "Reimbursable Acme Insurance: Clinic visit (changed in this scenario): gross cost "
        f"150.00; 90.00 expected back on {DUE.isoformat()}; 60.00 of what is owed is not "
        "expected and is projected as written off; net household cost 60.00."
    )


def test_nothing_expected_and_a_later_date(db, book):
    receivable = _receivable(db, book)
    scenario = _scenario(db)
    assert _change(db, scenario, receivable, amount=Money(0), on=LATER).ok

    event = _receipt_event(db, scenario)
    assert event.planned_date == LATER
    assert event.description == "Not expected from Acme Insurance: Clinic visit"
    assert {(s.account, s.amount) for s in event.splits} == {
        (receivable.account, Money("-150.00")),
        (book.groceries, Money("150.00")),
    }
    [outlook] = reimbursement_outlook(db, db.get_scenario(scenario.handle), TODAY, LATER)
    assert outlook.net_cost == Money("150.00")
    assert "nothing expected back (written off on" in reimbursement_outlook_text(outlook)


def test_projection_cash_and_net_worth_follow_the_scenario(db, book):
    receivable = _receivable(db, book)
    base = _scenario(db, "Base")
    careful = _scenario(db)
    assert _change(db, careful, receivable, amount=Money("90.00")).ok

    expected = projection.project(db, db.get_scenario(base.handle))
    reduced = projection.project(db, db.get_scenario(careful.handle))
    assert expected.rows[-1].cash_close - reduced.rows[-1].cash_close == Money("60.00")
    # The shortfall is a projected expense, so net worth falls by it too.
    assert expected.rows[-1].net_worth - reduced.rows[-1].net_worth == Money("60.00")
    assert sum((row.expense for row in reduced.rows), Money(0)) - sum(
        (row.expense for row in expected.rows), Money(0)
    ) == Money("60.00")
    assert all(row.ledger.reconciles() for row in reduced.rows)
    [base_outlook] = expected.reimbursements
    assert (base_outlook.expected, base_outlook.net_cost, base_outlook.changed) == (
        Money("150.00"),
        Money(0),
        False,
    )
    assert reduced.reimbursements[0].net_cost == Money("60.00")
    assert any("net household cost 60.00" in note for note in projection_notes(reduced))


def test_earlier_partial_reimbursement_counts_toward_the_net_cost(db, book):
    receivable = _receivable(db, book)
    back = Transaction.simple(TODAY, "Part paid", book.checking, book.groceries, "50.00")
    txn, split = _post(db, back, book.groceries)
    assert attach_reimbursement_split(db, receivable.handle, txn, split).ok
    scenario = _scenario(db)

    [outlook] = reimbursement_outlook(db, db.get_scenario(scenario.handle), TODAY, LATER)
    assert (outlook.gross, outlook.reimbursed, outlook.expected, outlook.net_cost) == (
        Money("150.00"),
        Money("50.00"),
        Money("100.00"),
        Money(0),
    )
    assert "50.00 already reimbursed or written off" in reimbursement_outlook_text(outlook)


@pytest.mark.parametrize(
    ("fields", "code"),
    [
        ({"amount": Money("150.01")}, "scenario.reimbursement.amount"),
        ({"amount": Money("-1")}, "scenario.reimbursement.amount"),
        ({"on": TODAY - timedelta(days=1)}, "scenario.reimbursement.date"),
    ],
)
def test_invalid_changes_are_refused_and_keep_the_scenario(db, book, fields, code):
    receivable = _receivable(db, book)
    scenario = _scenario(db)
    assert _change(db, scenario, receivable, amount=Money("10.00")).ok
    before = db.get_scenario(scenario.handle).serialize()

    result = _change(db, scenario, receivable, **fields)
    assert [error.code for error in result.errors] == [code]
    assert db.get_scenario(scenario.handle).serialize() == before


def test_a_reimbursement_not_expected_cannot_be_changed(db, book):
    overdue = _receivable(db, book, expected=TODAY - timedelta(days=1))
    scenario = _scenario(db)
    result = _change(db, scenario, overdue, amount=Money(0))
    assert [error.code for error in result.errors] == ["scenario.reimbursement.not_expected"]


def test_clearing_and_deleting_drop_the_change(db, book):
    receivable = _receivable(db, book)
    scenario = _scenario(db)
    assert _change(db, scenario, receivable, amount=Money(0)).ok
    cleared = _change(db, scenario, receivable)
    assert cleared.ok and cleared.value.reimbursement_overrides == {}

    assert _change(db, scenario, receivable, on=LATER).ok
    stored = db.get_scenario(scenario.handle).reimbursement_overrides[receivable.handle]
    assert (stored.amount, stored.on) == (None, LATER)
    assert delete_receivable(db, receivable.handle).ok
    assert db.get_scenario(scenario.handle).reimbursement_overrides == {}


def test_an_older_scenario_without_changes_still_loads(db, book):
    scenario = _scenario(db)
    data = scenario.serialize()
    del data["reimbursement_overrides"]
    older = Scenario()
    older.unserialize(data)
    assert older.reimbursement_overrides == {}


def test_cli_changes_and_reports_the_expected_reimbursement(db, book, capsys, tmp_path):
    from breadsched.cli.main import main

    receivable = _receivable(db, book)
    _scenario(db)
    path = tmp_path / "book.breadsched"
    db.close()

    def run_json(*argv):
        assert main([*argv, "--json"]) == 0
        return json.loads(capsys.readouterr().out)

    changed = run_json(
        "receivables",
        str(path),
        "--scenario",
        "Careful",
        "--expect",
        receivable.handle[:8],
        "--amount",
        "90",
    )
    assert changed["reimbursement_overrides"][receivable.handle]["amount"] == "90.00"
    projected = run_json("project", str(path), "--scenario", "Careful")
    [item] = projected["reimbursements"]
    assert (item["expected"], item["net_cost"]) == ("90.00", "60.00")
    assert main(["project", str(path), "--scenario", "Careful"]) == 0
    assert "net household cost 60.00" in capsys.readouterr().out
    cleared = run_json(
        "receivables", str(path), "--scenario", "Careful", "--expect", receivable.handle[:8]
    )
    assert cleared["reimbursement_overrides"] == {}


def test_web_projection_and_print_show_the_gross_and_net_cost(db, book):
    from breadsched.plugins.export.html_report import projection_report as projection_html
    from breadsched.web.projection_resource import projection_report

    receivable = _receivable(db, book)
    scenario = _scenario(db)
    assert _change(db, scenario, receivable, amount=Money("90.00")).ok
    stored = db.get_scenario(scenario.handle)

    payload = projection_report(db, stored)
    [item] = payload["reimbursements"]
    assert (item["receivable"], item["expected"], item["net_cost"]) == (
        receivable.handle,
        Money("90.00"),
        Money("60.00"),
    )
    [note] = payload["reimbursement_notes"]
    assert note.endswith("net household cost 60.00.")

    html = projection_html(projection.project(db, stored))
    assert "Reimbursable expenses: gross and net cost" in html
    assert "net household cost 60.00" in html


def test_web_route_changes_and_lists_a_scenario_expectation(db, book):
    from breadsched.web.context import Api
    from breadsched.web.controls import ResourceError
    from breadsched.web.receivable_resource import receivable_scenario, receivables
    from breadsched.web.resources import QueryParams

    receivable = _receivable(db, book)
    scenario = _scenario(db)
    api = Api(db)
    request = {
        "scenario": scenario.handle,
        "receivable": receivable.handle,
        "amount": "1,000.00",
        "on": "",
    }
    with pytest.raises(ResourceError) as refused:
        receivable_scenario(api, request)
    assert refused.value.code == "scenario.reimbursement.amount"
    assert db.get_scenario(scenario.handle).reimbursement_overrides == {}

    changed = receivable_scenario(api, {**request, "amount": "0"})
    assert changed["text"] == "Careful: nothing expected"
    listing = receivables(api, QueryParams(""))
    assert listing["scenarios"] == [{"handle": scenario.handle, "name": "Careful"}]
    [row] = listing["receivables"]
    assert row["scenario_changes"] == [
        {
            "scenario": scenario.handle,
            "amount": Money(0),
            "on": None,
            "text": "Careful: nothing expected",
        }
    ]

    cleared = receivable_scenario(api, {**request, "amount": ""})
    assert cleared["text"] == "Careful expects what the receivable says"
    assert receivables(api, QueryParams(""))["receivables"][0]["scenario_changes"] == []
