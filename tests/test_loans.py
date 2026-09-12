"""Loan mathematics and loan setup.

Checked against figures a spreadsheet or a lender would produce, because that is
the comparison a user will make. A 200,000 loan at 6% over 30 years pays 1,199.10 a
month, and its first payment is 1,000.00 of interest and 199.10 of principal — the
interest being exactly one month of 6% on the full balance is what makes this a
good anchor test.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from breadsched.gen.engine.loans import LoanTerms, build_schedule, create_loan
from breadsched.gen.lib import Assumptions, Money, PlanningFlowKind, ScheduledSplit
from breadsched.gen.lib.finance import amortisation_schedule, fv, ipmt, nper, pmt, ppmt, pv
from breadsched.gen.lib.formula import FormulaError, evaluate, normalise


def q(value) -> Decimal:
    return round(Decimal(value), 2)


class TestPayment:
    def test_a_textbook_mortgage(self):
        assert q(pmt(Decimal("0.06") / 12, 360, 200000)) == Decimal("-1199.10")

    def test_a_zero_rate_loan_is_just_division(self):
        assert q(pmt(0, 10, 1000)) == Decimal("-100.00")

    def test_payment_at_the_start_of_the_period_is_smaller(self):
        ordinary = pmt(Decimal("0.005"), 360, 200000)
        due = pmt(Decimal("0.005"), 360, 200000, 0, 1)
        assert due > ordinary  # less negative: less is owed

    def test_a_loan_with_no_periods_is_refused(self):
        with pytest.raises(ValueError):
            pmt(Decimal("0.005"), 0, 200000)


class TestInterestAndPrincipal:
    def test_the_first_month_is_a_month_of_interest(self):
        assert q(ipmt(Decimal("0.06") / 12, 1, 360, 200000)) == Decimal("-1000.00")

    def test_the_parts_sum_to_the_payment(self):
        rate = Decimal("0.06") / 12
        for period in (1, 12, 180, 360):
            total = ipmt(rate, period, 360, 200000) + ppmt(rate, period, 360, 200000)
            assert q(total) == q(pmt(rate, 360, 200000))

    def test_interest_falls_and_principal_rises(self):
        rate = Decimal("0.06") / 12
        early = ipmt(rate, 1, 360, 200000)
        late = ipmt(rate, 300, 360, 200000)
        assert late > early
        assert ppmt(rate, 300, 360, 200000) < ppmt(rate, 1, 360, 200000)

    def test_the_schedule_repays_the_loan_exactly(self):
        rows = amortisation_schedule(Decimal("0.06") / 12, 360, 200000)
        assert len(rows) == 360
        assert abs(rows[-1]["balance"]) < Decimal("0.01")

    def test_start_of_period_interest_matches_annuity_due_timing(self):
        rate = Decimal("0.005")
        assert q(ipmt(rate, 1, 360, 200000, due=1)) == Decimal("0.00")
        assert q(ipmt(rate, 2, 360, 200000, due=1)) == Decimal("-994.03")
        assert q(ipmt(rate, 120, 360, 200000, due=1)) == Decimal("-834.49")
        assert q(ipmt(rate, 360, 360, 200000, due=1)) == Decimal("-5.94")

    def test_start_of_period_parts_sum_to_payment_and_repay(self):
        rate = Decimal("0.005")
        payment = pmt(rate, 360, 200000, due=1)
        for period in (1, 2, 120, 360):
            total = ipmt(rate, period, 360, 200000, due=1) + ppmt(rate, period, 360, 200000, due=1)
            assert q(total) == q(payment)
        rows = amortisation_schedule(rate, 360, 200000, due=1)
        assert abs(rows[-1]["balance"]) < Decimal("0.01")

    def test_a_period_outside_the_term_is_refused(self):
        with pytest.raises(ValueError):
            ipmt(Decimal("0.005"), 400, 360, 200000)


class TestOtherFunctions:
    def test_future_value_of_regular_saving(self):
        assert q(fv(Decimal("0.005"), 120, -500)) == Decimal("81939.67")

    def test_present_value_matches_payment(self):
        payment = pmt(Decimal("0.005"), 360, 200000)
        assert q(pv(Decimal("0.005"), 360, payment)) == Decimal("200000.00")

    def test_number_of_periods(self):
        assert round(nper(Decimal("0.005"), -1199.10, 200000)) == 360


class TestFormulaIntegration:
    """The reason these exist: GnuCash loans are stored as formulas."""

    VARIABLES = {"principal": 200000, "rate": Decimal("0.005"), "periods": 360}

    def test_pmt_is_callable_from_a_formula(self):
        """In a formula a payment is a positive amount, as GnuCash writes them.

        The finance module keeps the spreadsheet's signed convention; the formula
        language follows the one its own templates use.
        """
        result = evaluate("pmt(rate, periods, principal)", self.VARIABLES)
        assert q(result) == Decimal("1199.10")
        assert q(pmt(Decimal("0.005"), 360, 200000)) == Decimal("-1199.10")

    def test_gnucash_colon_syntax_is_accepted(self):
        """GnuCash separates arguments with colons, which Python cannot parse."""
        assert normalise("pmt(rate : periods : principal)") == ("pmt(rate , periods , principal)")
        result = evaluate("pmt(rate : periods : principal)", self.VARIABLES)
        assert q(result) == Decimal("1199.10")

    def test_a_colon_outside_brackets_is_still_an_error(self):
        with pytest.raises(FormulaError):
            evaluate("rate : periods", self.VARIABLES)

    def test_ipmt_and_ppmt_are_callable(self):
        interest = evaluate("ipmt(rate, 1, periods, principal)", self.VARIABLES)
        principal = evaluate("ppmt(rate, 1, periods, principal)", self.VARIABLES)
        assert q(interest) == Decimal("1000.00")
        assert q(principal) == Decimal("199.10")

    def test_an_unknown_function_is_refused_by_name(self):
        with pytest.raises(FormulaError, match="unknown function"):
            evaluate("system('rm -rf /')", {})

    def test_the_whitelist_still_blocks_attribute_access(self):
        with pytest.raises(FormulaError):
            evaluate("pmt.__class__", self.VARIABLES)

    def test_fractional_powers_are_not_truncated(self):
        result = evaluate("1.06 ** (1 / 12)")
        assert abs(result - Decimal("1.004867550565343")) < Decimal("1e-15")

    def test_python_argument_commas_are_not_stripped_as_grouping(self):
        result = evaluate("pmt(0.005,360,200000,0)")
        assert q(result) == Decimal("1199.10")

    @pytest.mark.parametrize(
        "formula",
        [
            "9**9**9",
            "10 % 0",
            "'abc'",
            "-" * 80 + "1",
        ],
    )
    def test_evaluator_failures_are_reported_as_formula_errors(self, formula):
        with pytest.raises(FormulaError):
            evaluate(formula)


class TestLoanSetup:
    @pytest.fixture
    def terms(self, book):
        return LoanTerms(
            name="Mortgage",
            principal=Money("200000.00"),
            annual_rate=Decimal("0.06"),
            years=30,
            start=date(2026, 1, 1),
            liability=book.card,
            interest_account=book.utilities,
            payment_account=book.checking,
        )

    def test_the_payment_is_the_expected_figure(self, terms):
        assert terms.payment() == Money("1199.10")

    def test_the_schedule_uses_formulas_not_fixed_amounts(self, terms):
        schedule = build_schedule(terms)
        assert any(split.formula for split in schedule.splits)
        assert "ppmt" in " ".join(s.formula for s in schedule.splits)

    def test_principal_formula_is_classified_as_debt_service(self, terms):
        schedule = build_schedule(terms)
        principal = next(split for split in schedule.splits if "ppmt" in split.formula)
        interest = next(split for split in schedule.splits if "ipmt" in split.formula)
        assert principal.planning_flow is PlanningFlowKind.DEBT_PRINCIPAL
        assert interest.planning_flow is None

    def test_legacy_ppmt_split_infers_debt_principal_classification(self):
        split = ScheduledSplit.from_dict(
            {
                "account": "loan",
                "amount": None,
                "formula": "ppmt(rate : i : periods : principal)",
                "memo": "Principal",
            }
        )
        assert split.planning_flow is PlanningFlowKind.DEBT_PRINCIPAL

    def test_every_occurrence_balances(self, terms):
        schedule = build_schedule(terms)
        for when in (date(2026, 1, 1), date(2030, 6, 1), date(2050, 12, 1)):
            residual = schedule.imbalance(schedule.context(when))
            assert residual.quantize(100) == Money(0)
            assert schedule.instantiate(when).is_balanced()

    def test_interest_falls_over_the_life_of_the_loan(self, terms):
        schedule = build_schedule(terms)
        first = dict(schedule.resolved_splits(when=date(2026, 1, 1)))
        later = dict(schedule.resolved_splits(when=date(2046, 1, 1)))
        assert later[terms.interest_account] < first[terms.interest_account]
        assert later[terms.liability] > first[terms.liability]

    def test_the_first_payment_matches_the_arithmetic(self, terms):
        schedule = build_schedule(terms)
        legs = dict(schedule.resolved_splits(when=date(2026, 1, 1)))
        assert legs[terms.interest_account].quantize(100) == Money("1000.00")
        assert legs[terms.liability].quantize(100) == Money("199.10")

    def test_creating_a_loan_stores_it_with_an_opening_balance(self, db, terms):
        create_loan(db, terms)
        assert [s.name for s in db.iter_scheduled()] == ["Mortgage"]
        from breadsched.gen.engine import ledger

        assert ledger.balance(db, terms.liability) == Money("200000.00")

    def test_the_opening_balance_can_be_skipped(self, db, terms):
        from breadsched.gen.engine import ledger

        create_loan(db, terms, opening_balance=False)
        assert ledger.balance(db, terms.liability) == Money(0)

    def test_a_projection_shows_the_debt_falling(self, db, terms):
        from breadsched.gen.engine import projection
        from breadsched.gen.lib import ProjectionBasis, Scenario

        create_loan(db, terms)
        scenario = Scenario(
            name="With a mortgage",
            start=date(2026, 1, 1),
            years=5,
            basis=ProjectionBasis.SCHEDULED,
        )
        result = projection.project(db, scenario)
        assert result.rows[-1].liabilities < result.rows[0].liabilities
        assert not any("does not balance" in w for w in result.warnings)

    def test_formula_loan_projection_follows_its_amortisation_table(self, db, terms):
        from breadsched.gen.engine import projection
        from breadsched.gen.lib import ProjectionBasis, Scenario

        create_loan(db, terms)
        scenario = Scenario(
            name="Economic consistency",
            start=date(2026, 1, 1),
            years=5,
            basis=ProjectionBasis.SCHEDULED,
            assumptions=Assumptions(
                income_growth="0.04",
                expense_inflation="0.03",
                investment_return="0",
                cash_interest="0",
                liability_interest="0.07",
            ),
        )

        result = projection.project(db, scenario)
        expected = amortisation_schedule(terms.period_rate, terms.periods, terms.principal.rate())[
            59
        ]["balance"]

        assert abs(result.rows[-1].liabilities.to_decimal() - expected) < Decimal("0.05")
        first_payment = result.rows[0].expense + result.rows[0].debt_payments
        later_payment = result.rows[12].expense + result.rows[12].debt_payments
        assert first_payment == terms.payment()
        assert later_payment == terms.payment()
        assert result.rows[-1].liabilities > Money(0)

    def test_plan_separates_interest_expense_from_debt_principal(self, db, terms):
        from breadsched.gen.engine import activity

        create_loan(db, terms, opening_balance=False)
        report = activity.build_category_report(
            db, date(2026, 1, 1), date(2026, 1, 31), as_of=date(2026, 1, 31)
        )

        interest = next(row for row in report.categories if row.account == terms.interest_account)
        principal = next(
            row for row in report.planning_flows if row.kind is PlanningFlowKind.DEBT_PRINCIPAL
        )
        assert interest.planned == [Money("1000.00")]
        assert principal.planned[0].quantize(100) == Money("199.10")


class TestGnuCashMortgageFormulas:
    """The formulas GnuCash's loan assistant actually writes.

    Taken verbatim from an imported book. Three things in one expression defeat a
    naive parser: colons separating the arguments, a thousands separator inside
    the principal, and ``i`` as the period. Any one of them left unhandled makes
    every mortgage payment in the book evaluate to nothing — which reads as a
    household whose loans cost it zero a month.
    """

    PRINCIPAL = "ppmt( .05375 / 12.00 : i : 180.00 : 399,200.00 : 0 : 0 )"
    INTEREST = "ipmt( .05375 / 12.00 : i : 180.00 : 399,200.00 : 0 : 0 )"

    def test_the_thousands_separator_is_not_an_argument(self):
        assert normalise("ppmt( 1 : 2 : 3 : 399,200.00 : 0 : 0 )").count(",") == 5

    def test_a_leading_dot_rate_is_read(self):
        assert q(evaluate(self.INTEREST, {"i": 1})) == Decimal("1788.08")

    def test_the_first_payment_is_a_month_of_interest(self):
        # 399,200 x 5.375% / 12
        assert q(evaluate(self.INTEREST, {"i": 1})) == Decimal("1788.08")

    PAYMENT = "pmt( .05375 / 12.00 : 180.00 : 399,200.00 )"

    def test_interest_and_principal_sum_to_the_payment(self):
        expected = q(evaluate(self.PAYMENT, {}))
        assert expected == Decimal("3235.38")
        for period in (1, 60, 180):
            total = evaluate(self.INTEREST, {"i": period}) + evaluate(self.PRINCIPAL, {"i": period})
            assert q(total) == expected

    def test_the_split_moves_over_the_life_of_the_loan(self):
        early = evaluate(self.INTEREST, {"i": 1})
        late = evaluate(self.INTEREST, {"i": 170})
        assert late < early
        assert evaluate(self.PRINCIPAL, {"i": 170}) > evaluate(self.PRINCIPAL, {"i": 1})

    def test_the_amounts_are_positive_as_gnucash_expects(self):
        """They go straight into a debit slot; a negative would reverse the entry."""
        assert evaluate(self.PRINCIPAL, {"i": 1}) > 0
        assert evaluate(self.INTEREST, {"i": 1}) > 0

    @pytest.mark.parametrize(
        "formula,period,expected",
        [
            ("ipmt( 0.03500 / 12.00 : i : 305.00 : 285215.05 : 0 : 0 )", 1, "831.88"),
            ("ppmt( .06990 / 12.00 : i : 180.00 : 90,636.00 : 0 : 0 )", 1, "286.20"),
            ("ipmt( .06990 / 12.00 : i : 180.00 : 90,636.00 : 0 : 0 )", 1, "527.95"),
        ],
    )
    def test_other_mortgages_from_the_same_book(self, formula, period, expected):
        assert q(evaluate(formula, {"i": period})) == Decimal(expected)

    def test_a_schedule_supplies_i_for_each_occurrence(self, db, book):
        """`i` is the period number, and must change from one payment to the next."""
        from breadsched.gen.lib import (
            PeriodType,
            Recurrence,
            ScheduledSplit,
            ScheduledTransaction,
        )

        sched = ScheduledTransaction(
            name="Mortgage",
            recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 1, 1)),
            splits=[
                ScheduledSplit(book.card, formula=self.PRINCIPAL, memo="Principal"),
                ScheduledSplit(book.utilities, formula=self.INTEREST, memo="Interest"),
                ScheduledSplit(book.checking, formula=f"-({self.PAYMENT})", memo="Payment"),
            ],
        )
        first = dict(sched.resolved_splits(when=date(2026, 1, 1)))
        later = dict(sched.resolved_splits(when=date(2030, 1, 1)))

        assert first[book.utilities].quantize(100) == Money("1788.08")
        assert later[book.utilities] < first[book.utilities]
        assert later[book.card] > first[book.card]

    def test_such_a_schedule_balances(self, db, book):
        from breadsched.gen.lib import (
            PeriodType,
            Recurrence,
            ScheduledSplit,
            ScheduledTransaction,
        )

        sched = ScheduledTransaction(
            name="Mortgage",
            recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 1, 1)),
            splits=[
                ScheduledSplit(book.card, formula=self.PRINCIPAL),
                ScheduledSplit(book.utilities, formula=self.INTEREST),
                # The funding leg comes from the same arithmetic as the other two,
                # so the three agree to full precision and only rounding remains.
                ScheduledSplit(book.checking, formula=f"-({self.PAYMENT})"),
            ],
        )
        assert sched.instantiate(date(2026, 5, 1)).is_balanced()

    def test_no_warning_is_logged_for_a_usable_formula(self, db, book, breadsched_logs):
        from breadsched.gen.lib import ScheduledSplit

        split = ScheduledSplit(book.card, formula=self.PRINCIPAL)
        split.resolve({"i": 1})
        assert breadsched_logs.containing("unusable formula") == []
