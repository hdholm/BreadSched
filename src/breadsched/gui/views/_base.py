"""Shared plumbing for views.

Every view is a ``Gtk.Box`` that knows how to attach itself to a database and
repaint.  Signal connections are made once, in :meth:`set_db`, and the handlers do
nothing but call :meth:`refresh` -- a view that recomputed partial state in response
to individual signals would drift out of step with the ledger the first time an
import fired a hundred of them.
"""

from __future__ import annotations

from ...gen.db.sqlite import DbSQLite  # noqa: E402
from ...gen.lib.money import Money  # noqa: E402
from ..gi_setup import GLib, GObject, Gtk, Pango

__all__ = [
    "BaseView",
    "Row",
    "money_label",
    "column",
    "column_menu",
    "sorted_model",
    "toolbar",
    "unwrap",
]


class Row(GObject.Object):
    """Wraps a plain Python object so it can live in a ``Gio.ListStore``."""

    __gtype_name__ = "BreadSchedRow"

    def __init__(self, payload: object, children: list | None = None) -> None:
        super().__init__()
        self.payload = payload
        self.children = children or []


class BaseView(Gtk.Box):
    """A category view: owns a database reference and knows how to repaint."""

    #: Database signals that should trigger a repaint.
    WATCHES: tuple[str, ...] = ("database-changed",)

    def __init__(self, manager) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.manager = manager
        self.db: DbSQLite | None = None
        self._handlers: list[int] = []
        self._refresh_pending = False
        self._refresh_source_id: int | None = None

    def set_db(self, db: DbSQLite | None) -> None:
        """Attach to ``db``, or detach when ``None`` is supplied.

        A view can have an idle repaint queued when a book is switched or a window
        is destroyed.  Cancel that source *before* disconnecting the old database:
        otherwise a later GTK main-context iteration can repaint the orphaned view
        against a database that has already been closed.
        """
        self.cancel_refresh()
        for handle in self._handlers:
            if self.db is not None:
                self.db.disconnect(handle)
        self._handlers.clear()
        self.db = db
        if db is None:
            return
        for signal in self.WATCHES:
            self._handlers.append(db.connect(signal, self._on_change))
        self.refresh()

    def _on_change(self, *_args) -> None:
        self.refresh()

    def schedule_refresh(self) -> None:
        """Repaint once the current event has finished being handled.

        Rebuilding a view from inside an event handler destroys the very widget
        whose gesture is still running: GTK then continues that gesture against a
        disposed widget and complains that it has no event, which is where
        ``gdk_event_triggers_context_menu: assertion 'event != NULL' failed`` comes
        from. Editing a cell and clicking straight into the next one is exactly
        that sequence.

        Repeated calls before the idle runs collapse into one repaint.
        """
        if self._refresh_pending:
            return
        self._refresh_pending = True
        scheduled_db = self.db

        def run() -> bool:
            # Clear ownership before repainting. A refresh can itself arrange a new
            # repaint, and that new source must not be mistaken for this one.
            self._refresh_pending = False
            self._refresh_source_id = None

            # Cancellation is the normal lifecycle path, but a GLib source can
            # already be ready for dispatch when teardown removes it.  Never let a
            # stale callback repaint a replacement book, or a book whose SQLite
            # connection has since been closed.  The database identity is captured
            # when the work is scheduled so switching books cannot retarget it.
            if scheduled_db is not None and self.db is scheduled_db and scheduled_db.is_open:
                self.refresh()
            return GLib.SOURCE_REMOVE

        self._refresh_source_id = GLib.idle_add(run, priority=GLib.PRIORITY_DEFAULT_IDLE)

    def cancel_refresh(self) -> None:
        """Cancel a queued idle repaint without touching the database."""
        source_id = self._refresh_source_id
        self._refresh_source_id = None
        self._refresh_pending = False
        if source_id is not None:
            GLib.source_remove(source_id)

    def flush_refresh(self) -> None:
        """Carry out a scheduled repaint now. For callers with no main loop.

        Removing the GLib source is essential: merely clearing
        ``_refresh_pending`` leaves its callback registered, allowing it to fire
        later after the view's book has been closed.
        """
        if self._refresh_pending:
            self.cancel_refresh()
            if self.db is not None and self.db.is_open:
                self.refresh()

    def refresh_on_close(self, *_args) -> bool:
        """Refresh after a child dialog closes and allow its default close action."""
        self.refresh()
        return False

    def refresh(self) -> None:
        """Rebuild from the database.  Subclasses override."""

    # ------------------------------------------------------------- convenience

    def full_name(self, handle: str) -> str:
        return self.db.full_name(handle) if self.db else ""


def money_label(amount: Money, symbol: str = "", bold: bool = False) -> Gtk.Label:
    """A right-aligned, tabular-figure amount, coloured when negative."""
    label = Gtk.Label(label=amount.format(symbol, parens_negative=True), xalign=1)
    label.add_css_class("numeric")
    if amount < 0:
        label.add_css_class("negative")
    if bold:
        label.add_css_class("total-row")
    return label


def unwrap(item):
    """Get the payload out of whatever the list model handed back.

    A ``Gtk.TreeListModel`` built with ``passthrough=False`` wraps each item in a
    ``Gtk.TreeListRow``, so a cell factory shared between flat and tree views sees
    two different things. Unwrapping in one place lets the same ``column()`` helper
    serve both.
    """
    if isinstance(item, Gtk.TreeListRow):
        item = item.get_item()
    return item.payload if isinstance(item, Row) else item


def column(
    title: str,
    bind,
    expand: bool = False,
    numeric: bool = False,
    sort_key=None,
):
    """Build a ``Gtk.ColumnViewColumn`` whose cells are labels.

    ``bind`` receives the row's payload and returns the display string.
    """
    factory = Gtk.SignalListItemFactory()

    def on_setup(_factory, item) -> None:
        label = Gtk.Label(xalign=1 if numeric else 0)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        if numeric:
            label.add_css_class("numeric")
        item.set_child(label)

    def on_bind(_factory, item) -> None:
        payload = unwrap(item.get_item())
        label = item.get_child()
        value = bind(payload)
        label.set_text(str(value))
        if numeric:
            negative = str(value).startswith(("-", "("))
            if negative:
                label.add_css_class("negative")
            else:
                label.remove_css_class("negative")

    factory.connect("setup", on_setup)
    factory.connect("bind", on_bind)
    col = Gtk.ColumnViewColumn(title=title, factory=factory)
    col.set_expand(expand)
    # Every column in every view drags to resize and clicks to sort. Doing this
    # here rather than per view is what keeps the three list views behaving the
    # same as each other.
    col.set_resizable(True)
    col.set_sorter(Gtk.CustomSorter.new(_compare_by(bind, sort_key, numeric)))
    return col


def _compare_by(bind, sort_key, numeric: bool):
    """Comparison over a column's values, ordering numbers as numbers."""
    key_of = sort_key or bind

    def compare(left, right, _data=None) -> int:
        try:
            first, second = key_of(unwrap(left)), key_of(unwrap(right))
        except Exception:  # noqa: BLE001 - an unsortable row compares equal
            return 0
        if numeric and not isinstance(first, (int, float)):
            first, second = _numeric(first), _numeric(second)
        if first == second:
            return 0
        try:
            return -1 if first < second else 1
        except TypeError:
            return -1 if str(first) < str(second) else 1

    return compare


def _numeric(value) -> float:
    """Read a formatted amount back as a number, so 1,000 sorts above 900."""
    text = str(value).strip().replace(",", "")
    negative = text.startswith("(") or text.startswith("-")
    text = text.strip("()-$ ")
    try:
        number = float(text or 0)
    except ValueError:
        return 0.0
    return -number if negative else number


def sorted_model(column_view: Gtk.ColumnView, model):
    """Wrap a model so clicking a column header actually reorders the rows.

    Setting a sorter on a column only tells the header what to draw; nothing moves
    until the model itself is sorted by the view's sorter. For a tree, the sorter
    has to be wrapped in a ``Gtk.TreeListRowSorter`` so each level is sorted within
    its parent — sorting the flattened list would tear children away from the rows
    they belong to.
    """
    sorter = column_view.get_sorter()
    if sorter is None:
        return model
    if isinstance(model, Gtk.TreeListModel):
        sorter = Gtk.TreeListRowSorter.new(sorter)
    return Gtk.SortListModel.new(model, sorter)


def column_menu(view_id: str, column_view: Gtk.ColumnView, settings=None) -> Gtk.MenuButton:
    """A menu at the end of the header row for showing and hiding columns.

    Hidden columns are remembered by *title* rather than by position, so adding a
    column in a later release cannot shuffle a saved layout onto the wrong ones —
    which would make remembering worse than not remembering.
    """
    columns = column_view.get_columns()
    section = f"columns:{view_id}"

    if settings is not None:
        hidden = set(settings.get_list(section, "hidden"))
        for index in range(columns.get_n_items()):
            column = columns.get_item(index)
            if column.get_title() in hidden:
                column.set_visible(False)

    popover = Gtk.Popover()
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    for side in ("top", "bottom", "start", "end"):
        getattr(box, f"set_margin_{side}")(8)
    caption = Gtk.Label(label="Columns", xalign=0)
    caption.add_css_class("summary-label")
    box.append(caption)

    def remember() -> None:
        if settings is None:
            return
        names = [
            columns.get_item(i).get_title()
            for i in range(columns.get_n_items())
            if not columns.get_item(i).get_visible()
        ]
        settings.set(section, "hidden", names)
        settings.save()

    for index in range(columns.get_n_items()):
        column = columns.get_item(index)
        title = column.get_title()
        check = Gtk.CheckButton(label=title or f"Column {index + 1}")
        check.set_active(column.get_visible())
        if index == 0:
            # Hiding the first column leaves a view with nothing to read, and in a
            # tree view it takes the expander with it.
            check.set_sensitive(False)

        def on_toggled(button, target=column) -> None:
            target.set_visible(button.get_active())
            remember()

        check.connect("toggled", on_toggled)
        box.append(check)

    popover.set_child(box)
    button = Gtk.MenuButton(icon_name="open-menu-symbolic", popover=popover)
    button.set_tooltip_text("Show or hide columns")
    return button


def toolbar(*widgets: Gtk.Widget) -> Gtk.Box:
    box = Gtk.Box(spacing=8)
    for margin in ("top", "bottom", "start", "end"):
        getattr(box, f"set_margin_{margin}")(8)
    for widget in widgets:
        box.append(widget)
    return box
