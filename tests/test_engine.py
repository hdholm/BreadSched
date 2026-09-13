"""Ledger reading, reporting, and scheduled-transaction execution."""

from datetime import date

import pytest

from breadsched.gen.engine import ledger, schedule
from breadsched.gen.lib import (
    AccountClass,
    Money,
    PeriodType,
    Recurrence,
    ScheduledSplit,
    ScheduledTransaction,
    Transaction,
)


class TestBalances:
    def test_bank_balance_after_a_month(self, db, funded_book):
        # 5000 opening + 4200 salary - 1800 rent - 310.55 groceries
        assert ledger.balance(db, funded_book.checking, as_of=date(2026, 1, 31)) == Money("7089.45")

    def test_credit_card_reports_what_is_owed_as_positive(self, db, funded_book):
        """A liability's natural sign is the amount owed, not the ledger's minus."""
        assert ledger.balance(db, funded_book.card) == Money("86.40")

    def test_raw_sign_is_available_for_arithmetic_that_must_balance(self, db, funded_book):
        assert ledger.balance(db, funded_book.card, natural_sign=False) == Money("-86.40")

    def test_expense_balance_over_a_window(self, db, funded_book):
        assert ledger.balance(
            db, funded_book.rent, since=date(2026, 2, 1), as_of=date(2026, 2, 28)
        ) == Money("1800.00")

    def test_parent_totals_include_children(self, db, funded_book):
        assert ledger.balance_recursive(db, funded_book.expenses, as_of=date(2026, 2, 28)) == Money(
            "3996.95"
        )

    def test_a_placeholder_holds_nothing_of_its_own(self, db, funded_book):
        assert ledger.balance(db, funded_book.expenses) == Money(0)

    def test_every_transaction_nets_to_zero_across_the_book(self, db, funded_book):
        total = Money(0)
        for account in db.iter_accounts():
            total = total + ledger.balance(db, account.handle, natural_sign=False)
        assert total == Money(0)

    def test_net_worth_is_assets_less_liabilities(self, db, funded_book):
        # Checking 5289.45 at the end of February, less 86.40 on the card.
        assert ledger.net_worth(db, as_of=date(2026, 2, 28)) == Money("5203.05")

    def test_cash_on_hand_ignores_investments(self, db, funded_book):
        assert ledger.cash_on_hand(db, as_of=date(2026, 1, 31)) == Money("7089.45")


class TestRegister:
    def test_running_balance_accumulates_in_date_order(self, db, funded_book):
        rows = ledger.register(db, funded_book.checking)
        assert [row.description for row in rows] == [
            "Opening balance",
            "January rent",
            "Weekly shop",
            "January salary",
            "February rent",
        ]
        assert rows[-1].running == Money("5289.45")

    def test_debit_and_credit_columns_are_exclusive(self, db, funded_book):
        rows = ledger.register(db, funded_book.checking)
        rent = next(r for r in rows if r.description == "January rent")
        assert rent.credit == Money("1800.00")
        assert rent.debit is None

    def test_transfer_column_names_the_other_side(self, db, funded_book):
        rows = ledger.register(db, funded_book.checking)
        rent = next(r for r in rows if r.description == "January rent")
        assert rent.transfer_label(db) == "Expenses:Rent"

    def test_a_windowed_register_opens_from_the_prior_balance(self, db, funded_book):
        rows = ledger.register(db, funded_book.checking, start=date(2026, 2, 1))
        assert len(rows) == 1
        assert rows[0].running == Money("5289.45")

    def test_multi_split_transactions_show_as_split(self, db, book):
        from breadsched.gen.lib import Split, Transaction

        with db.transaction("Split purchase") as txn:
            purchase = Transaction(post_date=date(2026, 3, 1), description="Big shop")
            purchase.add_split(Split(book.groceries, Money("60.00")))
            purchase.add_split(Split(book.utilities, Money("40.00")))
            purchase.add_split(Split(book.checking, Money("-100.00")))
            db.add_transaction(purchase, txn)

        rows = ledger.register(db, book.checking)
        assert rows[0].transfer_label(db) == "-- Split --"


class TestTotalsByClass:
    def test_groups_by_account_class(self, db, funded_book):
        totals = ledger.totals_by_class(db, as_of=date(2026, 2, 28))
        assert totals[AccountClass.LIABILITY] == Money("86.40")
        assert totals[AccountClass.INCOME] == Money("4200.00")


class TestScheduleEngine:
    def test_due_list_covers_missed_occurrences(self, db, payday_schedule):
        due = schedule.due_occurrences(db, as_of=date(2026, 2, 1), horizon_days=0)
        assert [occ.when for occ in due] == [
            date(2026, 1, 2),
            date(2026, 1, 16),
            date(2026, 1, 30),
        ]

    def test_posting_writes_real_transactions(self, db, book, payday_schedule):
        posted = schedule.post_due(db, as_of=date(2026, 1, 20))
        assert len(posted) == 2
        assert ledger.balance(db, book.checking) == Money("3876.92")

    def test_posting_twice_does_not_duplicate(self, db, payday_schedule):
        schedule.post_due(db, as_of=date(2026, 1, 20))
        again = schedule.post_due(db, as_of=date(2026, 1, 20))
        assert again == []

    def test_manual_schedules_are_left_alone_by_default(self, db, book, payday_schedule):
        payday_schedule.auto_create = False
        with db.transaction("Make manual") as txn:
            db.commit_scheduled(payday_schedule, txn)
        assert schedule.post_due(db, as_of=date(2026, 1, 20)) == []
        assert len(schedule.post_due(db, as_of=date(2026, 1, 20), only_auto=False)) == 2

    def test_posting_advances_the_last_posted_marker(self, db, payday_schedule):
        schedule.post_due(db, as_of=date(2026, 1, 20))
        assert db.get_scheduled(payday_schedule.handle).last_posted == date(2026, 1, 16)

    def test_forecast_ignores_whether_anything_was_posted(self, db, payday_schedule):
        schedule.post_due(db, as_of=date(2026, 1, 20))
        forecast = schedule.forecast_occurrences(db, date(2026, 1, 1), date(2026, 1, 31))
        assert len(forecast) == 3

    def test_disabled_schedules_do_not_fire(self, db, payday_schedule):
        payday_schedule.enabled = False
        with db.transaction("Disable") as txn:
            db.commit_scheduled(payday_schedule, txn)
        assert schedule.due_occurrences(db, as_of=date(2026, 6, 1)) == []

    def test_a_card_payment_is_an_account_linked_definition(self, db, funded_book):
        card = db.get_account(funded_book.card)
        card.payment_day = 22
        card.card_payment_account = funded_book.checking
        with db.transaction("Configure card payment") as txn:
            db.commit_account(card, txn)

        definitions = schedule.account_payment_definitions(db, as_of=date(2026, 2, 20))

        assert len(definitions) == 1
        payment = definitions[0]
        assert payment.handle == f"account-payment:{card.handle}"
        assert payment.next_due == date(2026, 2, 22)
        assert payment.amount_due == Money("86.40")
        assert [(split.account, split.amount) for split in payment.splits] == [
            (funded_book.card, Money("86.40")),
            (funded_book.checking, Money("-86.40")),
        ]

        assert schedule.post_due(db, as_of=date(2026, 2, 22), only_auto=False) == []
        assert schedule.forecast_occurrences(db, date(2026, 2, 20), date(2026, 3, 31)) == []

    def test_an_overdue_card_stays_due_until_an_actual_payment(self, db, funded_book):
        card = db.get_account(funded_book.card)
        card.payment_day = 22
        card.card_payment_account = funded_book.checking
        with db.transaction("Configure card payment") as txn:
            db.commit_account(card, txn)

        overdue = schedule.account_payment_definitions(db, as_of=date(2026, 2, 25))[0]
        assert overdue.next_due == date(2026, 2, 22)
        assert schedule.upcoming_occurrences(db, as_of=date(2026, 2, 25), horizon_days=0)[
            0
        ].when == date(2026, 2, 22)

        with db.transaction("Pay card") as txn:
            db.add_transaction(
                Transaction.simple(
                    date(2026, 2, 20),
                    "Card payment",
                    funded_book.card,
                    funded_book.checking,
                    "86.40",
                ),
                txn,
            )
        following = schedule.account_payment_definitions(db, as_of=date(2026, 2, 25))[0]
        assert following.next_due == date(2026, 3, 22)
        assert following.amount_due == Money(0)

    def test_a_payment_on_the_due_date_advances_the_card_cycle(self, db, funded_book):
        card = db.get_account(funded_book.card)
        card.payment_day = 22
        card.card_payment_account = funded_book.checking
        with db.transaction("Configure and pay card") as txn:
            db.commit_account(card, txn)
            db.add_transaction(
                Transaction.simple(
                    date(2026, 2, 22),
                    "Card payment",
                    funded_book.card,
                    funded_book.checking,
                    "86.40",
                ),
                txn,
            )

        following = schedule.account_payment_definitions(db, as_of=date(2026, 2, 22))[0]

        assert following.next_due == date(2026, 3, 22)

    def test_a_carried_card_payment_is_capped_at_the_balance(self, db, funded_book):
        card = db.get_account(funded_book.card)
        card.pays_in_full = False
        card.usual_payment = Money("400.00")
        card.payment_day = 22
        with db.transaction("Configure carried card") as txn:
            db.commit_account(card, txn)

        payment = schedule.account_payment_definitions(db, as_of=date(2026, 2, 20))[0]
        assert payment.amount_due == Money("86.40")

    def test_an_explicit_card_schedule_suppresses_the_account_definition(self, db, funded_book):
        card = db.get_account(funded_book.card)
        card.payment_day = 22
        explicit = ScheduledTransaction(
            name="Explicit card payment",
            recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 2, 22)),
            splits=[
                ScheduledSplit(funded_book.card, Money("50.00")),
                ScheduledSplit(funded_book.checking, Money("-50.00")),
            ],
        )
        with db.transaction("Configure explicit card payment") as txn:
            db.commit_account(card, txn)
            db.add_scheduled(explicit, txn)

        assert schedule.account_payment_definitions(db, as_of=date(2026, 2, 20)) == []

    def test_a_completed_bounded_card_schedule_no_longer_suppresses_the_account_definition(
        self, db, funded_book
    ):
        card = db.get_account(funded_book.card)
        card.payment_day = 22
        explicit = ScheduledTransaction(
            name="Finished card payment",
            recurrence=Recurrence(PeriodType.ONCE, start=date(2026, 1, 22)),
            splits=[
                ScheduledSplit(funded_book.card, Money("50.00")),
                ScheduledSplit(funded_book.checking, Money("-50.00")),
            ],
        )
        explicit.last_posted = date(2026, 1, 22)
        with db.transaction("Store completed card schedule") as txn:
            db.commit_account(card, txn)
            db.add_scheduled(explicit, txn)

        definitions = schedule.account_payment_definitions(db, as_of=date(2026, 2, 20))

        assert [definition.account for definition in definitions] == [funded_book.card]

    def test_duplicate_has_independent_identity_and_pending_state(self, payday_schedule):
        payday_schedule.last_posted = date(2026, 1, 16)
        payday_schedule.skipped = [date(2026, 1, 30)]

        copied = schedule.duplicate_definition(payday_schedule)

        assert copied.handle != payday_schedule.handle
        assert copied.name == "Salary copy"
        assert copied.last_posted is None
        assert copied.skipped == []
        assert [split.serialize() for split in copied.splits] == [
            split.serialize() for split in payday_schedule.splits
        ]

    def test_protected_definition_can_be_saved_as_an_exact_copy(self, db, payday_schedule):
        payday_schedule.unsupported_reason = "custom recurrence"
        payday_schedule.source_recurrence = {"source": "custom-cycle"}
        with db.transaction("Protect custom schedule") as txn:
            db.commit_scheduled(payday_schedule, txn)

        copied = schedule.duplicate_saved_definition(
            db, payday_schedule.handle, name="Reviewed custom copy"
        )

        assert copied.handle != payday_schedule.handle
        assert copied.name == "Reviewed custom copy"
        assert copied.unsupported_reason == "custom recurrence"
        assert copied.source_recurrence == {"source": "custom-cycle"}
        assert db.get_scheduled(copied.handle) is not None

    def test_actual_becomes_an_unsaved_one_time_template(self, db, book):
        from breadsched.gen.lib import PlanningFlowKind, Split, Transaction

        actual = Transaction(post_date=date(2026, 4, 9), description="Annual service")
        actual.add_split(
            Split(
                book.utilities,
                Money("90.00"),
                memo="service leg",
                planning_flow=PlanningFlowKind.BENEFIT_FUNDING,
            )
        )
        actual.add_split(Split(book.checking, Money("-90.00"), memo="cash leg"))

        draft = schedule.from_transaction(actual)

        assert draft.recurrence.period.value == "once"
        assert draft.recurrence.start == actual.post_date
        assert [split.account for split in draft.splits] == [book.utilities, book.checking]
        assert [split.memo for split in draft.splits] == ["service leg", "cash leg"]
        assert draft.splits[0].planning_flow is PlanningFlowKind.BENEFIT_FUNDING

    def test_deletion_is_atomic_and_undoable(self, db, payday_schedule):
        deleted = schedule.delete_definition(db, payday_schedule.handle)

        assert deleted.handle == payday_schedule.handle
        assert db.get_scheduled(payday_schedule.handle) is None
        assert db.undo() is True
        assert db.get_scheduled(payday_schedule.handle) is not None

    def test_deletion_refuses_live_scenario_overrides(self, db, payday_schedule):
        from breadsched.gen.lib import Scenario, ScenarioSchedule

        scenario = Scenario(name="Alternative")
        scenario.schedule_overrides.append(ScenarioSchedule.from_scheduled(payday_schedule))
        with db.transaction("Add scenario") as txn:
            db.add_scenario(scenario, txn)

        with pytest.raises(ValueError, match="Alternative"):
            schedule.delete_definition(db, payday_schedule.handle)
        assert db.get_scheduled(payday_schedule.handle) is not None


class TestScheduledTemplates:
    def test_a_formula_split_resolves_at_instantiation(self, db, book):
        from breadsched.gen.lib import (
            PeriodType,
            Recurrence,
            ScheduledSplit,
            ScheduledTransaction,
        )

        sched = ScheduledTransaction(
            name="Interest",
            recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 1, 1)),
            splits=[
                ScheduledSplit(book.utilities, formula="principal * rate / 12"),
                ScheduledSplit(book.checking, formula="-(principal * rate / 12)"),
            ],
        )
        sched.variables = {"principal": "240000", "rate": "0.05"}
        txn = sched.instantiate(date(2026, 1, 1))
        assert txn.is_balanced()
        assert txn.value_for(book.utilities).quantize(100) == Money("1000.00")

    def test_instantiated_transactions_remember_their_schedule(self, db, payday_schedule):
        txn = payday_schedule.instantiate(date(2026, 1, 2))
        assert txn.scheduled_from == payday_schedule.handle


class TestUnbalancedSchedules:
    """A schedule whose calculated legs disagree must not stop a forecast."""

    @pytest.fixture
    def lopsided(self, db, book):
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
                ScheduledSplit(book.rent, formula="1000 + 200"),
                # References something only GnuCash can supply.
                ScheduledSplit(book.checking, formula="-(principal * rate)"),
            ],
        )
        with db.transaction("Add") as txn:
            db.add_scheduled(sched, txn)
        return sched

    def test_the_imbalance_is_measurable(self, lopsided):
        assert lopsided.imbalance() == Money("1200.00")

    def test_strict_instantiation_still_refuses(self, lopsided):
        from breadsched.gen.lib import UnbalancedError

        with pytest.raises(UnbalancedError):
            lopsided.instantiate(date(2026, 1, 1))

    def test_lenient_instantiation_is_allowed_for_forecasting(self, lopsided):
        txn = lopsided.instantiate(date(2026, 1, 1), strict=False)
        assert len(txn.splits) == 2

    def test_a_projection_completes_and_says_which_schedule_is_wrong(self, db, book, lopsided):
        from breadsched.gen.engine import projection
        from breadsched.gen.lib import Scenario

        scenario = Scenario(
            name="With a broken schedule",
            start=date(2026, 1, 1),
            years=2,
        )
        result = projection.project(db, scenario)

        assert len(result.rows) == 24, "the projection must still run"
        assert any("Mortgage" in warning for warning in result.warnings)
        assert any("excluded from projection" in warning for warning in result.warnings)

    def test_the_warning_is_not_repeated_for_every_month(self, db, book, lopsided):
        from breadsched.gen.engine import projection
        from breadsched.gen.lib import Scenario

        scenario = Scenario(
            name="Long",
            start=date(2026, 1, 1),
            years=10,
        )
        result = projection.project(db, scenario)
        assert len([w for w in result.warnings if "Mortgage" in w]) == 1
