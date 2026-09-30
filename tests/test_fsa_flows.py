"""FSA money moves in distinct flows, each counted once.

Payroll funds the custodial account; a direct payment from the FSA card pays a
provider; a reimbursement sends money to a bank account after the household paid
(perhaps by credit card, later paid from the bank); a provider refund puts money
back on the FSA card; a repayment returns an over-reimbursement. Benefit usage and
medical expense must each be counted exactly once, whichever path the money took.
"""

from __future__ import annotations

from datetime import date

import pytest

from breadsched.gen.engine import activity, fsa, fsa_flows
from breadsched.gen.engine.fsa_flows import FsaFlowKind
from breadsched.gen.lib import (
    Account,
    AccountType,
    FsaFundingYear,
    Money,
    PlanningFlowKind,
    Transaction,
)

YEAR = date(2026, 1, 1)


@pytest.fixture
def flows(db, book):
    fsa_account = Account(name="Health FSA", atype=AccountType.FSA, parent=book.assets)
    fsa_account.fsa_years = [
        FsaFundingYear(YEAR, date(2026, 12, 31), Money("1200.00"), date(2027, 3, 31))
    ]
    medical = Account(name="Medical", atype=AccountType.EXPENSE, parent=book.expenses)
    with db.transaction("FSA") as txn:
        db.add_account(fsa_account, txn)
        db.add_account(medical, txn)
    fsa_handle, medical_handle = fsa_account.handle, medical.handle

    def post(when, description, to, source, amount):
        transaction = Transaction.simple(when, description, to, source, amount)
        with db.transaction(description) as txn:
            db.add_transaction(transaction, txn)
        return transaction

    posted = {
        "payroll": post(date(2026, 3, 1), "Payroll FSA", fsa_handle, book.salary, "100.00"),
        "direct": post(date(2026, 3, 5), "Clinic (FSA card)", medical_handle, fsa_handle, "80"),
        "charge": post(date(2026, 3, 8), "Dentist", medical_handle, book.card, "200.00"),
        "card_paid": post(date(2026, 3, 20), "Card payment", book.card, book.checking, "200"),
        "reimbursed": post(date(2026, 3, 25), "FSA claim paid", book.checking, fsa_handle, "200"),
        "refund": post(date(2026, 3, 28), "Clinic refund", fsa_handle, medical_handle, "30.00"),
    }
    db_account = db.get_account(fsa_handle)
    return db_account, medical_handle, posted


def _split(transaction, account):
    return next(split for split in transaction.splits if split.account == account)


class TestClassification:
    def test_each_fsa_split_has_one_flow(self, db, book, flows):
        account, _medical, posted = flows
        accounts = {item.handle: item for item in db.iter_accounts()}

        def kind(name):
            transaction = posted[name]
            return fsa_flows.classify(transaction, _split(transaction, account.handle), accounts)

        assert kind("payroll") is FsaFlowKind.FUNDING
        assert kind("direct") is FsaFlowKind.DIRECT_PAYMENT
        assert kind("reimbursed") is FsaFlowKind.REIMBURSEMENT
        assert kind("refund") is FsaFlowKind.PROVIDER_REFUND

    def test_a_tagged_positive_split_is_a_repayment(self, db, book, flows):
        account, _medical, _posted = flows
        back = Transaction.simple(date(2026, 4, 2), "Repay FSA", account.handle, book.checking, "5")
        back.splits[0].fsa_year_start = YEAR
        accounts = {item.handle: item for item in db.iter_accounts()}
        assert fsa_flows.classify(back, back.splits[0], accounts) is FsaFlowKind.REPAYMENT

    def test_a_move_between_fsa_accounts_is_a_transfer(self, db, book, flows):
        account, _medical, _posted = flows
        other = Account(name="Dependent care FSA", atype=AccountType.FSA, parent=book.assets)
        with db.transaction("other FSA") as txn:
            db.add_account(other, txn)
        move = Transaction.simple(date(2026, 4, 3), "Move", other.handle, account.handle, "10")
        accounts = {item.handle: item for item in db.iter_accounts()}
        assert fsa_flows.classify(move, move.splits[1], accounts) is FsaFlowKind.TRANSFER


class TestBenefitUsage:
    def test_usage_counts_each_path_once_and_a_refund_restores_election(self, db, flows):
        account, _medical, _posted = flows

        status = fsa.year_status(db, account, account.fsa_years[0], as_of=date(2026, 4, 1))

        # Payroll is the only funding: the provider refund is not a contribution.
        assert status.funded == Money("100.00")
        assert status.direct_payments == Money("80.00")
        assert status.reimbursements == Money("200.00")
        assert status.provider_refunds == Money("30.00")
        # 80 paid directly + 200 reimbursed - 30 refunded to the card.
        assert status.used == Money("250.00")
        assert status.remaining == Money("950.00")


class TestPlanCountsExpenseOnce:
    def test_medical_expense_and_fsa_flows_in_plan(self, db, book, flows):
        account, medical, _posted = flows

        report = activity.build_category_report(
            db, date(2026, 3, 1), date(2026, 3, 31), as_of=date(2026, 3, 31)
        )

        row = next(item for item in report.expenses if item.account == medical)
        # The card charge, the direct FSA payment, less the refund: never the
        # reimbursement or the card payment as well.
        assert row.actual == [Money("250.00")]
        benefit = {(item.kind, item.account): item.actual[0] for item in report.planning_flows}
        # The row is the FSA's net movement: 100 in from payroll and 30 refunded,
        # less 80 paid directly and 200 reimbursed to checking.
        assert benefit[(PlanningFlowKind.BENEFIT_FUNDING, account.handle)] == Money("-150.00")
        bridge = {item.kind: item.actual[0] for item in report.cash_bridge}
        # FSA money covered 150 of the month's medical cost, so every cash
        # dollar is explained and nothing is left as a residual.
        assert bridge[activity.CashBridgeKind.BENEFIT_FUNDING] == Money("150.00")
        assert bridge.get(activity.CashBridgeKind.OTHER, Money(0)) == Money(0)


class TestClaimsLinkBothSides:
    def _claim(self, db):
        from breadsched.gen.lib import FsaClaim

        claim = FsaClaim(service_date=date(2026, 3, 5), provider="Clinic")
        with db.transaction("claim") as txn:
            db.add_fsa_claim(claim, txn)
        return claim

    def test_a_direct_payment_is_payment_and_reimbursement_at_once(self, db, flows):
        from breadsched.gen.engine import fsa_claims

        _account, _medical, posted = flows
        claim = self._claim(db)
        roles = [item.role for item in fsa_claims.attachment_roles(db, posted["direct"])]
        assert roles[0] == "direct_payment"
        suggestion = fsa_claims.suggest_claims_for_transaction(db, posted["direct"])[0]
        assert suggestion.role == "direct_payment"
        assert "paid from the FSA card" in suggestion.reason

        saved = fsa_claims.attach_transaction_to_claim(
            db, claim.handle, posted["direct"].handle, role="direct_payment"
        )
        summary = fsa_claims.claim_summary(db, saved)
        assert (summary.paid, summary.reimbursed) == (Money("80.00"), Money("80.00"))
        assert summary.over_reimbursed == Money(0)

    def test_a_refund_to_the_card_is_refund_and_repayment_at_once(self, db, flows):
        from breadsched.gen.engine import fsa_claims

        account, _medical, posted = flows
        claim = self._claim(db)
        fsa_claims.attach_transaction_to_claim(
            db, claim.handle, posted["direct"].handle, role="direct_payment"
        )
        saved = fsa_claims.attach_transaction_to_claim(
            db, claim.handle, posted["refund"].handle, role="direct_refund"
        )
        summary = fsa_claims.claim_summary(db, saved)
        # Paid 80, 30 came back to the FSA card: nothing is over-reimbursed.
        assert summary.net_paid == Money("50.00")
        assert summary.reimbursed == Money("50.00")
        assert summary.over_reimbursed == Money(0)
        # The refund split is now tagged with the claim's year; usage is unchanged.
        status = fsa.year_status(db, account, account.fsa_years[0], as_of=date(2026, 4, 1))
        assert status.used == Money("250.00")

    def test_a_card_chain_links_the_charge_and_the_bank_reimbursement(self, db, flows):
        from breadsched.gen.engine import fsa_claims

        _account, _medical, posted = flows
        claim = self._claim(db)
        fsa_claims.attach_transaction_to_claim(
            db, claim.handle, posted["charge"].handle, role="payment"
        )
        saved = fsa_claims.attach_transaction_to_claim(
            db, claim.handle, posted["reimbursed"].handle, role="reimbursement"
        )
        summary = fsa_claims.claim_summary(db, saved)
        assert (summary.paid, summary.reimbursed) == (Money("200.00"), Money("200.00"))
        # The card payment is a transfer, never a second payment.
        assert fsa_claims.attachment_roles(db, posted["card_paid"]) == []


def test_the_command_line_lists_benefit_years_with_how_each_was_used(db, flows, tmp_path, capsys):
    import json

    from breadsched.cli.main import main

    path = tmp_path / "fsa.breadsched"
    db.backup_to(str(path))
    assert main(["claims", str(path), "--years", "--as-of", "2026-04-01"]) == 0
    out = capsys.readouterr().out
    assert "How used" in out and "Health FSA" in out
    assert "80.00 paid from the card; 200.00 reimbursed; 30.00 refunded to the card" in out
    assert main(["claims", str(path), "--years", "--as-of", "2026-04-01", "--json"]) == 0
    [year] = json.loads(capsys.readouterr().out)["years"]
    assert (year["funded"], year["used"], year["remaining"]) == ("100.00", "250.00", "950.00")
    assert (year["direct_payments"], year["reimbursements"], year["provider_refunds"]) == (
        "80.00",
        "200.00",
        "30.00",
    )
