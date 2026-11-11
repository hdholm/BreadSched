"""The Dashboard's own items as dashboard breadlets: one per ``dashboard.SECTIONS``.

Each reads ``book.dashboard()`` and returns its section of the Dashboard
(``report_layout.dashboard_section_blocks``): the summary figures, the cash-flow
chart, the account groups, pending bills, expected income, pending reimbursables,
FSA claims needing attention, and savings goals. Their rows link to what they are
(a schedule, an account, the savings goals...), so the screen can open it, and their
tables sort and choose columns. An interface running several at once wraps the book
in :func:`sharing`, so they share one Dashboard calculation.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

from ...gen.engine.dashboard import DEFAULT_SECTIONS, SECTIONS
from ...gen.plug._breadlets import DASHBOARD
from ...gen.plug._manager import Plugin, PluginManager
from ..export.report_layout import ReportDocument, Section, dashboard_section_blocks

__all__ = ["register", "sharing"]

#: What each section shows, for Configure Dashboard's descriptions.
_DESCRIPTIONS = {
    "summary": "Net worth, liquid cash, what is needed soon, and the emergency fund",
    "cash_flow": "The cash you will have each day of the liquidity period",
    "groups": "Each group of accounts with its value, what is owed, and equity",
    "bills": "Bills due in the liquidity period, each with what is in its jar",
    "income": "Pay and other income expected in the liquidity period",
    "reimbursables": "Each reimbursable expense still owed",
    "claims": "FSA claims that need something from you",
    "goals": "Each savings goal with what is in its jar and what remains",
}


class _Sharing:
    """A book whose Dashboard and reporting currency are read once."""

    def __init__(self, book: Any) -> None:
        self._book = book
        self._found: dict[str, Any] = {}

    def dashboard(self, *args, **kwargs):
        if args or kwargs:
            return self._book.dashboard(*args, **kwargs)
        if "dashboard" not in self._found:
            self._found["dashboard"] = self._book.dashboard()
        return self._found["dashboard"]

    def reporting_currency(self) -> str:
        if "currency" not in self._found:
            self._found["currency"] = self._book.reporting_currency()
        return self._found["currency"]

    def __getattr__(self, name: str):
        return getattr(self._book, name)


def sharing(book: Any, board: Any = None) -> Any:
    """``book``, reading the Dashboard and reporting currency at most once; ``board``
    is a Dashboard already calculated (over other horizons, say) for them to use."""
    shared = book if isinstance(book, _Sharing) else _Sharing(book)
    if board is not None:
        shared._found["dashboard"] = board
    return shared


def _section(name: str):
    def run(book: Any, _options: Mapping[str, Any], _today: date) -> ReportDocument:
        board = book.dashboard()
        blocks = dashboard_section_blocks(board, name, book.reporting_currency())
        return ReportDocument(
            SECTIONS[name],
            f"As at {board.as_of.isoformat()}",
            (Section(blocks, name=name),),
            kind="dashboard",
        )

    return run


def register(manager: PluginManager) -> None:
    """Register a dashboard breadlet for each of the Dashboard's own sections."""
    for name, title in SECTIONS.items():
        manager.register(
            Plugin(
                name,
                title,
                DASHBOARD,
                _section(name),
                _DESCRIPTIONS[name],
                shown_by_default=DEFAULT_SECTIONS[name],
            )
        )
