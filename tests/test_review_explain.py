"""Review says why it suggests a planned item, why it suggests none, and what
each action does."""

from __future__ import annotations

from datetime import date

from breadsched.gen.engine import planning, review_explain
from breadsched.gen.lib import (
    Account,
    AccountType,
    Commodity,
    Money,
    PeriodType,
    Recurrence,
    ScheduledSplit,
    ScheduledTransaction,
    Transaction,
)
from breadsched.presentation import REVIEW_ACTION_HELP


def _bill(db, book, when=date(2026, 5, 7), amount="180.00", currency=None, name="Electric"):
    bill = ScheduledTransaction(
        name=name,
        recurrence=Recurrence(PeriodType.ONCE, start=when),
        splits=[
            ScheduledSplit(book.utilities, Money(amount)),
            ScheduledSplit(book.checking, Money(f"-{amount}")),
        ],
        currency=currency,
    )
    with db.transaction("plan") as txn:
        db.add_scheduled(bill, txn)
    return bill


def test_a_close_candidate_says_what_it_shares(db, book):
    _bill(db, book)
    actual = Transaction.simple(
        date(2026, 5, 8), "ELECTRIC CO", book.utilities, book.checking, "180.00"
    )
    [explained] = review_explain.explain_candidates(db, actual)
    assert explained.confidence == review_explain.CLOSE
    assert explained.label == "Close match"
    assert explained.reasons == (
        "Uses the same account: Checking, Utilities",
        "Same amount",
        "1 day after the planned date",
        "Description shares “electric”",
    )


def test_a_distant_or_different_candidate_is_only_possible(db, book):
    _bill(db, book)
    actual = Transaction.simple(date(2026, 5, 2), "Power", book.utilities, book.checking, "193.42")
    [explained] = review_explain.explain_candidates(db, actual)
    assert explained.confidence == review_explain.POSSIBLE
    assert "13.42 more than expected (7%)" in explained.reasons
    assert "5 days before the planned date" in explained.reasons
    assert not any(reason.startswith("Description") for reason in explained.reasons)
    assert explained.as_dict()["label"] == "Possible match"


def test_no_candidate_names_the_nearest_item_outside_the_window(db, book):
    _bill(db, book, when=date(2026, 5, 30))
    actual = Transaction.simple(
        date(2026, 5, 8), "Electric", book.utilities, book.checking, "180.00"
    )
    assert planning.match_candidates(db, actual) == []
    reason = review_explain.no_candidate_reason(db, actual)
    assert "“Electric” on 2026-05-30, is 22 days away" in reason
    assert "within 7 days" in reason


def test_no_candidate_after_rejecting_them(db, book):
    _bill(db, book)
    actual = Transaction.simple(
        date(2026, 5, 8), "Electric", book.utilities, book.checking, "180.00"
    )
    candidate = planning.match_candidates(db, actual)[0]
    planning.reject_candidate(actual, candidate.event)
    reason = review_explain.no_candidate_reason(db, actual)
    assert reason.startswith("You rejected 1 candidate for this transaction.")


def test_no_candidate_in_another_currency(db, book):
    euro = Commodity(namespace="CURRENCY", mnemonic="EUR", fullname="Euro")
    with db.transaction("euro") as txn:
        db.add_commodity(euro, txn)
    _bill(db, book, currency=euro.handle)
    actual = Transaction.simple(
        date(2026, 5, 8), "Electric", book.utilities, book.checking, "180.00"
    )
    reason = review_explain.no_candidate_reason(db, actual)
    assert "is planned in EUR" in reason


def test_no_schedule_uses_the_accounts(db, book):
    actual = Transaction.simple(date(2026, 5, 8), "Gift", book.groceries, book.checking, "20")
    assert review_explain.no_candidate_reason(db, actual).startswith(
        "No schedule uses this transaction's accounts."
    )


def test_an_fsa_movement_names_its_flow_and_what_to_do(db, book):
    fsa_account = Account(name="Health FSA", atype=AccountType.FSA, parent=book.assets)
    medical = Account(name="Medical", atype=AccountType.EXPENSE, parent=book.expenses)
    with db.transaction("FSA") as txn:
        db.add_account(fsa_account, txn)
        db.add_account(medical, txn)
    direct = Transaction.simple(
        date(2026, 5, 8), "Clinic", medical.handle, fsa_account.handle, "40"
    )
    reimbursed = Transaction.simple(
        date(2026, 5, 9), "FSA claim", book.checking, fsa_account.handle, "40"
    )
    payroll = Transaction.simple(date(2026, 5, 1), "Payroll", fsa_account.handle, book.salary, "50")
    assert "Paid from the FSA card" in review_explain.fsa_hint(db, direct)
    assert "“FSA reimbursement”" in review_explain.fsa_hint(db, reimbursed)
    assert "needs no claim" in review_explain.fsa_hint(db, payroll)
    ordinary = Transaction.simple(date(2026, 5, 8), "Food", book.groceries, book.checking, "9")
    assert review_explain.fsa_hint(db, ordinary) is None


def test_every_action_is_explained():
    assert set(REVIEW_ACTION_HELP) == {"match", "reject", "skip", "unexpected", "fsa"}
    assert all(text.split(":")[0] for text in REVIEW_ACTION_HELP.values())


def test_the_command_line_lists_candidates_with_their_reasons(db, book, tmp_path, capsys):
    import json

    from breadsched.cli.main import main

    _bill(db, book)
    actual = Transaction.simple(
        date(2026, 5, 8), "ELECTRIC CO", book.utilities, book.checking, "180.00"
    )
    with db.transaction("actual") as txn:
        db.add_transaction(actual, txn)
    path = tmp_path / "review.breadsched"
    db.backup_to(str(path))

    assert main(["review", str(path), "--transaction", actual.handle[:10]]) == 0
    out = capsys.readouterr().out
    assert "Close match: 2026-05-07 Electric (expected 180.00)" in out
    assert "    - Same amount" in out
    assert REVIEW_ACTION_HELP["match"] in out

    assert main(["review", str(path), "--transaction", actual.handle, "--json"]) == 0
    [item] = json.loads(capsys.readouterr().out)["transactions"]
    [candidate] = item["candidates"]
    assert candidate["confidence"] == "close"
    assert "1 day after the planned date" in candidate["reasons"]
    assert item["no_candidate_reason"] is None
    assert main(["review", str(path), "--transaction", "zzzz"]) != 0
