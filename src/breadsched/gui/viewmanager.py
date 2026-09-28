"""The main window.

A menu bar and an icon toolbar above a stack of views, each view owning its own
toolbar. The toolbar carries one icon per view (the View menu lists the same
views); the icon for the current view stays pressed. A category sidebar used to
duplicate that list and took width from every view, so it was removed (#155).
Registers can also open in independent windows, so several accounts can be
compared without losing your place in any of them.

Views are constructed lazily and told about the book through :meth:`set_db`.  A
switch of book therefore never rebuilds the window, and a view that has never been
looked at costs nothing.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass

from .. import APP_NAME  # noqa: E402
from ..gen.db.sqlite import DbSQLite  # noqa: E402
from .gi_setup import Gio, GLib, Gtk, Pango
from .paths import default_book_path  # noqa: E402

__all__ = ["ViewManager"]

#: (label, icon, action, tooltip) for the toolbar's actions; a None action inserts
#: a separator. The view icons (from ``CATEGORIES``) follow these.
TOOLBAR = [
    ("Open", "document-open-symbolic", "app.open", "Open a book"),
    ("Import", "document-import-symbolic", "app.import", "Import a GnuCash book"),
    (None, None, None, None),
    ("Undo", "edit-undo-symbolic", "app.undo", "Undo the last change"),
    ("Redo", "edit-redo-symbolic", "app.redo", "Redo the last undone change"),
    (None, None, None, None),
    ("Transaction", "list-add-symbolic", "app.new-transaction", "Enter a new transaction"),
    (None, None, None, None),
    ("Print", "document-print-symbolic", "win.print-view", "Print the current report"),
]

#: (key, label, icon) for each view. Every view has one toolbar icon and one View
#: menu item; no two views share an icon.
CATEGORIES = [
    ("dashboard", "Dashboard", "go-home-symbolic"),
    ("fsa-dashboard", "FSA Dashboard", "view-calendar-symbolic"),
    ("accounts", "Accounts", "view-list-symbolic"),
    ("register", "Register", "text-x-generic-symbolic"),
    ("scheduled", "Scheduled", "alarm-symbolic"),
    ("upcoming", "Upcoming", "x-office-calendar-symbolic"),
    ("resolution", "Review", "dialog-question-symbolic"),
    ("plan", "Plan", "x-office-spreadsheet-symbolic"),
    ("projection", "Projection", "network-cellular-signal-excellent-symbolic"),
]


@dataclass(frozen=True)
class ViewAction:
    """One view-level command, exposed as ``win.<view>-<name>`` (#156).

    Every view action appears in the menus; ``toolbar`` ones also appear as icons
    while their view is current, and ``toggle`` ones are checkable View-menu items
    whose handler receives the new boolean. The handler is a method of that view.
    ``caption`` is the toolbar icon's short caption; it defaults to the label.
    """

    name: str
    label: str
    handler: str
    icon: str | None = None
    toolbar: bool = False
    toggle: bool = False
    caption: str | None = None


#: Commands per view. Buttons that act on a table's selected row (a schedule, a
#: review candidate) and controls that apply a view's own settings stay beside
#: that table or setting; everything else lives here, not in stray view buttons.
VIEW_ACTIONS: dict[str, tuple[ViewAction, ...]] = {
    "dashboard": (
        ViewAction(
            "configure-groups",
            "Configure Dashboard _Groups…",
            "_on_configure",
            "emblem-system-symbolic",
            toolbar=True,
        ),
    ),
    "fsa-dashboard": (
        ViewAction(
            "manage-claims",
            "Manage _FSA Claims…",
            "_on_fsa_claims",
            "document-edit-symbolic",
            toolbar=True,
        ),
    ),
    "accounts": (
        ViewAction("new-account", "_New Account…", "_on_new_account", "folder-new-symbolic", True),
        ViewAction(
            "edit-account",
            "_Edit Account…",
            "_on_edit_selected",
            "document-properties-symbolic",
            True,
        ),
        ViewAction(
            "security-price",
            "_Security Price…",
            "_on_security_price",
            "accessories-calculator-symbolic",
            True,
        ),
        ViewAction(
            "exchange-rate",
            "E_xchange Rate…",
            "_on_exchange_rate",
            "mail-send-receive-symbolic",
            True,
        ),
        ViewAction("hide-empty", "_Hide Empty Accounts", "set_hide_empty", toggle=True),
        ViewAction("show-hidden", "Show Hi_dden Accounts", "set_show_hidden", toggle=True),
    ),
    "register": (
        ViewAction("edit-in-place", "Edit Transaction in _Place", "edit_selected_in_place"),
        ViewAction("full-editor", "New Transaction in _Editor…", "_on_add_clicked"),
        ViewAction("new-window", "Open in New _Window", "_on_open_window_clicked"),
        ViewAction("reconcile", "_Reconcile…", "_on_reconcile_clicked"),
    ),
    # A command whose button changes with the view's state (Review due,
    # Reconcile, Save as scenario) stays in its view; the register keeps its own
    # buttons because a separate register window has no toolbar.
    "scheduled": (
        ViewAction(
            "new-scheduled",
            "_New Scheduled Transaction…",
            "_on_new_clicked",
            "document-new-symbolic",
            True,
            caption="New Scheduled",
        ),
        ViewAction(
            "suggest",
            "_Suggest Estimates from History…",
            "_on_suggest_clicked",
            "edit-find-symbolic",
            True,
            caption="Suggest",
        ),
        ViewAction(
            "new-loan",
            "New _Loan…",
            "_on_loan_clicked",
            "accessories-calculator-symbolic",
            True,
            caption="New Loan",
        ),
    ),
    "upcoming": (ViewAction("review-due", "_Review Due…", "_on_post_clicked"),),
    "plan": (
        ViewAction(
            "new-scenario",
            "_New Scenario…",
            "_on_new_scenario",
            "document-new-symbolic",
            True,
            caption="New Scenario",
        ),
        ViewAction(
            "manage-scenarios",
            "_Manage Scenarios…",
            "_on_manage_scenarios",
            "view-list-symbolic",
            True,
            caption="Scenarios",
        ),
        ViewAction(
            "explore-expenses",
            "_Explore Expenses…",
            "_open_expense_explorer",
            "edit-find-symbolic",
            True,
            caption="Explore",
        ),
    ),
    "projection": (
        ViewAction(
            "compare",
            "_Compare With…",
            "_on_compare_clicked",
            "view-dual-symbolic",
            True,
            caption="Compare",
        ),
        ViewAction("save-scenario", "_Save as Scenario", "_on_save_clicked"),
        ViewAction(
            "export",
            "E_xport Projection…",
            "_on_export_clicked",
            "document-save-symbolic",
            True,
            caption="Export",
        ),
    ),
}


def view_action_name(key: str, action: ViewAction) -> str:
    return f"{key}-{action.name}"


class ViewManager(Gtk.ApplicationWindow):
    """One window onto one book."""

    def __init__(self, application: Gtk.Application, *, prompt_due_on_open: bool = True) -> None:
        super().__init__(application=application, title=APP_NAME)
        self.set_default_size(1180, 760)
        self.db: DbSQLite | None = None
        self._views: dict[str, Gtk.Widget] = {}
        self._register_windows: list[tuple[Gtk.Window, Gtk.Widget]] = []
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
        # A window created while a book is already open joins that book rather
        # than showing the start screen with its view actions disabled.
        db = getattr(application, "db", None)
        path = getattr(application, "book_path", None)
        if db is not None and path:
            self.book_opened(db, path)

    def _install_window_actions(self) -> None:
        """``win.show-category`` takes the category name; its state is the current view.

        A stateful action makes the toolbar's view icons and the View menu items
        radio-style: the one matching the current view is shown as active, however
        that view was reached.
        """
        self.category_action = Gio.SimpleAction.new_stateful(
            "show-category", GLib.VariantType.new("s"), GLib.Variant.new_string("")
        )
        self.category_action.connect("activate", self._on_show_category)
        self.category_action.set_enabled(False)
        self.add_action(self.category_action)
        self.print_action = Gio.SimpleAction.new("print-view", None)
        self.print_action.connect("activate", self._on_print_view)
        self.print_action.set_enabled(False)
        self.add_action(self.print_action)
        self.view_actions: dict[str, Gio.SimpleAction] = {}
        for key, actions in VIEW_ACTIONS.items():
            for item in actions:
                name = view_action_name(key, item)
                if item.toggle:
                    action = Gio.SimpleAction.new_stateful(
                        name, None, GLib.Variant.new_boolean(False)
                    )
                    action.connect("change-state", self._on_view_toggle, key, item)
                else:
                    action = Gio.SimpleAction.new(name, None)
                    action.connect("activate", self._on_view_action, key, item)
                action.set_enabled(False)
                self.add_action(action)
                self.view_actions[name] = action
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
        self.view_buttons: dict[str, Gtk.ToggleButton] = {}
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

        separator = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        separator.set_margin_start(6)
        separator.set_margin_end(6)
        self.toolbar.append(separator)
        for key, label, icon in CATEGORIES:
            button = _view_button(key, label, icon)
            self.view_buttons[key] = button
            self.toolbar.append(button)

        # Icons for the current view's own commands (for example "Manage FSA
        # claims" on the FSA Dashboard); rebuilt whenever the view changes.
        self.view_tools = Gtk.Box(spacing=2)
        self.toolbar.append(self.view_tools)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        self.toolbar.append(spacer)

        self.status = Gtk.Label(label="No book open")
        self.status.add_css_class("dim")
        self.status.set_valign(Gtk.Align.CENTER)
        self.toolbar.append(self.status)

    def _build_body(self) -> None:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        # A narrow window scrolls the toolbar rather than refusing to shrink.
        toolbar_scroll = Gtk.ScrolledWindow(child=self.toolbar)
        toolbar_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        toolbar_scroll.set_propagate_natural_height(True)
        outer.append(toolbar_scroll)
        outer.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        self.set_child(outer)

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_vexpand(True)
        outer.append(self.stack)

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
        self.category_action.set_enabled(False)
        for action in self.view_actions.values():
            action.set_enabled(False)

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
        self.category_action.set_enabled(True)
        for action in self.view_actions.values():
            action.set_enabled(True)
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
                held = self.prompt_for_held_imports()
                if held is None:
                    self.prompt_for_due()
                else:
                    # One modal review at a time: due schedules follow the
                    # GnuCash review rather than stacking over it.
                    def due_after_review(*_args) -> bool:
                        self.prompt_for_due()
                        return False

                    held.connect("close-request", due_after_review)
            return GLib.SOURCE_REMOVE

        self._due_prompt_source = GLib.idle_add(present_when_ready)

    def prompt_for_held_imports(self) -> Gtk.Window | None:
        """Ask about GnuCash changes held back from reconciled transactions."""
        if self.db is None:
            return None
        from ..gen.services import pending_import_changes
        from .dialogs.import_review_dialog import ImportReviewDialog

        changes = pending_import_changes(self.db)
        if not changes:
            return None
        dialog = ImportReviewDialog(self, self.db, changes)
        dialog.connect("close-request", lambda *_: (self._refresh_views(), False)[1])
        dialog.present()
        return dialog

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

    def _on_show_category(self, _action, target) -> None:
        if self.db is None:
            return
        self.show_category(target.get_string())

    def _show_view_tools(self, key: str) -> None:
        child = self.view_tools.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self.view_tools.remove(child)
            child = following
        tools = [item for item in VIEW_ACTIONS.get(key, ()) if item.toolbar]
        if tools:
            separator = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
            separator.set_margin_start(6)
            separator.set_margin_end(6)
            self.view_tools.append(separator)
        for item in tools:
            self.view_tools.append(
                _tool_button(
                    item.caption or item.label.replace("_", "").rstrip("…"),
                    item.icon or "system-run-symbolic",
                    f"win.{view_action_name(key, item)}",
                    item.label.replace("_", ""),
                )
            )

    def _view_for_action(self, key: str):
        self.show_category(key)
        return self._views.get(key)

    def _on_view_action(self, _action, _parameter, key: str, item: ViewAction) -> None:
        if self.db is None:
            return
        view = self._view_for_action(key)
        handler = getattr(view, item.handler, None) if view is not None else None
        if handler is None:
            return
        # Most handlers were button callbacks and take the button as an argument.
        if inspect.signature(handler).parameters:
            handler(None)
        else:
            handler()

    def _on_view_toggle(self, action, value, key: str, item: ViewAction) -> None:
        if self.db is None:
            return
        action.set_state(value)
        view = self._view_for_action(key)
        handler = getattr(view, item.handler, None) if view is not None else None
        if handler is not None:
            handler(value.get_boolean())

    @property
    def current_category(self) -> str:
        """The view the toolbar icons and View menu show as active."""
        return self.category_action.get_state().get_string()

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
        # The active icon and menu item follow however the view was reached --
        # toolbar, menu, or a jump from another view.
        self.category_action.set_state(GLib.Variant.new_string(key))
        self._show_view_tools(key)
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

        def close_requested(*_args) -> bool:
            # Closing with a half-typed blank row asks first (#158).
            if register.has_unsaved() and not getattr(window, "leave_confirmed", False):

                def proceed() -> None:
                    window.leave_confirmed = True
                    window.close()

                register.confirm_leave(proceed)
                return True
            return detach()

        register.account_picker.connect("notify::selected", update_title)
        window.connect("close-request", close_requested)
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


def _view_button(key: str, label: str, icon: str) -> Gtk.ToggleButton:
    """A toolbar icon that shows one view and stays pressed while it is current."""
    button = Gtk.ToggleButton()
    button.set_has_frame(False)
    button.set_tooltip_text(f"Show {label}")
    button.set_action_name("win.show-category")
    button.set_action_target_value(GLib.Variant.new_string(key))
    button.category_key = key
    content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    content.append(Gtk.Image.new_from_icon_name(icon))
    caption = Gtk.Label(label=label)
    caption.add_css_class("summary-label")
    content.append(caption)
    button.set_child(content)
    return button
