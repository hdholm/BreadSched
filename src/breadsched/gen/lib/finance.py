"""Loan and annuity mathematics.

The standard time-value-of-money functions, using the same sign convention as
every spreadsheet: a payment you make is negative, an amount you receive is
positive. ``pmt`` on a 200,000 mortgage therefore returns a negative number,
because the money leaves you.

Everything is :class:`~decimal.Decimal`. A mortgage schedule is a few hundred
compounding steps, and binary floating point drifts by cents over that distance —
which is exactly the discrepancy that makes a projection disagree with a lender's
statement and destroys a user's trust in both.

The rate argument is the rate *per period*, not per year. A 6% annual mortgage
paid monthly uses 0.005, and getting this wrong is the commonest error in loan
arithmetic, so the callers in this codebase divide explicitly at the call site.
"""

from __future__ import annotations

from decimal import Decimal, localcontext

__all__ = ["pmt", "ipmt", "ppmt", "fv", "pv", "nper", "amortisation_schedule"]

_PRECISION = 40


def _d(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    return Decimal(value)


def pmt(rate, periods, present_value, future_value=0, due: int = 0) -> Decimal:
    """The constant payment for a loan.

    ``due`` is 0 for payment at the end of each period (an ordinary annuity, which
    is how nearly every mortgage works) and 1 for payment at the start.
    """
    with localcontext() as context:
        context.prec = _PRECISION
        rate, periods = _d(rate), _d(periods)
        present_value, future_value = _d(present_value), _d(future_value)
        if periods == 0:
            raise ValueError("a loan needs at least one period")
        if rate == 0:
            return -(present_value + future_value) / periods
        growth = (1 + rate) ** periods
        payment = -(present_value * growth + future_value) * rate / (growth - 1)
        return payment / (1 + rate) if due else payment


def _balance_before(rate, periods, present_value, future_value, due, period) -> Decimal:
    """Outstanding balance at the start of ``period`` (1-based)."""
    with localcontext() as context:
        context.prec = _PRECISION
        rate = _d(rate)
        payment = pmt(rate, periods, present_value, future_value, due)
        elapsed = _d(period) - 1
        if rate == 0:
            return _d(present_value) + payment * elapsed
        growth = (1 + rate) ** elapsed
        return _d(present_value) * growth + payment * (growth - 1) / rate


def ipmt(rate, period, periods, present_value, future_value=0, due: int = 0) -> Decimal:
    """The interest portion of the payment in ``period`` (1-based)."""
    with localcontext() as context:
        context.prec = _PRECISION
        rate = _d(rate)
        index = _d(period)
        if index < 1 or index > _d(periods):
            raise ValueError(f"period {period} is outside 1..{periods}")
        if rate == 0:
            return Decimal(0)
        balance = _balance_before(rate, periods, present_value, future_value, due, index)
        interest = -balance * rate
        if due and index > 1:
            interest = interest / (1 + rate)
        return interest


def ppmt(rate, period, periods, present_value, future_value=0, due: int = 0) -> Decimal:
    """The principal portion of the payment in ``period`` (1-based)."""
    with localcontext() as context:
        context.prec = _PRECISION
        payment = pmt(rate, periods, present_value, future_value, due)
        interest = ipmt(rate, period, periods, present_value, future_value, due)
        return payment - interest


def fv(rate, periods, payment, present_value=0, due: int = 0) -> Decimal:
    """Value after ``periods`` of regular payments."""
    with localcontext() as context:
        context.prec = _PRECISION
        rate, periods = _d(rate), _d(periods)
        payment, present_value = _d(payment), _d(present_value)
        if rate == 0:
            return -(present_value + payment * periods)
        growth = (1 + rate) ** periods
        factor = payment * (1 + rate) if due else payment
        return -(present_value * growth + factor * (growth - 1) / rate)


def pv(rate, periods, payment, future_value=0, due: int = 0) -> Decimal:
    """How much can be borrowed for a given payment."""
    with localcontext() as context:
        context.prec = _PRECISION
        rate, periods = _d(rate), _d(periods)
        payment, future_value = _d(payment), _d(future_value)
        if rate == 0:
            return -(future_value + payment * periods)
        growth = (1 + rate) ** periods
        factor = payment * (1 + rate) if due else payment
        return -(future_value + factor * (growth - 1) / rate) / growth


def nper(rate, payment, present_value, future_value=0) -> Decimal:
    """How many periods a loan takes to repay."""
    with localcontext() as context:
        context.prec = _PRECISION
        rate, payment = _d(rate), _d(payment)
        present_value, future_value = _d(present_value), _d(future_value)
        if payment == 0:
            raise ValueError("a payment of zero never repays the loan")
        if rate == 0:
            return -(present_value + future_value) / payment
        numerator = payment - future_value * rate
        denominator = present_value * rate + payment
        # Both are negative for a normal loan (a payment leaves you), so it is the
        # ratio that has to be positive, not each term.
        if denominator == 0 or numerator / denominator <= 0:
            raise ValueError("this payment never repays the loan at this rate")
        return (numerator / denominator).ln() / (1 + rate).ln()


def amortisation_schedule(
    rate, periods, present_value, future_value=0, due: int = 0
) -> list[dict[str, Decimal]]:
    """Period-by-period interest, principal and closing balance.

    Useful for showing a user why their payment barely touches the balance in year
    one, and for checking this module against a lender's own schedule.
    """
    rows: list[dict[str, Decimal]] = []
    with localcontext() as context:
        context.prec = _PRECISION
        payment = pmt(rate, periods, present_value, future_value, due)
        balance = _d(present_value)
        for period in range(1, int(_d(periods)) + 1):
            interest = ipmt(rate, period, periods, present_value, future_value, due)
            principal = payment - interest
            balance = balance + principal
            rows.append(
                {
                    "period": Decimal(period),
                    "payment": payment,
                    "interest": interest,
                    "principal": principal,
                    "balance": balance,
                }
            )
    return rows
