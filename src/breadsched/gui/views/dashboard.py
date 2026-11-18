"""The dashboard: the view a book opens on, drawn entirely from dashboard breadlets.

Every item on the Dashboard is a dashboard breadlet (``plugins.breadlets``): the
summary figures, the cash-flow chart, the account groups, pending bills, expected
income, pending reimbursables, FSA claims needing attention, savings goals, and any
other (shipped or installed) the household shows. Each returns a document this view
draws with :class:`DocumentView`; their rows and cards link to what they are (a
schedule, an account, the savings goals...), which :meth:`DashboardView.follow`
opens, and their tables sort and choose columns like every other table.

The items sit on a grid (``engine.dashboard_grid``): each has a place in the order,
a width in columns and a height in rows, and scrolls inside its tile when it holds
more. **Arrange Dashboard** lets the household move them (dragging an item by its
title, or with its arrows), resize them (dragging its corner, or with its width and
height), hide them, and add hidden ones back; **Done** saves the arrangement with
the rest of the Dashboard's settings. A narrow window has fewer columns and stacks
the same items in the same order.

Everything else the Dashboard can be told lives in **Configure Dashboard** (#381):
the two horizons, which items show, the account groups, and each table's columns.
The breadlets share one Dashboard calculation per refresh
(``breadlets.dashboard.sharing``).
"""

from __future__ import annotations

from dataclasses import replace

from ...api import ApiError
from ...api.local import book_server
from ...gen.engine import dashboard as engine
from ...gen.engine import dashboard_grid as grid
from ...gen.plug import DASHBOARD, PluginManager
from ...gen.services import DashboardTileInput, SaveDashboardConfig
from ...gen.services.dashboard_config import DashboardBreadlet, default_layout, settings_of
from ...plugins.breadlets.dashboard import sharing
from ..gi_setup import Gdk, GObject, Gtk, Pango
from ..widgets.document_view import DocumentView
from ..widgets.model_chart import ModelChartView
from ._base import BaseView

__all__ = ["DashboardView"]

#: Space between tiles, in pixels.
GAP = 12
#: The narrowest a column may be before the grid has one fewer.
MIN_COLUMN_WIDTH = 240


#: The least height a chart in a tile asks for; a taller tile's chart grows to fill it.
_CHART_HEIGHT = 140


def _tile_height(rows: int) -> int:
    return rows * grid.ROW_HEIGHT + (rows - 1) * GAP


def _charts(widget: Gtk.Widget):
    """Every chart drawn inside ``widget``."""
    child = widget.get_first_child()
    while child is not None:
        if isinstance(child, ModelChartView):
            yield child
        else:
            yield from _charts(child)
        child = child.get_next_sibling()


class DashboardView(BaseView):
    """The Dashboard's breadlets on their grid, in the order arranged."""

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
        #: Every item, shown or not, in order with its size and name.
        self.items: list[DashboardBreadlet] = []
        #: Each drawn item's tile, by its key.
        self.tiles: dict[str, Gtk.Widget] = {}
        #: While arranging: the items as they are being arranged, not yet saved.
        self.arranging = False
        self.columns = grid.COLUMNS
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
        # Configure and Arrange Dashboard are toolbar icons and Actions items (#156).
        self._bar = bar
        self.append_toolbar(bar)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("start", "end", "bottom"):
            getattr(content, f"set_margin_{side}")(12)
        self.arrange_bar = self._arrange_bar()
        self.arrange_bar.set_visible(False)
        content.append(self.arrange_bar)
        self.grid = Gtk.Grid(column_spacing=GAP, row_spacing=GAP)
        self.grid.set_column_homogeneous(True)
        self.grid.set_row_homogeneous(True)
        self.grid.set_valign(Gtk.Align.START)
        content.append(self.grid)
        self.failures = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        content.append(self.failures)
        outer = Gtk.ScrolledWindow(child=content)
        outer.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        outer.set_vexpand(True)
        # A narrower window has fewer columns (the same items, in the same order).
        outer.get_hadjustment().connect("notify::page-size", self._on_width)
        self.outer = outer
        self.append(outer)

    def _arrange_bar(self) -> Gtk.Box:
        bar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        bar.add_css_class("dashboard-arrange-bar")
        hint = Gtk.Label(
            label=(
                "Drag an item by its title to move it, or its corner to resize it; "
                "its arrows and size button do the same from the keyboard."
            ),
            xalign=0,
            wrap=True,
        )
        hint.add_css_class("dim-label")
        bar.append(hint)
        buttons = Gtk.Box(spacing=8)
        self.add_menu = Gtk.MenuButton(label="Add Item")
        self.add_menu.set_tooltip_text("Show an item the Dashboard hides")
        self.add_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.add_menu.set_popover(Gtk.Popover(child=self.add_list))
        buttons.append(self.add_menu)
        for label, handler in (
            ("Reset Layout", self.reset_layout),
            ("Cancel", self.cancel_arranging),
        ):
            button = Gtk.Button(label=label)
            button.connect("clicked", lambda _b, call=handler: call())
            buttons.append(button)
        done = Gtk.Button(label="Done")
        done.add_css_class("suggested-action")
        done.connect("clicked", lambda _b: self.finish_arranging())
        buttons.append(done)
        bar.append(buttons)
        self.arrange_status = Gtk.Label(xalign=0, wrap=True, visible=False)
        self.arrange_status.add_css_class("error")
        bar.append(self.arrange_status)
        return bar

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
        if not self.arranging:
            self.items = list(settings_of(self.config).breadlets)
        self._render(book)

    def redraw(self) -> None:
        """Draw the Dashboard again from the board already calculated."""
        if self.db is not None and self.board is not None:
            self._render(sharing(book_server(self.db), self.board))

    def _render(self, book) -> None:
        while (child := self.grid.get_first_child()) is not None:
            self.grid.remove(child)
        while (child := self.failures.get_first_child()) is not None:
            self.failures.remove(child)
        self.documents = {}
        self.breadlet_documents = []
        self.tiles = {}
        manager = PluginManager.instance()
        for item in self.items:
            plugin = manager.get(DASHBOARD, item.id)
            if not item.shown or plugin is None:
                continue
            try:
                document = plugin.produce(book, {}, self._today())
            except ApiError as error:
                failed = Gtk.Label(label=f"{item.name}: {error}", xalign=0, wrap=True)
                failed.add_css_class("error")
                self.failures.append(failed)
                continue
            view = DocumentView(titled=False, on_link=self.follow, settings=self._settings())
            view.set_name(f"breadlet-{item.id}")
            view.show_document(document)
            if not view.get_first_child() and not self.arranging:
                continue  # an item with nothing to show takes no room
            self.documents[item.id] = view
            self.breadlet_documents.append((item.id, document))
            self.tiles[item.key] = self._tile(item, view)
        self._add_items_menu()
        self.place()

    def _tile(self, item: DashboardBreadlet, view: DocumentView) -> Gtk.Widget:
        """One item's tile: its document, scrolling inside it, and while arranging a
        title to drag it by, its controls, and a corner to resize it by."""
        tile = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        tile.add_css_class("dashboard-section")
        tile.set_name(f"tile-{item.id}")
        tile.key = item.key
        for section in view.table_sections.values():
            # Columns are chosen in Configure Dashboard (#381), not in each heading;
            # the tile scrolls a long or wide table, not the table itself, so each
            # column keeps its natural width as in the browser.
            section.chooser.set_visible(False)
            section.scroller.set_propagate_natural_height(True)
            section.scroller.set_propagate_natural_width(True)
        for chart in _charts(view):
            # A chart fills its tile's height, as small as a short tile needs.
            chart.set_content_height(_CHART_HEIGHT)
            chart.set_vexpand(True)
        if self.arranging:
            tile.add_css_class("arranging")
            tile.append(self._tile_header(item, tile))
        if self.arranging and not view.get_first_child():
            empty = Gtk.Label(label="Nothing to show right now.", xalign=0)
            empty.add_css_class("dim-label")
            tile.append(empty)
        scroller = Gtk.ScrolledWindow(child=view)
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        if view.tables:
            # A table as wide as its columns; wrapped text as wide as its tile.
            scroller.get_child().set_hscroll_policy(Gtk.ScrollablePolicy.NATURAL)
        tile.append(scroller)
        if self.arranging:
            tile.append(self._resize_grip(item, tile))
            drop = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.MOVE)
            drop.connect("drop", self._on_drop, tile)
            tile.add_controller(drop)
        return tile

    def _tile_header(self, item: DashboardBreadlet, tile: Gtk.Box) -> Gtk.Widget:
        header = Gtk.Box(spacing=4)
        header.add_css_class("dashboard-tile-header")
        handle = Gtk.Box(spacing=6)
        handle.set_hexpand(True)
        handle.set_tooltip_text(f"Drag to move {item.name}")
        handle.append(Gtk.Image.new_from_icon_name("list-drag-handle-symbolic"))
        name = Gtk.Label(label=item.name, xalign=0)
        name.add_css_class("heading")
        name.set_ellipsize(Pango.EllipsizeMode.END)
        handle.append(name)
        drag = Gtk.DragSource(actions=Gdk.DragAction.MOVE)
        drag.connect(
            "prepare",
            lambda _source, _x, _y, key=item.key: Gdk.ContentProvider.new_for_value(key),
        )
        handle.add_controller(drag)
        header.append(handle)
        for icon, tip, step in (
            ("go-previous-symbolic", "Move earlier", -1),
            ("go-next-symbolic", "Move later", 1),
        ):
            button = Gtk.Button.new_from_icon_name(icon)
            button.set_tooltip_text(f"{tip}: {item.name}")
            button.add_css_class("flat")
            button.connect("clicked", lambda _b, key=item.key, s=step: self.move(key, s))
            header.append(button)
        # Its size in one compact button, so a narrow tile keeps its controls on
        # screen; the button opens the width and height.
        tile.width_spin = self._size_spin(item, "width", grid.COLUMNS, "Width in columns")
        tile.height_spin = self._size_spin(item, "height", grid.MAX_ROWS, "Height in rows")
        sizes = Gtk.Grid(column_spacing=8, row_spacing=6)
        for row, (label, spin) in enumerate(
            (
                ("Width in columns", tile.width_spin),
                ("Height in rows", tile.height_spin),
            )
        ):
            sizes.attach(Gtk.Label(label=label, xalign=0), 0, row, 1, 1)
            sizes.attach(spin, 1, row, 1, 1)
        tile.size_button = Gtk.MenuButton(
            label=f"{item.width} × {item.height}", popover=Gtk.Popover(child=sizes)
        )
        tile.size_button.set_tooltip_text(f"Size of {item.name}: columns × rows")
        header.append(tile.size_button)
        hide = Gtk.Button.new_from_icon_name("window-close-symbolic")
        hide.set_tooltip_text(f"Hide {item.name}")
        hide.add_css_class("flat")
        hide.connect("clicked", lambda _b, key=item.key: self.show_item(key, False))
        header.append(hide)
        return header

    def _size_spin(self, item: DashboardBreadlet, side: str, most: int, tip: str):
        spin = Gtk.SpinButton.new_with_range(1, most, 1)
        spin.set_value(getattr(item, side))
        spin.set_tooltip_text(f"{tip}: {item.name}")
        spin.connect(
            "value-changed",
            lambda button, key=item.key: self.resize(key, **{side: int(button.get_value())}),
        )
        return spin

    def _resize_grip(self, item: DashboardBreadlet, tile: Gtk.Box) -> Gtk.Widget:
        grip = Gtk.Image.new_from_icon_name("view-fullscreen-symbolic")
        grip.set_halign(Gtk.Align.END)
        grip.set_tooltip_text(f"Drag to resize {item.name}")
        grip.set_cursor(Gdk.Cursor.new_from_name("se-resize"))
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_resize_begin, item.key, tile)
        drag.connect("drag-update", self._on_resize_update, item.key)
        grip.add_controller(drag)
        return grip

    # ------------------------------------------------------------------ placing

    def _on_width(self, adjustment, _spec=None) -> None:
        width = int(adjustment.get_page_size()) - 24
        columns = max(1, min(grid.COLUMNS, (width + GAP) // (MIN_COLUMN_WIDTH + GAP)))
        if width > 0 and columns != self.columns:
            self.columns = columns
            self.place()

    def place(self) -> None:
        """Put each drawn tile at its place on the grid, packed as the browser packs
        them, without drawing anything again."""
        tiles = [
            grid.Tile(item.key, item.width, item.height)
            for item in self.items
            if item.key in self.tiles
        ]
        layout = self.grid.get_layout_manager()
        for placed in grid.pack(tiles, self.columns):
            widget = self.tiles[placed.tile.key]
            widget.set_size_request(-1, _tile_height(placed.tile.height))
            if widget.get_parent() is None:
                self.grid.attach(
                    widget, placed.column, placed.row, placed.width, placed.tile.height
                )
                continue
            child = layout.get_layout_child(widget)
            child.set_column(placed.column)
            child.set_row(placed.row)
            child.set_column_span(placed.width)
            child.set_row_span(placed.tile.height)

    def placement(self, key: str) -> tuple[int, int, int, int] | None:
        """Where an item's tile is: its column, row, width, and height."""
        widget = self.tiles.get(key)
        if widget is None or widget.get_parent() is None:
            return None
        child = self.grid.get_layout_manager().get_layout_child(widget)
        return (child.get_column(), child.get_row(), child.get_column_span(), child.get_row_span())

    # ---------------------------------------------------------------- arranging

    def _on_arrange(self, *_args) -> None:
        """Arrange Dashboard: begin arranging, or finish when already arranging."""
        if self.arranging:
            self.finish_arranging()
        else:
            self.start_arranging()

    def start_arranging(self) -> None:
        if self.db is None:
            return
        self.arranging = True
        self.arrange_bar.set_visible(True)
        self.arrange_status.set_text("")
        self.arrange_status.set_visible(False)
        self.redraw()

    def cancel_arranging(self) -> None:
        """Put the arrangement back as it was saved."""
        self.arranging = False
        self.arrange_bar.set_visible(False)
        self.refresh()

    def finish_arranging(self) -> bool:
        """Save the arrangement; False, with the reason shown, if it was refused."""
        if self.db is None:
            return False
        request = SaveDashboardConfig(
            layout=tuple(
                DashboardTileInput(item.key, item.width, item.height) for item in self.items
            ),
            sections={item.key: item.shown for item in self.items},
        )
        try:
            book_server(self.db).save_dashboard_settings(request)
        except ApiError as error:
            self.arrange_status.set_text(str(error))
            self.arrange_status.set_visible(True)
            return False
        self.arranging = False
        self.arrange_bar.set_visible(False)
        self.refresh()
        return True

    def reset_layout(self) -> None:
        """Every item back in its own place and size (shown items stay shown)."""
        sizes = {tile.key: tile for tile in default_layout()}
        by_key = {item.key: item for item in self.items}
        self.items = [
            replace(by_key[key], width=tile.width, height=tile.height)
            for key, tile in sizes.items()
            if key in by_key
        ] + [item for item in self.items if item.key not in sizes]
        self.redraw()

    def _index(self, key: str) -> int:
        return next(index for index, item in enumerate(self.items) if item.key == key)

    def move(self, key: str, step: int) -> None:
        """Move an item one place earlier or later among those shown."""
        shown = [item.key for item in self.items if item.shown]
        if key not in shown:
            return
        position = shown.index(key) + step
        if not 0 <= position < len(shown):
            return
        self.move_before(key, shown[position], after=step > 0)

    def move_before(self, key: str, target: str, *, after: bool = False) -> None:
        """Put an item just before (or after) another."""
        if key == target:
            return
        item = self.items.pop(self._index(key))
        self.items.insert(self._index(target) + (1 if after else 0), item)
        self.place()

    def resize(self, key: str, width: int | None = None, height: int | None = None) -> None:
        index = self._index(key)
        item = self.items[index]
        width = min(grid.COLUMNS, max(1, width if width is not None else item.width))
        height = min(grid.MAX_ROWS, max(1, height if height is not None else item.height))
        if (width, height) == (item.width, item.height):
            return
        self.items[index] = replace(item, width=width, height=height)
        tile = self.tiles.get(key)
        if tile is not None and hasattr(tile, "size_button"):
            tile.size_button.set_label(f"{width} × {height}")
        for spin, value in (
            (getattr(tile, "width_spin", None), width),
            (getattr(tile, "height_spin", None), height),
        ):
            if spin is not None and int(spin.get_value()) != value:
                spin.set_value(value)
        self.place()

    def show_item(self, key: str, shown: bool) -> None:
        """Hide an item, or show a hidden one (at the end of those shown)."""
        index = self._index(key)
        item = replace(self.items[index], shown=shown)
        if shown:
            # A hidden item comes back after every item shown.
            self.items.pop(index)
            self.items.append(item)
        else:
            self.items[index] = item
        self.redraw()

    def _add_items_menu(self) -> None:
        while (child := self.add_list.get_first_child()) is not None:
            self.add_list.remove(child)
        hidden = [item for item in self.items if not item.shown]
        for item in hidden:
            button = Gtk.Button(label=item.name)
            button.add_css_class("flat")
            button.set_tooltip_text(item.description)
            button.connect("clicked", self._on_add_item, item.key)
            self.add_list.append(button)
        self.add_menu.set_sensitive(bool(hidden))

    def _on_add_item(self, _button, key: str) -> None:
        self.add_menu.popdown()
        self.show_item(key, True)

    def _on_drop(self, _target, value, x: float, _y: float, tile) -> bool:
        if not isinstance(value, str) or value not in {item.key for item in self.items}:
            return False
        self.move_before(value, tile.key, after=x > tile.get_width() / 2)
        return True

    def _on_resize_begin(self, _gesture, _x, _y, key: str, tile) -> None:
        item = self.items[self._index(key)]
        self._resizing = (item.width, item.height, self._column_width())

    def _on_resize_update(self, _gesture, dx: float, dy: float, key: str) -> None:
        width, height, column = self._resizing
        self.resize(
            key,
            width=width + round(dx / (column + GAP)),
            height=height + round(dy / (grid.ROW_HEIGHT + GAP)),
        )

    def _column_width(self) -> float:
        span = self.grid.get_width() or self.columns * (MIN_COLUMN_WIDTH + GAP)
        return max(1.0, (span - GAP * (self.columns - 1)) / self.columns)

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
        """The Dashboard as shown: its items, in the order arranged."""
        if self.board is None:
            return None
        from ...plugins.export.report_layout import dashboard_layout

        currency = book_server(self.db).reporting_currency() if self.db is not None else ""
        document = dashboard_layout(self.board, book_name=self.book_name(), currency=currency)
        sections = tuple(
            section for _key, item in self.breadlet_documents for section in item.sections
        )
        return replace(document, sections=sections)

    def printable_html(self) -> str | None:
        """The same report as a web page, for printing from a browser."""
        from ...plugins.export.html_report import render_html

        document = self.printable_report()
        return render_html(document) if document is not None else None

    # ----------------------------------------------------------------- actions

    def follow(self, link: str) -> None:
        """Open what a Dashboard row or card names (``report_layout.LINK_KINDS``)."""
        if self.db is None or not link or self.arranging:
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
