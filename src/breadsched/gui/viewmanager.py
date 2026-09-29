"""The main window.

A menu bar and an icon toolbar above a stack of views, each view owning its own
toolbar. The main toolbar is arranged around the current view (#182): after the
commands that work anywhere come the current view's name and its own commands,
then one icon for each other view (the View menu lists every view). The current
view has no icon, since choosing it would do nothing, and the Actions menu lists
the current view's commands first. A category sidebar used to duplicate the view
list and took width from every view, so it was removed (#155).
A tab bar below the toolbar keeps every opened view one click away, with one
tab per open register account (#183): each register tab is its own register,
with its own place, filter, and half-typed entry. Registers can also open in
independent windows, so several accounts can be compared side by side.

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
        ViewAction("track-reimbursable", "Track as Rei_mbursable…", "track_selected_reimbursable"),
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
        #: Every register tab's view; ``_views["register"]`` is the one shown.
        self._registers: list[Gtk.Widget] = []
        #: Open tabs in order: (category, register view or None, tab widget).
        self._tabs: list[_Tab] = []
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
        # With several main windows, the Actions menu follows the focused one.
        self.connect(
            "notify::is-active",
            lambda *_a: self._share_view_actions() if self.is_active() else None,
        )
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

        # The current view's name and its own commands (for example "Manage FSA
        # claims" on the FSA Dashboard), next to the general commands where they
        # are easiest to find; rebuilt whenever the view changes (#182).
        self.view_heading = Gtk.Box(spacing=2)
        separator = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        separator.set_margin_start(6)
        separator.set_margin_end(6)
        self.view_heading.append(separator)
        self.view_title = Gtk.Label()
        self.view_title.add_css_class("heading")
        self.view_title.set_valign(Gtk.Align.CENTER)
        self.view_title.set_margin_start(4)
        self.view_title.set_margin_end(6)
        self.view_heading.append(self.view_title)
        self.view_heading.set_visible(False)
        self.toolbar.append(self.view_heading)
        self.view_tools = Gtk.Box(spacing=2)
        self.toolbar.append(self.view_tools)

        separator = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        separator.set_margin_start(6)
        separator.set_margin_end(6)
        self.toolbar.append(separator)
        for key, label, icon in CATEGORIES:
            button = _view_button(key, label, icon)
            self.view_buttons[key] = button
            self.toolbar.append(button)

        # The book summary sits right after the view icons, stacked on two
        # lines so it takes one short column instead of a long run of text.
        self.status = Gtk.Label(label="No book open")
        self.status.add_css_class("dim")
        self.status.add_css_class("book-summary")
        self.status.set_valign(Gtk.Align.CENTER)
        self.status.set_xalign(0)
        self.status.set_margin_start(8)
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

        # One tab per opened view and per open register account (#183).
        self.tab_bar = Gtk.Box(spacing=2)
        self.tab_bar.add_css_class("tab-bar")
        for side in ("start", "end"):
            getattr(self.tab_bar, f"set_margin_{side}")(4)
        self.tab_scroll = Gtk.ScrolledWindow(child=self.tab_bar)
        self.tab_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        self.tab_scroll.set_propagate_natural_height(True)
        self.tab_scroll.set_visible(False)
        outer.append(self.tab_scroll)

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_vexpand(True)
        outer.append(self.stack)
        # The "register" page holds one register per register tab.
        self.register_stack = Gtk.Stack()
        self.register_stack.set_vexpand(True)
        self.stack.add_named(self.register_stack, "register")

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
        for view in self._all_views():
            view.set_db(None)
        # Tabs name accounts of this book; the next book starts with its own.
        for tab in list(self._tabs):
            self._remove_tab(tab)
        for register in list(self._registers):
            self._drop_register(register)
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
        for view in self._all_views():
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

    def _all_views(self) -> list[Gtk.Widget]:
        """Every view in this window, including every register tab's register."""
        views = [view for key, view in self._views.items() if key != "register"]
        return views + list(self._registers)

    def refresh_views(self) -> None:
        """Repaint every built view. Used when a book-wide setting changes."""
        for view in self._all_views():
            view.refresh()

    #: Kept for callers that used the private name.
    _refresh_views = refresh_views

    def _refresh_status(self) -> None:
        if self.db is None:
            return
        counts = self.db.summary()
        self.status.set_text(f"{counts['account']} accounts\n{counts['txn']} transactions")

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
        labels = {item_key: label for item_key, label, _icon in CATEGORIES}
        self.view_title.set_label(labels.get(key, ""))
        self.view_heading.set_visible(key in labels)
        # Choosing the view already shown would do nothing, so its icon is hidden.
        for view_key, button in self.view_buttons.items():
            button.set_visible(view_key != key)
        self._share_view_actions()
        for item in tools:
            self.view_tools.append(
                _tool_button(
                    item.caption or item.label.replace("_", "").rstrip("…"),
                    item.icon or "system-run-symbolic",
                    f"win.{view_action_name(key, item)}",
                    item.label.replace("_", ""),
                )
            )

    def _share_view_actions(self, *_args) -> None:
        """Arrange the application's Actions menu around this window's view."""
        show_actions = getattr(self.get_application(), "show_view_actions", None)
        if show_actions is not None:
            show_actions(self.current_category or None)

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
        if key == "register":
            # The register tab shown most recently, or a first one.
            view = self._views.get("register") or self._new_register()
            self._activate_register(view)
        else:
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
        self._select_tab(key, view if key == "register" else None)
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
        """Show one account's register, in its own tab (#183).

        An account that already has a register tab switches to it; otherwise a
        new register tab opens, so the register you were in keeps its place.
        """
        register = next(
            (view for view in self._registers if view.account_handle == account_handle),
            None,
        )
        if register is None:
            register = self._new_register()
        self._activate_register(register)
        self.show_category("register")
        register.show_account(account_handle)
        self._update_tab_label(register)

    # ------------------------------------------------------------------- tabs

    def _new_register(self):
        from .views.register import RegisterView

        register = RegisterView(self)
        self._registers.append(register)
        self.register_stack.add_child(register)
        register.account_picker.connect(
            "notify::selected", lambda *_a: self._update_tab_label(register)
        )
        if self.db is not None:
            register.set_db(self.db)
        return register

    def _activate_register(self, register) -> None:
        self._views["register"] = register
        self.register_stack.set_visible_child(register)

    def _drop_register(self, register) -> None:
        register.set_db(None)
        if register in self._registers:
            self._registers.remove(register)
        self.register_stack.remove(register)
        if self._views.get("register") is register:
            self._views.pop("register")
            if self._registers:
                self._activate_register(self._registers[-1])

    def _tab_title(self, key: str, register) -> str:
        if register is None:
            return next((label for item, label, _icon in CATEGORIES if item == key), key)
        handle = register.account_handle
        account = self.db.get_account(handle) if self.db is not None and handle else None
        if account is None:
            return "Register"
        return account.name

    def _tab_for(self, key: str, register) -> _Tab | None:
        return next(
            (tab for tab in self._tabs if tab.key == key and tab.register is register), None
        )

    def _select_tab(self, key: str, register) -> None:
        """Mark the tab for what is shown, opening one if needed."""
        tab = self._tab_for(key, register)
        if tab is None:
            tab = _Tab(key, register, self._tab_title(key, register))
            tab.button.connect("clicked", lambda *_a: self._on_tab_clicked(tab))
            tab.close.connect("clicked", lambda *_a: self.close_tab(tab))
            self._tabs.append(tab)
            self.tab_bar.append(tab.widget)
        for item in self._tabs:
            item.set_current(item is tab)
        self._update_tab_label(register)
        self.tab_scroll.set_visible(True)

    def _update_tab_label(self, register) -> None:
        if register is None:
            return
        tab = self._tab_for("register", register)
        if tab is not None:
            title = self._tab_title("register", register)
            full = ""
            if self.db is not None and register.account_handle:
                account = self.db.get_account(register.account_handle)
                full = self.db.full_name(account) if account is not None else ""
            tab.set_title(title, full or title)

    def _on_tab_clicked(self, tab: _Tab) -> None:
        if tab.register is not None:
            self._activate_register(tab.register)
        self.show_category(tab.key)

    @property
    def tabs(self) -> list[tuple[str, str]]:
        """(view key, title) of every open tab, in order."""
        return [(tab.key, tab.title) for tab in self._tabs]

    def close_tab(self, tab: _Tab) -> None:
        """Close a tab; a register with a half-typed entry asks first."""
        register = tab.register
        if register is not None and register.has_unsaved():
            register.confirm_leave(lambda: self._remove_and_follow(tab))
            return
        self._remove_and_follow(tab)

    def _remove_and_follow(self, tab: _Tab) -> None:
        if tab not in self._tabs:
            return
        index = self._tabs.index(tab)
        was_current = tab.current
        self._remove_tab(tab)
        if tab.register is not None:
            self._drop_register(tab.register)
        if not was_current or self.db is None:
            return
        if self._tabs:
            following = self._tabs[min(index, len(self._tabs) - 1)]
            self._on_tab_clicked(following)
        else:
            self.show_category(CATEGORIES[0][0])

    def _remove_tab(self, tab: _Tab) -> None:
        if tab in self._tabs:
            self._tabs.remove(tab)
            self.tab_bar.remove(tab.widget)
        self.tab_scroll.set_visible(bool(self._tabs))

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


class _Tab:
    """One tab: a view, or one register, with a close button (#183)."""

    def __init__(self, key: str, register, title: str) -> None:
        self.key = key
        self.register = register
        self.title = title
        self.current = False
        self.widget = Gtk.Box(spacing=0)
        self.widget.add_css_class("linked")
        self.button = Gtk.Button(label=title)
        self.button.set_has_frame(False)
        self.button.get_child().set_ellipsize(Pango.EllipsizeMode.END)
        self.button.get_child().set_max_width_chars(28)
        self.close = Gtk.Button.new_from_icon_name("window-close-symbolic")
        self.close.set_has_frame(False)
        self.close.set_tooltip_text(f"Close {title}")
        self.widget.append(self.button)
        self.widget.append(self.close)

    def set_title(self, title: str, tooltip: str) -> None:
        self.title = title
        self.button.set_label(title)
        self.button.get_child().set_ellipsize(Pango.EllipsizeMode.END)
        self.button.set_tooltip_text(tooltip)
        self.close.set_tooltip_text(f"Close {title}")

    def set_current(self, current: bool) -> None:
        self.current = current
        if current:
            self.widget.add_css_class("current-tab")
        else:
            self.widget.remove_css_class("current-tab")


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
