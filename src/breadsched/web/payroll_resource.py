"""HTTP input and output for paychecks, pay changes, and payroll templates.

This adapter only parses JSON into the shared payroll service's requests and
translates its results. Paycheck arithmetic stays in ``gen/engine/payroll`` and
validation and every write in ``gen/services/payroll``, so a rejected request never
changes the book.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import TYPE_CHECKING, Any

from ..gen.engine.payroll import (
    PayrollError,
    PayrollLine,
    PayrollTemplate,
    leg_kind,
    parse_percent,
)
from ..gen.lib.account import AccountClass
from ..gen.lib.recurrence import PeriodType, Recurrence
from ..gen.services.contracts import ServiceError
from ..gen.services.payroll import (
    CreatePaycheck,
    PayChange,
    SavePayrollTemplate,
    apply_pay_change,
    create_paycheck_schedule,
    delete_payroll_template,
    list_payroll_templates,
    paychecks,
    preview_pay_change,
    save_payroll_template,
    template_from_schedule,
)

if TYPE_CHECKING:
    from .resources import QueryParams
    from .server import Api

#: Pay periods a new paycheck may use: key -> (period, interval).
PAY_PERIODS = {
    "biweekly": (PeriodType.WEEK, 2),
    "weekly": (PeriodType.WEEK, 1),
    "semimonthly": (PeriodType.SEMI_MONTH, 1),
    "monthly": (PeriodType.MONTH, 1),
}


def _text(payload: Mapping[str, Any], key: str, *, optional: bool = False) -> str | None:
    value = payload.get(key)
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be text")
    return value


def _date(payload: Mapping[str, Any], key: str) -> date:
    try:
        return date.fromisoformat((_text(payload, key) or "").strip())
    except ValueError:
        raise ValueError(f"{key} must be YYYY-MM-DD") from None


def _result(api: Api, result) -> Any:
    if result.value is None:
        raise api._service_resource_error(result.errors[0])
    return result.value


def template_json(api: Api, template: PayrollTemplate) -> dict[str, object]:
    db = api.db
    return {
        "name": template.name,
        "income_account": template.income_account,
        "income_name": db.full_name(template.income_account),
        "deposit_account": template.deposit_account,
        "deposit_name": db.full_name(template.deposit_account),
        "gross": template.gross,
        "lines": [
            {
                "account": line.account,
                "name": db.full_name(line.account),
                "amount": line.amount,
                "percent": str(line.percent) if line.percent is not None else None,
            }
            for line in template.lines
        ],
    }


def payroll(api: Api, query: QueryParams) -> dict[str, object]:
    """Paychecks as of ``as_of`` (default today), templates, and form choices."""
    raw = query.text("as_of")
    query.finish()
    try:
        as_of = date.fromisoformat(raw) if raw else None
    except ValueError:
        raise ValueError("as_of must be YYYY-MM-DD") from None
    db = api.db
    accounts = [
        account for account in db.iter_accounts() if not account.placeholder and not account.is_root
    ]

    def choices(wanted) -> list[dict[str, str]]:
        return sorted(
            (
                {"handle": account.handle, "name": db.full_name(account) or account.name}
                for account in accounts
                if wanted(account)
            ),
            key=lambda item: item["name"].casefold(),
        )

    return {
        "paychecks": [item.as_dict() for item in paychecks(db, as_of)],
        "templates": [template_json(api, item) for item in list_payroll_templates(db)],
        "income_accounts": choices(lambda a: a.account_class is AccountClass.INCOME),
        "deposit_accounts": choices(lambda a: a.is_spendable_cash),
        "line_accounts": choices(lambda a: leg_kind(a, db.full_name(a) or "") is not None),
        "periods": list(PAY_PERIODS),
    }


def _template(api: Api, payload: Mapping[str, Any]) -> PayrollTemplate:
    raw_lines = payload.get("lines") or []
    if not isinstance(raw_lines, list):
        raise ValueError("lines must be a list")
    lines: list[PayrollLine] = []
    for index, item in enumerate(raw_lines):
        if not isinstance(item, Mapping):
            raise ValueError("each line must be an object")
        account = _text(item, "account") or ""
        percent = item.get("percent")
        amount = item.get("amount")
        if percent not in (None, ""):
            try:
                lines.append(PayrollLine(account, percent=parse_percent(str(percent))))
            except PayrollError as exc:
                raise api._service_resource_error(
                    ServiceError(exc.code, (f"lines.{index}.percent",))
                ) from None
        elif amount not in (None, ""):
            lines.append(PayrollLine(account, amount=api._input_money(dict(payload), amount)))
        else:
            raise api._service_resource_error(
                ServiceError("payroll.line.amount_or_percent", (f"lines.{index}",))
            )
    return PayrollTemplate(
        name=_text(payload, "name") or "",
        income_account=_text(payload, "income_account") or "",
        deposit_account=_text(payload, "deposit_account") or "",
        gross=api._input_money(dict(payload), payload.get("gross")),
        lines=tuple(lines),
    )


def payroll_template_save(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    template = _template(api, payload)
    existing = _text(payload, "existing_name", optional=True)
    saved = _result(api, save_payroll_template(api.db, SavePayrollTemplate(template, existing)))
    return template_json(api, saved)


def payroll_template_delete(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    removed = _result(api, delete_payroll_template(api.db, _text(payload, "name") or ""))
    return {"deleted": removed.name}


def payroll_template_from_schedule(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    """A template describing a paycheck; nothing is saved."""
    template = _result(
        api,
        template_from_schedule(
            api.db, _text(payload, "schedule") or "", _text(payload, "name") or ""
        ),
    )
    return template_json(api, template)


def payroll_create(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    period_key = _text(payload, "period", optional=True) or "biweekly"
    if period_key not in PAY_PERIODS:
        raise ValueError("period must be one of " + ", ".join(PAY_PERIODS))
    period, interval = PAY_PERIODS[period_key]
    start = _date(payload, "start")
    recurrence = (
        Recurrence(period, start=start, day_of_month=15, second_day_of_month=-1)
        if period is PeriodType.SEMI_MONTH
        else Recurrence(period, interval=interval, start=start)
    )
    raw_gross = payload.get("gross")
    saved = _result(
        api,
        create_paycheck_schedule(
            api.db,
            CreatePaycheck(
                template=_text(payload, "template") or "",
                name=_text(payload, "name") or "",
                recurrence=recurrence,
                gross=(
                    api._input_money(dict(payload), raw_gross)
                    if raw_gross not in (None, "")
                    else None
                ),
            ),
        ),
    )
    return {"handle": saved.handle, "name": saved.name}


def _change(api: Api, payload: Mapping[str, Any]) -> PayChange:
    raw_scaled = payload.get("scaled")
    if raw_scaled is not None and (
        not isinstance(raw_scaled, list) or not all(isinstance(item, str) for item in raw_scaled)
    ):
        raise ValueError("scaled must be a list of account handles")
    raw_amounts = payload.get("amounts") or {}
    if not isinstance(raw_amounts, Mapping):
        raise ValueError("amounts must map account handles to amounts")
    return PayChange(
        schedule=_text(payload, "schedule") or "",
        start=_date(payload, "start"),
        gross=api._input_money(dict(payload), payload.get("gross")),
        scaled=frozenset(raw_scaled) if raw_scaled is not None else None,
        amounts={
            str(account): api._input_money(dict(payload), value)
            for account, value in raw_amounts.items()
        },
    )


def payroll_change_preview(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    return _result(api, preview_pay_change(api.db, _change(api, payload))).as_dict()


def payroll_change(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    return _result(api, apply_pay_change(api.db, _change(api, payload))).as_dict()
