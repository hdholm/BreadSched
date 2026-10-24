"""What a schedule editor needs to show a definition without reading the book.

``engine.schedule.schedule_edit_projection`` decides how a definition can be edited
and which split is which; this view carries that projection with the definition
and the class of every account its splits use, so an editor lays the definition
out (and decides whether its simple form can show it) from the view alone.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from ..db.sqlite import DbSQLite
from ..engine import schedule as schedule_engine
from ..engine.schedule import ScheduleEditProjection
from ..lib.account import AccountClass
from ..lib.scheduled import ScheduledTransaction

__all__ = ["ScheduleEditorView", "editor_view"]


@dataclass(frozen=True, slots=True)
class ScheduleEditorView:
    schedule: ScheduledTransaction
    projection: ScheduleEditProjection
    #: The class of each account the definition's splits use, by handle.
    classes: Mapping[str, AccountClass] = field(default_factory=dict)


def editor_view(db: DbSQLite, scheduled: ScheduledTransaction) -> ScheduleEditorView:
    classes = {}
    for split in scheduled.splits:
        account = db.get_account(split.account)
        if account is not None:
            classes[split.account] = account.account_class
    return ScheduleEditorView(
        scheduled, schedule_engine.schedule_edit_projection(db, scheduled), classes
    )
