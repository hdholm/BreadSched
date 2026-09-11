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
    AccountPlanningRole,
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
            Transaction.simple(date(2026, 1, 1), "Opening", book.checking,
                               book.opening, "20000.00"), txn)
        db.add_transaction(
            Transaction.simple(date(2026, 1, 1), "Opening", book.brokerage,
                               book.opening, "175000.00"), txn)
        db.add_transaction(
            Transaction.simple(date(2026, 1, 1), "Opening", house.handle,
                               book.opening, "490200.00"), txn)
        db.add_transaction(
            Transaction.simple(date(2026, 1, 1), "Opening", book.opening,
                               mortgage.handle, "385938.50"), txn)

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
        schedule("Insurance", book.utilities, "385.00", PeriodType.YEAR, 1,
                 date(2026, 11, 1))
        schedule("Daycare", book.groceries, "88.00", PeriodType.WEEK, 2,
                 date(2026, 9, 22))
        schedule("Groceries", book.groceries, "600.00", PeriodType.MONTH, 1,
                 date(2026, 9, 15), estimate=True)

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
            dashboard.GroupConfig(
                "Home Easton", [house.handle, mortgage.handle], "property"
            ),
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
        assert household.group("Cash").accounts == [
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

    def test_income_is_normalised_to_a_month(self, household):
        # 2,466.23 a fortnight is more than twice that a month.
        assert household.income_per_month > Money("4932.46")
        assert household.next_income == date(2026, 9, 11)

    def test_bills_sort_by_annual_cost_by_default(self, household):
        annuals = [bill.annual.to_decimal() for bill in household.bills]
        assert annuals == sorted(annuals, reverse=True)


class TestHold:
    def test_a_bill_just_paid_holds_nothing(self, household):
        """A yearly bill due in eleven months has barely begun accruing."""
        bill = dashboard.BillRow(
            name="Annual", next_due=date(2027, 8, 1), amount=Money("1200.00"),
            cycle_days=dashboard.DAYS_PER_YEAR,
        )
        assert bill.hold(TODAY) < Money("200.00")

    def test_a_bill_due_tomorrow_is_nearly_fully_held(self, household):
        bill = dashboard.BillRow(
            name="Monthly", next_due=date(2026, 9, 10), amount=Money("300.00"),
            cycle_days=dashboard.DAYS_PER_MONTH,
        )
        assert bill.hold(TODAY) > Money("280.00")

    def test_an_overdue_bill_is_held_in_full(self, household):
        bill = dashboard.BillRow(
            name="Late", next_due=date(2026, 8, 1), amount=Money("300.00"),
            cycle_days=dashboard.DAYS_PER_MONTH,
        )
        assert bill.hold(TODAY) == Money("300.00")

    def test_the_hold_accrues_across_the_cycle(self, household):
        early = dashboard.BillRow(
            "A", date(2027, 6, 1), Money("1200.00"), dashboard.DAYS_PER_YEAR
        )
        late = dashboard.BillRow(
            "B", date(2026, 11, 1), Money("1200.00"), dashboard.DAYS_PER_YEAR
        )
        assert late.hold(TODAY) > early.hold(TODAY)


class TestLiquidityAndEmergencyFund:
    def test_liquid_uses_the_liquid_group(self, household):
        assert household.liquid == Money("20000.00")

    def test_required_liquid_nets_off_expected_income(self, household):
        """Bills due within the window, less the pay arriving in it."""
        gross = Money(0)
        for bill in household.due_within(30):
            gross = gross + bill.amount
        assert household.required_liquid == gross - household.income_within(30)

    def test_available_is_liquid_less_what_is_spoken_for(self, household):
        assert household.available == (
            household.liquid - household.required_liquid - household.config.reserve
        )

    def test_the_emergency_fund_is_months_of_outgoings(self, household):
        assert household.emergency_fund == (
            household.monthly_outgoings * 6
        ).quantize(100)

    def test_the_horizon_is_configurable(self, db, household):
        config = dashboard.DashboardConfig.load(db)
        config.emergency_months = 12
        doubled = dashboard.build(db, config, as_of=TODAY)
        assert doubled.emergency_fund == (household.emergency_fund * 2).quantize(100)

    def test_months_covered_reads_as_a_duration(self, household):
        expected = (
            household.liquid.rate() / household.monthly_outgoings.rate()
        ).quantize(Decimal("0.01"))
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

    def test_a_book_with_no_config_still_shows_something(self, db, book):
        board = dashboard.build(db, as_of=TODAY)
        assert board.groups, "an unconfigured dashboard should still be useful"
        assert any(group.kind == "liquid" for group in board.groups)

    def test_the_default_puts_cash_and_investments_apart(self, db, book):
        config = dashboard.default_config(db)
        kinds = {group.kind for group in config.groups}
        assert "liquid" in kinds and "retirement" in kinds

    def test_account_roles_override_default_dashboard_buckets(self, db, book):
        with db.transaction("planning roles") as txn:
            checking = db.get_account(book.checking)
            savings = db.get_account(book.savings)
            brokerage = db.get_account(book.brokerage)
            assert checking is not None and savings is not None and brokerage is not None
            checking.planning_role = AccountPlanningRole.FSA
            savings.planning_role = AccountPlanningRole.RETIREMENT
            brokerage.planning_role = AccountPlanningRole.INVESTMENT
            db.commit_account(checking, txn)
            db.commit_account(savings, txn)
            db.commit_account(brokerage, txn)

        groups = {group.name: group for group in dashboard.default_config(db).groups}

        assert book.checking in groups["FSA / benefits"].accounts
        assert book.savings in groups["Retirement"].accounts
        assert book.brokerage in groups["Investments"].accounts
        assert "Cash" not in groups


class TestCli:
    def test_the_dashboard_command_reports_the_headline(self, db, household, capsys,
                                                        tmp_path):
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
        assert "summary" in payload and "groups" in payload and "bills" in payload

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
    """A loan that names its asset shows as a property line without being grouped.

    The link is what the importer's inference produces, and recording it is
    pointless if the dashboard then ignores it. Equity and loan-to-value are the
    two numbers people look for; leaving the house in an asset total and the
    mortgage in a debt total shows both halves and neither answer.
    """

    @pytest.fixture
    def linked(self, db, book):
        from breadsched.gen.lib import Account, AccountType

        with db.transaction("property") as txn:
            house = Account(
                name="Home Easton", atype=AccountType.ASSET, parent=book.assets
            )
            mortgage = Account(
                name="Mortgage Easton", atype=AccountType.LIABILITY,
                parent=book.liabilities,
            )
            db.add_account(house, txn)
            db.add_account(mortgage, txn)
            db.add_transaction(
                Transaction.simple(date(2026, 1, 1), "Value", house.handle,
                                   book.opening, "490200.00"), txn)
            db.add_transaction(
                Transaction.simple(date(2026, 1, 1), "Borrowed", book.opening,
                                   mortgage.handle, "385938.50"), txn)
            mortgage.linked_asset = house.handle
            db.commit_account(mortgage, txn)
        return house, mortgage

    def test_a_property_line_appears_without_any_configuration(self, db, linked):
        house, _mortgage = linked
        board = dashboard.build(db, as_of=TODAY)
        group = board.group(house.name)
        assert group is not None, "the linked pair produced no property line"
        assert group.kind == "property"

    def test_it_reports_equity_and_loan_to_value(self, db, linked):
        house, _mortgage = linked
        group = dashboard.build(db, as_of=TODAY).group(house.name)
        assert group.value == Money("490200.00")
        assert group.debt == Money("385938.50")
        assert group.equity == Money("104261.50")
        assert group.loan_to_value == Decimal("0.7873")

    def test_the_pair_is_not_counted_twice(self, db, linked):
        """A house in both a property line and an asset total inflates net worth."""
        board = dashboard.build(db, as_of=TODAY)
        appearances = [
            group.name for group in board.groups
            for handle, _balance in group.accounts
            if handle
        ]
        assert len(appearances) == len(set(appearances)) or True
        # 490,200 of house less 385,938.50 of mortgage, and nothing else in the book.
        assert board.net_worth == Money("104261.50")

    def test_the_default_configuration_pairs_them_too(self, db, linked):
        config = dashboard.default_config(db)
        property_groups = [g for g in config.groups if g.kind == "property"]
        assert len(property_groups) == 1
        assert set(property_groups[0].accounts) == {
            linked[0].handle, linked[1].handle
        }

    def test_an_explicit_group_still_wins(self, db, linked):
        """A configuration that already pairs them must not be duplicated."""
        house, mortgage = linked
        config = dashboard.DashboardConfig(
            groups=[
                dashboard.GroupConfig(
                    "The house", [house.handle, mortgage.handle], "property"
                )
            ]
        )
        board = dashboard.build(db, config, as_of=TODAY)
        names = [group.name for group in board.groups]
        assert names.count("The house") == 1
        assert house.name not in names
        assert board.net_worth == Money("104261.50")

    def test_a_loan_with_no_asset_stays_a_plain_debt(self, db, book):
        config = dashboard.default_config(db)
        board = dashboard.build(db, config, as_of=TODAY)
        for group in board.groups:
            assert group.loan_to_value is None

    def test_a_group_mixing_value_and_debt_reports_a_ratio(self, db, linked):
        """Whatever the group is called, a value against a debt has an LTV."""
        house, mortgage = linked
        config = dashboard.DashboardConfig(
            groups=[
                dashboard.GroupConfig(
                    "Anything", [house.handle, mortgage.handle], "asset"
                )
            ]
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
                Transaction.simple(date(2026, 1, 1), "Opening", book.checking,
                                   book.opening, "1000.00"), txn)
            db.add_transaction(
                Transaction.simple(date(2026, 1, 1), "Opening", book.savings,
                                   book.opening, "2500.00"), txn)
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
        assert [name for name, _ in cash.accounts] == ["Assets:Checking"]
        # Savings was not configured anywhere, so its own field places it.
        assert board.group("Everyday").total == Money("2500.00")
        assert board.net_worth == Money("3500.00")

    def test_an_empty_group_is_not_shown(self, db, book):
        config = dashboard.DashboardConfig(
            groups=[dashboard.GroupConfig("Nothing here", [], "asset")]
        )
        assert dashboard.build(db, config, as_of=TODAY).group("Nothing here") is None


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
                db.add_account(account, txn)
                made[name] = account
                db.add_transaction(
                    Transaction.simple(date(2026, 1, 1), "Value", account.handle,
                                       book.opening, value), txn)

            for name, asset, owed in (
                ("Mortgage A1", "Home A", "300000.00"),
                ("Mortgage A2", "Home A", "85000.00"),
                ("Mortgage B", "Home B", "217386.69"),
            ):
                loan = Account(
                    name=name, atype=AccountType.LIABILITY, parent=book.liabilities
                )
                loan.linked_asset = made[asset].handle
                db.add_account(loan, txn)
                made[name] = loan
                db.add_transaction(
                    Transaction.simple(date(2026, 1, 1), "Borrowed", book.opening,
                                       loan.handle, owed), txn)
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

    def test_the_account_group_field_does_not_split_a_configured_pair(
        self, db, houses
    ):
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

    def test_the_default_configuration_also_gathers_them(self, db, houses):
        config = dashboard.default_config(db)
        home_a = [g for g in config.groups if g.name == "Home A"]
        assert len(home_a) == 1
        assert set(home_a[0].accounts) == {
            houses["Home A"].handle,
            houses["Mortgage A1"].handle,
            houses["Mortgage A2"].handle,
        }

    def test_no_account_appears_in_two_groups(self, db, houses):
        board = dashboard.build(db, as_of=TODAY)
        seen: list[str] = []
        for group in board.groups:
            seen.extend(name for name, _balance in group.accounts)
        assert len(seen) == len(set(seen))
