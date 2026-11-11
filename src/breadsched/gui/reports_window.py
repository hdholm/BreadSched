"""The Reports window: every report in one place, each open report in its own tab.

The Reports menu, a view's report button, and the window's own **Open Report** menu
all open a report here. Each report is a tab with a close button, so several can be
open at once and compared. The interactive reports (Expense Explorer, Budget Jars,
Net Worth History, Holdings and Cost Basis, Realized Gains, Tax Year) keep their
controls; every other report breadlet opens as its document with its options.

A report is built as the window it has always been and its content is moved into
the tab; the window object stays alive with the tab as the report's controller, and
its Close button closes the tab.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from ..gen.engine.activity import ReportingPeriod
from ..gen.lib.scenario import Scenario
from ..gen.plug import REPORT, PluginManager
from ..gen.services.plan import PlanQuery
from .gi_setup import Gio, GLib, Gtk
from .widgets.bounded import BoundedWindow
from .widgets.help import help_row

__all__ = ["REPORTS", "ReportsWindow", "ReportSpec", "open_report", "report_specs"]


@dataclass(frozen=True)
class ReportSpec:
    """One report the window offers: its key, its menu label, and how to build it."""

    key: str
    label: str
    #: ``build(parent, db, manager)`` returns the report as a window.
    build: Callable[[Gtk.Window, object, object], Gtk.Window]


def _plan_context(
    manager,
) -> tuple[date | None, date | None, ReportingPeriod, str | None, Scenario | None]:
    """The Plan view's range, grouping and scenario when it is open; else no range,
    which the Plan's own default fills."""
    from .planning_context import baseline_scenario

    view = getattr(manager, "_views", {}).get("plan") if manager is not None else None
    if view is not None and getattr(view, "db", None) is not None:
        return (
            view._start_date,
            view._end_date,
            view._grouping(),
            view._scenario_handle,
            baseline_scenario(manager, view.db),
        )
    return None, None, ReportingPeriod.MONTH, None, None


def _expense_explorer(parent, db, manager):
    from .dialogs.expense_explorer_dialog import ExpenseExplorerDialog

    start, end, period, scenario, baseline = _plan_context(manager)
    query = PlanQuery(start=start, end=end, period=period, scenario=scenario, baseline=baseline)
    return ExpenseExplorerDialog(parent, db, query)


def _budget_jars(parent, db, manager):
    from .dialogs.budget_jars_dialog import BudgetJarsDialog

    start, end, period, _scenario, _baseline = _plan_context(manager)
    today = date.today()
    return BudgetJarsDialog(
        parent, db, start or date(today.year, 1, 1), end or date(today.year, 12, 31), period
    )


def _net_worth(parent, db, _manager):
    from .dialogs.net_worth_history_dialog import NetWorthHistoryDialog

    return NetWorthHistoryDialog(parent, db)


def _holdings(parent, db, _manager):
    from .dialogs.holdings_dialog import HoldingsDialog

    return HoldingsDialog(parent, db)


def _realized_gains(parent, db, _manager):
    from .dialogs.realized_gains_dialog import RealizedGainsDialog

    return RealizedGainsDialog(parent, db)


def _tax_year(parent, db, _manager):
    from .dialogs.tax_year_dialog import TaxYearDialog

    return TaxYearDialog(parent, db)


#: The interactive reports, in menu order. A report breadlet with the same key is
#: this report, so it is not offered twice.
REPORTS = (
    ReportSpec("expense-explorer", "Expense Explorer", _expense_explorer),
    ReportSpec("budget-jars", "Budget Jars", _budget_jars),
    ReportSpec("net-worth", "Net Worth History", _net_worth),
    ReportSpec("holdings", "Holdings and Cost Basis", _holdings),
    ReportSpec("realized-gains", "Realized Gains", _realized_gains),
    ReportSpec("tax-year", "Tax Year", _tax_year),
)


def _breadlet(breadlet: str):
    def build(parent, db, _manager):
        from .dialogs.breadlets_dialog import BreadletReportsDialog

        return BreadletReportsDialog(parent, db, breadlet, fixed=True)

    return build


def report_specs() -> tuple[ReportSpec, ...]:
    """Every report: the interactive ones, then each other report breadlet by name."""
    known = {spec.key for spec in REPORTS}
    extra = tuple(
        ReportSpec(plugin.id, plugin.name, _breadlet(plugin.id))
        for plugin in PluginManager.instance().by_category(REPORT)
        if plugin.id not in known
    )
    return REPORTS + extra


class _Tab:
    """An open report: the window that built it, and its page in the notebook."""

    def __init__(self, report: Gtk.Window, page: Gtk.Widget, label: str) -> None:
        self.report = report
        self.page = page
        self.label = label


class ReportsWindow(BoundedWindow):
    """Open reports, one per tab."""

    def __init__(self, parent: Gtk.Window | None, db, manager=None) -> None:
        super().__init__(title="Reports", transient_for=None)
        self.set_default_size(1200, 820)
        self.db = db
        self.manager = manager
        self.parent_window = parent
        self.tabs: list[_Tab] = []

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for side in ("top", "bottom", "start", "end"):
            getattr(outer, f"set_margin_{side}")(8)
        self.set_child(outer)
        bar = Gtk.Box(spacing=8)
        menu = Gio.Menu()
        for spec in report_specs():
            menu.append(f"{spec.label}…", f"reports.open::{spec.key}")
        self.open_button = Gtk.MenuButton(label="Open Report", menu_model=menu)
        self.open_button.set_tooltip_text("Open a report in a new tab")
        bar.append(self.open_button)
        help_box = help_row("reports")
        help_box.set_hexpand(True)
        bar.append(help_box)
        outer.append(bar)
        actions = Gio.SimpleActionGroup()
        action = Gio.SimpleAction.new("open", GLib.VariantType.new("s"))
        action.connect("activate", lambda _a, key: self.open(key.get_string()))
        actions.add_action(action)
        self.insert_action_group("reports", actions)

        self.notebook = Gtk.Notebook(scrollable=True, vexpand=True)
        self.notebook.connect("page-removed", lambda *_: self._empty_note())
        outer.append(self.notebook)
        self.empty = Gtk.Label(
            label="Choose Open Report to open a report. Each opens in its own tab.",
            vexpand=True,
        )
        self.empty.add_css_class("dim-label")
        outer.append(self.empty)
        self._empty_note()

    def _empty_note(self) -> None:
        shown = bool(self.tabs)
        self.notebook.set_visible(shown)
        self.empty.set_visible(not shown)

    def open(self, key: str) -> _Tab | None:
        """Open the report ``key`` in a new tab and show it."""
        spec = next((item for item in report_specs() if item.key == key), None)
        if spec is None:
            return None
        return self.adopt(spec.build(self, self.db, self.manager), spec.label)

    def adopt(self, report: Gtk.Window, label: str | None = None) -> _Tab:
        """Move ``report``'s content into a new tab; its Close button closes the tab."""
        content = report.get_child()
        report.set_child(None)
        title = label or report.get_title() or "Report"
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        page.append(content)
        content.set_vexpand(True)
        tab = _Tab(report, page, title)
        header = Gtk.Box(spacing=4)
        header.append(Gtk.Label(label=title))
        close = Gtk.Button(icon_name="window-close-symbolic", has_frame=False)
        close.set_tooltip_text(f"Close {title}")
        close.update_property([Gtk.AccessibleProperty.LABEL], [f"Close {title}"])
        close.connect("clicked", lambda _b: self.close_tab(tab))
        header.append(close)
        self.tabs.append(tab)
        index = self.notebook.append_page(page, header)
        self.notebook.set_tab_reorderable(page, True)
        # The report's own Close button closes its tab. (A window never shown is not
        # sent close-request, so the tab sends it, letting the report stop its work.)
        report.close = lambda: self.close_tab(tab)
        self._empty_note()
        self.notebook.set_current_page(index)
        self.present()
        return tab

    def close_tab(self, tab: _Tab) -> bool:
        """Close one report: it hears close-request (to stop its work), then its tab goes."""
        if tab not in self.tabs:
            return False
        tab.report.emit("close-request")
        self.tabs.remove(tab)
        index = self.notebook.page_num(tab.page)
        if index >= 0:
            self.notebook.remove_page(index)
        tab.report.destroy()
        self._empty_note()
        return True

    def close_all(self) -> None:
        for tab in list(self.tabs):
            self.close_tab(tab)

    @property
    def current(self) -> _Tab | None:
        page = self.notebook.get_nth_page(self.notebook.get_current_page())
        return next((tab for tab in self.tabs if tab.page is page), None)


def reports_window(window: Gtk.Window | None, db) -> ReportsWindow:
    """The Reports window of ``window``'s main window, created on first use."""
    from .viewmanager import ViewManager

    manager = window
    while manager is not None and not isinstance(manager, ViewManager):
        manager = manager.get_transient_for()
    holder = manager if manager is not None else window
    found = getattr(holder, "reports_window", None)
    if found is None or found.db is not db:
        if found is not None:
            found.destroy()
        found = ReportsWindow(manager, db, manager)
        if holder is not None:
            holder.reports_window = found
    return found


def open_report(window: Gtk.Window | None, db, key: str) -> _Tab | None:
    """Open report ``key`` in a tab of the Reports window."""
    return reports_window(window, db).open(key)
