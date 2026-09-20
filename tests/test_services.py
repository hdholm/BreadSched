"""Application-service contracts shared by presentation adapters."""

from datetime import date

from breadsched.gen.engine.activity import PlanMeasure, ReportingPeriod
from breadsched.gen.lib import (
    DEFAULT_CURRENCY_HANDLE,
    Account,
    AccountType,
    Amount,
    AssumptionPeriod,
    Assumptions,
    Commodity,
    InvestmentActivityKind,
    Money,
    PeriodType,
    PlanningFlowKind,
    PlanningResolution,
    Recurrence,
    Scenario,
    ScenarioSchedule,
    ScheduledSplit,
    Transaction,
)
from breadsched.gen.lib.scheduled import ScheduledTransaction
from breadsched.gen.plug import remembered_import_source
from breadsched.gen.services import (
    BASE_SCENARIO,
    ClaimAttachment,
    DeleteAccount,
    DeleteAssumptionPeriod,
    DeleteScenario,
    DeleteSchedule,
    DeleteTransaction,
    DuplicateScenario,
    DuplicateSchedule,
    FixedScheduleInput,
    FixedSplitInput,
    FormulaScenarioScheduleInput,
    FormulaScheduleInput,
    ImportBook,
    PlanQuery,
    ReviewOccurrence,
    ReviewTransaction,
    SaveAccount,
    SaveAssumptionPeriod,
    SaveBaseAssumptions,
    SaveFixedScenarioSchedule,
    SaveFixedSchedule,
    SaveScenario,
    SaveScenarioAssumptions,
    SaveScenarioSchedule,
    SaveSchedule,
    SaveTransaction,
    ServiceError,
    SuppressScenarioSchedule,
    TransactionInput,
    TransactionSplitInput,
    build_fixed_schedule,
    build_formula_scenario_schedule,
    delete_account,
    delete_assumption_period,
    delete_scenario,
    delete_schedule,
    delete_transaction,
    duplicate_scenario,
    duplicate_schedule,
    import_book,
    mark_review_unexpected,
    match_review,
    query_plan,
    reject_review,
    save_account,
    save_assumption_period,
    save_base_assumptions,
    save_fixed_scenario_schedule,
    save_fixed_schedule,
    save_formula_scenario_schedule,
    save_formula_schedule,
    save_scenario,
    save_scenario_assumptions,
    save_scenario_schedule,
    save_schedule,
    save_transaction,
    skip_review,
    suppress_scenario_schedule,
    transaction_currency,
)


def _transaction_request(db, book, **changes) -> SaveTransaction:
    currency = transaction_currency(db)
    values = {
        "post_date": date(2026, 2, 1),
        "description": "Typed entry",
        "splits": (
            TransactionSplitInput(book.rent, Amount(Money("125"), currency)),
            TransactionSplitInput(book.checking, Amount(Money("-125"), currency)),
        ),
        "currency": currency,
    }
    values.update(changes)
    return SaveTransaction(TransactionInput(**values))


def test_import_service_selects_runs_and_remembers_the_importer(db, tmp_path):
    source = tmp_path / "statement.qif"
    source.write_text(
        "!Account\nNImported checking\nTBank\n^\n!Type:Bank\nD01/02/2026\nT12.50\nPExample\n^\n"
    )

    outcome = import_book(
        db,
        ImportBook(str(source), number_format="dot", date_format="month-first"),
    )

    assert outcome.ok
    assert outcome.value is not None
    assert outcome.value.format_id == "qif"
    assert outcome.value.result.transactions == 1
    assert remembered_import_source(db) == str(source.resolve())


def test_import_service_returns_stable_preflight_errors_without_running(db, tmp_path):
    missing = import_book(db, ImportBook(str(tmp_path / "missing.qif")))
    invalid = tmp_path / "statement.qif"
    invalid.write_text("!Type:Bank\n")
    option = import_book(db, ImportBook(str(invalid), number_format="guess"))

    assert missing.errors == (ServiceError("import.source.not_found", ("source",)),)
    assert option.errors == (ServiceError("import.number_format.invalid", ("number_format",)),)
    assert remembered_import_source(db) is None


def test_account_service_atomically_adds_an_opening_balance(db, book):
    account = Account(name="New savings", atype=AccountType.BANK, parent=book.assets)

    result = save_account(
        db,
        SaveAccount(
            account,
            opening_balance=Money("250"),
            opening_date=date(2026, 2, 1),
        ),
    )

    assert result.ok
    stored = db.get_account(account.handle)
    assert stored is not None
    opening = next(
        txn for txn in db.iter_transactions() if txn.description == "New savings opening balance"
    )
    assert opening.value_for(account.handle) == Money("250")
    assert db.undo_stack[-1].message == "Add account New savings"


def test_account_service_validates_parent_identity_and_source_owned_fields(db, book):
    duplicate = Account(name="Savings", atype=AccountType.BANK, parent=book.assets)
    duplicate_result = save_account(db, SaveAccount(duplicate))
    imported = db.get_account(book.checking)
    assert imported is not None
    imported.source_guid = "source-account"
    with db.transaction("Imported identity") as txn:
        db.commit_account(imported, txn)
    imported.name = "Renamed"
    source_result = save_account(db, SaveAccount(imported, existing_handle=imported.handle))

    assert duplicate_result.errors == (ServiceError("account.name.duplicate", ("name", "parent")),)
    assert source_result.errors == (ServiceError("account.source_fields.read_only", ("name",)),)


def test_account_service_deletes_unused_accounts_and_rejects_accounts_in_use(db, book):
    unused = Account(name="Unused", atype=AccountType.BANK, parent=book.assets)
    assert save_account(db, SaveAccount(unused)).ok

    deleted = delete_account(db, DeleteAccount(unused.handle))
    in_use = delete_account(db, DeleteAccount(book.assets))
    missing = delete_account(db, DeleteAccount("missing"))

    assert deleted.ok
    assert in_use.errors == (ServiceError("account.in_use", ("handle",)),)
    assert missing.errors == (ServiceError("account.not_found", ("handle",)),)


def _review_fixture(db, book):
    schedule = ScheduledTransaction(
        name="Review utility",
        recurrence=Recurrence(period=PeriodType.MONTH, start=date(2026, 5, 7)),
        splits=[
            ScheduledSplit(book.utilities, Money("100")),
            ScheduledSplit(book.checking, Money("-100")),
        ],
    )
    actual = Transaction.simple(
        date(2026, 5, 8), "Actual utility", book.utilities, book.checking, Money("105")
    )
    with db.transaction("Review fixture") as txn:
        db.add_scheduled(schedule, txn)
        db.add_transaction(actual, txn)
    return actual.handle, schedule.occurrence_key(date(2026, 5, 7)), schedule.handle


def test_review_service_matches_an_actual_atomically(db, book):
    transaction, occurrence, _schedule = _review_fixture(db, book)

    result = match_review(db, ReviewOccurrence(transaction, occurrence))

    assert result.ok
    saved = db.get_transaction(transaction)
    assert saved is not None
    assert saved.planning_resolution is PlanningResolution.MATCHED
    assert saved.planned_occurrence == occurrence


def test_review_service_rejects_and_skips_candidates(db, book):
    transaction, occurrence, schedule = _review_fixture(db, book)

    rejected = reject_review(db, ReviewOccurrence(transaction, occurrence))
    skipped = skip_review(db, ReviewOccurrence(transaction, occurrence))

    assert rejected.ok and skipped.ok
    saved = db.get_transaction(transaction)
    assert saved is not None and occurrence in saved.rejected_plan_occurrences
    stored_schedule = db.get_scheduled(schedule)
    assert stored_schedule is not None and date(2026, 5, 7) in stored_schedule.skipped


def test_review_service_marks_an_actual_unexpected(db, book):
    transaction, _occurrence, _schedule = _review_fixture(db, book)

    result = mark_review_unexpected(db, ReviewTransaction(transaction))

    assert result.ok
    saved = db.get_transaction(transaction)
    assert saved is not None
    assert saved.planning_resolution is PlanningResolution.UNEXPECTED


def test_review_service_returns_stable_errors_without_writing(db, book):
    transaction, occurrence, _schedule = _review_fixture(db, book)
    assert mark_review_unexpected(db, ReviewTransaction(transaction)).ok

    repeated = match_review(db, ReviewOccurrence(transaction, occurrence))
    missing = reject_review(db, ReviewOccurrence("missing", occurrence))

    assert repeated.errors == (ServiceError("review.transaction.not_unresolved", ("transaction",)),)
    assert missing.errors == (ServiceError("review.transaction.not_found", ("transaction",)),)


def test_scenario_service_owns_save_duplicate_and_delete(db):
    scenario = Scenario(name="Alternative", start=date(2026, 1, 1), years=2)

    saved = save_scenario(db, SaveScenario(scenario))
    duplicated = duplicate_scenario(db, DuplicateScenario(scenario))

    assert saved.ok and duplicated.ok
    assert duplicated.value is not None
    assert duplicated.value.name == "Alternative copy"
    deleted = delete_scenario(db, DeleteScenario(duplicated.value.handle))
    assert deleted.ok
    assert db.get_scenario(duplicated.value.handle) is None


def test_scenario_service_returns_stable_name_parent_and_child_errors(db):
    parent = Scenario(name="Parent")
    assert save_scenario(db, SaveScenario(parent)).ok
    duplicate = Scenario(name="Parent")
    duplicate_result = save_scenario(db, SaveScenario(duplicate))
    child = Scenario.derived_from_base(
        parent.effective_assumptions(), name="Child", parent_handle=parent.handle
    )
    assert save_scenario(db, SaveScenario(child)).ok
    delete_result = delete_scenario(db, DeleteScenario(parent.handle))
    parent.parent_handle = child.handle
    parent.inherits_base_assumptions = True
    cycle_result = save_scenario(db, SaveScenario(parent, existing_handle=parent.handle))

    assert duplicate_result.errors == (ServiceError("scenario.name.duplicate", ("name",)),)
    assert delete_result.errors == (ServiceError("scenario.children.exist", ("handle",)),)
    assert cycle_result.errors == (ServiceError("scenario.parent.cycle", ("parent",)),)


def test_scenario_service_suppresses_a_baseline_schedule(db, book):
    scenario = Scenario(name="Alternative")
    schedule = _monthly_schedule(book)
    with db.transaction("Scenario fixture") as txn:
        db.add_scenario(scenario, txn)
        db.add_scheduled(schedule, txn)

    result = suppress_scenario_schedule(
        db, SuppressScenarioSchedule(scenario.handle, schedule.handle)
    )

    assert result.ok
    stored = db.get_scenario(scenario.handle)
    assert stored is not None
    assert len(stored.schedule_overrides) == 1
    assert stored.schedule_overrides[0].source_schedule == schedule.handle
    assert stored.schedule_overrides[0].enabled is False


def test_schedule_service_owns_duplicate_and_delete(db, book):
    schedule = _monthly_schedule(book)
    assert save_schedule(db, SaveSchedule(schedule)).ok

    duplicated = duplicate_schedule(db, DuplicateSchedule(schedule.handle, name="Monthly copy"))
    deleted = delete_schedule(db, DeleteSchedule(schedule.handle))

    assert duplicated.value is not None
    assert duplicated.value.name == "Monthly copy"
    assert deleted.value is not None
    assert db.get_scheduled(schedule.handle) is None


def test_schedule_delete_rejects_live_scenario_references(db, book):
    schedule = _monthly_schedule(book)
    scenario = Scenario(name="Alternative")
    scenario.schedule_overrides.append(ScenarioSchedule.from_scheduled(schedule))
    assert save_schedule(db, SaveSchedule(schedule)).ok
    assert save_scenario(db, SaveScenario(scenario)).ok

    result = delete_schedule(db, DeleteSchedule(schedule.handle))

    assert result.errors == (ServiceError("schedule.scenario_reference.exists", ("handle",)),)
    assert db.get_scheduled(schedule.handle) is not None


def test_assumption_service_persists_valid_base_rates(db, book):
    assumptions = Assumptions(investment_return="0.06", per_account={book.brokerage: "0.08"})

    result = save_base_assumptions(db, SaveBaseAssumptions(assumptions))

    assert result.ok
    assert db.get_metadata("planning.base_assumptions", {}) == assumptions.serialize()


def test_assumption_service_returns_stable_rate_and_account_errors(db, book):
    assumptions = Assumptions(
        income_growth="2", per_account={book.checking: "0.01", "missing": "0.02"}
    )

    result = save_base_assumptions(db, SaveBaseAssumptions(assumptions))

    assert result.errors == (
        ServiceError("assumptions.rate.out_of_range", ("income_growth",)),
        ServiceError("assumptions.account.unsupported", (f"per_account.{book.checking}",)),
        ServiceError("assumptions.account.not_found", ("per_account.missing",)),
    )
    assert db.get_metadata("planning.base_assumptions", None) is None


def test_assumption_period_service_owns_add_replace_and_delete(db, book):
    scenario = Scenario(name="Regimes")
    assert save_scenario(db, SaveScenario(scenario)).ok
    first = AssumptionPeriod(date(2027, 1, 1), investment_return="0.04")
    replacement = AssumptionPeriod(
        date(2027, 1, 1), investment_return="0.05", per_account={book.brokerage: "0.07"}
    )

    added = save_assumption_period(db, SaveAssumptionPeriod(scenario.handle, first))
    replaced = save_assumption_period(
        db, SaveAssumptionPeriod(scenario.handle, replacement, index=0)
    )
    deleted = delete_assumption_period(db, DeleteAssumptionPeriod(scenario.handle, 0))

    assert added.ok and replaced.ok and deleted.ok
    assert db.get_scenario(scenario.handle).assumption_periods == []


def test_assumption_period_service_rejects_stale_identity_and_index(db):
    period = AssumptionPeriod(date(2027, 1, 1), cash_interest="0.03")

    missing = save_assumption_period(db, SaveAssumptionPeriod("missing", period))
    scenario = Scenario(name="Regimes")
    assert save_scenario(db, SaveScenario(scenario)).ok
    stale = delete_assumption_period(db, DeleteAssumptionPeriod(scenario.handle, 0))

    assert missing.errors == (ServiceError("assumptions.scenario.not_found", ("scenario",)),)
    assert stale.errors == (ServiceError("assumptions.period.not_found", ("index",)),)


def test_scenario_assumption_service_validates_horizon_and_dated_rates(db):
    scenario = Scenario(name="Invalid assumptions", years=101)
    scenario.assumption_periods.append(AssumptionPeriod(date(2027, 1, 1), investment_return="1.01"))

    result = save_scenario_assumptions(db, SaveScenarioAssumptions(scenario))

    assert result.errors == (
        ServiceError("assumptions.years.out_of_range", ("years",)),
        ServiceError(
            "assumptions.rate.out_of_range",
            ("periods.0.investment_return",),
        ),
    )
    assert db.get_scenario(scenario.handle) is None


def test_transaction_service_owns_construction_validation_and_atomic_write(db, book):
    result = save_transaction(db, _transaction_request(db, book))

    assert result.ok
    assert result.value is not None
    stored = db.get_transaction(result.value.handle)
    assert stored is not None
    assert stored.description == "Typed entry"
    assert stored.value_for(book.rent) == Money("125")
    assert stored.currency == transaction_currency(db)
    assert db.undo_stack[-1].message == "Add Typed entry"


def test_transaction_service_adds_default_currency_atomically_for_legacy_empty_book(db, book):
    with db.transaction("Simulate legacy empty book") as txn:
        db.remove_commodity(DEFAULT_CURRENCY_HANDLE, txn)
        db.set_metadata("default_currency", None, txn)

    result = save_transaction(db, _transaction_request(db, book))

    assert result.ok
    currency = db.get_commodity(DEFAULT_CURRENCY_HANDLE)
    assert currency is not None
    assert currency.mnemonic == "USD"
    assert db.get_metadata("default_currency") == DEFAULT_CURRENCY_HANDLE


def test_transaction_service_owns_delete_and_reports_stale_handles(db, book):
    saved = save_transaction(db, _transaction_request(db, book))
    assert saved.value is not None

    deleted = delete_transaction(db, DeleteTransaction(saved.value.handle))
    stale = delete_transaction(db, DeleteTransaction(saved.value.handle))

    assert deleted.value == saved.value
    assert stale.errors == (ServiceError("transaction.not_found", ("handle",)),)


def test_transaction_service_returns_stable_errors_without_partial_write(db, book):
    hidden = db.get_account(book.rent)
    assert hidden is not None
    hidden.hidden = True
    with db.transaction("Hide category") as txn:
        db.commit_account(hidden, txn)
    before = db.summary()["txn"]

    result = save_transaction(db, _transaction_request(db, book))

    assert result.value is None
    assert ServiceError("transaction.account.hidden", ("splits.0.account",)) in result.errors
    assert db.summary()["txn"] == before


def test_transaction_service_preserves_hidden_accounts_and_source_metadata_on_edit(db, book):
    hidden = Account(name="Archived", atype=AccountType.EXPENSE, parent=book.expenses, hidden=True)
    source = Transaction.simple(
        date(2026, 1, 1), "Imported entry", hidden.handle, book.checking, Money("20")
    )
    source.source_notes = "Imported provenance"
    source.splits[0].quantity = Money("2.5")
    with db.transaction("Fixture") as txn:
        db.add_account(hidden, txn)
        db.add_transaction(source, txn)

    result = save_transaction(
        db,
        SaveTransaction(
            TransactionInput(
                post_date=date(2026, 1, 2),
                description="Corrected entry",
                splits=tuple(
                    TransactionSplitInput(
                        split.account,
                        Amount(split.value, transaction_currency(db)),
                        handle=split.handle,
                        memo=split.memo,
                    )
                    for split in source.splits
                ),
            ),
            existing_handle=source.handle,
        ),
    )

    assert result.ok
    stored = db.get_transaction(source.handle)
    assert stored is not None
    assert stored.source_notes == "Imported provenance"
    assert stored.splits[0].quantity == Money("2.5")


def test_transaction_service_rejects_unbalanced_and_unknown_accounts(db, book):
    currency = transaction_currency(db)
    request = _transaction_request(
        db,
        book,
        splits=(
            TransactionSplitInput("missing", Amount(Money("100"), currency)),
            TransactionSplitInput(book.checking, Amount(Money("-90"), currency)),
        ),
    )

    result = save_transaction(db, request)

    assert result.errors == (
        ServiceError("transaction.account.not_found", ("splits.0.account",)),
        ServiceError("transaction.unbalanced", ("splits",)),
    )


def test_transaction_service_rejects_unlike_value_commodities_without_netting(db, book):
    currency = transaction_currency(db)
    request = _transaction_request(
        db,
        book,
        splits=(
            TransactionSplitInput(book.rent, Amount(Money("100"), currency)),
            TransactionSplitInput(book.checking, Amount(Money("-100"), "EUR")),
        ),
    )

    result = save_transaction(db, request)

    assert result.errors == (ServiceError("transaction.value.commodity", ("splits.1.value",)),)


def test_transaction_service_preserves_value_and_quantity_as_distinct_dimensions(db, book):
    currency = transaction_currency(db)
    security = Commodity(namespace="FUND", mnemonic="INDEX", fraction=1000)
    holding = Account(
        name="Index holding",
        atype=AccountType.ASSET,
        parent=book.assets,
        commodity=security.handle,
        commodity_scu=1000,
    )
    with db.transaction("Security fixture") as txn:
        db.add_commodity(security, txn)
        db.add_account(holding, txn)
    request = _transaction_request(
        db,
        book,
        splits=(
            TransactionSplitInput(
                holding.handle,
                Amount(Money("1250"), currency),
                quantity=Amount(Money("10"), security.handle),
            ),
            TransactionSplitInput(book.checking, Amount(Money("-1250"), currency)),
        ),
    )

    result = save_transaction(db, request)

    assert result.value is not None
    stored = db.get_transaction(result.value.handle)
    assert stored is not None
    assert stored.splits[0].value == Money("1250")
    assert stored.splits[0].quantity == Money("10")


def test_transaction_service_rejects_quantity_in_another_commodity(db, book):
    currency = transaction_currency(db)
    request = _transaction_request(
        db,
        book,
        splits=(
            TransactionSplitInput(
                book.rent,
                Amount(Money("100"), currency),
                quantity=Amount(Money("100"), "EUR"),
            ),
            TransactionSplitInput(book.checking, Amount(Money("-100"), currency)),
        ),
    )

    result = save_transaction(db, request)

    assert result.errors == (
        ServiceError("transaction.quantity.commodity", ("splits.0.quantity",)),
    )


def test_transaction_service_requires_security_quantity_separately_from_value(db, book):
    currency = transaction_currency(db)
    security = Commodity(namespace="FUND", mnemonic="INDEX", fraction=1000)
    holding = Account(
        name="Index holding",
        atype=AccountType.ASSET,
        parent=book.assets,
        commodity=security.handle,
        commodity_scu=1000,
    )
    with db.transaction("Security fixture") as txn:
        db.add_commodity(security, txn)
        db.add_account(holding, txn)
    request = _transaction_request(
        db,
        book,
        splits=(
            TransactionSplitInput(holding.handle, Amount(Money("1250"), currency)),
            TransactionSplitInput(book.checking, Amount(Money("-1250"), currency)),
        ),
    )

    result = save_transaction(db, request)

    assert result.errors == (ServiceError("transaction.quantity.required", ("splits.0.quantity",)),)


def test_transaction_service_rolls_back_when_claim_attachment_fails(db, book):
    before = db.summary()["txn"]
    base = _transaction_request(db, book)
    request = SaveTransaction(
        base.definition,
        claim_attachment=ClaimAttachment("missing-claim", "payment"),
    )

    result = save_transaction(db, request)

    assert result.errors == (ServiceError("transaction.claim.not_found", ("claim",)),)
    assert db.summary()["txn"] == before


def test_transaction_service_native_round_trip_preserves_exact_splits(db, book, tmp_path):
    result = save_transaction(db, _transaction_request(db, book))
    assert result.value is not None
    path = tmp_path / "transaction-service.bread"
    db.backup_to(str(path))

    from breadsched.gen.db.sqlite import DbSQLite

    reopened = DbSQLite()
    reopened.load(str(path), mode="r")
    try:
        stored = reopened.get_transaction(result.value.handle)
        assert stored is not None
        assert [split.value for split in stored.splits] == [Money("125"), Money("-125")]
        assert stored.is_balanced()
    finally:
        reopened.close()


def _monthly_schedule(book) -> ScheduledTransaction:
    return ScheduledTransaction(
        name="Monthly utilities",
        recurrence=Recurrence(period=PeriodType.MONTH, start=date(2026, 1, 1)),
        splits=[
            ScheduledSplit(book.utilities, Money("100")),
            ScheduledSplit(book.checking, Money("-100")),
        ],
    )


def test_schedule_service_owns_the_atomic_create_and_update(db, book):
    candidate = _monthly_schedule(book)

    created = save_schedule(db, SaveSchedule(candidate))

    assert created.value is not None
    assert db.get_scheduled(candidate.handle).name == "Monthly utilities"
    assert db.undo_stack[-1].message == "Add scheduled Monthly utilities"

    candidate.name = "Updated utilities"
    updated = save_schedule(db, SaveSchedule(candidate, existing_handle=candidate.handle))

    assert updated.value is not None
    assert db.get_scheduled(candidate.handle).name == "Updated utilities"
    assert db.undo_stack[-1].message == "Update scheduled Updated utilities"


def test_schedule_service_returns_stable_fields_and_does_not_partially_write(db, book):
    candidate = _monthly_schedule(book)
    candidate.splits[1].account = "missing"
    before = len(db.undo_stack)

    result = save_schedule(db, SaveSchedule(candidate))

    assert result.value is None
    assert ServiceError("schedule.account.not_found", ("splits.1.account",)) in result.errors
    assert db.get_scheduled(candidate.handle) is None
    assert len(db.undo_stack) == before


def test_schedule_service_protects_formula_owned_split_structure(db, book):
    source = ScheduledTransaction(
        name="Formula schedule",
        recurrence=Recurrence(period=PeriodType.MONTH, start=date(2026, 1, 1)),
        splits=[
            ScheduledSplit(book.utilities, formula="payment"),
            ScheduledSplit(book.checking, formula="-payment"),
        ],
    )
    source.variables = {"payment": "100"}
    assert save_schedule(db, SaveSchedule(source)).ok
    changed = ScheduledTransaction.from_dict(source.serialize())
    changed.splits[0].account = book.groceries

    result = save_schedule(db, SaveSchedule(changed, existing_handle=source.handle))

    assert ServiceError("schedule.formula.ownership", ("splits",)) in result.errors
    assert db.get_scheduled(source.handle).splits[0].account == book.utilities


def test_scenario_schedule_service_replaces_source_override_atomically(db, book):
    source = _monthly_schedule(book)
    scenario = Scenario(name="Alternative", start=date(2026, 1, 1), years=1)
    with db.transaction("Scenario fixture") as txn:
        db.add_scheduled(source, txn)
        db.add_scenario(scenario, txn)
    change = ScenarioSchedule.from_scheduled(source)
    change.splits[0].amount = Money("125")
    change.splits[1].amount = Money("-125")

    first = save_scenario_schedule(db, SaveScenarioSchedule(scenario.handle, change))
    change.splits[0].amount = Money("150")
    change.splits[1].amount = Money("-150")
    second = save_scenario_schedule(db, SaveScenarioSchedule(scenario.handle, change))

    assert first.ok and second.ok
    stored = db.get_scenario(scenario.handle)
    assert stored is not None
    assert len(stored.schedule_overrides) == 1
    assert stored.schedule_overrides[0].splits[0].amount == Money("150")
    assert db.get_scheduled(source.handle).splits[0].amount == Money("100")


def test_fixed_request_constructs_identical_baseline_and_scenario_splits(db, book):
    definition = FixedScheduleInput(
        name="Payroll plan",
        recurrence=Recurrence(period=PeriodType.MONTH, start=date(2026, 2, 1), count=2),
        category=book.salary,
        funding=book.checking,
        amount=Money("5000"),
        additional_splits=(
            FixedSplitInput(book.rent, Money("1000"), memo="deduction"),
            FixedSplitInput(
                book.brokerage,
                Money("500"),
                planning_flow=PlanningFlowKind.RETIREMENT_SAVING,
                investment_activity=InvestmentActivityKind.CONTRIBUTION,
                memo="employee contribution",
            ),
        ),
        placeholder=True,
    )
    scenario = Scenario(name="Alternative", start=date(2026, 1, 1), years=1)
    with db.transaction("Scenario") as txn:
        db.add_scenario(scenario, txn)

    baseline = save_fixed_schedule(db, SaveFixedSchedule(definition))
    scenario_result = save_fixed_scenario_schedule(
        db,
        SaveFixedScenarioSchedule(scenario.handle, definition),
    )

    assert baseline.ok and scenario_result.ok
    saved_baseline = db.get_scheduled(baseline.value.handle)
    saved_scenario = db.get_scenario(scenario.handle).schedule_overrides[0]
    assert [split.serialize() for split in saved_baseline.splits] == [
        split.serialize() for split in saved_scenario.splits
    ]
    assert [split.amount for split in saved_baseline.splits] == [
        Money("-5000"),
        Money("1000"),
        Money("500"),
        Money("3500"),
    ]


def test_fixed_construction_can_preview_an_unsaved_source_snapshot(db, book):
    source = _monthly_schedule(book)
    source.estimate_evidence = {"history_start": "2025-01-01"}
    definition = FixedScheduleInput(
        name="Adjusted preview",
        recurrence=source.recurrence,
        category=book.utilities,
        funding=book.checking,
        amount=Money("125"),
    )

    result = build_fixed_schedule(
        db,
        SaveFixedSchedule(definition, existing_handle=source.handle, source=source),
    )

    assert result.ok
    assert result.value is not None
    assert result.value.handle == source.handle
    assert result.value.estimate_evidence == source.estimate_evidence
    assert db.get_scheduled(source.handle) is None


def test_fixed_construction_preserves_a_proven_repeated_account(db, book):
    source = ScheduledTransaction(
        name="Shared expense",
        recurrence=Recurrence(period=PeriodType.MONTH, start=date(2026, 1, 1)),
        splits=[
            ScheduledSplit(book.utilities, Money("60")),
            ScheduledSplit(book.checking, Money("-100")),
            ScheduledSplit(book.utilities, Money("40")),
        ],
    )
    definition = FixedScheduleInput(
        name=source.name,
        recurrence=source.recurrence,
        category=book.utilities,
        funding=book.checking,
        amount=Money("60"),
        additional_splits=(FixedSplitInput(book.utilities, Money("40")),),
    )

    result = build_fixed_schedule(
        db,
        SaveFixedSchedule(definition, existing_handle=source.handle, source=source),
    )

    assert result.ok
    assert result.value is not None
    assert [split.account for split in result.value.splits] == [
        book.utilities,
        book.utilities,
        book.checking,
    ]


def test_formula_requests_preserve_owned_structure_across_baseline_and_scenario(db, book):
    source = ScheduledTransaction(
        name="Formula loan",
        recurrence=Recurrence(period=PeriodType.MONTH, start=date(2026, 1, 1)),
        splits=[
            ScheduledSplit(book.utilities, formula="payment"),
            ScheduledSplit(book.checking, formula="-payment"),
        ],
    )
    source.variables = {"payment": "100"}
    scenario = Scenario(name="Alternative", start=date(2026, 1, 1), years=1)
    with db.transaction("Formula fixture") as txn:
        db.add_scheduled(source, txn)
        db.add_scenario(scenario, txn)
    common = dict(
        name="Adjusted formula loan",
        recurrence=source.recurrence,
        formulas={0: "payment + 25", 1: "-(payment + 25)"},
        variables={"payment": "100"},
        growth_policy=source.growth_policy,
        skipped=(),
    )

    baseline = save_formula_schedule(
        db,
        FormulaScheduleInput(
            existing_handle=source.handle,
            enabled=True,
            auto_create=False,
            placeholder=False,
            **common,
        ),
    )
    scenario_result = save_formula_scenario_schedule(
        db,
        FormulaScenarioScheduleInput(
            scenario_handle=scenario.handle,
            source_schedule=source.handle,
            **common,
        ),
    )

    assert baseline.ok and scenario_result.ok
    stored = db.get_scheduled(source.handle)
    scenario_change = db.get_scenario(scenario.handle).schedule_overrides[0]
    assert [split.account for split in stored.splits] == [book.utilities, book.checking]
    assert [split.account for split in scenario_change.splits] == [book.utilities, book.checking]
    assert [split.formula for split in stored.splits] == ["payment + 25", "-(payment + 25)"]
    assert [split.formula for split in scenario_change.splits] == [
        "payment + 25",
        "-(payment + 25)",
    ]


def test_formula_scenario_construction_can_preview_unsaved_snapshots(db, book):
    source = ScheduledTransaction(
        name="Transient formula",
        recurrence=Recurrence(period=PeriodType.MONTH, start=date(2026, 1, 1)),
        splits=[
            ScheduledSplit(book.utilities, formula="payment"),
            ScheduledSplit(book.checking, formula="-payment"),
        ],
    )
    source.variables = {"payment": "100"}
    scenario = Scenario(name="Transient scenario", start=date(2026, 1, 1), years=1)
    current = ScenarioSchedule.from_scheduled(source)

    result = build_formula_scenario_schedule(
        db,
        FormulaScenarioScheduleInput(
            scenario_handle=scenario.handle,
            source_schedule=source.handle,
            name="Transient preview",
            recurrence=source.recurrence,
            formulas={0: "payment + 25", 1: "-(payment + 25)"},
            variables={"payment": "100"},
            growth_policy=source.growth_policy,
            source=source,
            current=current,
        ),
    )

    assert result.ok
    assert result.value is not None
    assert [split.formula for split in result.value.splits] == [
        "payment + 25",
        "-(payment + 25)",
    ]
    assert db.get_scenario(scenario.handle) is None


def test_plan_query_returns_one_typed_result_for_base_and_comparison(db, book):
    schedule = _monthly_schedule(book)
    alternate = Scenario(name="Higher costs", start=date(2026, 1, 1), years=1)
    alternate.schedule_overrides.append(ScenarioSchedule.from_scheduled(schedule))
    alternate.schedule_overrides[0].splits[0].amount = Money("125")
    alternate.schedule_overrides[0].splits[1].amount = Money("-125")
    with db.transaction("Plan service fixture") as txn:
        db.add_scheduled(schedule, txn)
        db.add_scenario(alternate, txn)

    result = query_plan(
        db,
        PlanQuery(
            start=date(2026, 1, 1),
            end=date(2026, 3, 31),
            period=ReportingPeriod.MONTH,
            measure=PlanMeasure.VARIANCE,
            compare=alternate.handle,
            today=date(2026, 1, 15),
        ),
    )

    assert result.ok
    assert result.value is not None
    assert result.value.measure is PlanMeasure.VARIANCE
    assert result.value.report.activity.planned_cash_change == Money("-300")
    assert result.value.comparison is not None
    assert result.value.comparison.scenario.handle == alternate.handle
    assert result.value.comparison.report.activity.planned_cash_change == Money("-375")


def test_plan_query_returns_stable_field_errors_without_interface_text(db, book):
    result = query_plan(
        db,
        PlanQuery(
            start=date(2026, 4, 1),
            end=date(2026, 3, 31),
            today=date(2026, 1, 15),
        ),
    )

    assert not result.ok
    assert result.value is None
    assert result.errors == (ServiceError("plan.end.before_start", ("end", "start")),)


def test_plan_query_rejects_comparing_a_scenario_with_itself(db, book):
    scenario = Scenario(name="Same", start=date(2026, 1, 1), years=1)
    with db.transaction("Scenario") as txn:
        db.add_scenario(scenario, txn)

    result = query_plan(
        db,
        PlanQuery(
            start=date(2026, 1, 1),
            end=date(2026, 12, 31),
            scenario=scenario.handle,
            compare=scenario.handle,
            today=date(2026, 1, 15),
        ),
    )

    assert [error.code for error in result.errors] == ["plan.comparison.same"]


def test_plan_query_uses_base_marker_for_comparison(db, book):
    scenario = Scenario(name="Alternative", start=date(2026, 1, 1), years=1)
    with db.transaction("Scenario") as txn:
        db.add_scenario(scenario, txn)

    result = query_plan(
        db,
        PlanQuery(
            start=date(2026, 1, 1),
            end=date(2026, 12, 31),
            scenario=scenario.handle,
            compare=BASE_SCENARIO,
            today=date(2026, 1, 15),
        ),
    )

    assert result.value is not None
    assert result.value.comparison is not None
    assert result.value.comparison.scenario.handle is None
