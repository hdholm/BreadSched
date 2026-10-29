"""What an imported account brought from GnuCash, and how a re-import merges it.

GnuCash is not the book's system of record: once imported, an account's name,
parent, code, description, placeholder and hidden flags, commodity, SCU, and the
other GnuCash fields it carries are BreadSched's to change. The account keeps the
values it was last imported with (``Account.imported_values``), so a re-import
updates only what changed in GnuCash since, and keeps what was changed here. A field
changed on both sides keeps BreadSched's value, as account notes do (#385).

Accounts imported before these fields could be changed have no stored values; they
were read-only until then, so the values the account holds are the ones last
imported (:func:`baseline`).
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from .account import Account

__all__ = ["CHART_FIELDS", "baseline", "imported_values", "merge_reimport"]

#: The chart fields an import fills and a person may then change.
CHART_FIELDS = (
    "name",
    "parent",
    "code",
    "description",
    "placeholder",
    "hidden",
    "commodity",
    "commodity_scu",
)
_FIELD = "field:"


def imported_values(account: Account) -> dict[str, Any]:
    """The values to remember as imported: chart fields and each GnuCash field."""
    values: dict[str, Any] = {name: getattr(account, name) for name in CHART_FIELDS}
    values.update({_FIELD + field.name: field.value for field in account.source_fields})
    return values


def baseline(account: Account) -> dict[str, Any]:
    """The values ``account`` was last imported with."""
    return dict(account.imported_values) or imported_values(account)


def merge_reimport(local: Account, incoming: Account) -> int:
    """Keep in ``incoming`` (the account as GnuCash has it now) every field changed
    in BreadSched since the last import; the number changed on both sides, where
    BreadSched's value is kept too. ``incoming`` remembers what GnuCash sent."""
    base = baseline(local)
    sent = imported_values(incoming)
    conflicts = 0
    for name in CHART_FIELDS:
        mine, theirs = getattr(local, name), sent[name]
        if mine == base.get(name, theirs) or mine == theirs:
            continue
        if theirs != base.get(name, mine):
            conflicts += 1
        setattr(incoming, name, mine)
    mine_fields = {field.name: field for field in local.source_fields}
    merged = []
    for field in incoming.source_fields:
        mine_field = mine_fields.get(field.name)
        key = _FIELD + field.name
        if mine_field is None or mine_field.value in (base.get(key, field.value), field.value):
            merged.append(field)
            continue
        if field.value != base.get(key, mine_field.value):
            conflicts += 1
        merged.append(replace(field, value=mine_field.value))
    incoming.source_fields = merged
    incoming.imported_values = sent
    return conflicts
