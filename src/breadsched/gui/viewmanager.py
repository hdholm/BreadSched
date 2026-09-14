"""The main window.

Gramps' arrangement: a category sidebar on the left switching a stack of views on
the right, with each view owning its own toolbar.  Two things are borrowed from
GnuCash instead.  First, a persistent account tree, because a chart of accounts is
the map of a book and hiding it behind a mode switch makes the rest harder to read.
Second, opening a register in a tab, so several accounts can be compared without
losing your place in any of them.

Views are constructed lazily and told about the book through :meth:`set_db`.  A
switch of book therefore never rebuilds the window, and a view that has never been
looked at costs nothing.
"""

from __future__ import annotations

from .. import APP_NAME  # noqa: E402
from ..gen.db.sqlite import DbSQLite  # noqa: E402
from .gi_setup import Gio, GLib, Gtk, Pango
from .paths import default_book_path  # noqa: E402

__all__ = ["ViewManager"]

#: (identifier, label, icon, factory path) for each category in the sidebar.
#: (label, icon, action, tooltip); a None action inserts a separator.
TOOLBAR = [
    ("Open", "document-open-symbolic", "app.open", "Open a book"),
    ("Import", "document-import-symbolic", "app.import", "Import a GnuCash book"),
    (None, None, None, None),
    ("Undo", "edit-undo-symbolic", "app.undo", "Undo the last change"),
    ("Redo", "edit-redo-symbolic", "app.redo", "Redo the last undone change"),
    (None, None, None, None),
    ("Transaction", "list-add-symbolic", "app.new-transaction", "Enter a new transaction"),
    ("Plan", "view-grid-symbolic", "win.show-category::plan", "Show the event-driven plan"),
    ("Accounts", "view-list-symbolic", "win.show-category::accounts", "Show the chart of accounts"),
    (
        "Projection",
        "network-cellular-signal-excellent-symbolic",
        "win.show-category::projection",
        "Show the projection",
    ),
    (None, None, None, None),
    ("Print", "document-print-symbolic", "win.print-view", "Print the current report"),
]

CATEGORIES = [
    ("dashboard", "Dashboard", "view-grid-symbolic"),
    ("fsa-dashboard", "FSA Dashboard", "view-calendar-symbolic"),
    ("accounts", "Accounts", "view-list-symbolic"),
    ("register", "Register", "text-x-generic-symbolic"),
    ("scheduled", "Scheduled", "alarm-symbolic"),
    ("upcoming", "Upcoming", "x-office-calendar-symbolic"),
    ("resolution", "Review", "dialog-question-symbolic"),
    ("plan", "Plan", "view-grid-symbolic"),
    ("projection", "Projection", "network-cellular-signal-excellent-symbolic"),
]


class ViewManager(Gtk.ApplicationWindow):
    """One window onto one book."""

    def __init__(self, application: Gtk.Application, *, prompt_due_on_open: bool = True) -> None:
        super().__init__(application=application, title=APP_NAME)
        self.set_default_size(1180, 760)
        self.db: DbSQLite | None = None
        self._views: dict[str, Gtk.Widget] = {}
        self._register_windows: list[tuple[Gtk.Window, Gtk.Widget]] = []
        #: Guards against show_category and the sidebar calling each other.
        self._selecting = False
        # Automatic due review is a user-facing startup policy, not required for
        # binding views to a book. Tests and embedded windows can suppress it so
        # presenting a modal window does not pump the GLib main context.
        self._prompt_due_on_open = prompt_due_on_open
        self._due_prompt_source: int | None = None

        self._install_window_actions()
        self._build_header()
        self._build_body()
        self._show_placeholder()
        self.connect("close-request", self._on_close_request)

    def _install_window_actions(self) -> None:
        """``win.show-category`` takes the category name as its parameter."""
        action = Gio.SimpleAction.new("show-category", GLib.VariantType.new("s"))
        action.connect("activate", lambda _a, target: self.show_category(target.get_string()))
        self.add_action(action)
        self.print_action = Gio.SimpleAction.new("print-view", None)
        self.print_action.connect("activate", self._on_print_view)
        self.print_action.set_enabled(False)
        self.add_action(self.print_action)
        application = self.get_application()
        if application is not None:
            application.set_accels_for_action("win.print-view", ["<Control>p"])

    # ----------------------------------------------------------------- chrome

    def _build_header(self) -> None:
        """Menu bar plus an icon toolbar, in the GnuCash and Gramps arrangement.

        ``set_show_menubar`` draws the application's menu model inside the window
        on desktops that do not export a global menu, which is most of them.
        """
        self.set_show_menubar(True)

        self.toolbar = Gtk.Box(spacing=2)
        self.toolbar.add_css_class("toolbar")
        for side in ("top", "bottom", "start", "end"):
            getattr(self.toolbar, f"set_margin_{side}")(4)

        self.tool_buttons: dict[str, Gtk.Button] = {}
        for label, icon, action, tooltip in TOOLBAR:
            if action is None:
                separator = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
                separator.set_margin_start(6)
                separator.set_margin_end(6)
                self.toolbar.append(separator)
                continue
            assert label is not None and icon is not None and tooltip is not None
            button = _tool_button(label, icon, action, tooltip)
            self.tool_buttons[action] = button
            self.toolbar.append(button)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        self.toolbar.append(spacer)

        self.status = Gtk.Label(label="No book open")
        self.status.add_css_class("dim")
        self.status.set_valign(Gtk.Align.CENTER)
        self.toolbar.append(self.status)

    def _build_body(self) -> None:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.append(self.toolbar)
        outer.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        self.set_child(outer)

        self.paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.paned.set_position(190)
        self.paned.set_shrink_start_child(False)
        self.paned.set_vexpand(True)
        outer.append(self.paned)

        self.navigator = Gtk.ListBox()
        self.navigator.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.navigator.add_css_class("navigation-sidebar")
        self.navigator.connect("row-selected", self._on_category_selected)
        for key, label, icon in CATEGORIES:
            self.navigator.append(_category_row(key, label, icon))

        sidebar_scroll = Gtk.ScrolledWindow(child=self.navigator)
        sidebar_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.paned.set_start_child(sidebar_scroll)

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.paned.set_end_child(self.stack)

    def _show_placeholder(self) -> None:
        """The start screen: four ways in, and no book opened behind your back.

        Creating a book unasked leaves a file in someone's home directory that
        they did not ask for and may not notice, and makes "which book am I
        looking at" the first question rather than the last. The default book is
        offered as a choice instead, for people who only ever want one.
        """
        empty = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        empty.set_valign(Gtk.Align.CENTER)
        empty.set_halign(Gtk.Align.CENTER)

        title = Gtk.Label(label=APP_NAME)
        title.add_css_class("category-title")
        empty.append(title)
        empty.append(Gtk.Label(label="Open a book to begin, or start a new one."))

        choices = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        choices.set_halign(Gtk.Align.CENTER)
        for label, subtitle, action in (
            ("New book…", "An empty chart of accounts", "app.new"),
            ("Open book…", "A book you already have", "app.open"),
            (
                "Import a GnuCash book…",
                "Read a GnuCash file into a new book",
                "app.import-new",
            ),
            (
                "Use the default book",
                str(default_book_path()),
                "app.open-default",
            ),
        ):
            button = Gtk.Button()
            button.set_action_name(action)
            content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            for margin in ("top", "bottom", "start", "end"):
                getattr(content, f"set_margin_{margin}")(6)
            heading = Gtk.Label(label=label, xalign=0)
            heading.add_css_class("total-row")
            caption = Gtk.Label(label=subtitle, xalign=0)
            caption.add_css_class("dim")
            caption.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            content.append(heading)
            content.append(caption)
            button.set_child(content)
            choices.append(button)

        empty.append(choices)
        self.stack.add_named(empty, "empty")
        self.stack.set_visible_child_name("empty")

    # ------------------------------------------------------------------- book

    def _detach_views(self) -> None:
        """Disconnect views and cancel work that belongs to the current book."""
        if self._due_prompt_source is not None:
            GLib.source_remove(self._due_prompt_source)
            self._due_prompt_source = None
        for view in self._views.values():
            view.set_db(None)
        for window, view in list(self._register_windows):
            view.set_db(None)
            window.destroy()
        self._register_windows.clear()
        self.db = None
        self.print_action.set_enabled(False)

    def book_closing(self) -> None:
        """Detach from the current book before its database connection is closed."""
        self._detach_views()

    def _on_close_request(self, *_args) -> bool:
        self._detach_views()
        return False

    def destroy(self) -> None:
        """Destroy the window without leaving GLib callbacks owned by its views.

        GTK's normal close path is covered by ``close-request``; tests and a few
        programmatic callers use ``destroy()`` directly, so that path needs the
        same teardown guarantee.
        """
        self._detach_views()
        super().destroy()

    def book_opened(self, db: DbSQLite, path: str) -> None:
        self.db = db
        self.set_title(f"{path.rsplit('/', 1)[-1]} — {APP_NAME}")
        db.connect("undo-available", self._on_undo_available)
        db.connect("redo-available", self._on_redo_available)
        db.connect("database-changed", lambda *_: self._refresh_status())
        for view in self._views.values():
            view.set_db(db)
        self._refresh_status()
        self._on_undo_available(False)
        self._on_redo_available(False)
        # Selecting a row that is already selected emits nothing, so opening a
        # second book would leave the first book's view on screen. Show the
        # category explicitly and let the selection follow.
        self.navigator.select_row(self.navigator.get_row_at_index(0))
        self.show_category(CATEGORIES[0][0])
        if self._prompt_due_on_open:
            self._schedule_due_prompt()

    def _schedule_due_prompt(self) -> None:
        """Present the due review after the main window has reached the screen.

        During application activation a remembered book can be opened before the
        main window is presented. Presenting a modal transient in that interval
        lets the later parent presentation cover the dialog on some window
        managers. Deferring one main-loop turn guarantees the parent is mapped
        first while preserving the automatic due review.
        """
        if self._due_prompt_source is not None:
            GLib.source_remove(self._due_prompt_source)
        expected_db = self.db

        def present_when_ready() -> bool:
            self._due_prompt_source = None
            if self.db is expected_db and self.db is not None:
                self.prompt_for_due()
            return GLib.SOURCE_REMOVE

        self._due_prompt_source = GLib.idle_add(present_when_ready)

    def prompt_for_due(self) -> None:
        """Ask about anything due, once, on opening the book."""
        if self.db is None:
            return
        from ..gen.engine import schedule as schedule_engine
        from .dialogs.due_dialog import DueDialog

        due = schedule_engine.due_occurrences(self.db, horizon_days=0)
        if not due:
            return
        dialog = DueDialog(self, self.db, due)
        dialog.connect("close-request", lambda *_: (self._refresh_views(), False)[1])
        dialog.present()

    def refresh_views(self) -> None:
        """Repaint every built view. Used when a book-wide setting changes."""
        for view in self._views.values():
            view.refresh()

    #: Kept for callers that used the private name.
    _refresh_views = refresh_views

    def _refresh_status(self) -> None:
        if self.db is None:
            return
        counts = self.db.summary()
        self.status.set_text(f"{counts['account']} accounts · {counts['txn']} transactions")

    def _on_undo_available(self, available: bool) -> None:
        application = self.get_application()
        if application is not None:
            application.set_action_enabled("undo", available)
        message = self.db.undo_message() if self.db is not None else None
        button = self.tool_buttons.get("app.undo")
        if button is not None:
            button.set_tooltip_text(
                f"Undo: {message}" if (available and message) else "Nothing to undo"
            )

    def _on_redo_available(self, available: bool) -> None:
        application = self.get_application()
        if application is not None:
            application.set_action_enabled("redo", available)

    # ------------------------------------------------------------------ views

    def _on_category_selected(self, _listbox, row) -> None:
        if row is None or self.db is None or self._selecting:
            return
        self.show_category(row.category_key)

    def _select_navigator(self, key: str) -> None:
        """Move the sidebar highlight to ``key`` without re-entering show_category."""
        index = 0
        while (row := self.navigator.get_row_at_index(index)) is not None:
            if row.category_key == key:
                if self.navigator.get_selected_row() is not row:
                    self._selecting = True
                    try:
                        self.navigator.select_row(row)
                    finally:
                        self._selecting = False
                return
            index += 1

    def show_category(self, key: str) -> None:
        view = self._views.get(key)
        if view is None:
            view = self._build_view(key)
            if view is None:
                return
            self._views[key] = view
            self.stack.add_named(view, key)
            if self.db is not None:
                view.set_db(self.db)
        self.stack.set_visible_child_name(key)
        # The sidebar is the map of where you are, so it follows however the view
        # was reached -- toolbar, menu, or a jump from another view.
        self._select_navigator(key)
        view.refresh()
        self.print_action.set_enabled(
            bool(self.db is not None and getattr(view, "PRINTABLE", False))
        )

    def _on_print_view(self, *_args) -> None:
        """Print the applied state of a report-capable current view."""
        view = self.stack.get_visible_child()
        if view is None or not getattr(view, "PRINTABLE", False):
            return
        try:
            view.flush_refresh()
            wait_for_background = getattr(view, "wait_for_background", None)
            if callable(wait_for_background) and not wait_for_background():
                raise TimeoutError("the current report is still calculating")
            document = view.printable_html()
            if not document:
                return
            from .printing import open_print_preview

            open_print_preview(document)
        except Exception as exc:  # noqa: BLE001 - opening the desktop handler may fail
            application = self.get_application()
            reporter = getattr(application, "_report", None)
            if reporter is not None:
                reporter(f"Could not open the print preview: {exc}")

    def _build_view(self, key: str):
        from .views.accounts import AccountTreeView
        from .views.dashboard import DashboardView
        from .views.fsa_dashboard import FsaDashboardView
        from .views.plan import PlanView
        from .views.projection import ProjectionView
        from .views.register import RegisterView
        from .views.resolution import ResolutionView
        from .views.scheduled import ScheduledView, UpcomingView

        factories = {
            "dashboard": lambda: DashboardView(self),
            "fsa-dashboard": lambda: FsaDashboardView(self),
            "accounts": lambda: AccountTreeView(self),
            "register": lambda: RegisterView(self),
            "scheduled": lambda: ScheduledView(self),
            "upcoming": lambda: UpcomingView(self),
            "resolution": lambda: ResolutionView(self),
            "plan": lambda: PlanView(self),
            "projection": lambda: ProjectionView(self),
        }
        factory = factories.get(key)
        return factory() if factory else None

    def open_schedule(self, schedule_handle: str) -> None:
        """Show the scheduled transactions with one of them selected."""
        self.show_category("scheduled")
        view = self._views.get("scheduled")
        if view is not None and hasattr(view, "select_schedule"):
            view.select_schedule(schedule_handle)

    def open_register(self, account_handle: str) -> None:
        """Jump to the register view focused on one account.

        The sidebar follows from show_category. Selecting a row by index here
        broke the moment a category was added above it, and silently switched to
        whichever view had inherited that position.
        """
        self.show_category("register")
        register = self._views.get("register")
        if register is not None:
            register.show_account(account_handle)

    def open_register_window(self, account_handle: str) -> Gtk.Window | None:
        """Open an independently navigable register sharing the current book."""
        if self.db is None:
            return None
        from .views.register import RegisterView

        window = Gtk.Window(transient_for=self)
        window.set_destroy_with_parent(True)
        window.set_default_size(1050, 620)
        register = RegisterView(self)
        window.set_child(register)
        pair = (window, register)
        self._register_windows.append(pair)

        def update_title(*_args) -> None:
            handle = register.account_handle
            account = self.db.get_account(handle) if self.db is not None and handle else None
            name = self.db.full_name(account) if self.db is not None and account is not None else ""
            window.set_title(f"{name or 'Register'} — {APP_NAME}")

        def detach(*_args) -> bool:
            register.set_db(None)
            if pair in self._register_windows:
                self._register_windows.remove(pair)
            return False

        register.account_picker.connect("notify::selected", update_title)
        window.connect("close-request", detach)
        register.set_db(self.db)
        register.show_account(account_handle)
        update_title()
        window.present()
        return window


def _tool_button(label: str, icon: str, action: str, tooltip: str) -> Gtk.Button:
    """Icon above a caption, the way GnuCash and Gramps present a toolbar."""
    button = Gtk.Button()
    button.set_has_frame(False)
    button.set_tooltip_text(tooltip)
    button.set_action_name(action.split("::")[0])
    if "::" in action:
        button.set_action_target_value(GLib.Variant.new_string(action.split("::")[1]))

    content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    content.append(Gtk.Image.new_from_icon_name(icon))
    caption = Gtk.Label(label=label)
    caption.add_css_class("summary-label")
    content.append(caption)
    button.set_child(content)
    return button


def _category_row(key: str, label: str, icon: str) -> Gtk.ListBoxRow:
    row = Gtk.ListBoxRow()
    row.category_key = key
    box = Gtk.Box(spacing=10)
    box.set_margin_top(8)
    box.set_margin_bottom(8)
    box.set_margin_start(10)
    box.set_margin_end(10)
    box.append(Gtk.Image.new_from_icon_name(icon))
    box.append(Gtk.Label(label=label, xalign=0))
    row.set_child(box)
    return row
