"""Payroll templates, paycheck schedules, and dated pay changes.

CLI, GTK, and web adapters call these functions. Templates are BreadSched-owned
book settings (one undoable metadata write each). A paycheck schedule built from a
template is an ordinary fixed schedule saved through ``schedules``; a pay change
adds per-leg future amounts to an existing paycheck, so occurrences before its date
keep their amounts. A rejected request leaves the book unchanged.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from ..db.base import DbTxn
from ..db.sqlite import DbSQLite
from ..engine.payroll import (
    PayChangePlan,
    PaycheckBreakdown,
    PayLegKind,
    PayrollError,
    PayrollLine,
    PayrollTemplate,
    compute_paycheck,
    leg_kind,
    paycheck_breakdown,
    plan_pay_change,
)
from ..lib.account import AccountClass
from ..lib.money import Money
from ..lib.recurrence import Recurrence
from ..lib.scheduled import ScheduledSplitAmountChange, ScheduledTransaction
from ..lib.transaction import InvestmentActivityKind
from .contracts import ServiceError, ServiceResult
from .schedules import (
    FixedScheduleInput,
    FixedSplitInput,
    SavedSchedule,
    SaveFixedSchedule,
    SaveSchedule,
    save_fixed_schedule,
    save_schedule,
)

__all__ = [
    "TEMPLATES_KEY",
    "CreatePaycheck",
    "PayChange",
    "SavePayrollTemplate",
    "apply_pay_change",
    "create_paycheck_schedule",
    "delete_payroll_template",
    "list_payroll_templates",
    "paychecks",
    "preview_pay_change",
    "save_payroll_template",
    "template_from_schedule",
]

TEMPLATES_KEY = "payroll_templates"


@dataclass(frozen=True, slots=True)
class SavePayrollTemplate:
    template: PayrollTemplate
    #: The name of the template this replaces, if any.
    existing_name: str | None = None


@dataclass(frozen=True, slots=True)
class CreatePaycheck:
    template: str
    name: str
    recurrence: Recurrence
    #: Gross for this schedule; ``None`` uses the template's.
    gross: Money | None = None
    auto_create: bool = False


@dataclass(frozen=True, slots=True)
class PayChange:
    schedule: str
    start: date
    gross: Money
    #: Accounts whose amount scales with gross; ``None`` scales the taxes.
    scaled: frozenset[str] | None = None
    amounts: Mapping[str, Money] = field(default_factory=dict)


def _failure(exc: PayrollError) -> ServiceResult:
    return ServiceResult.failure(ServiceError(exc.code, (exc.field,) if exc.field else ()))


# ------------------------------------------------------------------ templates


def list_payroll_templates(db: DbSQLite) -> list[PayrollTemplate]:
    """Saved templates, by name."""
    stored = db.get_metadata(TEMPLATES_KEY, None) or []
    templates = [PayrollTemplate.from_dict(item) for item in stored]
    return sorted(templates, key=lambda item: item.name.casefold())


def _store(db: DbSQLite, templates: list[PayrollTemplate], txn: DbTxn) -> None:
    ordered = sorted(templates, key=lambda item: item.name.casefold())
    db.set_metadata(TEMPLATES_KEY, [item.serialize() for item in ordered], txn)


def _find(templates: list[PayrollTemplate], name: str) -> PayrollTemplate | None:
    wanted = name.strip().casefold()
    return next((item for item in templates if item.name.casefold() == wanted), None)


def _template_errors(db: DbSQLite, template: PayrollTemplate) -> list[ServiceError]:
    errors: list[ServiceError] = []
    if not template.name.strip():
        errors.append(ServiceError("payroll.name.required", ("name",)))
    income = db.get_account(template.income_account)
    if income is None or income.placeholder or income.account_class is not AccountClass.INCOME:
        errors.append(ServiceError("payroll.income.invalid", ("income_account",)))
    deposit = db.get_account(template.deposit_account)
    if deposit is None or deposit.placeholder or not deposit.is_spendable_cash:
        errors.append(ServiceError("payroll.deposit.invalid", ("deposit_account",)))
    seen = Counter(line.account for line in template.lines)
    for index, line in enumerate(template.lines):
        account = db.get_account(line.account)
        name = db.full_name(line.account) or ""
        if (
            account is None
            or account.placeholder
            or leg_kind(account, name) is None
            or line.account in (template.income_account, template.deposit_account)
        ):
            errors.append(ServiceError("payroll.line.account_invalid", (f"lines.{index}.account",)))
        elif seen[line.account] > 1:
            errors.append(ServiceError("payroll.line.duplicate", (f"lines.{index}.account",)))
    if not errors:
        try:
            compute_paycheck(template)
        except PayrollError as exc:
            errors.append(ServiceError(exc.code, (exc.field,) if exc.field else ()))
    return errors


def save_payroll_template(
    db: DbSQLite, request: SavePayrollTemplate
) -> ServiceResult[PayrollTemplate]:
    """Add a template or replace ``existing_name``; names are unique ignoring case."""
    template = PayrollTemplate(
        name=request.template.name.strip(),
        income_account=request.template.income_account,
        deposit_account=request.template.deposit_account,
        gross=request.template.gross,
        lines=request.template.lines,
    )
    templates = list_payroll_templates(db)
    replaced = None
    if request.existing_name is not None:
        replaced = _find(templates, request.existing_name)
        if replaced is None:
            return ServiceResult.failure(ServiceError("payroll.template.not_found", ("name",)))
    clash = _find(templates, template.name)
    if clash is not None and clash is not replaced:
        return ServiceResult.failure(ServiceError("payroll.name.duplicate", ("name",)))
    errors = _template_errors(db, template)
    if errors:
        return ServiceResult.failure(*errors)
    kept = [item for item in templates if item is not replaced]
    action = "Add" if replaced is None else "Update"
    with db.transaction(f"{action} payroll template {template.name}") as txn:
        _store(db, [*kept, template], txn)
    return ServiceResult.success(template)


def delete_payroll_template(db: DbSQLite, name: str) -> ServiceResult[PayrollTemplate]:
    templates = list_payroll_templates(db)
    found = _find(templates, name)
    if found is None:
        return ServiceResult.failure(ServiceError("payroll.template.not_found", ("name",)))
    with db.transaction(f"Delete payroll template {found.name}") as txn:
        _store(db, [item for item in templates if item is not found], txn)
    return ServiceResult.success(found)


def template_from_schedule(
    db: DbSQLite, handle: str, name: str, *, when: date | None = None
) -> ServiceResult[PayrollTemplate]:
    """A template (not yet saved) describing an existing paycheck as of ``when``.

    Taxes become a percentage of gross when a percentage reproduces the amount to
    the cent; every other line is a fixed amount.
    """
    schedule = db.get_scheduled(handle)
    if schedule is None:
        return ServiceResult.failure(ServiceError("schedule.not_found", ("handle",)))
    breakdown = paycheck_breakdown(db, schedule, when or schedule.recurrence.start)
    gross_legs = [] if breakdown is None else _legs(breakdown, PayLegKind.GROSS)
    deposits = [] if breakdown is None else _legs(breakdown, PayLegKind.NET)
    if breakdown is None or len(gross_legs) != 1 or len(deposits) != 1:
        return ServiceResult.failure(ServiceError("payroll.schedule.not_paycheck", ("handle",)))
    gross = breakdown.gross
    lines: list[PayrollLine] = []
    for leg in breakdown.legs:
        if leg.kind in (PayLegKind.GROSS, PayLegKind.NET):
            continue
        line = PayrollLine(leg.account, amount=leg.amount)
        if leg.kind is PayLegKind.TAX:
            percent = (leg.amount.rate() / gross.rate() * 100).quantize(Decimal("0.0001"))
            percent = percent.normalize()
            candidate = PayrollLine(leg.account, percent=percent)
            if 0 < percent < 100 and candidate.amount_for(gross) == leg.amount:
                line = candidate
        lines.append(line)
    return ServiceResult.success(
        PayrollTemplate(
            name.strip(), gross_legs[0].account, deposits[0].account, gross, tuple(lines)
        )
    )


def _legs(breakdown: PaycheckBreakdown, kind: PayLegKind):
    return [leg for leg in breakdown.legs if leg.kind is kind]


# ------------------------------------------------------------------ schedules


def create_paycheck_schedule(db: DbSQLite, request: CreatePaycheck) -> ServiceResult[SavedSchedule]:
    """Save a new fixed paycheck schedule from a template."""
    template = _find(list_payroll_templates(db), request.template)
    if template is None:
        return ServiceResult.failure(ServiceError("payroll.template.not_found", ("template",)))
    errors = _template_errors(db, template)
    if errors:
        return ServiceResult.failure(*errors)
    try:
        paycheck = compute_paycheck(template, request.gross)
    except PayrollError as exc:
        return _failure(exc)
    additional: list[FixedSplitInput] = []
    for line, amount in paycheck.lines:
        account = db.get_account(line.account)
        assert account is not None
        additional.append(
            FixedSplitInput(
                account=line.account,
                amount=amount,
                memo=line.memo,
                investment_activity=(
                    InvestmentActivityKind.CONTRIBUTION if account.atype.is_investment else None
                ),
                opposite_direction=account.account_class is AccountClass.LIABILITY,
            )
        )
    return save_fixed_schedule(
        db,
        SaveFixedSchedule(
            FixedScheduleInput(
                name=request.name,
                recurrence=request.recurrence,
                category=template.income_account,
                funding=template.deposit_account,
                amount=paycheck.gross,
                additional_splits=tuple(additional),
                auto_create=request.auto_create,
            )
        ),
    )


def paychecks(db: DbSQLite, when: date | None = None) -> list[PaycheckBreakdown]:
    """Every enabled, usable schedule still paying on or after ``when`` (default today)
    that reads as a paycheck there."""
    as_of = when or date.today()
    found: list[PaycheckBreakdown] = []
    for schedule in db.iter_scheduled():
        if not schedule.enabled or not schedule.usable:
            continue
        last = schedule.recurrence.last_occurrence()
        if last is not None and last < as_of:
            continue
        on = max(as_of, schedule.recurrence.start)
        breakdown = paycheck_breakdown(db, schedule, on)
        if breakdown is not None:
            found.append(breakdown)
    return sorted(found, key=lambda item: item.name.casefold())


# ----------------------------------------------------------------- pay change


def _pay_change(
    db: DbSQLite, request: PayChange
) -> ServiceResult[tuple[ScheduledTransaction, PayChangePlan]]:
    schedule = db.get_scheduled(request.schedule)
    if schedule is None:
        return ServiceResult.failure(ServiceError("schedule.not_found", ("schedule",)))
    if request.start < schedule.recurrence.start:
        return ServiceResult.failure(ServiceError("payroll.change.before_start", ("start",)))
    if any(split.formula for split in schedule.splits):
        return ServiceResult.failure(ServiceError("payroll.schedule.formula", ("schedule",)))
    if (
        schedule.amount_changes
        or schedule.seasonal_amounts
        or any(item.when >= request.start for item in schedule.occurrence_adjustments)
    ):
        return ServiceResult.failure(ServiceError("payroll.schedule.whole_amounts", ("schedule",)))
    if len({split.account for split in schedule.splits}) != len(schedule.splits):
        return ServiceResult.failure(
            ServiceError("payroll.schedule.repeated_account", ("schedule",))
        )
    if any(
        change.start > request.start for split in schedule.splits for change in split.amount_changes
    ):
        return ServiceResult.failure(ServiceError("payroll.change.later_exists", ("start",)))
    breakdown = paycheck_breakdown(db, schedule, request.start)
    if breakdown is None:
        return ServiceResult.failure(ServiceError("payroll.schedule.not_paycheck", ("schedule",)))
    try:
        plan = plan_pay_change(
            breakdown,
            request.start,
            request.gross,
            scaled=request.scaled,
            amounts=dict(request.amounts),
        )
    except PayrollError as exc:
        return _failure(exc)
    return ServiceResult.success((schedule, plan))


def preview_pay_change(db: DbSQLite, request: PayChange) -> ServiceResult[PayChangePlan]:
    """The paycheck from ``request.start`` after the change; writes nothing."""
    result = _pay_change(db, request)
    if result.value is None:
        return ServiceResult.failure(*result.errors)
    return ServiceResult.success(result.value[1])


def apply_pay_change(db: DbSQLite, request: PayChange) -> ServiceResult[PayChangePlan]:
    """Save the change as per-leg future amounts from ``request.start``."""
    result = _pay_change(db, request)
    if result.value is None:
        return ServiceResult.failure(*result.errors)
    schedule, plan = result.value
    candidate = ScheduledTransaction.from_dict(schedule.serialize())
    after = {line.account: line for line in plan.lines}
    for split in candidate.splits:
        line = after[split.account]
        value = -line.after if line.kind is PayLegKind.GROSS else line.after
        if line.before == line.after and not any(
            change.start == request.start for change in split.amount_changes
        ):
            continue
        split.amount_changes = sorted(
            [
                *(change for change in split.amount_changes if change.start != request.start),
                ScheduledSplitAmountChange(request.start, value),
            ],
            key=lambda change: change.start,
        )
    saved = save_schedule(db, SaveSchedule(candidate, schedule.handle))
    if saved.value is None:
        return ServiceResult.failure(*saved.errors)
    return ServiceResult.success(plan)
