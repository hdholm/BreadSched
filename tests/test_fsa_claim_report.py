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
    FsaFundingYear,
    Money,
    Split,
    SplitLink,
    Transaction,
)

YEAR = date(2026, 7, 1)


def _fsa(db, book, name="Health FSA", runout=date(2027, 9, 30)):
    account = Account(name=name, atype=AccountType.FSA, parent=book.assets)
    account.fsa.years = [FsaFundingYear(YEAR, date(2027, 6, 30), Money("3000.00"), runout)]
    with db.transaction("Add FSA") as txn:
        db.add_account(account, txn)
    return account


def _paid(db, book, when, amount, text="Service"):
    payment = Transaction.simple(when, text, book.groceries, book.checking, amount)
    with db.transaction("Payment") as txn:
        db.add_transaction(payment, txn)
    return SplitLink(payment.handle, payment.splits[0].handle)


def _reimbursed(db, book, account, when, amount):
    credit = Transaction.simple(when, "FSA reimbursement", book.checking, account.handle, amount)
    with db.transaction("Reimbursement") as txn:
        db.add_transaction(credit, txn)
    return SplitLink(credit.handle, credit.splits[1].handle)


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


def test_the_dashboard_lists_every_claim_of_an_open_benefit_year(db, claims):
    """#366: settled claims stay listed while their year can still be claimed."""
    _health, _spouse, made = claims
    open_now = claim_report(db, as_of=AS_OF)
    assert all(line.open_year for line in open_now.lines)
    # The fully reimbursed claim is listed with the rest, to tell claims apart.
    assert {line.handle for line in open_now.dashboard_lines} == {
        claim.handle for claim in made.values()
    }
    # Once every run-out has passed, only claims still open or needing attention stay.
    later = claim_report(db, as_of=date(2028, 1, 15))
    assert not any(line.open_year for line in later.lines)
    listed = {line.handle for line in later.dashboard_lines}
    assert made["done"].handle not in listed
    assert made["waiting"].handle in listed


def _split_claim(db, account, provider, service, payment, target, *, eob=None):
    claim = FsaClaim(
        service_date=service,
        provider=provider,
        eob_responsibility=Money(eob) if eob is not None else None,
        payments=[payment],
        allocations=[FsaClaimAllocation(account.handle, YEAR, target=Money(target))],
    )
    fsa_claims.save_claim(db, claim)
    return claim


def test_a_payment_shared_by_two_claims_counts_each_claims_part_once(db, book):
    """Field report: one healthcare payment on two claims, each for part of it,
    was shown at its full amount on each and counted twice in the totals, though
    each claim's allocation said its part."""
    health = _fsa(db, book)
    payment = _paid(db, book, date(2026, 8, 3), "300.00", "Clinic, two visits")
    first = _split_claim(db, health, "Clinic", date(2026, 8, 1), payment, "120.00")
    second = _split_claim(db, health, "Clinic", date(2026, 8, 2), payment, "180.00")

    one = fsa_claims.claim_summary(db, db.get_fsa_claim(first.handle))
    two = fsa_claims.claim_summary(db, db.get_fsa_claim(second.handle))
    assert (one.paid, two.paid) == (Money("120.00"), Money("180.00"))
    assert fsa_claims.link_share(db, first, "payment", payment) == (Money("120.00"), 2)

    report = claim_report(db, as_of=date(2026, 9, 1))
    assert report.totals.net_paid == Money("300.00")  # the payment, once
    by_handle = {line.handle: line for line in report.lines}
    assert by_handle[first.handle].summary.net_paid == Money("120.00")


def test_a_shared_payment_without_targets_follows_the_eobs_or_is_halved(db, book):
    health = _fsa(db, book)
    payment = _paid(db, book, date(2026, 8, 3), "100.00")
    first = _claim(db, health, "Lab", date(2026, 8, 1), [payment], eob="25.00")
    second = _claim(db, health, "Lab", date(2026, 8, 2), [payment], eob="75.00")
    assert fsa_claims.claim_summary(db, first).paid == Money("25.00")
    assert fsa_claims.claim_summary(db, second).paid == Money("75.00")

    other = _paid(db, book, date(2026, 8, 4), "90.00")
    a = _claim(db, health, "X", date(2026, 8, 3), [other])
    b = _claim(db, health, "Y", date(2026, 8, 3), [other])
    assert fsa_claims.claim_summary(db, a).paid == Money("45.00")
    assert fsa_claims.claim_summary(db, b).paid == Money("45.00")


def test_a_payment_on_one_claim_still_counts_in_full(db, book):
    health = _fsa(db, book)
    payment = _paid(db, book, date(2026, 8, 3), "60.00")
    claim = _claim(db, health, "Clinic", date(2026, 8, 1), [payment])
    assert fsa_claims.claim_summary(db, claim).paid == Money("60.00")


def test_an_eob_above_what_was_paid_keeps_the_claim_open_for_the_next_bill(db, book):
    """Field report: payments and reimbursements matched, but the EOB said more
    was owed; the claim showed fully reimbursed, though another bill is coming."""
    health = _fsa(db, book)
    payment = _paid(db, book, date(2026, 8, 3), "200.00")
    back = _reimbursed(db, book, health, date(2026, 8, 20), "200.00")
    claim = _claim(
        db, health, "Surgeon", date(2026, 8, 1), [payment], eob="300.00", reimbursed=[back]
    )
    summary = fsa_claims.claim_summary(db, claim)
    assert summary.status is fsa_claims.FsaClaimStatus.PARTIAL
    assert summary.unbilled == Money("100.00")
    assert not summary.status.settled
    report = claim_report(db, as_of=date(2026, 9, 1))
    assert _codes(report, claim) == ["bill"]
    [item] = next(line for line in report.lines if line.handle == claim.handle).attention
    assert item.text.startswith("The EOB says 100.00 more is owed than was paid")
    # The next bill arrives and is reimbursed: now it is settled.
    second = _paid(db, book, date(2026, 9, 3), "100.00")
    again = _reimbursed(db, book, health, date(2026, 9, 20), "100.00")
    claim.payments.append(second)
    claim.allocations[0].reimbursements.append(again)
    fsa_claims.save_claim(db, claim)
    settled = fsa_claims.claim_summary(db, db.get_fsa_claim(claim.handle))
    assert settled.status is fsa_claims.FsaClaimStatus.FULLY_REIMBURSED
    assert settled.unbilled == Money(0)


def test_an_eob_equal_to_what_was_paid_is_fully_reimbursed(db, book):
    health = _fsa(db, book)
    payment = _paid(db, book, date(2026, 8, 3), "200.00")
    back = _reimbursed(db, book, health, date(2026, 8, 20), "200.00")
    claim = _claim(
        db, health, "Clinic", date(2026, 8, 1), [payment], eob="200.00", reimbursed=[back]
    )
    summary = fsa_claims.claim_summary(db, claim)
    assert summary.status is fsa_claims.FsaClaimStatus.FULLY_REIMBURSED
    assert summary.unbilled == Money(0)


def test_one_fsa_card_payment_for_several_visits_reimburses_each_its_part(db, book):
    """Paid from the FSA card, one FSA leg pays three visits, each its own expense
    leg and claim: saving each claim links that FSA leg as its reimbursement, and
    each counts only its part of it (the part it paid), not the whole."""
    health = _fsa(db, book)
    medical = book.groceries
    paid = Transaction(
        post_date=date(2026, 8, 3),
        description="Clinic",
        splits=[
            Split(account=medical, value=Money("50")),
            Split(account=medical, value=Money("60")),
            Split(account=medical, value=Money("70")),
            Split(account=health.handle, value=Money("-180")),
        ],
    )
    with db.transaction("Clinic") as txn:
        db.add_transaction(paid, txn)
    made = []
    for index in range(3):
        claim = FsaClaim(
            service_date=date(2026, 8, 1),
            provider=f"Visit {index + 1}",
            payments=[SplitLink(paid.handle, paid.splits[index].handle)],
            allocations=[FsaClaimAllocation(health.handle, YEAR)],
        )
        made.append(fsa_claims.save_claim(db, claim))
    summaries = [fsa_claims.claim_summary(db, db.get_fsa_claim(c.handle)) for c in made]
    assert [s.paid for s in summaries] == [Money("50"), Money("60"), Money("70")]
    assert [s.reimbursed for s in summaries] == [Money("50"), Money("60"), Money("70")]
    report = claim_report(db, as_of=date(2026, 9, 1))
    assert report.totals.reimbursed == Money("180")
