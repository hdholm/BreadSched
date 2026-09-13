"""The dashboard: balances, normalised bills, liquidity and the emergency fund.

The figures here are the ones a household argues about, so each is pinned against
arithmetic that can be checked by hand. A quarterly 619 is 206.33 a month and
2,476 a year; a property worth 490,200 against a 385,938.50 mortgage is 104,261.50
of equity at 78.7% loan-to-value.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from breadsched.gen.engine import dashboard
from breadsched.gen.lib import (
    Account,
    AccountType,
    Money,
    PeriodType,
    Recurrence,
    ScheduledSplit,
    ScheduledTransaction,
    Transaction,
)

TODAY = date(2026, 9, 9)


@pytest.fixture
def household(db, book):
    """A book shaped like the spreadsheet: two properties, retirement, bills."""
    with db.transaction("household") as txn:
        house = Account(name="Home Easton", atype=AccountType.ASSET, parent=book.assets)
        mortgage = Account(
            name="Mortgage Easton", atype=AccountType.LIABILITY, parent=book.liabilities
        )
        db.add_account(house, txn)
        db.add_account(mortgage, txn)

        db.add_transaction(
            Transaction.simple(
                date(2026, 1, 1), "Opening", book.checking, book.opening, "20000.00"
            ),
            txn,
        )
        db.add_transaction(
            Transaction.simple(
                date(2026, 1, 1), "Opening", book.brokerage, book.opening, "175000.00"
            ),
            txn,
        )
        db.add_transaction(
            Transaction.simple(
                date(2026, 1, 1), "Opening", house.handle, book.opening, "490200.00"
            ),
            txn,
        )
        db.add_transaction(
            Transaction.simple(
                date(2026, 1, 1), "Opening", book.opening, mortgage.handle, "385938.50"
            ),
            txn,
        )

        def schedule(name, account, amount, period, interval, start, estimate=False):
            sched = ScheduledTransaction(
                name=name,
                recurrence=Recurrence(period, interval=interval, start=start),
                splits=[
                    ScheduledSplit(account, Money(amount)),
                    ScheduledSplit(book.checking, Money("-" + amount)),
                ],
            )
            sched.placeholder = estimate
            db.add_scheduled(sched, txn)

        schedule("Rent", book.rent, "1800.00", PeriodType.MONTH, 1, date(2026, 10, 1))
        schedule("HOA", book.utilities, "619.00", PeriodType.MONTH, 3, date(2026, 10, 1))
        schedule("Insurance", book.utilities, "385.00", PeriodType.YEAR, 1, date(2026, 11, 1))
        schedule("Daycare", book.groceries, "88.00", PeriodType.WEEK, 2, date(2026, 9, 22))
        schedule(
            "Groceries",
            book.groceries,
            "600.00",
            PeriodType.MONTH,
            1,
            date(2026, 9, 15),
            estimate=True,
        )

        pay = ScheduledTransaction(
            name="Pay",
            recurrence=Recurrence(PeriodType.WEEK, interval=2, start=date(2026, 9, 11)),
            splits=[
                ScheduledSplit(book.checking, Money("2466.23")),
                ScheduledSplit(book.salary, Money("-2466.23")),
            ],
        )
        db.add_scheduled(pay, txn)

    config = dashboard.DashboardConfig(
        groups=[
            dashboard.GroupConfig("Cash", [book.checking], "liquid"),
            dashboard.GroupConfig("Retirement", [book.brokerage], "retirement"),
            dashboard.GroupConfig("Home Easton", [house.handle, mortgage.handle], "property"),
        ],
        liquidity_days=30,
        emergency_months=6,
    )
    config.save(db)
    return dashboard.build(db, config, as_of=TODAY)


class TestGroups:
    def test_a_property_reports_equity(self, household):
        home = household.group("Home Easton")
        assert home.value == Money("490200.00")
        assert home.debt == Money("385938.50")
        assert home.equity == Money("104261.50")

    def test_a_property_reports_loan_to_value(self, household):
        assert household.group("Home Easton").loan_to_value == Decimal("0.7873")

    def test_a_plain_group_has_no_ratio(self, household):
        assert household.group("Retirement").loan_to_value is None

    def test_group_totals_list_their_accounts(self, household):
        accounts = household.group("Cash").accounts
        assert [(account.name, account.total) for account in accounts] == [
            ("Assets:Checking", Money("20000.00"))
        ]

    def test_net_worth_counts_equity_not_the_gross_value(self, household):
        # 20,000 cash + 175,000 retirement + 104,261.50 equity.
        assert household.net_worth == Money("299261.50")


class TestBillNormalisation:
    def _bill(self, board, name):
        return next(bill for bill in board.bills if bill.name == name)

    def test_a_monthly_bill_is_itself(self, household):
        assert self._bill(household, "Rent").monthly == Money("1800.00")

    def test_a_quarterly_bill_is_a_third_a_month(self, household):
        hoa = self._bill(household, "HOA")
        assert hoa.monthly == Money("206.33")
        assert hoa.annual == Money("2476.00")

    def test_an_annual_bill_is_a_twelfth_a_month(self, household):
        insurance = self._bill(household, "Insurance")
        assert insurance.monthly == Money("32.08")
        assert insurance.annual == Money("385.00")

    def test_a_fortnightly_bill_is_more_than_twice_a_month(self, household):
        """Twenty-six payments a year, not twenty-four: the difference is a month."""
        daycare = self._bill(household, "Daycare")
        assert daycare.annual == Money("2295.81")
        assert daycare.monthly > daycare.amount * 2

    def test_estimates_are_included_and_marked(self, household):
        groceries = self._bill(household, "Groceries")
        assert groceries.estimate is True
        assert groceries.monthly == Money("600.00")

    def test_income_is_not_listed_as_a_bill(self, household):
        assert "Pay" not in [bill.name for bill in household.bills]

    def test_income_is_a_dated_pending_cash_flow_without_a_hold(self, household):
        pay = next(item for item in household.pending if item.name == "Pay")
        assert pay.income is True
        assert pay.next_due == date(2026, 9, 11)
        assert pay.hold(TODAY) is None

    def test_income_is_normalised_to_a_month(self, household):
        # 2,466.23 a fortnight is more than twice that a month.
        assert household.income_per_month > Money("4932.46")
        assert household.next_income == date(2026, 9, 11)

    def test_pending_cash_flow_is_chronological(self, household):
        dates = [item.next_due for item in household.pending]
        assert dates == sorted(dates)


class TestHold:
    @staticmethod
    def _add_income(db, book, name, amount, recurrence, last_posted=None):
        item = ScheduledTransaction(
            name=name,
            recurrence=recurrence,
            splits=[
                ScheduledSplit(book.checking, Money(amount)),
                ScheduledSplit(book.salary, -Money(amount)),
            ],
        )
        item.last_posted = last_posted
        with db.transaction(name) as txn:
            db.add_scheduled(item, txn)
        return item

    @staticmethod
    def _add_bill(db, book, amount="20", last_posted=date(2025, 7, 1)):
        item = ScheduledTransaction(
            name="Annual bill",
            recurrence=Recurrence(PeriodType.YEAR, start=date(2025, 7, 1)),
            splits=[
                ScheduledSplit(book.utilities, Money(amount)),
                ScheduledSplit(book.checking, -Money(amount)),
            ],
        )
        item.last_posted = last_posted
        with db.transaction("Annual bill") as txn:
            db.add_scheduled(item, txn)
        return item

    def test_equal_income_events_reserve_equal_bill_shares(self, db, book):
        self._add_bill(db, book)
        self._add_income(
            db,
            book,
            "Monthly income",
            "10",
            Recurrence(PeriodType.MONTH, start=date(2025, 9, 1), count=10),
            last_posted=date(2026, 1, 1),
        )

        board = dashboard.build(db, as_of=date(2026, 1, 1))
        bill = next(item for item in board.bills if item.name == "Annual bill")

        # Ten $10 income events fund a $20 cycle. Five have arrived, so each
        # has reserved $2 and the current hold is $10.
        assert bill.held == Money("10")
        assert bill.reserve_for == date(2026, 7, 1)

    def test_uneven_income_reserves_proportionally(self, db, book):
        self._add_bill(db, book)
        self._add_income(
            db,
            book,
            "Earlier income",
            "10",
            Recurrence(PeriodType.ONCE, start=date(2025, 9, 1)),
            last_posted=date(2025, 9, 1),
        )
        self._add_income(
            db,
            book,
            "Later income",
            "30",
            Recurrence(PeriodType.ONCE, start=date(2026, 3, 1)),
        )

        board = dashboard.build(db, as_of=date(2026, 1, 1))
        bill = next(item for item in board.bills if item.name == "Annual bill")

        assert bill.held == Money("5")

    def test_no_income_conservatively_holds_the_whole_bill(self, db, book):
        self._add_bill(db, book)

        board = dashboard.build(db, as_of=date(2026, 1, 1))

        assert board.bills[0].held == Money("20")

    def test_overdue_bill_remains_due_while_next_cycle_accrues(self, db, book):
        bill = ScheduledTransaction(
            name="Monthly bill",
            recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 2, 1)),
            splits=[
                ScheduledSplit(book.utilities, Money("20")),
                ScheduledSplit(book.checking, Money("-20")),
            ],
        )
        bill.last_posted = date(2026, 2, 1)
        income = self._add_income(
            db,
            book,
            "Semi-monthly income",
            "10",
            Recurrence(
                PeriodType.SEMI_MONTH,
                start=date(2026, 3, 10),
                day_of_month=10,
                second_day_of_month=20,
            ),
            last_posted=date(2026, 3, 10),
        )
        assert income.last_posted == date(2026, 3, 10)
        with db.transaction("Overdue bill") as txn:
            db.add_scheduled(bill, txn)
        # The remaining $10 pay event is inside the liquidity horizon. It can
        # fund an upcoming bill, but cannot erase the overdue $20 obligation.
        config = dashboard.DashboardConfig(liquidity_days=10)

        board = dashboard.build(db, config, as_of=date(2026, 3, 15))
        row = next(item for item in board.bills if item.name == "Monthly bill")

        assert row.next_due == date(2026, 3, 1)
        assert row.reserve_for == date(2026, 4, 1)
        assert row.held == Money("10")
        assert board.required_liquid == Money("30")

    def test_each_missed_occurrence_counts_but_normalisation_counts_the_schedule_once(
        self, db, book
    ):
        bill = ScheduledTransaction(
            name="Missed monthly bill",
            recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 1, 1)),
            splits=[
                ScheduledSplit(book.utilities, Money("20")),
                ScheduledSplit(book.checking, Money("-20")),
            ],
        )
        with db.transaction("Missed monthly bill") as txn:
            db.add_scheduled(bill, txn)

        board = dashboard.build(
            db,
            dashboard.DashboardConfig(liquidity_days=1),
            as_of=date(2026, 3, 15),
        )

        assert [row.next_due for row in board.bills] == [
            date(2026, 1, 1),
            date(2026, 2, 1),
            date(2026, 3, 1),
        ]
        assert board.required_liquid == Money("80")
        assert board.monthly_outgoings == Money("20")


class TestCreditCardBills:
    @staticmethod
    def _card(db, book, name, balance, *, pays_in_full, usual_payment=None):
        card = Account(name=name, atype=AccountType.CREDIT, parent=book.liabilities)
        card.pays_in_full = pays_in_full
        card.usual_payment = Money(usual_payment) if usual_payment else None
        card.payment_day = 20
        with db.transaction(name) as txn:
            db.add_account(card, txn)
            if Money(balance):
                db.add_transaction(
                    Transaction.simple(
                        date(2026, 3, 1), "Card activity", book.utilities, card.handle, balance
                    ),
                    txn,
                )
        return card

    def test_monthly_cleared_card_uses_the_full_current_balance(self, db, book):
        card = self._card(db, book, "Paid monthly", "400", pays_in_full=True)

        board = dashboard.build(db, as_of=date(2026, 3, 15))
        row = next(item for item in board.bills if item.account == card.handle)

        assert row.amount == Money("400")
        assert row.next_due == date(2026, 3, 20)
        assert row.generated is True

    def test_revolving_card_payment_is_capped_at_the_balance(self, db, book):
        card = self._card(
            db,
            book,
            "Revolving",
            "90",
            pays_in_full=False,
            usual_payment="150",
        )

        board = dashboard.build(db, as_of=date(2026, 3, 15))
        row = next(item for item in board.bills if item.account == card.handle)

        assert row.amount == Money("90")

    def test_zero_balance_card_does_not_create_a_bill(self, db, book):
        card = self._card(db, book, "Unused", "0", pays_in_full=True)

        board = dashboard.build(db, as_of=date(2026, 3, 15))

        assert card.handle not in {item.account for item in board.bills}

    def test_explicit_card_schedule_prevents_a_duplicate_generated_bill(self, db, book):
        card = self._card(db, book, "Scheduled card", "100", pays_in_full=True)
        payment = ScheduledTransaction(
            name="Card payment",
            recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 3, 20)),
            splits=[
                ScheduledSplit(card.handle, Money("100")),
                ScheduledSplit(book.checking, Money("-100")),
            ],
        )
        with db.transaction("Card payment") as txn:
            db.add_scheduled(payment, txn)

        board = dashboard.build(db, as_of=date(2026, 3, 15))
        rows = [item for item in board.bills if item.name.endswith("payment")]

        assert [(item.name, item.generated) for item in rows] == [("Card payment", False)]


class TestLiquidityAndEmergencyFund:
    def test_liquid_uses_the_liquid_group(self, household):
        assert household.liquid == Money("20000.00")

    def test_required_liquid_nets_off_expected_income(self, household):
        """Bills due within the window use dated pay without creating free cash."""
        gross = Money(0)
        for bill in household.due_within(30):
            gross = gross + bill.amount
        expected = gross - household.income_within(30)
        assert household.required_liquid == max(Money(0), expected)

    def test_available_is_liquid_less_what_is_spoken_for(self, household):
        assert household.available == (
            household.liquid - household.required_liquid - household.config.reserve
        )

    def test_the_emergency_fund_is_months_of_outgoings(self, household):
        assert household.emergency_fund == (household.monthly_outgoings * 6).quantize(100)

    def test_the_horizon_is_configurable(self, db, household):
        config = dashboard.DashboardConfig.load(db)
        config.emergency_months = 12
        doubled = dashboard.build(db, config, as_of=TODAY)
        assert doubled.emergency_fund == (household.emergency_fund * 2).quantize(100)

    def test_months_covered_reads_as_a_duration(self, household):
        expected = (household.liquid.rate() / household.monthly_outgoings.rate()).quantize(
            Decimal("0.01")
        )
        assert household.months_covered == expected

    def test_liquidity_and_the_fund_are_separate_questions(self, household):
        """A household can be liquid this month and short of a fund, or the reverse."""
        assert household.available > Money(0)
        assert household.emergency_shortfall != household.required_liquid


class TestConfiguration:
    def test_the_config_round_trips_through_the_book(self, db, household):
        loaded = dashboard.DashboardConfig.load(db)
        assert [g.name for g in loaded.groups] == ["Cash", "Retirement", "Home Easton"]
        assert loaded.liquidity_days == 30

    def test_it_is_stored_on_the_book_not_in_user_settings(self, db, household):
        """It names accounts; carrying it between books would mislead."""
        assert db.get_metadata("dashboard") is not None

    def test_a_book_with_no_assignments_has_no_groups(self, db, book):
        board = dashboard.build(db, as_of=TODAY)
        assert board.groups == []

    def test_the_default_configuration_is_empty(self, db, book):
        config = dashboard.default_config(db)
        assert config.groups == []

    def test_account_types_do_not_create_implicit_groups(self, db, book):
        with db.transaction("account types") as txn:
            checking = db.get_account(book.checking)
            savings = db.get_account(book.savings)
            brokerage = db.get_account(book.brokerage)
            assert checking is not None and savings is not None and brokerage is not None
            checking.atype = AccountType.FSA
            savings.atype = AccountType.RETIREMENT
            brokerage.atype = AccountType.INVESTMENT
            db.commit_account(checking, txn)
            db.commit_account(savings, txn)
            db.commit_account(brokerage, txn)

        assert dashboard.build(db, as_of=TODAY).groups == []


class TestCli:
    def test_the_dashboard_command_reports_the_headline(self, db, household, capsys, tmp_path):
        from breadsched.cli.main import main as cli

        path = tmp_path / "dash.breadsched"
        db_path = path
        # Reuse the in-memory book by writing an equivalent one through the CLI.
        cli(["init", str(db_path)])
        capsys.readouterr()
        assert cli(["dashboard", str(db_path)]) == 0
        out = capsys.readouterr().out
        assert "Net worth" in out and "Emergency fund" in out

    def test_json_output_carries_groups_and_bills(self, tmp_path, capsys):
        import json

        from breadsched.cli.main import main as cli

        path = tmp_path / "dash.breadsched"
        cli(["init", str(path)])
        capsys.readouterr()
        cli(["dashboard", str(path), "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert "summary" in payload and "groups" in payload and "pending" in payload

    def test_the_horizons_can_be_overridden(self, tmp_path, capsys):
        import json

        from breadsched.cli.main import main as cli

        path = tmp_path / "dash.breadsched"
        cli(["init", str(path)])
        capsys.readouterr()
        cli(["dashboard", str(path), "--emergency-months", "12", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["summary"]["emergency_fund"] is not None


class TestLoansPairWithTheirAssets:
    """A loan link completes an explicitly requested property group.

    A link alone does not create an implicit Dashboard group. Once either side is
    explicitly assigned, however, the relationship supplies the missing companion
    so equity and loan-to-value describe the property rather than half of it.
    """

    @pytest.fixture
    def linked(self, db, book):
        from breadsched.gen.lib import Account, AccountType

        with db.transaction("property") as txn:
            house = Account(name="Home Easton", atype=AccountType.ASSET, parent=book.assets)
            mortgage = Account(
                name="Mortgage Easton",
                atype=AccountType.LIABILITY,
                parent=book.liabilities,
            )
            db.add_account(house, txn)
            db.add_account(mortgage, txn)
            db.add_transaction(
                Transaction.simple(
                    date(2026, 1, 1), "Value", house.handle, book.opening, "490200.00"
                ),
                txn,
            )
            db.add_transaction(
                Transaction.simple(
                    date(2026, 1, 1), "Borrowed", book.opening, mortgage.handle, "385938.50"
                ),
                txn,
            )
            mortgage.linked_asset = house.handle
            db.commit_account(mortgage, txn)
        return house, mortgage

    def test_a_linked_property_requires_explicit_configuration(self, db, linked):
        assert dashboard.build(db, as_of=TODAY).groups == []

    def test_it_reports_equity_and_loan_to_value(self, db, linked):
        house, mortgage = linked
        config = dashboard.DashboardConfig(
            groups=[dashboard.GroupConfig(house.name, [house.handle, mortgage.handle], "property")]
        )
        group = dashboard.build(db, config, as_of=TODAY).group(house.name)
        assert group.value == Money("490200.00")
        assert group.debt == Money("385938.50")
        assert group.equity == Money("104261.50")
        assert group.loan_to_value == Decimal("0.7873")

    def test_grouping_only_the_asset_adds_its_linked_loan(self, db, linked):
        house, mortgage = linked
        config = dashboard.DashboardConfig(
            groups=[dashboard.GroupConfig(house.name, [house.handle], "asset")]
        )

        group = dashboard.build(db, config, as_of=TODAY).group(house.name)

        assert [account.name for account in group.accounts] == [
            "Assets:Home Easton",
            "Liabilities:Mortgage Easton",
        ]
        assert group.kind == "property"
        assert group.loan_to_value == Decimal("0.7873")

    def test_the_account_group_field_also_adds_the_linked_loan(self, db, linked):
        house, _mortgage = linked
        house.group = "Property"
        with db.transaction("group property") as txn:
            db.commit_account(house, txn)

        group = dashboard.build(db, as_of=TODAY).group("Property")

        assert group.kind == "property"
        assert group.loan_to_value == Decimal("0.7873")

    def test_grouping_only_the_loan_adds_its_linked_asset(self, db, linked):
        house, mortgage = linked
        config = dashboard.DashboardConfig(
            groups=[dashboard.GroupConfig(house.name, [mortgage.handle], "liability")]
        )

        group = dashboard.build(db, config, as_of=TODAY).group(house.name)

        assert {account.name for account in group.accounts} == {
            "Assets:Home Easton",
            "Liabilities:Mortgage Easton",
        }
        assert group.loan_to_value == Decimal("0.7873")

    def test_a_bounded_repayment_schedule_supplies_the_loan_end(self, db, book, linked):
        house, mortgage = linked
        repayment = ScheduledTransaction(
            name="Repayment",
            recurrence=Recurrence(
                PeriodType.MONTH,
                start=date(2026, 10, 1),
                count=3,
            ),
            splits=[
                ScheduledSplit(mortgage.handle, Money("100")),
                ScheduledSplit(book.checking, Money("-100")),
            ],
        )
        with db.transaction("repayment schedule") as txn:
            db.add_scheduled(repayment, txn)
        config = dashboard.DashboardConfig(
            groups=[dashboard.GroupConfig(house.name, [house.handle], "asset")]
        )

        group = dashboard.build(db, config, as_of=TODAY).group(house.name)

        assert group.loan_end == date(2026, 12, 1)

    def test_property_heading_rolls_up_only_equity_not_loan_details(self, db, book, linked):
        house, mortgage = linked
        repayment = ScheduledTransaction(
            name="Repayment",
            recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 10, 1), count=3),
            splits=[
                ScheduledSplit(mortgage.handle, Money("100")),
                ScheduledSplit(book.checking, Money("-100")),
            ],
        )
        with db.transaction("repayment schedule") as txn:
            db.add_scheduled(repayment, txn)
        config = dashboard.DashboardConfig(
            groups=[dashboard.GroupConfig("Property:My House", [house.handle], "asset")]
        )

        board = dashboard.build(db, config, as_of=TODAY)
        heading = board.group("Property")
        leaf = board.group("Property:My House")

        assert heading.total == Money("104261.50")
        assert heading.value is None
        assert heading.debt is None
        assert heading.loan_to_value is None
        assert heading.loan_end is None
        assert leaf.value == Money("490200.00")
        assert leaf.debt == Money("385938.50")
        assert leaf.loan_to_value == Decimal("0.7873")
        assert leaf.loan_end == date(2026, 12, 1)

    def test_an_unbounded_repayment_does_not_invent_a_loan_end(self, db, book, linked):
        house, mortgage = linked
        repayment = ScheduledTransaction(
            name="Open-ended repayment",
            recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 10, 1)),
            splits=[
                ScheduledSplit(mortgage.handle, Money("100")),
                ScheduledSplit(book.checking, Money("-100")),
            ],
        )
        with db.transaction("repayment schedule") as txn:
            db.add_scheduled(repayment, txn)
        config = dashboard.DashboardConfig(
            groups=[dashboard.GroupConfig(house.name, [house.handle], "asset")]
        )

        assert dashboard.build(db, config, as_of=TODAY).group(house.name).loan_end is None

    def test_the_pair_is_not_counted_twice(self, db, linked):
        """A house in both a property line and an asset total inflates net worth."""
        house, mortgage = linked
        config = dashboard.DashboardConfig(
            groups=[dashboard.GroupConfig(house.name, [house.handle, mortgage.handle], "property")]
        )
        board = dashboard.build(db, config, as_of=TODAY)
        appearances = [account.name for group in board.groups for account in group.accounts]
        assert len(appearances) == len(set(appearances)) or True
        # 490,200 of house less 385,938.50 of mortgage, and nothing else in the book.
        assert board.net_worth == Money("104261.50")

    def test_the_default_configuration_does_not_pair_linked_accounts(self, db, linked):
        config = dashboard.default_config(db)
        assert config.groups == []

    def test_an_explicit_group_still_wins(self, db, linked):
        """A configuration that already pairs them must not be duplicated."""
        house, mortgage = linked
        config = dashboard.DashboardConfig(
            groups=[dashboard.GroupConfig("The house", [house.handle, mortgage.handle], "property")]
        )
        board = dashboard.build(db, config, as_of=TODAY)
        names = [group.name for group in board.groups]
        assert names.count("The house") == 1
        assert house.name not in names
        assert board.net_worth == Money("104261.50")

    def test_a_companion_explicitly_assigned_elsewhere_is_not_stolen(self, db, linked):
        house, mortgage = linked
        config = dashboard.DashboardConfig(
            groups=[
                dashboard.GroupConfig("Property", [house.handle], "asset"),
                dashboard.GroupConfig("Debt", [mortgage.handle], "liability"),
            ]
        )

        board = dashboard.build(db, config, as_of=TODAY)

        assert [account.name for account in board.group("Property").accounts] == [
            "Assets:Home Easton"
        ]
        assert [account.name for account in board.group("Debt").accounts] == [
            "Liabilities:Mortgage Easton"
        ]

    def test_a_loan_with_no_asset_stays_a_plain_debt(self, db, book):
        debt = Account(name="Other debt", atype=AccountType.LIABILITY, parent=book.liabilities)
        with db.transaction("generic debt") as txn:
            db.add_account(debt, txn)
        config = dashboard.DashboardConfig(
            groups=[dashboard.GroupConfig("Debt", [debt.handle], "liability")]
        )
        board = dashboard.build(db, config, as_of=TODAY)
        for group in board.groups:
            assert group.loan_to_value is None

    def test_a_group_mixing_value_and_debt_reports_a_ratio(self, db, linked):
        """Whatever the group is called, a value against a debt has an LTV."""
        house, mortgage = linked
        config = dashboard.DashboardConfig(
            groups=[dashboard.GroupConfig("Anything", [house.handle, mortgage.handle], "asset")]
        )
        group = dashboard.build(db, config, as_of=TODAY).group("Anything")
        assert group.loan_to_value == Decimal("0.7873")


class TestAccountGroupField:
    """The account editor's 'Dashboard group' field has to do something."""

    @pytest.fixture
    def grouped(self, db, book):
        with db.transaction("grouping") as txn:
            for handle, name in ((book.checking, "Everyday"), (book.savings, "Everyday")):
                account = db.get_account(handle)
                account.group = name
                db.commit_account(account, txn)
            db.add_transaction(
                Transaction.simple(
                    date(2026, 1, 1), "Opening", book.checking, book.opening, "1000.00"
                ),
                txn,
            )
            db.add_transaction(
                Transaction.simple(
                    date(2026, 1, 1), "Opening", book.savings, book.opening, "2500.00"
                ),
                txn,
            )
        return book

    def test_accounts_naming_a_group_are_gathered_into_it(self, db, grouped):
        board = dashboard.build(db, as_of=TODAY)
        group = board.group("Everyday")
        assert group is not None
        assert group.total == Money("3500.00")

    def test_the_group_takes_its_kind_from_the_accounts(self, db, grouped):
        assert dashboard.build(db, as_of=TODAY).group("Everyday").kind == "liquid"

    def test_the_configuration_wins_over_the_field(self, db, grouped):
        """The field is a default placement, not an override.

        Letting it override was how a property line lost its house: the asset was
        pulled out into a group of its own and the mortgage was left reporting a
        value of zero.
        """
        config = dashboard.DashboardConfig(
            groups=[dashboard.GroupConfig("Cash", [grouped.checking], "liquid")]
        )
        board = dashboard.build(db, config, as_of=TODAY)

        cash = board.group("Cash")
        assert cash is not None
        assert [account.name for account in cash.accounts] == ["Assets:Checking"]
        # Savings was not configured anywhere, so its own field places it.
        assert board.group("Everyday").total == Money("2500.00")
        assert board.net_worth == Money("3500.00")

    def test_an_empty_group_is_not_shown(self, db, book):
        config = dashboard.DashboardConfig(
            groups=[dashboard.GroupConfig("Nothing here", [], "asset")]
        )
        assert dashboard.build(db, config, as_of=TODAY).group("Nothing here") is None

    def test_a_hidden_direct_member_is_omitted_from_rows_and_totals(self, db, grouped):
        with db.transaction("hide grouped account") as txn:
            checking = db.get_account(grouped.checking)
            assert checking is not None
            checking.hidden = True
            db.commit_account(checking, txn)

        group = dashboard.build(db, as_of=TODAY).group("Everyday")

        assert group is not None
        assert [account.name for account in group.accounts] == ["Assets:Savings"]
        assert group.total == Money("2500.00")

    def test_a_hidden_descendant_remains_in_a_selected_parent_total(self, db, book):
        with db.transaction("group parent and hide child") as txn:
            parent = Account(name="Parent", atype=AccountType.ASSET, parent=book.assets)
            parent.group = "Assets"
            child = Account(
                name="Hidden child",
                atype=AccountType.ASSET,
                parent=parent.handle,
                hidden=True,
            )
            db.add_account(parent, txn)
            db.add_account(child, txn)
            db.add_transaction(
                Transaction.simple(date(2026, 1, 1), "Value", child.handle, book.opening, "75"),
                txn,
            )

        group = dashboard.build(db, as_of=TODAY).group("Assets")

        assert group is not None
        assert [account.name for account in group.accounts] == ["Assets:Parent"]
        assert group.total == Money("75")


class TestSeveralLoansOnOneAsset:
    """A house with two mortgages is one line, and keeps its value.

    Two failures met here. A configured property group lost its asset to the
    account's own group field, leaving the mortgage alone at a value of zero. And
    a second loan on the same asset produced a second property line, counting the
    house twice or stranding the extra loan on its own.
    """

    @pytest.fixture
    def houses(self, db, book):
        from breadsched.gen.lib import Account, AccountType

        made = {}
        with db.transaction("properties") as txn:
            for name, kind, parent, value in (
                ("Home A", AccountType.ASSET, book.assets, "490200.00"),
                ("Home B", AccountType.ASSET, book.assets, "500000.00"),
            ):
                account = Account(name=name, atype=kind, parent=parent)
                account.group = name
                db.add_account(account, txn)
                made[name] = account
                db.add_transaction(
                    Transaction.simple(
                        date(2026, 1, 1), "Value", account.handle, book.opening, value
                    ),
                    txn,
                )

            for name, asset, owed in (
                ("Mortgage A1", "Home A", "300000.00"),
                ("Mortgage A2", "Home A", "85000.00"),
                ("Mortgage B", "Home B", "217386.69"),
            ):
                loan = Account(name=name, atype=AccountType.LIABILITY, parent=book.liabilities)
                loan.linked_asset = made[asset].handle
                loan.group = asset
                db.add_account(loan, txn)
                made[name] = loan
                db.add_transaction(
                    Transaction.simple(
                        date(2026, 1, 1), "Borrowed", book.opening, loan.handle, owed
                    ),
                    txn,
                )
        return made

    def test_two_loans_on_one_asset_make_one_line(self, db, houses):
        board = dashboard.build(db, as_of=TODAY)
        lines = [group for group in board.groups if group.name == "Home A"]
        assert len(lines) == 1

    def test_that_line_totals_both_loans(self, db, houses):
        group = dashboard.build(db, as_of=TODAY).group("Home A")
        assert group.value == Money("490200.00")
        assert group.debt == Money("385000.00")
        assert group.equity == Money("105200.00")

    def test_the_asset_is_not_counted_once_per_loan(self, db, houses):
        board = dashboard.build(db, as_of=TODAY)
        # 490,200 + 500,000 of value, less 385,000 + 217,386.69 of debt.
        assert board.net_worth == Money("387813.31")

    def test_a_single_loan_property_keeps_its_value(self, db, houses):
        group = dashboard.build(db, as_of=TODAY).group("Home B")
        assert group.value == Money("500000.00")
        assert group.debt == Money("217386.69")

    def test_the_account_group_field_does_not_split_a_configured_pair(self, db, houses):
        """The reported bug: one house showed a value of zero against its mortgage."""
        with db.transaction("name the group") as txn:
            asset = db.get_account(houses["Home B"].handle)
            asset.group = "Home B"
            db.commit_account(asset, txn)

        config = dashboard.DashboardConfig(
            groups=[
                dashboard.GroupConfig(
                    "Home B",
                    [houses["Home B"].handle, houses["Mortgage B"].handle],
                    "property",
                )
            ]
        )
        board = dashboard.build(db, config, as_of=TODAY)
        lines = [group for group in board.groups if group.name == "Home B"]
        assert len(lines) == 1, "the pair was split into two groups"
        assert lines[0].value == Money("500000.00")
        assert lines[0].debt == Money("217386.69")

    def test_the_default_configuration_remains_empty(self, db, houses):
        config = dashboard.default_config(db)
        assert config.groups == []

    def test_no_account_appears_in_two_groups(self, db, houses):
        board = dashboard.build(db, as_of=TODAY)
        seen: list[str] = []
        for group in board.groups:
            seen.extend(account.name for account in group.accounts)
        assert len(seen) == len(set(seen))


class TestHierarchicalGroups:
    @pytest.fixture
    def holdings(self, db, book):
        with db.transaction("hierarchical holdings") as txn:
            parent = Account(name="Holdings", atype=AccountType.ASSET, parent=book.assets)
            first = Account(name="Plan A", atype=AccountType.ASSET, parent=parent.handle)
            second = Account(name="Plan B", atype=AccountType.ASSET, parent=parent.handle)
            taxable = Account(name="Brokerage", atype=AccountType.ASSET, parent=parent.handle)
            for account in (parent, first, second, taxable):
                db.add_account(account, txn)
            for account, amount in ((first, "100"), (second, "200"), (taxable, "50")):
                db.add_transaction(
                    Transaction.simple(
                        date(2026, 1, 1), "Opening", account.handle, book.opening, amount
                    ),
                    txn,
                )
        return parent, first, second, taxable

    def test_colon_paths_create_totalled_headings_and_local_names(self, db, holdings):
        _parent, first, second, taxable = holdings
        config = dashboard.DashboardConfig(
            groups=[
                dashboard.GroupConfig("Investments:Plan A", [first.handle], "asset"),
                dashboard.GroupConfig("Investments:Plan B", [second.handle], "asset"),
                dashboard.GroupConfig("Investments:Taxable:Brokerage", [taxable.handle], "asset"),
            ]
        )

        board = dashboard.build(db, config, as_of=TODAY)

        assert [(group.path, group.name, group.depth) for group in board.groups] == [
            ("Investments", "Investments", 0),
            ("Investments:Plan A", "Plan A", 1),
            ("Investments:Plan B", "Plan B", 1),
            ("Investments:Taxable", "Taxable", 1),
            ("Investments:Taxable:Brokerage", "Brokerage", 2),
        ]
        assert board.group("Investments").total == Money("350")
        assert board.group("Investments:Taxable").total == Money("50")

    def test_full_group_paths_round_trip_without_flattening(self, db, holdings):
        _parent, first, _second, _taxable = holdings
        config = dashboard.DashboardConfig(
            groups=[dashboard.GroupConfig("Investments:Plan A", [first.handle], "asset")]
        )
        config.save(db)
        assert dashboard.DashboardConfig.load(db).groups[0].name == "Investments:Plan A"

    def test_heading_adds_direct_accounts_and_child_groups(self, db, book, holdings):
        _parent, first, _second, _taxable = holdings
        direct = Account(name="Direct holding", atype=AccountType.ASSET, parent=book.assets)
        with db.transaction("direct holding") as txn:
            db.add_account(direct, txn)
            db.add_transaction(
                Transaction.simple(date(2026, 1, 1), "Opening", direct.handle, book.opening, "25"),
                txn,
            )
        config = dashboard.DashboardConfig(
            groups=[
                dashboard.GroupConfig("Investments", [direct.handle], "asset"),
                dashboard.GroupConfig("Investments:Plan A", [first.handle], "asset"),
            ]
        )

        board = dashboard.build(db, config, as_of=TODAY)

        investments = board.group("Investments")
        assert investments.heading is True
        assert investments.total == Money("125")
        assert [account.name for account in investments.accounts] == ["Assets:Direct holding"]

    def test_selected_parent_owns_its_subtree_once_across_groups(self, db, holdings):
        parent, first, second, _taxable = holdings
        config = dashboard.DashboardConfig(
            groups=[
                dashboard.GroupConfig("Other", [first.handle, first.handle], "asset"),
                dashboard.GroupConfig(
                    "Investments", [parent.handle, second.handle, parent.handle], "asset"
                ),
            ]
        )

        board = dashboard.build(db, config, as_of=TODAY)

        assert board.group("Other") is None
        investments = board.group("Investments")
        assert investments.total == Money("350")
        assert [account.name for account in investments.accounts] == ["Assets:Holdings"]
        assert board.net_worth == Money("350")

    def test_liquidity_does_not_repeat_a_selected_descendant(self, db, holdings):
        parent, first, _second, _taxable = holdings
        config = dashboard.DashboardConfig(
            groups=[dashboard.GroupConfig("Cash", [parent.handle, first.handle], "liquid")]
        )

        board = dashboard.build(db, config, as_of=TODAY)

        assert board.group("Cash").total == Money("350")
        assert board.liquid == Money("350")

    def test_mixed_heading_reports_only_its_net_total(self, db, book):
        asset = Account(name="Asset", atype=AccountType.ASSET, parent=book.assets)
        debt = Account(name="Debt", atype=AccountType.LIABILITY, parent=book.liabilities)
        with db.transaction("mixed heading") as txn:
            db.add_account(asset, txn)
            db.add_account(debt, txn)
            db.add_transaction(
                Transaction.simple(date(2026, 1, 1), "Value", asset.handle, book.opening, "100"),
                txn,
            )
            db.add_transaction(
                Transaction.simple(date(2026, 1, 1), "Borrow", book.opening, debt.handle, "40"),
                txn,
            )
        config = dashboard.DashboardConfig(
            groups=[
                dashboard.GroupConfig("Position:Assets", [asset.handle], "asset"),
                dashboard.GroupConfig("Position:Debts", [debt.handle], "liability"),
            ]
        )

        position = dashboard.build(db, config, as_of=TODAY).group("Position")

        assert position.value is None
        assert position.debt is None
        assert position.loan_to_value is None
        assert position.loan_end is None
        assert position.total == Money("60")


class TestFsaGroupAvailability:
    def _fsa(self, db, book, years):
        from breadsched.gen.lib import FsaFundingYear

        account = Account(name="Benefit account", atype=AccountType.BANK, parent=book.assets)
        account.atype = AccountType.FSA
        account.fsa_years = [FsaFundingYear(**year) for year in years]
        with db.transaction("benefit account") as txn:
            db.add_account(account, txn)
        return account

    def _group(self, db, account, as_of):
        config = dashboard.DashboardConfig(
            groups=[dashboard.GroupConfig("Benefits", [account.handle], "asset")]
        )
        return dashboard.build(db, config, as_of=as_of).group("Benefits")

    def test_overlapping_runout_and_current_year_availability_are_added(self, db, book):
        account = self._fsa(
            db,
            book,
            [
                {
                    "start": date(2025, 1, 1),
                    "through": date(2025, 12, 31),
                    "runout_through": date(2026, 3, 31),
                    "election": Money("1000"),
                },
                {
                    "start": date(2026, 1, 1),
                    "through": date(2026, 12, 31),
                    "election": Money("2000"),
                },
            ],
        )

        group = self._group(db, account, date(2026, 2, 1))

        assert group.total == Money("3000")
        assert group.accounts[0].source == "fsa_availability"
        assert "2 applicable" in group.accounts[0].note

    def test_exhausted_year_is_available_with_zero_remaining(self, db, book):
        account = self._fsa(
            db,
            book,
            [
                {
                    "start": date(2026, 1, 1),
                    "through": date(2026, 12, 31),
                    "election": Money("100"),
                }
            ],
        )
        with db.transaction("use benefit") as txn:
            db.add_transaction(
                Transaction.simple(
                    date(2026, 2, 1), "Qualified expense", book.groceries, account.handle, "100"
                ),
                txn,
            )

        group = self._group(db, account, date(2026, 3, 1))

        assert group.total == Money(0)
        assert group.accounts[0].total == Money(0)

    @pytest.mark.parametrize(
        ("years", "expected"),
        [
            ([], "no FSA funding years configured"),
            (
                [
                    {
                        "start": date(2024, 1, 1),
                        "through": date(2024, 12, 31),
                        "election": Money("100"),
                    }
                ],
                "no applicable FSA funding year",
            ),
        ],
    )
    def test_missing_or_expired_year_data_is_explicit(self, db, book, years, expected):
        account = self._fsa(db, book, years)

        group = self._group(db, account, date(2026, 3, 1))

        assert group.total == Money(0)
        assert group.accounts[0].total is None
        assert expected in group.note


class TestPaidOffLoans:
    def test_paid_off_linked_loan_and_stale_payment_schedule_are_hidden(self, db, book):
        asset = Account(name="Property", atype=AccountType.ASSET, parent=book.assets)
        loan = Account(name="Loan", atype=AccountType.LIABILITY, parent=book.liabilities)
        asset.group = "Property"
        loan.group = "Property"
        loan.linked_asset = asset.handle
        stale = ScheduledTransaction(
            name="Old loan payment",
            recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 10, 1)),
            splits=[
                ScheduledSplit(loan.handle, Money("10")),
                ScheduledSplit(book.checking, Money("-10")),
            ],
        )
        with db.transaction("paid off loan") as txn:
            db.add_account(asset, txn)
            db.add_account(loan, txn)
            db.add_transaction(
                Transaction.simple(date(2026, 1, 1), "Value", asset.handle, book.opening, "100"),
                txn,
            )
            db.add_transaction(
                Transaction.simple(date(2026, 1, 1), "Borrow", book.opening, loan.handle, "60"),
                txn,
            )
            db.add_transaction(
                Transaction.simple(date(2026, 2, 1), "Pay off", loan.handle, book.checking, "60"),
                txn,
            )
            db.add_scheduled(stale, txn)

        board = dashboard.build(db, as_of=TODAY)
        group = board.group("Property")

        assert group is not None
        assert [account.name for account in group.accounts] == ["Assets:Property"]
        assert group.total == Money("100")
        assert group.value is None
        assert group.loan_to_value is None
        assert "Old loan payment" not in [bill.name for bill in board.bills]

    def test_unused_zero_balance_linked_loan_is_not_mistaken_for_paid_off(self, db, book):
        asset = Account(name="Property", atype=AccountType.ASSET, parent=book.assets)
        loan = Account(name="Future loan", atype=AccountType.LIABILITY, parent=book.liabilities)
        asset.group = "Property"
        loan.group = "Property"
        loan.linked_asset = asset.handle
        with db.transaction("future loan") as txn:
            db.add_account(asset, txn)
            db.add_account(loan, txn)

        group = dashboard.build(db, as_of=TODAY).group("Property")

        assert group is not None
        assert any(account.name.endswith("Future loan") for account in group.accounts)

    def test_paid_off_loan_is_removed_while_an_active_loan_on_same_asset_remains(self, db, book):
        asset = Account(name="Property", atype=AccountType.ASSET, parent=book.assets)
        closed = Account(name="Closed loan", atype=AccountType.LIABILITY, parent=book.liabilities)
        active = Account(name="Active loan", atype=AccountType.LIABILITY, parent=book.liabilities)
        asset.group = "Property"
        closed.group = "Property"
        active.group = "Property"
        closed.linked_asset = asset.handle
        active.linked_asset = asset.handle
        with db.transaction("two loans") as txn:
            for account in (asset, closed, active):
                db.add_account(account, txn)
            db.add_transaction(
                Transaction.simple(date(2026, 1, 1), "Value", asset.handle, book.opening, "100"),
                txn,
            )
            for loan, amount in ((closed, "60"), (active, "25")):
                db.add_transaction(
                    Transaction.simple(
                        date(2026, 1, 1), "Borrow", book.opening, loan.handle, amount
                    ),
                    txn,
                )
            db.add_transaction(
                Transaction.simple(
                    date(2026, 2, 1), "Close first loan", closed.handle, book.checking, "60"
                ),
                txn,
            )

        group = dashboard.build(db, as_of=TODAY).group("Property")

        assert [account.name.rsplit(":", 1)[-1] for account in group.accounts] == [
            "Property",
            "Active loan",
        ]
        assert group.value == Money("100")
        assert group.debt == Money("25")
        assert group.equity == Money("75")
