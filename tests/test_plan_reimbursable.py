"""Plan shows a reimbursable expense's gross cost beside its net household cost.

An expense category's actual activity is already the net household cost: the
receivable's reclassification moves what is owed out of the category, and a linked
reimbursement credit is cancelled by its posting. Adding those splits back gives the
gross cost, so nothing is counted twice. A write-off stays in the net cost.
"""

from __future__ import annotations

from datetime import date

from breadsched.gen.engine import activity, category_report, plan_detail
from breadsched.gen.lib import Money, Transaction
from breadsched.gen.services.receivables import (
    RecordWriteOff,
    SaveReceivable,
    attach_expense_split,
    attach_reimbursement_split,
    record_write_off,
    save_receivable,
)

MARCH = date(2026, 3, 1)
APRIL_END = date(2026, 4, 30)


def _post(db, transaction, account):
    with db.transaction(transaction.description) as txn:
        db.add_transaction(transaction, txn)
    return transaction.handle, next(s for s in transaction.splits if s.account == account).handle


def _clinic(db, book, *, reimbursed=None, written_off=None):
    receivable = save_receivable(
        db,
        SaveReceivable(
            incurred_date=date(2026, 3, 5),
            payer="Acme Insurance",
            description="Clinic visit",
            expected_amount=Money("150.00"),
        ),
    ).value
    spent = Transaction.simple(date(2026, 3, 5), "Clinic", book.groceries, book.checking, "200.00")
    assert attach_expense_split(db, receivable.handle, *_post(db, spent, book.groceries)).ok
    if reimbursed is not None:
        back = Transaction.simple(
            date(2026, 4, 10), "Acme payment", book.checking, book.groceries, reimbursed
        )
        assert attach_reimbursement_split(
            db, receivable.handle, *_post(db, back, book.groceries)
        ).ok
    if written_off is not None:
        assert record_write_off(
            db,
            RecordWriteOff(receivable.handle, Money(written_off), date(2026, 4, 20), "Denied"),
        ).ok
    return receivable


def _groceries(db, book):
    report = category_report.build_category_report(db, MARCH, APRIL_END, as_of=APRIL_END)
    return report, next(row for row in report.categories if row.account == book.groceries)


def test_an_open_receivable_separates_gross_from_net_cost(db, book):
    _clinic(db, book)
    report, row = _groceries(db, book)
    assert row.actual == [Money("50.00"), Money(0)]
    assert row.reimbursable == [Money("150.00"), Money(0)]
    assert row.gross == [Money("200.00"), Money(0)]
    assert row.own_reimbursable == Money("150.00")
    assert report.reimbursable_categories == (row,)
    parent = next(
        r for r in report.categories if r.account == db.get_account(book.groceries).parent
    )
    # A parent rolls up its children's figures but lists none of its own.
    assert parent.reimbursable_total == Money("150.00")
    assert parent not in report.reimbursable_categories


def test_a_reimbursement_and_write_off_are_never_counted_twice(db, book):
    _clinic(db, book, reimbursed="100.00", written_off="50.00")
    _, row = _groceries(db, book)
    # Net household cost: 200 spent, 100 back; the 50 written off stays a cost.
    assert sum(row.actual, Money(0)) == Money("100.00")
    assert row.reimbursable_total == Money("100.00")
    assert sum(row.gross, Money(0)) == Money("200.00")
    # April holds no new spending: its gross cost is zero.
    assert row.gross[1] == Money(0)


def test_plan_detail_reports_the_same_gross_and_net_cost(db, book):
    _clinic(db, book, reimbursed="100.00")
    detail = plan_detail.explain_category_period(
        db, book.groceries, MARCH, date(2026, 3, 31), as_of=APRIL_END
    )
    assert (detail.actual, detail.reimbursable, detail.gross) == (
        Money("50.00"),
        Money("150.00"),
        Money("200.00"),
    )
    april = plan_detail.explain_category_period(
        db, book.groceries, date(2026, 4, 1), APRIL_END, as_of=APRIL_END
    )
    assert (april.actual, april.reimbursable, april.gross) == (Money(0), Money(0), Money(0))


def test_without_receivables_gross_is_the_actual_cost(db, book):
    spent = Transaction.simple(date(2026, 3, 5), "Store", book.groceries, book.checking, "80.00")
    _post(db, spent, book.groceries)
    report, row = _groceries(db, book)
    assert row.gross == row.actual and row.reimbursable_total == Money(0)
    assert report.reimbursable_categories == ()


def test_an_earlier_reimbursement_written_off_raises_net_cost_without_spending(db, book):
    from breadsched.presentation import plan_reimbursable_text

    _clinic(db, book, written_off="50.00")
    report = category_report.build_category_report(db, date(2026, 4, 1), APRIL_END, as_of=APRIL_END)
    [row] = report.reimbursable_categories
    assert (row.reimbursable_total, sum(row.gross, Money(0))) == (Money("-50.00"), Money(0))
    assert plan_reimbursable_text(row) == (
        f"{row.full_name}: net household cost 50.00 includes 50.00 written off from an "
        "earlier reimbursable expense; gross cost 0.00."
    )


def test_the_printed_plan_carries_the_same_sentences(db, book):
    from breadsched.plugins.export.html_report import render_html
    from breadsched.plugins.export.report_layout import plan_layout
    from breadsched.presentation import PLAN_REIMBURSABLE_HEADING, plan_reimbursable_text

    _clinic(db, book)
    report, row = _groceries(db, book)
    page = render_html(plan_layout(report, activity.PlanMeasure.ACTUAL, scenario_name="Base"))
    assert PLAN_REIMBURSABLE_HEADING in page
    assert plan_reimbursable_text(row) in page


def test_the_command_line_lists_gross_and_net_cost(tmp_path, capsys):
    import json

    from breadsched.cli.main import main
    from breadsched.gen.db.sqlite import DbSQLite

    path = tmp_path / "book.breadsched"
    assert main(["init", str(path)]) == 0
    for name, kind, parent in (("Checking", "BANK", "Assets"), ("Medical", "EXPENSE", "Expenses")):
        command = ["account", str(path), "add", "--name", name, "--type", kind, "--parent", parent]
        assert main(command) == 0
    db = DbSQLite()
    db.load(str(path))
    try:
        medical = db.get_account_by_name("Medical").handle
        checking = db.get_account_by_name("Checking").handle
        receivable = save_receivable(
            db,
            SaveReceivable(
                incurred_date=date(2026, 3, 5),
                payer="Acme Insurance",
                expected_amount=Money("150.00"),
            ),
        ).value
        spent = Transaction.simple(date(2026, 3, 5), "Clinic", medical, checking, "200.00")
        assert attach_expense_split(db, receivable.handle, *_post(db, spent, medical)).ok
    finally:
        db.close()
    capsys.readouterr()

    assert main(["receivables", str(path), "--costs", "2026-03-01", "2026-03-31", "--json"]) == 0
    [row] = json.loads(capsys.readouterr().out)
    assert (row["category"], row["gross"], row["reimbursable"], row["net"]) == (
        "Expenses:Medical",
        "200.00",
        "150.00",
        "50.00",
    )
    assert main(["receivables", str(path), "--costs", "2026-03-01", "2026-03-31"]) == 0
    assert "Expenses:Medical: gross cost 200.00, 150.00 reimbursed" in capsys.readouterr().out
    assert main(["receivables", str(path), "--costs", "2026-04-01", "2026-04-30"]) == 0
    assert "No expense category had reimbursements" in capsys.readouterr().out
