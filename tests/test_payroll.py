"""Payroll templates, paycheck breakdowns, and dated pay changes."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from breadsched.gen.engine import planning
from breadsched.gen.engine.payroll import (
    PayLegKind,
    PayrollError,
    PayrollLine,
    PayrollTemplate,
    compute_paycheck,
    paycheck_breakdown,
)
from breadsched.gen.lib import Account, AccountType, Money
from breadsched.gen.lib.recurrence import PeriodType, Recurrence
from breadsched.gen.services.payroll import (
    CreatePaycheck,
    PayChange,
    SavePayrollTemplate,
    apply_pay_change,
    create_paycheck_schedule,
    delete_payroll_template,
    list_payroll_templates,
    paychecks,
    preview_pay_change,
    save_payroll_template,
    template_from_schedule,
)
from breadsched.presentation import service_error_message

START = date(2026, 1, 2)


@pytest.fixture
def payroll(db, book):
    with db.transaction("Payroll accounts") as txn:

        def add(name, atype, parent):
            account = Account(name=name, atype=atype, parent=parent)
            db.add_account(account, txn)
            return account.handle

        taxes = add("Taxes", AccountType.EXPENSE, book.expenses)
        federal = add("Federal", AccountType.EXPENSE, taxes)
        social = add("Social Security", AccountType.EXPENSE, taxes)
        health = add("Health Insurance", AccountType.EXPENSE, book.expenses)
        retirement = add("401k", AccountType.RETIREMENT, book.assets)
        loan = add("401k Loan", AccountType.LOAN, book.liabilities)
    return {
        "federal": federal,
        "social": social,
        "health": health,
        "retirement": retirement,
        "loan": loan,
    }


def _template(book, payroll, name="Acme"):
    return PayrollTemplate(
        name=name,
        income_account=book.salary,
        deposit_account=book.checking,
        gross=Money("3000.00"),
        lines=(
            PayrollLine(payroll["federal"], amount=Money("360.00")),
            PayrollLine(payroll["social"], percent=Decimal("6.2")),
            PayrollLine(payroll["health"], amount=Money("85.50")),
            PayrollLine(payroll["retirement"], percent=Decimal("5")),
            PayrollLine(payroll["loan"], amount=Money("100.00")),
        ),
    )


def _biweekly():
    return Recurrence(PeriodType.WEEK, interval=2, start=START)


def _paycheck(db, book, payroll):
    assert save_payroll_template(db, SavePayrollTemplate(_template(book, payroll))).ok
    saved = create_paycheck_schedule(db, CreatePaycheck("acme", "Acme paycheck", _biweekly()))
    assert saved.ok, saved.errors
    return db.get_scheduled(saved.value.handle)


def test_template_computes_exact_lines_and_net(book, payroll):
    paycheck = compute_paycheck(_template(book, payroll))

    assert [amount for _line, amount in paycheck.lines] == [
        Money("360.00"),
        Money("186.00"),
        Money("85.50"),
        Money("150.00"),
        Money("100.00"),
    ]
    assert paycheck.net == Money("2118.50")
    assert compute_paycheck(_template(book, payroll), Money("3100.00")).net == Money("2207.30")
    with pytest.raises(PayrollError) as raised:
        compute_paycheck(_template(book, payroll), Money("500.00"))
    assert raised.value.code == "payroll.net.non_positive"


def test_paycheck_schedule_balances_and_reads_back_as_a_breakdown(db, book, payroll):
    schedule = _paycheck(db, book, payroll)

    assert sum((split.amount for split in schedule.splits), Money(0)) == Money(0)
    breakdown = paycheck_breakdown(db, schedule, START)
    assert breakdown is not None
    assert breakdown.gross == Money("3000.00")
    assert breakdown.net == Money("2118.50")
    assert breakdown.total(PayLegKind.TAX) == Money("546.00")
    assert breakdown.total(PayLegKind.DEDUCTION) == Money("85.50")
    assert breakdown.total(PayLegKind.SAVED) == Money("150.00")
    assert breakdown.total(PayLegKind.REPAYMENT) == Money("100.00")
    assert breakdown.take_home_percent == Decimal("70.6")
    retirement = next(s for s in schedule.splits if s.account == payroll["retirement"])
    assert retirement.investment_activity is not None
    loan = next(s for s in schedule.splits if s.account == payroll["loan"])
    assert loan.amount == Money("100.00")  # a debit pays the loan down
    assert [item.name for item in paychecks(db, START)] == ["Acme paycheck"]
    ended = db.get_scheduled(schedule.handle)
    ended.recurrence.end = date(2026, 2, 27)
    with db.transaction("End") as txn:
        db.commit_scheduled(ended, txn)
    assert [item.name for item in paychecks(db, date(2026, 2, 27))] == ["Acme paycheck"]
    assert paychecks(db, date(2026, 3, 1)) == []


def test_a_schedule_that_is_not_a_paycheck_has_no_breakdown(db, book):
    from breadsched.gen.lib.scheduled import ScheduledSplit, ScheduledTransaction

    rent = ScheduledTransaction(
        name="Rent",
        recurrence=Recurrence(start=START),
        splits=[
            ScheduledSplit(book.rent, Money("1500")),
            ScheduledSplit(book.checking, Money("-1500")),
        ],
    )
    assert paycheck_breakdown(db, rent, START) is None


def test_templates_are_validated_and_named_uniquely(db, book, payroll):
    template = _template(book, payroll)
    assert save_payroll_template(db, SavePayrollTemplate(template)).ok

    duplicate = save_payroll_template(db, SavePayrollTemplate(_template(book, payroll, "ACME")))
    assert [error.code for error in duplicate.errors] == ["payroll.name.duplicate"]
    bad = PayrollTemplate(
        "Bad",
        book.checking,
        book.salary,
        Money("100"),
        (PayrollLine(payroll["federal"], amount=Money("150")),),
    )
    codes = {error.code for error in save_payroll_template(db, SavePayrollTemplate(bad)).errors}
    assert codes == {"payroll.income.invalid", "payroll.deposit.invalid"}
    overdrawn = PayrollTemplate(
        "Over",
        book.salary,
        book.checking,
        Money("100"),
        (PayrollLine(payroll["federal"], amount=Money("150")),),
    )
    result = save_payroll_template(db, SavePayrollTemplate(overdrawn))
    assert [error.code for error in result.errors] == ["payroll.net.non_positive"]
    assert service_error_message(result.errors[0]) != result.errors[0].code

    renamed = save_payroll_template(
        db, SavePayrollTemplate(_template(book, payroll, "Acme Corp"), existing_name="acme")
    )
    assert renamed.ok
    assert [item.name for item in list_payroll_templates(db)] == ["Acme Corp"]
    assert delete_payroll_template(db, "acme corp").ok
    assert list_payroll_templates(db) == []
    db.undo()
    assert [item.name for item in list_payroll_templates(db)] == ["Acme Corp"]


def test_template_from_schedule_keeps_tax_percentages_that_reproduce_the_cent(db, book, payroll):
    schedule = _paycheck(db, book, payroll)

    template = template_from_schedule(db, schedule.handle, "Copy").value

    assert template is not None
    assert template.gross == Money("3000.00")
    lines = {line.account: line for line in template.lines}
    assert lines[payroll["social"]].percent == Decimal("6.2")
    assert lines[payroll["federal"]].percent == Decimal("12")
    assert lines[payroll["retirement"]].amount == Money("150.00")
    assert compute_paycheck(template).net == Money("2118.50")


def test_pay_change_scales_taxes_keeps_the_rest_and_balances_the_deposit(db, book, payroll):
    schedule = _paycheck(db, book, payroll)
    raise_on = date(2026, 4, 10)
    change = PayChange(
        schedule.handle,
        raise_on,
        Money("3300.00"),
        scaled=frozenset({payroll["federal"], payroll["social"], payroll["retirement"]}),
        amounts={payroll["health"]: Money("90.00")},
    )

    plan = preview_pay_change(db, change).value
    assert plan is not None
    after = {line.account: (line.after, line.how) for line in plan.lines}
    assert after[book.salary] == (Money("3300.00"), "set")
    assert after[payroll["federal"]] == (Money("396.00"), "scaled")
    assert after[payroll["social"]] == (Money("204.60"), "scaled")
    assert after[payroll["health"]] == (Money("90.00"), "set")
    assert after[payroll["retirement"]] == (Money("165.00"), "scaled")
    assert after[payroll["loan"]] == (Money("100.00"), "kept")
    assert after[book.checking] == (Money("2344.40"), "balance")
    assert db.get_scheduled(schedule.handle).splits[0].amount_changes == []

    assert apply_pay_change(db, change).ok
    saved = db.get_scheduled(schedule.handle)
    before = paycheck_breakdown(db, saved, date(2026, 3, 27))
    later = paycheck_breakdown(db, saved, raise_on)
    assert (before.gross, before.net) == (Money("3000.00"), Money("2118.50"))
    assert (later.gross, later.net) == (Money("3300.00"), Money("2344.40"))
    loan = next(split for split in saved.splits if split.account == payroll["loan"])
    assert loan.amount_changes == []

    deposits = {
        event.planned_date: next(
            split.amount for split in event.expected_splits if split.account == book.checking
        )
        for event in planning.scheduled_events(db, date(2026, 3, 20), date(2026, 4, 30))
        if event.source_handle == schedule.handle
    }
    assert deposits == {
        date(2026, 3, 27): Money("2118.50"),
        date(2026, 4, 10): Money("2344.40"),
        date(2026, 4, 24): Money("2344.40"),
    }


def test_pay_change_refusals_leave_the_schedule_unchanged(db, book, payroll):
    schedule = _paycheck(db, book, payroll)
    stored = db.get_scheduled(schedule.handle).serialize()

    early = apply_pay_change(db, PayChange(schedule.handle, date(2025, 12, 1), Money("3300")))
    assert [error.code for error in early.errors] == ["payroll.change.before_start"]
    too_low = apply_pay_change(db, PayChange(schedule.handle, date(2026, 4, 10), Money("400")))
    assert [error.code for error in too_low.errors] == ["payroll.net.non_positive"]
    assert apply_pay_change(db, PayChange(schedule.handle, date(2026, 6, 5), Money("3300"))).ok
    stored = db.get_scheduled(schedule.handle).serialize()
    earlier = apply_pay_change(db, PayChange(schedule.handle, date(2026, 4, 10), Money("3100")))
    assert [error.code for error in earlier.errors] == ["payroll.change.later_exists"]
    assert db.get_scheduled(schedule.handle).serialize() == stored
    for error in (*early.errors, *too_low.errors, *earlier.errors):
        assert service_error_message(error) != error.code


def test_cli_saves_a_template_creates_a_paycheck_and_changes_pay(tmp_path, capsys):
    import json

    from breadsched.cli.main import main
    from breadsched.gen.db.sqlite import DbSQLite

    path = str(tmp_path / "book.breadsched")
    assert main(["sample", path]) == 0
    db = DbSQLite()
    db.load(path)
    try:
        wages = db.get_account_by_name("Income:Wages")
        checking = db.get_account_by_name("Assets:Checking")
        expenses = db.get_account_by_name("Expenses")
        tax = Account(name="Federal Tax", atype=AccountType.EXPENSE, parent=expenses.handle)
        with db.transaction("Tax account") as txn:
            db.add_account(tax, txn)
        template = PayrollTemplate(
            "Job",
            wages.handle,
            checking.handle,
            Money("4000.00"),
            (PayrollLine(tax.handle, percent=Decimal("15")),),
        )
        assert save_payroll_template(db, SavePayrollTemplate(template)).ok
    finally:
        db.close()
    capsys.readouterr()

    assert (
        main(["payroll", path, "--create", "Job pay", "--template", "job", "--start", "2026-10-02"])
        == 0
    )
    created = capsys.readouterr().out
    assert "take-home 3,400.00 (85.0%)" in created
    assert (
        main(
            ["payroll", path, "--pay-change", "Job pay", "--gross", "4400", "--start", "2027-01-08"]
        )
        == 0
    )
    changed = capsys.readouterr().out
    assert "Saved pay change for 'Job pay' from 2027-01-08" in changed
    assert main(["payroll", path, "--show", "Job pay", "--as-of", "2027-01-08", "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert (shown["gross"], shown["net"]) == ("4400.00", "3740.00")
    assert shown["totals"]["tax"] == "660.00"
    assert main(["payroll", path, "--as-of", "2026-10-02"]) == 0
    listing = capsys.readouterr().out
    assert (
        "Job pay" in listing
        and "Job: gross 4,000.00; Expenses:Federal Tax: 15% of gross" in listing
    )
    assert (
        main(["payroll", path, "--pay-change", "Job pay", "--gross", "10", "--start", "2026-01-01"])
        == 2
    )
    assert "cannot start before the schedule's first date" in capsys.readouterr().err
