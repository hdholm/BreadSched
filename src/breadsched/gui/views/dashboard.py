"""The dashboard: the view a book opens on, drawn entirely from dashboard breadlets.

Every item on the Dashboard is a dashboard breadlet (``plugins.breadlets``): the
summary figures, the cash-flow chart, the account groups, pending bills, expected
income, pending reimbursables, FSA claims needing attention, savings goals, and any
other (shipped or installed) the household shows. Each returns a document this view
draws with :class:`DocumentView`; their rows and cards link to what they are (a
schedule, an account, the savings goals...), which :meth:`DashboardView.follow`
opens, and their tables sort and choose columns like every other table.

Everything the Dashboard can be told lives in **Configure Dashboard** (#381): the two
horizons, which items show, the account groups, and each table's columns. The
breadlets share one Dashboard calculation per refresh (``breadlets.dashboard.sharing``).
"""

from __future__ import annotations

from dataclasses import replace

from ...api import ApiError
from ...api.local import book_server
from ...gen.engine import dashboard as engine
from ...gen.plug import DASHBOARD, PluginManager
from ...plugins.breadlets.dashboard import sharing
from ..gi_setup import Gtk
from ..widgets.document_view import DocumentView
from ..widgets.wrap_box import WrapBox
from ._base import BaseView

__all__ = ["DashboardView"]

#: Items as wide as the window, above the side-by-side sections.
_FULL_WIDTH = ("summary", "cash_flow")
#: Upper bounds for a table section's natural size; beyond them the table scrolls
#: inside its section instead of widening the window (#140, #152).
_SECTION_MAX_WIDTH = 900
_SECTION_MAX_HEIGHT = 420


class DashboardView(BaseView):
    """The Dashboard's breadlets, in the order Configure Dashboard keeps."""

    PRINTABLE = True

    WATCHES = (
        "database-changed",
        "transaction-add",
        "transaction-update",
        "transaction-delete",
        "scheduled-add",
        "scheduled-update",
        "scheduled-delete",
        "account-update",
        "savings-goal-add",
        "savings-goal-update",
        "savings-goal-delete",
    )

    def __init__(self, manager) -> None:
        super().__init__(manager)
        self.config: engine.DashboardConfig | None = None
        self.board: engine.Dashboard | None = None
        #: Each shown breadlet's view, by breadlet id, in the order shown.
        self.documents: dict[str, DocumentView] = {}
        #: The documents the shown breadlets made, in order, for printing.
        self.breadlet_documents: list = []
        self._build()

    # ------------------------------------------------------------------ layout

    def _build(self) -> None:
        bar = Gtk.Box(spacing=8)
        for side in ("top", "bottom", "start", "end"):
            getattr(bar, f"set_margin_{side}")(8)
        title = Gtk.Label(label="Dashboard", xalign=0)
        title.add_css_class("category-title")
        title.set_hexpand(True)
        bar.append(title)
        # Configure Dashboard is a toolbar icon and Actions menu item (#156).
        self._bar = bar
        self.append_toolbar(bar)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("start", "end", "bottom"):
            getattr(content, f"set_margin_{side}")(12)
        self.full_width = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.append(self.full_width)
        # The other items sit side by side when the window has room and stack when it
        # does not (#152); one not shown takes no room (#397).
        self.sections = WrapBox(column_spacing=24, row_spacing=12)
        self.sections.set_valign(Gtk.Align.START)
        content.append(self.sections)
        outer = Gtk.ScrolledWindow(child=content)
        outer.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        outer.set_vexpand(True)
        self.append(outer)

    def _settings(self):
        return getattr(self.manager.get_application(), "view_settings", None)

    def _today(self):
        from datetime import date

        return self.board.as_of if self.board else date.today()

    # ------------------------------------------------------------------- model

    def refresh(self) -> None:
        if self.db is None:
            return
        book = sharing(book_server(self.db))
        self.board = book.dashboard()
        self.config = self.board.config
        self._render(book)

    def redraw(self) -> None:
        """Draw the Dashboard again from the board already calculated."""
        if self.db is not None and self.board is not None:
            self._render(sharing(book_server(self.db), self.board))

    def _shown(self):
        from ...gen.services.dashboard_config import settings_of

        assert self.config is not None
        manager = PluginManager.instance()
        for item in settings_of(self.config).breadlets:
            plugin = manager.get(DASHBOARD, item.id)
            if item.shown and plugin is not None:
                yield item, plugin

    def _render(self, book) -> None:
        for container in (self.full_width, self.sections):
            while (child := container.get_first_child()) is not None:
                container.remove(child)
        self.documents = {}
        self.breadlet_documents = []
        for item, plugin in self._shown():
            try:
                document = plugin.produce(book, {}, self._today())
            except ApiError as error:
                failed = Gtk.Label(label=f"{item.name}: {error}", xalign=0, wrap=True)
                failed.add_css_class("error")
                self.sections.append(failed)
                continue
            view = DocumentView(titled=False, on_link=self.follow, settings=self._settings())
            view.set_name(f"breadlet-{item.id}")
            view.show_document(document)
            if not view.get_first_child():
                continue  # a section with nothing to show takes no room
            self.documents[item.id] = view
            self.breadlet_documents.append((item.id, document))
            if item.id in _FULL_WIDTH:
                self.full_width.append(view)
            else:
                self._add_section(view)

    def _add_section(self, view: DocumentView) -> None:
        """Size a section to its content, bounded so no row can widen the window."""
        view.add_css_class("dashboard-section")
        view.set_halign(Gtk.Align.START)
        view.set_valign(Gtk.Align.START)
        view.set_hexpand(False)
        for section in view.table_sections.values():
            # Columns are chosen in Configure Dashboard (#381), not in each heading.
            section.chooser.set_visible(False)
            section.scroller.set_propagate_natural_width(True)
            section.scroller.set_propagate_natural_height(True)
            section.scroller.set_max_content_width(_SECTION_MAX_WIDTH)
            section.scroller.set_max_content_height(_SECTION_MAX_HEIGHT)
        self.sections.append(view)

    # ------------------------------------------------------------------ access

    def shown_sections(self) -> list[str]:
        """The ids of the breadlets shown, in order."""
        return list(self.documents)

    def table(self, key: str) -> Gtk.ColumnView | None:
        """The interactive table shown under ``key`` (such as ``dashboard`` for bills)."""
        for view in self.documents.values():
            if key in view.tables:
                return view.tables[key]
        return None

    def section(self, breadlet: str) -> DocumentView | None:
        """The shown view of one Dashboard item, by its breadlet id."""
        return self.documents.get(breadlet)

    @property
    def cards(self) -> Gtk.FlowBox | None:
        """The summary figures' cards, when shown."""
        summary = self.documents.get("summary")
        return summary.card_rows[0] if summary is not None and summary.card_rows else None

    def column_tables(self) -> tuple[tuple[str, str, Gtk.ColumnView], ...]:
        """The Dashboard's tables as Configure Dashboard lists their columns."""
        found = []
        for view in self.documents.values():
            for key, table in view.tables.items():
                section = view.table_sections[key]
                found.append((section.heading.get_text().split(" (")[0], key, table))
        return tuple(found)

    # ----------------------------------------------------------------- printing

    def printable_report(self):
        """The Dashboard as shown: its sections, then its other breadlets."""
        if self.board is None:
            return None
        from ...plugins.export.report_layout import dashboard_layout

        currency = book_server(self.db).reporting_currency() if self.db is not None else ""
        document = dashboard_layout(self.board, book_name=self.book_name(), currency=currency)
        extra = tuple(
            section
            for key, item in self.breadlet_documents
            if key not in engine.SECTIONS
            for section in item.sections
        )
        return replace(document, sections=document.sections + extra)

    def printable_html(self) -> str | None:
        """The same report as a web page, for printing from a browser."""
        from ...plugins.export.html_report import render_html

        document = self.printable_report()
        return render_html(document) if document is not None else None

    # ----------------------------------------------------------------- actions

    def follow(self, link: str) -> None:
        """Open what a Dashboard row or card names (``report_layout.LINK_KINDS``)."""
        if self.db is None or not link:
            return
        kind, _, target = link.partition(":")
        app = self.manager.get_application()
        if kind == "schedule":
            self.manager.open_schedule(target)
        elif kind == "register":
            self.manager.open_register(target)
        elif kind == "account":
            try:
                account = book_server(self.db).account(target).account
            except ApiError:
                return
            from ..dialogs.account_dialog import AccountDialog

            dialog = AccountDialog(self.get_root(), self.db, account=account)
            dialog.connect("close-request", self.refresh_on_close)
            dialog.present()
        elif kind == "due-review":
            app.on_post_scheduled()
        elif kind == "goals":
            app.on_savings_goals()
        elif kind == "receivables":
            app.on_receivables(parent=self.get_root())
        elif kind == "fsa-claims":
            app.on_fsa_claims()
        elif kind == "report":
            from ..reports_window import open_report

            open_report(self.get_root(), self.db, target)

    def _on_configure(self, _button) -> None:
        if self.db is None:
            return
        from ..dialogs.dashboard_dialog import DashboardDialog

        dialog = DashboardDialog(
            self.get_root(), self.db, tables=self.column_tables(), settings=self._settings()
        )
        dialog.connect("close-request", self.refresh_on_close)
        dialog.present()

    def _open_net_worth_history(self, _button) -> None:
        if self.db is None:
            return
        from ..reports_window import open_report

        open_report(self.get_root(), self.db, "net-worth")
