"""Paychecks: gross pay, what comes out of it, and what reaches the bank.

A paycheck is an ordinary balanced schedule. One or more income legs credit the
gross pay; bank or cash legs receive the net deposit; every other leg is something
taken out of the gross. This module reads that shape for every interface alike:

- :func:`paycheck_breakdown` groups an existing schedule's legs, as of a date, into
  **taxes** (an expense account with "tax" in its full name), other **deductions**
  (any other expense), **saved** (retirement, FSA, investment, or other non-cash
  assets), and **repayments** (a liability such as a 401(k) loan), so gross minus
  all of those is the net deposit.
- A :class:`PayrollTemplate` describes one employer's paycheck: the income and
  deposit accounts, a usual gross, and each line out of it as a fixed amount or a
  percentage of gross. :func:`compute_paycheck` turns it into exact amounts for a
  gross; a schedule built from it is an ordinary fixed schedule.
- :func:`plan_pay_change` works out a raise or other pay change from a date: the
  new gross, each line either scaled with the gross, set to a new amount, or kept,
  and the net deposit balancing the rest. The service saves it as per-leg future
  amounts, so occurrences before the date keep their old amounts.

Nothing here writes; ``services.payroll`` validates and saves.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

from ..db.sqlite import DbSQLite
from ..lib.account import Account, AccountClass
from ..lib.money import Money
from ..lib.scheduled import ScheduledTransaction

__all__ = [
    "PayLeg",
    "PayLegKind",
    "PayChangePlan",
    "PayChangeLine",
    "Paycheck",
    "PaycheckBreakdown",
    "PayrollError",
    "PayrollLine",
    "PayrollTemplate",
    "compute_paycheck",
    "is_tax_account",
    "leg_kind",
    "paycheck_breakdown",
    "plan_pay_change",
]


class PayrollError(ValueError):
    """A paycheck that cannot be computed; ``code`` is a stable service code."""

    def __init__(self, code: str, field: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.field = field


class PayLegKind(str, Enum):
    GROSS = "gross"
    TAX = "tax"
    DEDUCTION = "deduction"
    SAVED = "saved"
    REPAYMENT = "repayment"
    NET = "net"

    @property
    def label(self) -> str:
        return _KIND_LABELS[self]


_KIND_LABELS = {
    PayLegKind.GROSS: "Gross pay",
    PayLegKind.TAX: "Taxes",
    PayLegKind.DEDUCTION: "Deductions",
    PayLegKind.SAVED: "Saved",
    PayLegKind.REPAYMENT: "Repayments",
    PayLegKind.NET: "Net deposit",
}


def is_tax_account(full_name: str) -> bool:
    return "tax" in full_name.lower()


def leg_kind(account: Account, full_name: str) -> PayLegKind | None:
    """What a leg out of gross pay into ``account`` is, or ``None`` for gross or net."""
    klass = account.account_class
    if klass is AccountClass.EXPENSE:
        return PayLegKind.TAX if is_tax_account(full_name) else PayLegKind.DEDUCTION
    if klass is AccountClass.LIABILITY:
        return PayLegKind.REPAYMENT
    if klass is AccountClass.ASSET and not account.is_spendable_cash:
        return PayLegKind.SAVED
    return None


# ----------------------------------------------------------------- breakdown


@dataclass(frozen=True, slots=True)
class PayLeg:
    account: str
    name: str
    kind: PayLegKind
    #: Positive: what this leg adds to gross (GROSS), takes out of it, or deposits (NET).
    amount: Money

    def as_dict(self) -> dict[str, object]:
        return {
            "account": self.account,
            "name": self.name,
            "kind": self.kind.value,
            "kind_label": self.kind.label,
            "amount": self.amount,
        }


@dataclass(frozen=True, slots=True)
class PaycheckBreakdown:
    schedule: str
    name: str
    when: date | None
    legs: tuple[PayLeg, ...]

    def total(self, kind: PayLegKind) -> Money:
        total = Money(0)
        for leg in self.legs:
            if leg.kind is kind:
                total = total + leg.amount
        return total

    @property
    def gross(self) -> Money:
        return self.total(PayLegKind.GROSS)

    @property
    def net(self) -> Money:
        return self.total(PayLegKind.NET)

    @property
    def withheld(self) -> Money:
        return self.gross - self.net

    @property
    def take_home_percent(self) -> Decimal:
        if not self.gross:
            return Decimal(0)
        return (self.net.rate() / self.gross.rate() * 100).quantize(Decimal("0.1"))

    def as_dict(self) -> dict[str, object]:
        return {
            "schedule": self.schedule,
            "name": self.name,
            "when": self.when,
            "gross": self.gross,
            "net": self.net,
            "withheld": self.withheld,
            "take_home_percent": str(self.take_home_percent),
            "totals": {
                kind.value: self.total(kind)
                for kind in PayLegKind
                if kind not in (PayLegKind.GROSS, PayLegKind.NET)
            },
            "legs": [leg.as_dict() for leg in self.legs],
        }


def paycheck_breakdown(
    db: DbSQLite, schedule: ScheduledTransaction, when: date | None = None
) -> PaycheckBreakdown | None:
    """``schedule`` read as a paycheck as of ``when``, or ``None`` if it is not one.

    A paycheck credits at least one income account, deposits into at least one bank
    or cash account, and every other leg takes money out of the gross (a positive
    value into an expense, non-cash asset, or liability).
    """
    try:
        resolved = schedule.resolved_splits(when=when)
    except (ArithmeticError, ValueError):
        return None
    legs: list[PayLeg] = []
    for account_handle, value in resolved:
        account = db.get_account(account_handle)
        if account is None:
            return None
        name = db.full_name(account_handle) or account.name
        klass = account.account_class
        if klass is AccountClass.INCOME:
            if value > 0:
                return None
            legs.append(PayLeg(account_handle, name, PayLegKind.GROSS, -value))
            continue
        if account.is_spendable_cash:
            if value < 0:
                return None
            legs.append(PayLeg(account_handle, name, PayLegKind.NET, value))
            continue
        kind = leg_kind(account, name)
        if kind is None or value < 0:
            return None
        legs.append(PayLeg(account_handle, name, kind, value))
    kinds = {leg.kind for leg in legs}
    if PayLegKind.GROSS not in kinds or PayLegKind.NET not in kinds:
        return None
    breakdown = PaycheckBreakdown(schedule.handle, schedule.name, when, tuple(legs))
    if not breakdown.gross:
        return None
    return breakdown


# ------------------------------------------------------------------ templates


def _money(data: Any) -> Money:
    return Money(*data) if isinstance(data, list) else Money(str(data))


@dataclass(frozen=True, slots=True)
class PayrollLine:
    """One line out of gross pay: a fixed amount or a percentage of gross."""

    account: str
    amount: Money | None = None
    percent: Decimal | None = None
    memo: str = ""

    def amount_for(self, gross: Money) -> Money:
        if self.percent is not None:
            return (gross * self.percent / Decimal(100)).quantize()
        assert self.amount is not None
        return self.amount

    def serialize(self) -> dict[str, Any]:
        return {
            "account": self.account,
            "amount": (
                [self.amount.numerator, self.amount.denominator]
                if self.amount is not None
                else None
            ),
            "percent": str(self.percent) if self.percent is not None else None,
            "memo": self.memo,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PayrollLine:
        amount = data.get("amount")
        percent = data.get("percent")
        return cls(
            account=str(data["account"]),
            amount=_money(amount) if amount is not None else None,
            percent=Decimal(str(percent)) if percent is not None else None,
            memo=str(data.get("memo") or ""),
        )


@dataclass(frozen=True, slots=True)
class PayrollTemplate:
    """One employer's paycheck, reusable for new schedules."""

    name: str
    income_account: str
    deposit_account: str
    gross: Money
    lines: tuple[PayrollLine, ...] = ()

    def serialize(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "income_account": self.income_account,
            "deposit_account": self.deposit_account,
            "gross": [self.gross.numerator, self.gross.denominator],
            "lines": [line.serialize() for line in self.lines],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PayrollTemplate:
        return cls(
            name=str(data["name"]),
            income_account=str(data["income_account"]),
            deposit_account=str(data["deposit_account"]),
            gross=_money(data["gross"]),
            lines=tuple(PayrollLine.from_dict(item) for item in data.get("lines") or ()),
        )


@dataclass(frozen=True, slots=True)
class Paycheck:
    gross: Money
    lines: tuple[tuple[PayrollLine, Money], ...]
    net: Money


def parse_percent(text: str) -> Decimal:
    """A percentage typed as ``6.2`` or ``6.2%``."""
    try:
        value = Decimal(text.strip().removesuffix("%").strip())
    except InvalidOperation as exc:
        raise PayrollError("payroll.percent.invalid") from exc
    if not value.is_finite():
        raise PayrollError("payroll.percent.invalid")
    return value


def compute_paycheck(template: PayrollTemplate, gross: Money | None = None) -> Paycheck:
    """Exact line amounts and net deposit for ``gross`` (default: the template's)."""
    pay = template.gross if gross is None else gross
    if pay <= 0:
        raise PayrollError("payroll.gross.non_positive", "gross")
    lines: list[tuple[PayrollLine, Money]] = []
    total = Money(0)
    for index, line in enumerate(template.lines):
        if (line.amount is None) == (line.percent is None):
            raise PayrollError("payroll.line.amount_or_percent", f"lines.{index}")
        if line.percent is not None and not 0 < line.percent < 100:
            raise PayrollError("payroll.percent.invalid", f"lines.{index}.percent")
        amount = line.amount_for(pay)
        if amount <= 0:
            raise PayrollError("payroll.line.non_positive", f"lines.{index}")
        lines.append((line, amount))
        total = total + amount
    net = pay - total
    if net <= 0:
        raise PayrollError("payroll.net.non_positive", "lines")
    return Paycheck(pay, tuple(lines), net)


# ----------------------------------------------------------------- pay change


@dataclass(frozen=True, slots=True)
class PayChangeLine:
    account: str
    name: str
    kind: PayLegKind
    before: Money
    after: Money
    #: "scaled", "set", "kept", or "balance" (the net deposit).
    how: str

    def as_dict(self) -> dict[str, object]:
        return {
            "account": self.account,
            "name": self.name,
            "kind": self.kind.value,
            "kind_label": self.kind.label,
            "before": self.before,
            "after": self.after,
            "how": self.how,
        }


@dataclass(frozen=True, slots=True)
class PayChangePlan:
    schedule: str
    start: date
    lines: tuple[PayChangeLine, ...]

    def total(self, kind: PayLegKind, *, after: bool = True) -> Money:
        total = Money(0)
        for line in self.lines:
            if line.kind is kind:
                total = total + (line.after if after else line.before)
        return total

    def as_dict(self) -> dict[str, object]:
        return {
            "schedule": self.schedule,
            "start": self.start,
            "gross_before": self.total(PayLegKind.GROSS, after=False),
            "gross_after": self.total(PayLegKind.GROSS),
            "net_before": self.total(PayLegKind.NET, after=False),
            "net_after": self.total(PayLegKind.NET),
            "lines": [line.as_dict() for line in self.lines],
        }


def plan_pay_change(
    breakdown: PaycheckBreakdown,
    start: date,
    gross: Money,
    *,
    scaled: frozenset[str] | None = None,
    amounts: dict[str, Money] | None = None,
) -> PayChangePlan:
    """The paycheck from ``start`` with gross pay ``gross``.

    ``breakdown`` is the paycheck as it stands on ``start``. A line whose account is
    in ``amounts`` takes that amount; one in ``scaled`` (default: the taxes) grows or
    shrinks in proportion to gross; any other is kept. With several income legs each
    is scaled in proportion. With several deposits the first absorbs the change and
    the others are kept.
    """
    amounts = dict(amounts or {})
    old_gross = breakdown.gross
    if gross <= 0:
        raise PayrollError("payroll.gross.non_positive", "gross")
    if scaled is None:
        scaled = frozenset(leg.account for leg in breakdown.legs if leg.kind is PayLegKind.TAX)
    accounts = {leg.account for leg in breakdown.legs}
    for account in [*amounts, *scaled]:
        if account not in accounts:
            raise PayrollError("payroll.line.not_found", account)
    ratio = gross / old_gross
    gross_legs = [leg for leg in breakdown.legs if leg.kind is PayLegKind.GROSS]
    lines: list[PayChangeLine] = []
    out_total = Money(0)
    assigned_gross = Money(0)
    for index, leg in enumerate(gross_legs):
        after = (
            gross - assigned_gross
            if index == len(gross_legs) - 1
            else (leg.amount * ratio).quantize()
        )
        assigned_gross = assigned_gross + after
        how = "set" if len(gross_legs) == 1 else "scaled"
        lines.append(PayChangeLine(leg.account, leg.name, leg.kind, leg.amount, after, how))
    deposits = [leg for leg in breakdown.legs if leg.kind is PayLegKind.NET]
    kept_deposits = Money(0)
    for leg in breakdown.legs:
        if leg.kind in (PayLegKind.GROSS, PayLegKind.NET):
            continue
        if leg.account in amounts:
            after, how = amounts[leg.account], "set"
            if after < 0:
                raise PayrollError("payroll.line.non_positive", leg.account)
        elif leg.account in scaled:
            after, how = (leg.amount * ratio).quantize(), "scaled"
        else:
            after, how = leg.amount, "kept"
        out_total = out_total + after
        lines.append(PayChangeLine(leg.account, leg.name, leg.kind, leg.amount, after, how))
    for leg in deposits[1:]:
        if leg.account in amounts:
            after, how = amounts[leg.account], "set"
        else:
            after, how = leg.amount, "kept"
        kept_deposits = kept_deposits + after
        lines.append(PayChangeLine(leg.account, leg.name, leg.kind, leg.amount, after, how))
    first = deposits[0]
    net = gross - out_total - kept_deposits
    if net <= 0:
        raise PayrollError("payroll.net.non_positive", "gross")
    lines.append(PayChangeLine(first.account, first.name, first.kind, first.amount, net, "balance"))
    order = {leg.account: index for index, leg in enumerate(breakdown.legs)}
    lines.sort(key=lambda line: order[line.account])
    return PayChangePlan(breakdown.schedule, start, tuple(lines))
