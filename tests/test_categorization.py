"""Reviewed categorization rules.

A rule maps a payee, or a normalized description key, to an income or expense
category. Rules are ordered: the first matching rule proposes the category, and any
later rule that would choose differently is reported as a conflict. Only
transactions still posted to an import placeholder (Uncategorized CSV or OFX) are
proposed, so a category the user already chose is never replaced. Nothing changes
until the user accepts, and accepting is one undo step.
"""

from __future__ import annotations

from datetime import date

import pytest

from breadsched.gen.services.categorization import (
    AddRule,
    add_rule,
    apply_category_proposals,
    delete_rule,
    list_rules,
    move_rule,
    preview_category_proposals,
)
from breadsched.gen.services.csv_import import CsvImportRequest, CsvMapping, import_csv
from breadsched.gen.services.payees import SavePayee, assign_payee, save_payee

STATEMENT = """Date,Description,Amount
2026-09-01,CORNER GROCER #1234,-42.10
2026-09-02,Corner Grocer 0987,-18.00
2026-09-03,City Power,-60.00
2026-09-04,Payroll,1500.00
"""


@pytest.fixture
def imported(db, book, tmp_path):
    path = tmp_path / "statement.csv"
    path.write_text(STATEMENT, encoding="utf-8")
    mapping = CsvMapping(date="Date", description="Description", amount="Amount")
    result = import_csv(db, CsvImportRequest(str(path), book.checking, mapping))
    assert result.ok, result.errors
    return {row.description: row.identity for row in result.value.preview.rows}


def _counter(db, book, handle):
    transaction = db.get_transaction(handle)
    [split] = [split for split in transaction.splits if split.account != book.checking]
    return split.account


def test_description_rule_proposes_then_accepts(db, book, imported):
    rule = add_rule(db, AddRule(category=book.groceries, description="CORNER GROCER #1")).value
    assert rule is not None and rule.key == "corner grocer"
    placeholder = _counter(db, book, imported["CORNER GROCER #1234"])

    proposals = preview_category_proposals(db).value
    assert sorted(item.description for item in proposals) == [
        "CORNER GROCER #1234",
        "Corner Grocer 0987",
    ]
    assert {item.category for item in proposals} == {book.groceries}
    assert all(item.rule_position == 1 and item.conflicts == () for item in proposals)
    assert _counter(db, book, imported["CORNER GROCER #1234"]) == placeholder

    applied = apply_category_proposals(db, (imported["CORNER GROCER #1234"],)).value
    assert (applied.assigned, applied.unchanged) == (1, 0)
    stored = db.get_transaction(imported["CORNER GROCER #1234"])
    assert _counter(db, book, stored.handle) == book.groceries
    assert stored.description == "CORNER GROCER #1234"
    assert sum(split.value for split in stored.splits) == 0
    assert [item.description for item in preview_category_proposals(db).value] == [
        "Corner Grocer 0987"
    ]


def test_payee_rule_and_priority_order_with_conflicts(db, book, imported):
    power = save_payee(db, SavePayee("City Power")).value
    assign_payee(db, imported["City Power"], power.handle)
    add_rule(db, AddRule(category=book.utilities, payee=power.handle))
    add_rule(db, AddRule(category=book.rent, description="city power"))

    [proposal] = [
        item for item in preview_category_proposals(db).value if item.description == "City Power"
    ]
    assert (proposal.category, proposal.rule_position) == (book.utilities, 1)
    assert [(conflict.rule_position, conflict.category) for conflict in proposal.conflicts] == [
        (2, book.rent)
    ]

    rules = list_rules(db)
    assert move_rule(db, rules[1].handle, 1).ok
    [proposal] = [
        item for item in preview_category_proposals(db).value if item.description == "City Power"
    ]
    assert (proposal.category, proposal.rule_position) == (book.rent, 1)


def test_accepted_categories_are_never_proposed_again(db, book, imported):
    handle = imported["CORNER GROCER #1234"]
    transaction = db.get_transaction(handle)
    for split in transaction.splits:
        if split.account != book.checking:
            split.account = book.utilities
    with db.transaction("User categorized") as txn:
        db.commit_transaction(transaction, txn)
    add_rule(db, AddRule(category=book.groceries, description="corner grocer"))

    assert handle not in {item.transaction for item in preview_category_proposals(db).value}
    stale = apply_category_proposals(db, (handle,)).value
    assert (stale.assigned, stale.unchanged) == (0, 1)
    assert _counter(db, book, handle) == book.utilities


def test_accepting_is_one_undo_step(db, book, imported):
    add_rule(db, AddRule(category=book.groceries, description="corner grocer"))
    placeholder = _counter(db, book, imported["CORNER GROCER #1234"])
    assert apply_category_proposals(db).value.assigned == 2

    assert db.undo() is True

    assert _counter(db, book, imported["CORNER GROCER #1234"]) == placeholder
    assert _counter(db, book, imported["Corner Grocer 0987"]) == placeholder


def test_rule_changes_are_undoable_and_delete_works(db, book):
    rule = add_rule(db, AddRule(category=book.groceries, description="grocer")).value
    assert [item.handle for item in list_rules(db)] == [rule.handle]
    assert db.undo() is True
    assert list_rules(db) == []
    rule = add_rule(db, AddRule(category=book.groceries, description="grocer")).value
    assert delete_rule(db, rule.handle).ok
    assert list_rules(db) == []


def test_rejected_rules_leave_the_book_unchanged(db, book):
    payee = save_payee(db, SavePayee("Landlord")).value
    add_rule(db, AddRule(category=book.rent, description="rent"))
    before = list_rules(db)

    for request, code in [
        (AddRule(category=book.rent), "rule.match.required"),
        (
            AddRule(category=book.rent, description="x", payee=payee.handle),
            "rule.match.required",
        ),
        (AddRule(category=book.rent, description="#123"), "rule.match.empty"),
        (AddRule(category=book.rent, description="RENT"), "rule.match.duplicate"),
        (AddRule(category=book.rent, payee="missing"), "rule.payee.not_found"),
        (AddRule(category=book.checking, description="deposit"), "rule.category.invalid"),
        (AddRule(category=book.expenses, description="deposit"), "rule.category.invalid"),
        (AddRule(category="missing", description="deposit"), "rule.category.invalid"),
    ]:
        assert [error.code for error in add_rule(db, request).errors] == [code]

    assert list_rules(db) == before
    assert [e.code for e in delete_rule(db, "missing").errors] == ["rule.not_found"]
    assert [e.code for e in move_rule(db, before[0].handle, 9).errors] == ["rule.position.invalid"]
    assert [e.code for e in apply_category_proposals(db, ("missing",)).errors] == [
        "rule.transaction.not_found"
    ]


def test_split_transactions_on_placeholders_are_not_guessed(db, book, imported):
    from breadsched.gen.lib import Money, Split

    handle = imported["City Power"]
    transaction = db.get_transaction(handle)
    placeholder = _counter(db, book, handle)
    transaction.splits = [
        split for split in transaction.splits if split.account == book.checking
    ] + [Split(placeholder, Money("30.00")), Split(placeholder, Money("30.00"))]
    with db.transaction("Split it") as txn:
        db.commit_transaction(transaction, txn)
    add_rule(db, AddRule(category=book.utilities, description="city power"))

    assert handle not in {item.transaction for item in preview_category_proposals(db).value}


def test_rules_are_ignored_before_any_exist(db, book, imported):
    assert preview_category_proposals(db).value == ()
    assert apply_category_proposals(db).value.assigned == 0


def test_cli_adds_previews_and_accepts(tmp_path, capsys):
    import json

    from breadsched.cli.main import main
    from breadsched.gen.sample_book import create_sample_book

    book = tmp_path / "cli.breadsched"
    create_sample_book(book, as_of=date(2026, 8, 15))
    statement = tmp_path / "statement.csv"
    statement.write_text(STATEMENT, encoding="utf-8")
    assert (
        main(
            [
                "import-csv",
                str(book),
                str(statement),
                "--account",
                "Checking",
                "--date",
                "Date",
                "--amount",
                "Amount",
                "--description",
                "Description",
            ]
        )
        == 0
    )
    capsys.readouterr()
    category = "Expenses:Groceries"

    command = ["rules", str(book)]
    assert main([*command, "--add-description", "corner grocer", "--category", category]) == 0
    capsys.readouterr()
    assert main([*command, "--json"]) == 0
    [rule] = json.loads(capsys.readouterr().out)
    assert (rule["position"], rule["key"]) == (1, "corner grocer")
    assert main([*command, "--preview", "--json"]) == 0
    proposals = json.loads(capsys.readouterr().out)
    assert len(proposals) == 2 and {item["category_name"] for item in proposals} == {category}
    assert main([*command, "--accept-all", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["assigned"] == 2
    assert main([*command, "--add-description", "#12", "--category", category]) == 2
    assert "at least one word without digits" in capsys.readouterr().err
