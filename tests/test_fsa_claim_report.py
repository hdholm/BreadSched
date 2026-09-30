"""FSA claim reports by account, funding year, provider, and status, and alerts."""

from datetime import date

import pytest

from breadsched.gen.engine import dashboard, fsa_claims
from breadsched.gen.engine.fsa_claim_report import claim_report
from breadsched.gen.lib import (
    Account,
    AccountType,
    FsaClaim,
    FsaClaimAllocation,
    FsaClaimRejection,
    FsaClaimSplitLink,
    FsaFundingYear,
    Money,
    Transaction,
)

YEAR = date(2026, 7, 1)


def _fsa(db, book, name="Health FSA", runout=date(2027, 9, 30)):
    account = Account(name=name, atype=AccountType.FSA, parent=book.assets)
    account.fsa_years = [FsaFundingYear(YEAR, date(2027, 6, 30), Money("3000.00"), runout)]
    with db.transaction("Add FSA") as txn:
        db.add_account(account, txn)
    return account


def _paid(db, book, when, amount, text="Service"):
    payment = Transaction.simple(when, text, book.groceries, book.checking, amount)
    with db.transaction("Payment") as txn:
        db.add_transaction(payment, txn)
    return FsaClaimSplitLink(payment.handle, payment.splits[0].handle)


def _reimbursed(db, book, account, when, amount):
    credit = Transaction.simple(when, "FSA reimbursement", book.checking, account.handle, amount)
    with db.transaction("Reimbursement") as txn:
        db.add_transaction(credit, txn)
    return FsaClaimSplitLink(credit.handle, credit.splits[1].handle)


def _claim(db, account, provider, service, payments, *, eob=None, reimbursed=(), rejected=()):
    claim = FsaClaim(
        service_date=service,
        provider=provider,
        eob_responsibility=Money(eob) if eob is not None else None,
        payments=list(payments),
        allocations=[
            FsaClaimAllocation(
                account.handle, YEAR, reimbursements=list(reimbursed), rejections=list(rejected)
            )
        ],
    )
    fsa_claims.save_claim(db, claim)
    return claim


@pytest.fixture
def claims(db, book):
    """Four claims across two FSA accounts, each needing a different kind of attention."""
    health = _fsa(db, book)
    spouse = _fsa(db, book, "Spouse FSA", runout=date(2027, 12, 31))
    # Fully reimbursed: nothing to do.
    done = _claim(
        db,
        health,
        "Dentist",
        date(2026, 8, 1),
        [_paid(db, book, date(2026, 8, 2), "200.00")],
        eob="200.00",
        reimbursed=[_reimbursed(db, book, health, date(2026, 8, 20), "200.00")],
    )
    # Waiting for an EOB since August.
    waiting = _claim(
        db, health, "Clinic", date(2026, 8, 10), [_paid(db, book, date(2026, 8, 11), "90.00")]
    )
    # Open, part rejected, and its run-out deadline (2027-09-30) is near.
    rejected = _claim(
        db,
        health,
        "clinic ",
        date(2027, 6, 1),
        [_paid(db, book, date(2027, 6, 2), "300.00")],
        eob="300.00",
        rejected=[FsaClaimRejection(date(2027, 7, 1), Money("300.00"), "No itemized bill")],
    )
    # Reimbursed more than the EOB allows.
    over = _claim(
        db,
        spouse,
        "Pharmacy",
        date(2027, 5, 1),
        [_paid(db, book, date(2027, 5, 2), "50.00")],
        eob="40.00",
        reimbursed=[_reimbursed(db, book, spouse, date(2027, 5, 20), "50.00")],
    )
    return health, spouse, {"done": done, "waiting": waiting, "rejected": rejected, "over": over}


AS_OF = date(2027, 9, 10)


def _codes(report, claim):
    line = next(line for line in report.lines if line.handle == claim.handle)
    return [item.code for item in line.attention]


def test_each_kind_of_attention_is_reported(db, claims):
    _health, _spouse, made = claims
    report = claim_report(db, as_of=AS_OF)
    assert _codes(report, made["done"]) == []
    assert _codes(report, made["waiting"]) == ["eob"]
    assert _codes(report, made["rejected"]) == ["deadline", "rejected"]
    assert _codes(report, made["over"]) == ["over"]
    texts = {item.code: item.text for line in report.lines for item in line.attention}
    assert texts["deadline"] == "Claim by 2027-09-30: 300.00 still to reimburse"
    assert texts["rejected"].startswith("300.00 rejected on 2027-07-01 (No itemized bill)")
    assert texts["over"] == (
        "Reimbursed 10.00 more than the claim allows: "
        "repay the FSA and link the repayment, or correct the claim"
    )
    assert texts["eob"].startswith("No EOB entered 396 days")
    assert {line.handle for line in report.needing_attention} == {
        made["waiting"].handle,
        made["rejected"].handle,
        made["over"].handle,
    }


def test_attention_waits_for_its_moment(db, claims):
    _health, _spouse, made = claims
    # Weeks after service, an EOB is not yet overdue, and the deadline is far off.
    early = claim_report(db, as_of=date(2026, 8, 20))
    assert _codes(early, made["waiting"]) == []
    summer = claim_report(db, as_of=date(2027, 7, 15))
    assert _codes(summer, made["rejected"]) == ["rejected"]


def test_claims_group_by_status_account_year_and_provider(db, claims):
    health, spouse, _made = claims
    by_status = claim_report(db, as_of=AS_OF)
    assert [group.label for group in by_status.groups] == [
        "Waiting for EOB",
        "Open",
        "Fully reimbursed",
        "Over-reimbursed",
    ]
    by_account = claim_report(db, as_of=AS_OF, by="account")
    assert [(g.label, g.claims, g.attention) for g in by_account.groups] == [
        ("Assets:Health FSA", 3, 2),
        ("Assets:Spouse FSA", 1, 1),
    ]
    health_group = by_account.groups[0]
    assert health_group.net_paid == Money("590.00")
    assert health_group.reimbursed == Money("200.00")
    assert health_group.rejected == Money("300.00")
    # A claim waiting for its EOB counts all it paid as still to reimburse.
    assert health_group.remaining == Money("390.00")
    by_year = claim_report(db, as_of=AS_OF, by="year")
    assert by_year.groups[0].label == "Assets:Health FSA 2026-07-01 – 2027-06-30"
    # Providers group ignoring case and surrounding space.
    by_provider = claim_report(db, as_of=AS_OF, by="provider")
    assert [(g.label, g.claims) for g in by_provider.groups] == [
        ("Clinic", 2),
        ("Dentist", 1),
        ("Pharmacy", 1),
    ]
    assert by_status.totals.claims == 4
    assert by_status.totals.net_paid == Money("640.00")


def test_filters_narrow_the_report(db, claims):
    health, spouse, made = claims
    assert len(claim_report(db, as_of=AS_OF, account=spouse.handle).lines) == 1
    assert len(claim_report(db, as_of=AS_OF, funding_year=YEAR).lines) == 4
    assert claim_report(db, as_of=AS_OF, funding_year=date(2025, 1, 1)).lines == ()
    assert len(claim_report(db, as_of=AS_OF, provider="CLINIC").lines) == 2
    waiting = claim_report(db, as_of=AS_OF, status=fsa_claims.FsaClaimStatus.WAITING_EOB)
    assert [line.handle for line in waiting.lines] == [made["waiting"].handle]
    attention = claim_report(db, as_of=AS_OF, attention_only=True)
    assert len(attention.lines) == 3
    with pytest.raises(ValueError):
        claim_report(db, by="payer")


def test_the_dashboard_counts_claims_needing_attention(db, claims):
    board = dashboard.build(db, as_of=AS_OF)
    assert board.summary()["fsa_claims_attention"] == 3
    assert len(board.claim_alerts) == 3


def test_a_book_without_claims_has_no_alerts(db, book):
    board = dashboard.build(db, as_of=AS_OF)
    assert board.claim_alerts == ()
    assert claim_report(db, as_of=AS_OF).groups == ()


def test_the_command_line_reports_claims_and_attention(db, claims, tmp_path, capsys):
    import json

    from breadsched.cli.main import main

    path = tmp_path / "claims.breadsched"
    db.backup_to(str(path))
    assert main(["claims", str(path), "--as-of", "2027-09-10", "--by", "provider"]) == 0
    out = capsys.readouterr().out
    assert "Provider" in out and "Clinic" in out and "Pharmacy" in out
    assert "Needs attention:" in out
    assert "Claim by 2027-09-30: 300.00 still to reimburse" in out
    assert main(["claims", str(path), "--as-of", "2027-09-10", "--attention", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["claims"]) == 3
    assert {code["code"] for claim in payload["claims"] for code in claim["attention"]} == {
        "eob",
        "deadline",
        "rejected",
        "over",
    }
    assert main(["claims", str(path), "--status", "bogus"]) != 0
