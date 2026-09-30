"""Every reporting-currency result says whether it is complete (#236).

Plan, Expense Explorer and Projection show a *partial* subtotal of what
converted; balances, net worth and the Dashboard *withhold* a total that would
be partial. Either way the result carries a structured status and the evidence
for each excluded input, and a genuine zero is complete.
"""

from __future__ import annotations

from datetime import date

import pytest
from test_projection import flat_assumptions

from breadsched.gen.engine import activity, dashboard, projection, valuation
from breadsched.gen.engine.completeness import (
    Completeness,
    CompletenessStatus,
    Excluded,
    Policy,
    combine,
)
from breadsched.gen.engine.currency import reporting_currency_handle
from breadsched.gen.lib import (
    Account,
    AccountType,
    Commodity,
    CommodityPrice,
    Money,
    PeriodType,
    Recurrence,
    Scenario,
    ScheduledSplit,
    ScheduledTransaction,
    Split,
    Transaction,
)
from breadsched.gen.services import query_net_worth_change, query_net_worth_history
from breadsched.gen.services.expense_explorer import query_expense_explorer
from breadsched.gen.services.plan import PlanQuery

AS_OF = date(2026, 9, 27)


@pytest.fixture
def euro(db):
    currency = Commodity(namespace="CURRENCY", mnemonic="EUR", fullname="Euro")
    with db.transaction("EUR") as txn:
        db.add_commodity(currency, txn)
    return currency


def _quote(db, euro, value="1.10", when=date(2026, 9, 1), *, inverse=False):
    reporting = reporting_currency_handle(db)
    valuation.save_currency_quote(
        db,
        source_handle=reporting if inverse else euro.handle,
        target_handle=euro.handle if inverse else reporting,
        quote_date=when,
        value=Money(value),
    )


def _euro_schedule(db, book, euro, on=date(2026, 10, 3), amount="500"):
    flat = ScheduledTransaction(
        name="Flat in Lyon",
        recurrence=Recurrence(PeriodType.ONCE, start=on),
        splits=[
            ScheduledSplit(book.rent, Money(amount)),
            ScheduledSplit(book.checking, Money(f"-{amount}")),
        ],
    )
    flat.currency = euro.handle
    with db.transaction("EUR schedule") as txn:
        db.add_scheduled(flat, txn)


def _book_starts_in_september(db, book):
    """The Plan starts no earlier than the book's first activity."""
    with db.transaction("first activity") as txn:
        db.add_transaction(
            Transaction.simple(date(2026, 9, 1), "Coffee", book.groceries, book.checking, "4"),
            txn,
        )


def _foreign_cash(db, book, euro, amount="10"):
    account = Account(
        name="Foreign cash", atype=AccountType.BANK, parent=book.assets, commodity=euro.handle
    )
    opening = Transaction(post_date=date(2026, 1, 2), description="Opening cash")
    opening.currency = euro.handle
    opening.splits = [Split(account.handle, Money(amount)), Split(book.opening, Money(-10))]
    with db.transaction("Foreign cash") as txn:
        db.add_account(account, txn)
        if Money(amount):
            db.add_transaction(opening, txn)
    return account


def _unpriced_fund(db, book):
    fund = Commodity(namespace="FUND", mnemonic="INDEX", fullname="Index fund", fraction=1000)
    account = Account(
        name="Index holding",
        atype=AccountType.INVESTMENT,
        parent=book.assets,
        commodity=fund.handle,
        commodity_scu=1000,
    )
    purchase = Transaction(post_date=date(2026, 1, 2), description="Opening holding")
    purchase.splits = [
        Split(account.handle, Money("1000"), quantity=Money("10")),
        Split(book.opening, Money("-1000")),
    ]
    with db.transaction("Security holding") as txn:
        db.add_commodity(fund, txn)
        db.add_account(account, txn)
        db.add_transaction(purchase, txn)
    return account, fund


class TestModel:
    def test_nothing_excluded_is_complete_even_when_zero(self):
        value = Completeness.of((), policy=Policy.WITHHOLD)
        assert value.status is CompletenessStatus.COMPLETE
        assert value.label == ""
        assert value.detail() == ()

    def test_policy_decides_partial_or_unavailable(self):
        item = Excluded("planned", "Flat", "EUR", Money(500), date(2026, 10, 3))
        partial = Completeness.of([item], policy=Policy.SUBTOTAL)
        withheld = Completeness.of([item], policy=Policy.WITHHOLD)
        assert partial.status is CompletenessStatus.PARTIAL
        assert partial.label == "Partial: excludes 1 unconverted amount"
        assert withheld.status is CompletenessStatus.UNAVAILABLE
        assert withheld.detail() == (
            "Flat 500.00 EUR (planned 2026-10-03): no exchange rate",
            "Add a EUR exchange rate in Accounts to include it.",
        )

    def test_a_delta_of_partial_inputs_is_partial_with_both_sides_evidence(self):
        left = Excluded("planned", "Flat", "EUR", Money(500), date(2026, 10, 3))
        right = Excluded("balance", "Savings", "GBP", Money(20), date(2026, 9, 30))
        combined = combine(
            Completeness.of([left], policy=Policy.SUBTOTAL),
            Completeness.of([right, left], policy=Policy.SUBTOTAL),
        )
        assert combined.status is CompletenessStatus.PARTIAL
        assert combined.excluded == (left, right)
        assert combine(Completeness(), Completeness()).complete


class TestPlanIsAPartialSubtotal:
    def test_missing_rate_marks_the_period_and_horizon_partial(self, db, book, euro):
        _euro_schedule(db, book, euro)

        report = activity.build_category_report(
            db, date(2026, 9, 1), date(2026, 10, 31), as_of=AS_OF
        )

        september, october = report.period_completeness
        assert september.complete
        assert october.status is CompletenessStatus.PARTIAL
        [item] = october.excluded
        assert (item.label, item.currency, item.amount, item.when) == (
            "Flat in Lyon",
            "EUR",
            Money(500),
            date(2026, 10, 3),
        )
        assert book.rent in item.accounts
        assert report.completeness.status is CompletenessStatus.PARTIAL
        # The exclusion is dated after as-of, so the through-as-of figures are whole.
        assert report.completeness_through_as_of.complete
        assert not report.cell_complete(book.rent, 1)
        assert report.cell_complete(book.rent, 0)
        # The subtotal reconciles to the included detail: nothing EUR is added.
        rent = next(row for row in report.expenses if row.account == book.rent)
        assert rent.planned == [Money(0), Money(0)]

    def test_a_quote_recovers_completeness(self, db, book, euro):
        _euro_schedule(db, book, euro)
        _quote(db, euro)

        report = activity.build_category_report(
            db, date(2026, 10, 1), date(2026, 10, 31), as_of=AS_OF
        )

        assert report.completeness.complete
        rent = next(row for row in report.expenses if row.account == book.rent)
        assert rent.planned == [Money("550.00")]

    def test_an_inverse_quote_is_complete_and_keeps_its_evidence(self, db, book, euro):
        _euro_schedule(db, book, euro)
        _quote(db, euro, "0.5", inverse=True)

        report = activity.build_category_report(
            db, date(2026, 10, 1), date(2026, 10, 31), as_of=AS_OF
        )

        assert report.completeness.complete
        [evidence] = report.activity.conversions
        assert evidence.path == "inverse"

    def test_expense_explorer_points_and_remaining_stay_flagged(self, db, book, euro):
        _book_starts_in_september(db, book)
        _euro_schedule(db, book, euro)
        request = PlanQuery(start=date(2026, 9, 1), end=date(2026, 10, 31), today=AS_OF)

        explorer = query_expense_explorer(db, request, rollover=True).value

        assert explorer is not None
        september, october = explorer.spending
        assert september.completeness.complete
        assert october.completeness.status is CompletenessStatus.PARTIAL
        assert october.currency_incomplete
        rent = next(row for row in explorer.categories if row.account == book.rent)
        # October has not started, so Remaining is not applicable before it is
        # unavailable; a started period with a gap says so instead.
        assert rent.periods[1].remaining is None

    def test_a_started_period_with_a_gap_has_unavailable_remaining(self, db, book, euro):
        _book_starts_in_september(db, book)
        _euro_schedule(db, book, euro, on=date(2026, 9, 10))
        request = PlanQuery(start=date(2026, 9, 1), end=date(2026, 9, 30), today=AS_OF)

        explorer = query_expense_explorer(db, request, rollover=True).value

        assert explorer is not None
        rent = next(row for row in explorer.categories if row.account == book.rent)
        assert rent.periods[0].remaining is None
        assert rent.periods[0].remaining_reason == "Currency conversion unavailable"
        assert explorer.spending[0].completeness.status is CompletenessStatus.PARTIAL


class TestProjectionIsAPartialSubtotal:
    def _project(self, db):
        scenario = Scenario(
            name="FX", start=date(2026, 10, 1), years=1, assumptions=flat_assumptions()
        )
        return projection.project(db, scenario)

    def test_months_before_an_excluded_event_are_complete(self, db, book, euro):
        _euro_schedule(db, book, euro, on=date(2026, 12, 3))

        result = self._project(db)

        assert result.completeness.status is CompletenessStatus.PARTIAL
        assert result.month_completeness(0).complete
        assert result.month_completeness(1).complete
        # December and every later month build on the missing payment.
        assert result.month_completeness(2).status is CompletenessStatus.PARTIAL
        assert result.month_completeness(11).status is CompletenessStatus.PARTIAL

    def test_an_excluded_opening_balance_affects_every_month(self, db, book, euro):
        _foreign_cash(db, book, euro)

        result = self._project(db)

        assert result.month_completeness(0).status is CompletenessStatus.PARTIAL
        excluded = {(item.kind, item.label, item.currency) for item in result.excluded}
        assert ("balance", "Assets:Foreign cash", "EUR") in excluded

    def test_comparison_rows_disclose_both_inputs(self, db, book, euro):
        base = self._project(db)
        _euro_schedule(db, book, euro, on=date(2026, 12, 3))
        other = self._project(db)

        rows = projection.compare(base, other)

        assert rows[0]["completeness"].complete
        assert rows[2]["completeness"].status is CompletenessStatus.PARTIAL


class TestBalancesAreWithheld:
    def test_dashboard_withholds_net_worth_and_names_the_balance(self, db, book, euro):
        account = _foreign_cash(db, book, euro)

        board = dashboard.build(db, as_of=AS_OF)

        assert board.report_summary()["net_worth"] is None
        assert board.completeness.status is CompletenessStatus.UNAVAILABLE
        [item] = board.completeness.excluded
        assert (item.label, item.currency, item.amount, item.reason) == (
            "Assets:Foreign cash",
            "EUR",
            Money(10),
            "exchange rate",
        )
        assert item.accounts == (account.handle,)
        assert board.liquid_completeness.status is CompletenessStatus.UNAVAILABLE

        _quote(db, euro)
        recovered = dashboard.build(db, as_of=AS_OF)
        assert recovered.completeness.complete
        assert recovered.report_summary()["net_worth"] is not None

    def test_a_zero_foreign_balance_needs_no_quote(self, db, book, euro):
        _foreign_cash(db, book, euro, amount="0")

        board = dashboard.build(db, as_of=AS_OF)

        assert board.completeness.complete
        assert board.report_summary()["net_worth"] is not None

    def test_an_unpriced_security_lacks_a_price_not_a_rate(self, db, book):
        account, fund = _unpriced_fund(db, book)

        board = dashboard.build(db, as_of=AS_OF)

        [item] = board.completeness.excluded
        assert (item.label, item.currency, item.amount, item.reason) == (
            "Assets:Index holding",
            "INDEX",
            Money("10"),
            "price",
        )
        assert item.action == "Add a INDEX price in Accounts to include it."
        group = dashboard.DashboardConfig(
            groups=[dashboard.GroupConfig("Investments", [account.handle], "asset")]
        )
        assert dashboard.build(db, group, as_of=AS_OF).groups[0].completeness.excluded == (item,)

        usd = db.get_commodity_by_mnemonic("USD")
        assert usd is not None
        with db.transaction("price") as txn:
            db.add_price(
                CommodityPrice(
                    commodity=fund.handle,
                    currency=usd.handle,
                    quote_date=date(2026, 9, 1),
                    value=Money(120),
                ),
                txn,
            )
        assert dashboard.build(db, as_of=AS_OF).completeness.complete

    def test_net_worth_history_mixes_complete_and_withheld_points(self, db, book, euro):
        _foreign_cash(db, book, euro)
        _quote(db, euro, when=date(2026, 2, 10))

        history = query_net_worth_history(
            db, date(2026, 1, 1), date(2026, 2, 28), today=date(2026, 3, 5)
        ).value

        assert history is not None
        january, february = history.points
        assert january.completeness.status is CompletenessStatus.UNAVAILABLE
        assert january.net_worth is None
        assert february.completeness.complete
        assert february.net_worth is not None

    def test_net_worth_change_withholds_with_every_gap(self, db, book, euro):
        _foreign_cash(db, book, euro)

        change = query_net_worth_change(
            db, date(2026, 1, 1), date(2026, 1, 31), today=date(2026, 3, 5)
        ).value

        assert change is not None
        assert change.change is None
        assert change.completeness.status is CompletenessStatus.UNAVAILABLE
        kinds = {(item.kind, item.label) for item in change.completeness.excluded}
        assert ("balance", "Assets:Foreign cash") in kinds
        assert ("posting", "Opening cash") in kinds


class TestSurfaces:
    """Every interface shows the same status beside the value, as text."""

    def test_web_plan_marks_the_period_cell_and_totals(self, db, book, euro):
        from breadsched.web.plan_resource import plan_report

        _euro_schedule(db, book, euro)

        payload = plan_report(db, "2026-10", "2026-10")

        assert payload["completeness"]["horizon"]["status"] == "partial"
        assert payload["completeness"]["horizon"]["label"] == (
            "Partial: excludes 1 unconverted amount"
        )
        [october] = payload["periods"]
        assert october["completeness"]["excluded"][0]["label"] == "Flat in Lyon"
        rent = next(row for row in payload["categories"] if row["account"] == book.rent)
        assert rent["complete"] == [False]

    def test_web_projection_marks_months_and_deltas(self, db, book, euro):
        from breadsched.web.projection_resource import (
            projection_comparison_report,
            projection_report,
        )

        _euro_schedule(db, book, euro, on=date(2026, 12, 3))
        scenario = Scenario(
            name="FX", start=date(2026, 10, 1), years=1, assumptions=flat_assumptions()
        )

        payload = projection_report(db, scenario)

        assert payload["completeness"]["status"] == "partial"
        assert [row["completeness"]["status"] for row in payload["rows"][:3]] == [
            "complete",
            "complete",
            "partial",
        ]
        other = Scenario(
            name="Other", start=date(2026, 10, 1), years=1, assumptions=flat_assumptions()
        )
        compared = projection_comparison_report(
            db, scenario, other, primary_base=False, comparison_base=False
        )
        assert compared["comparison"]["delta_completeness"]["status"] == "partial"
        assert compared["comparison"]["rows"][0]["completeness"]["status"] == "complete"

    def test_print_labels_partial_and_withheld_values(self, db, book, euro):
        from breadsched.gen.engine.activity import PlanMeasure
        from breadsched.plugins.export.html_report import dashboard_report, plan_report

        _euro_schedule(db, book, euro)
        _foreign_cash(db, book, euro)
        report = activity.build_category_report(
            db, date(2026, 10, 1), date(2026, 10, 31), as_of=AS_OF
        )

        plan = plan_report(report, PlanMeasure.PLANNED, scenario_name="Base scenario")
        board = dashboard_report(dashboard.build(db, as_of=AS_OF))

        assert "Plan totals: Partial: excludes 1 unconverted amount." in plan
        assert "Add a EUR exchange rate in Accounts to include it." in plan
        assert "Net worth: Unavailable: 1 amount lacks a quote." in board
        assert "Assets:Foreign cash 10.00 EUR (balance 2026-09-27): no exchange rate" in board

    def test_cli_and_csv_state_the_status(self, db, book, euro, tmp_path):
        from breadsched.plugins.export.csv_export import export_projection

        _euro_schedule(db, book, euro, on=date(2026, 12, 3))
        scenario = Scenario(
            name="FX", start=date(2026, 10, 1), years=1, assumptions=flat_assumptions()
        )
        path = tmp_path / "projection.csv"
        export_projection(projection.project(db, scenario), path)
        header, october, _november, december, *_rest = path.read_text().splitlines()
        assert header.endswith(",completeness")
        assert october.endswith(",complete")
        assert december.endswith(",partial")
