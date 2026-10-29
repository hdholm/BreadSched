"""The JSON form of the schedule editors' typed requests (#356, #357).

Saving a fixed or formula schedule takes the services' own
``SaveFixedSchedule`` and ``FormulaScheduleInput``. Over HTTP the request must
travel as JSON, so this module encodes every field losslessly and decodes it back,
strictly: a value of the wrong type is a ``ValueError`` (a 400), never a guess.

Top-level amounts are decimal strings, as everywhere in the book API's JSON. Parts
that already have a stored form (recurrences, dated amount changes, seasonal
amounts, one-time adjustments, and a schedule an edit is based on) travel in that
form, so nothing is translated twice.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date
from enum import Enum
from typing import Any, TypeVar

from ..gen.lib.money import Money
from ..gen.lib.recurrence import Recurrence
from ..gen.lib.scheduled import (
    ScheduledAmountChange,
    ScheduledMonthAmount,
    ScheduledOccurrenceAdjustment,
    ScheduledSplitAmountChange,
    ScheduledTransaction,
    ScheduleGrowthPolicy,
)
from ..gen.lib.transaction import InvestmentActivityKind, PlanningFlowKind
from ..gen.services import (
    FixedScheduleInput,
    FixedSplitInput,
    FormulaScheduleInput,
    SaveFixedSchedule,
)
from .contract import to_json

__all__ = [
    "fixed_request_from_json",
    "fixed_request_json",
    "formula_request_from_json",
    "formula_request_json",
]

E = TypeVar("E", bound=Enum)
T = TypeVar("T")


# ------------------------------------------------------------------ encoding


def _enum(value: Enum | None) -> str | None:
    return None if value is None else str(value.value)


def _source(source: ScheduledTransaction | None) -> dict[str, Any] | None:
    return None if source is None else source.serialize()


def _split_json(split: FixedSplitInput) -> dict[str, Any]:
    return {
        "account": split.account,
        "amount": to_json(split.amount),
        "planning_flow": _enum(split.planning_flow),
        "investment_activity": _enum(split.investment_activity),
        "memo": split.memo,
        "opposite_direction": split.opposite_direction,
    }


def fixed_request_json(request: SaveFixedSchedule) -> dict[str, Any]:
    """``request`` as JSON data, every field included."""
    item = request.definition
    return {
        "existing_handle": request.existing_handle,
        "source": _source(request.source),
        "definition": {
            "name": item.name,
            "recurrence": item.recurrence.serialize(),
            "category": item.category,
            "funding": item.funding,
            "amount": to_json(item.amount),
            "category_planning_flow": _enum(item.category_planning_flow),
            "funding_planning_flow": _enum(item.funding_planning_flow),
            "investment_activity": _enum(item.investment_activity),
            "category_ledger_direction": item.category_ledger_direction,
            "additional_splits": [_split_json(split) for split in item.additional_splits],
            "category_memo": item.category_memo,
            "funding_memo": item.funding_memo,
            "enabled": item.enabled,
            "auto_create": item.auto_create,
            "placeholder": item.placeholder,
            "growth_policy": _enum(item.growth_policy),
            "amount_changes": [change.serialize() for change in item.amount_changes],
            "split_amount_changes": (
                None
                if item.split_amount_changes is None
                else {
                    account: [change.serialize() for change in changes]
                    for account, changes in item.split_amount_changes.items()
                }
            ),
            "seasonal_amounts": [amount.serialize() for amount in item.seasonal_amounts],
            "skipped": [when.isoformat() for when in item.skipped],
            "occurrence_adjustments": [
                adjustment.serialize() for adjustment in item.occurrence_adjustments
            ],
            "estimate_evidence": item.estimate_evidence,
            "advance_days": item.advance_days,
            "remind_days": item.remind_days,
        },
    }


def formula_request_json(request: FormulaScheduleInput) -> dict[str, Any]:
    """``request`` as JSON data; split positions become text keys."""
    return {
        "existing_handle": request.existing_handle,
        "source": _source(request.source),
        "name": request.name,
        "recurrence": request.recurrence.serialize(),
        "formulas": {str(index): formula for index, formula in request.formulas.items()},
        "variables": dict(request.variables),
        "enabled": request.enabled,
        "auto_create": request.auto_create,
        "placeholder": request.placeholder,
        "growth_policy": _enum(request.growth_policy),
        "skipped": [when.isoformat() for when in request.skipped],
        "advance_days": request.advance_days,
        "remind_days": request.remind_days,
    }


# ------------------------------------------------------------------ decoding


def _object(data: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(data, Mapping):
        raise ValueError(f"{name} must be an object")
    return data


def _text(data: Mapping[str, Any], key: str, *, optional: bool = False) -> Any:
    value = data.get(key)
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be text")
    return value


def _flag(data: Mapping[str, Any], key: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be true or false")
    return value


def _whole(data: Mapping[str, Any], key: str) -> int | None:
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be a whole number")
    return value


def _money(value: Any, key: str) -> Money:
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a decimal amount in text")
    try:
        return Money(value)
    except (ArithmeticError, ValueError, TypeError):
        raise ValueError(f"{key} is not an amount") from None


def _choice(kind: type[E], value: Any, key: str) -> E | None:
    if value is None:
        return None
    try:
        return kind(value)
    except ValueError:
        raise ValueError(f"{key} is not one of the known values") from None


def _parts(data: Mapping[str, Any], key: str, read: Callable[[Any], T]) -> tuple[T, ...]:
    raw = data.get(key)
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"{key} must be a list")
    try:
        return tuple(read(item) for item in raw)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{key} is not valid: {exc}") from None


def _recurrence(data: Mapping[str, Any]) -> Recurrence:
    try:
        return Recurrence.from_dict(dict(_object(data.get("recurrence"), "recurrence")))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"recurrence is not valid: {exc}") from None


def _source_from(data: Mapping[str, Any]) -> ScheduledTransaction | None:
    raw = data.get("source")
    if raw is None:
        return None
    try:
        return ScheduledTransaction.from_dict(dict(_object(raw, "source")))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"source is not valid: {exc}") from None


def _skipped(data: Mapping[str, Any]) -> tuple[date, ...]:
    return _parts(data, "skipped", lambda raw: date.fromisoformat(str(raw)))


def _split(raw: Any) -> FixedSplitInput:
    item = _object(raw, "additional split")
    return FixedSplitInput(
        account=_text(item, "account"),
        amount=_money(item.get("amount"), "amount"),
        planning_flow=_choice(PlanningFlowKind, item.get("planning_flow"), "planning_flow"),
        investment_activity=_choice(
            InvestmentActivityKind, item.get("investment_activity"), "investment_activity"
        ),
        memo=_text(item, "memo"),
        opposite_direction=_flag(item, "opposite_direction"),
    )


def fixed_request_from_json(data: Any) -> SaveFixedSchedule:
    """A ``SaveFixedSchedule`` from untrusted JSON data, or a ``ValueError``."""
    outer = _object(data, "request")
    item = _object(outer.get("definition"), "definition")
    raw_changes = item.get("split_amount_changes")
    split_changes: dict[str, tuple[ScheduledSplitAmountChange, ...]] | None = None
    if raw_changes is not None:
        split_changes = {
            str(account): _parts(
                {"changes": changes}, "changes", ScheduledSplitAmountChange.from_dict
            )
            for account, changes in _object(raw_changes, "split_amount_changes").items()
        }
    evidence = item.get("estimate_evidence")
    if evidence is not None and not isinstance(evidence, dict):
        raise ValueError("estimate_evidence must be an object")
    policy = _choice(ScheduleGrowthPolicy, item.get("growth_policy"), "growth_policy")
    direction = _whole(item, "category_ledger_direction")
    return SaveFixedSchedule(
        FixedScheduleInput(
            name=_text(item, "name"),
            recurrence=_recurrence(item),
            category=_text(item, "category"),
            funding=_text(item, "funding"),
            amount=_money(item.get("amount"), "amount"),
            category_planning_flow=_choice(
                PlanningFlowKind, item.get("category_planning_flow"), "category_planning_flow"
            ),
            funding_planning_flow=_choice(
                PlanningFlowKind, item.get("funding_planning_flow"), "funding_planning_flow"
            ),
            investment_activity=_choice(
                InvestmentActivityKind, item.get("investment_activity"), "investment_activity"
            ),
            category_ledger_direction=direction,
            additional_splits=_parts(item, "additional_splits", _split),
            category_memo=_text(item, "category_memo"),
            funding_memo=_text(item, "funding_memo"),
            enabled=_flag(item, "enabled"),
            auto_create=_flag(item, "auto_create"),
            placeholder=_flag(item, "placeholder"),
            growth_policy=policy or ScheduleGrowthPolicy.AUTO,
            amount_changes=_parts(item, "amount_changes", ScheduledAmountChange.from_dict),
            split_amount_changes=split_changes,
            seasonal_amounts=_parts(item, "seasonal_amounts", ScheduledMonthAmount.from_dict),
            skipped=_skipped(item),
            occurrence_adjustments=_parts(
                item, "occurrence_adjustments", ScheduledOccurrenceAdjustment.from_dict
            ),
            estimate_evidence=evidence,
            advance_days=_whole(item, "advance_days"),
            remind_days=_whole(item, "remind_days"),
        ),
        existing_handle=_text(outer, "existing_handle", optional=True),
        source=_source_from(outer),
    )


def formula_request_from_json(data: Any) -> FormulaScheduleInput:
    """A ``FormulaScheduleInput`` from untrusted JSON data, or a ``ValueError``."""
    item = _object(data, "request")
    formulas_raw = _object(item.get("formulas"), "formulas")
    try:
        formulas = {int(index): str(formula) for index, formula in formulas_raw.items()}
    except ValueError:
        raise ValueError("formulas are keyed by split position") from None
    variables_raw = _object(item.get("variables"), "variables")
    if not all(isinstance(value, str) for value in variables_raw.values()):
        raise ValueError("each variable is text")
    policy = _choice(ScheduleGrowthPolicy, item.get("growth_policy"), "growth_policy")
    return FormulaScheduleInput(
        existing_handle=_text(item, "existing_handle", optional=True),
        name=_text(item, "name"),
        recurrence=_recurrence(item),
        formulas=formulas,
        variables={str(key): str(value) for key, value in variables_raw.items()},
        enabled=_flag(item, "enabled"),
        auto_create=_flag(item, "auto_create"),
        placeholder=_flag(item, "placeholder"),
        growth_policy=policy or ScheduleGrowthPolicy.AUTO,
        skipped=_skipped(item),
        source=_source_from(item),
        advance_days=_whole(item, "advance_days"),
        remind_days=_whole(item, "remind_days"),
    )
