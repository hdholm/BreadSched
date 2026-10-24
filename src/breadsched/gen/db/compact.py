"""Minimized transaction JSON: leave out what reading would supply anyway (#354).

Decision 0001's re-evaluation measured a stored transaction at about half the size
when keys at their default value are left out, with loading no slower. A key is left
out only when ``Transaction.from_dict`` or ``Split.from_dict`` already reads its
absence as exactly that value with a constant ``.get`` default. So any version of
BreadSched, including those before this change, reads a minimized book unchanged,
and no schema change is needed.

The defaults are pinned here, not computed, and ``tests/test_compact_storage.py``
fails if reading ever supplies a different value. Changing one means migrating
books. Keys a reader requires (handles, accounts, dates, values, quantities, the
change marker, and ``_schema``) are always written.

:func:`expand_transaction` restores the full form, so storage compares, journals,
and replays complete records exactly as before.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

__all__ = [
    "SPLIT_DEFAULTS",
    "TRANSACTION_DEFAULTS",
    "expand_transaction",
    "minimize_transaction",
]

#: Transaction keys read with these constant defaults when absent.
TRANSACTION_DEFAULTS: dict[str, Any] = {
    "gid": "",
    "private": False,
    "tags": [],
    "description": "",
    "currency": None,
    "num": "",
    "notes": "",
    "source_notes": "",
    "scheduled_from": None,
    "planned_occurrence": None,
    "planned_for": None,
    "planned_amount": None,
    "rejected_plan_occurrences": [],
    "attachments": [],
    "source_link": "",
}

#: Split keys read with these constant defaults when absent.
SPLIT_DEFAULTS: dict[str, Any] = {
    "memo": "",
    "action": "",
    "reconcile": "n",
    "reconcile_date": None,
    "planning_flow": None,
    "investment_activity": None,
    "fsa_year_start": None,
}


def _resolution_default(data: dict[str, Any]) -> str:
    """What reading supplies for a missing ``planning_resolution`` (see ``_unserialize``)."""
    planned = data.get("planned_occurrence") is not None or data.get("scheduled_from") is not None
    return "matched" if planned else "unresolved"


def minimize_transaction(data: dict[str, Any]) -> dict[str, Any]:
    """``data`` without the keys reading restores to the same value."""
    kept = {
        key: value
        for key, value in data.items()
        if key not in TRANSACTION_DEFAULTS or TRANSACTION_DEFAULTS[key] != value
    }
    if kept.get("planning_resolution") == _resolution_default(data):
        del kept["planning_resolution"]
    if "splits" in data:
        kept["splits"] = [
            {
                key: value
                for key, value in split.items()
                if key not in SPLIT_DEFAULTS or SPLIT_DEFAULTS[key] != value
            }
            for split in data["splits"]
        ]
    return kept


def expand_transaction(data: dict[str, Any]) -> dict[str, Any]:
    """The full stored form of a minimized (or already full) transaction."""
    full = {**deepcopy(TRANSACTION_DEFAULTS), **data}
    if "planning_resolution" not in full:
        full["planning_resolution"] = _resolution_default(full)
    if "splits" in full:
        full["splits"] = [{**deepcopy(SPLIT_DEFAULTS), **split} for split in full["splits"]]
    return full
