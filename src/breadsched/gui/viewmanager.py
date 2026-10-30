"""The main window.

A menu bar and an icon toolbar above a stack of views, each view owning its own
toolbar. The main toolbar is arranged around the current view (#182, #361): after
the commands that work anywhere comes one icon for each other view (the View menu
lists every view), then the current view's name in bold, dividing those icons from
the current view's own commands. The current view has no icon, since choosing it
would do nothing, and the Actions menu lists the current view's commands first.
The book's account and transaction counts show only on the Dashboard and
Accounts. A category sidebar used to duplicate the view list and took width
from every view, so it was removed (#155).
A tab bar below the toolbar keeps every opened view one click away, with one
tab per open register account (#183): each register tab is its own register,
with its own place, filter, and half-typed entry. Projection can also open one
tab per scenario, each keeping its scenario while the main Projection tab follows
Plan. Each book's tabs are remembered in ``views.ini`` and reopened with the book.
Registers can also open in independent windows, so several accounts can be
compared side by side.

Views are constructed lazily and told about the book through :meth:`set_db`.  A
switch of book therefore never rebuilds the window, and a view that has never been
looked at costs nothing.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Callable
from pathlib import Path

from .. import APP_NAME  # noqa: E402
from ..api import ApiError
from ..api.local import book_server
from ..api.protocol import Change
from ..gen.services.ledger_reads import AccountRecord
from ..gen.utils.settings import Settings  # noqa: E402
from . import hidden_accounts
from .background import SnapshotReader
from .gi_setup import Gdk, Gio, GLib, GObject, Gtk, Pango
from .paths import default_book_path  # noqa: E402
from .remote_book import OpenBook, RemoteBook
from .view_catalog import (
    CATEGORIES,
    TOOLBAR,
    VIEW_ACTIONS,
    ViewAction,
    view_action_name,
)
from .widgets.bounded import BoundedWindow
from .widgets.brand import logo_picture
from .widgets.presented import when_presented
from .widgets.window_sizes import remember_size, restore_size

# The catalog is re-exported for callers that import it from the window.
#: The views whose toolbar shows the book's account and transaction counts (#361).
SUMMARY_VIEWS = frozenset({"dashboard", "accounts"})

__all__ = [
    "CATEGORIES",
    "SUMMARY_VIEWS",
    "TOOLBAR",
    "VIEW_ACTIONS",
    "ViewAction",
    "ViewManager",
    "view_action_name",
]

#: The ``views.ini`` section keeping each book's open tabs. Keys are a digest of
#: the book's path, since a path's colons would split an INI key.
TAB_SECTION = "open-tabs"


class ViewManager(Gtk.ApplicationWindow):
    """One window onto one book."""

    def __init__(self, application: Gtk.Application, *, prompt_due_on_open: bool = True) -> None:
        super().__init__(application=application, title=APP_NAME)
        self.set_default_size(1180, 760)
        # The last size and maximized state, from the user's settings (#349).
        restore_size(self, "main")
        self.db: OpenBook | None = None
        self._views: dict[str, Gtk.Widget] = {}
        #: Every register tab's view; ``_views["register"]`` is the one shown.
        self._registers: list[Gtk.Widget] = []
        #: Extra Projection tabs, each keeping its own scenario;
        #: ``_views["projection"]`` is the one that follows Plan.
        self._projections: list[Gtk.Widget] = []
        #: Open tabs in order: (category, own view or None, tab widget).
        self._tabs: list[_Tab] = []
        #: Opening a book restores its tabs; nothing is remembered meanwhile.
        self._restoring_tabs = False
        #: Answers slow views' reads off GTK's loop while a book is open (#389).
        self.snapshot_reader: SnapshotReader | None = None
        self._prefill_source: int | None = None
        self._unsubscribe: Callable[[], None] | None = None
        self._register_windows: list[tuple[Gtk.Window, Gtk.Widget]] = []
        # Automatic due review is a user-facing startup policy, not required for
        # binding views to a book. Tests and embedded windows can suppress it so
        # presenting a modal window does not pump the GLib main context.
        self._prompt_due_on_open = prompt_due_on_open
        # Modal dialogs this window shows on its own (start-up notices, the
        # GnuCash change review, the due review) wait in one queue and open one
        # at a time; see queue_modal.
        self._modal_queue: list[Callable[[Callable[[], None]], bool]] = []
        self._modal_active = False
        self._modal_cancel: Callable[[], None] | None = None
        self._review_generation = 0

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
        # The HTML report in the system browser: on the File menu only on macOS,
        # until native macOS printing replaces it.
        self.print_browser_action = Gio.SimpleAction.new("print-browser", None)
        self.print_browser_action.connect("activate", self._on_print_in_browser)
        self.print_browser_action.set_enabled(False)
        self.add_action(self.print_browser_action)
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
        # View → Double Line: one setting for every register, also in Preferences.
        settings = getattr(self.get_application(), "view_settings", None)
        double_line = bool(settings is not None and settings.get_bool("register", "double_line"))
        self.double_line_action = Gio.SimpleAction.new_stateful(
            "double-line", None, GLib.Variant.new_boolean(double_line)
        )
        self.double_line_action.connect("change-state", self._on_double_line)
        self.add_action(self.double_line_action)
        # View → Show Hidden Accounts: one setting for the whole application, which
        # changes no view (#397); it is the window's, not the Accounts view's.
        self.show_hidden_action = Gio.SimpleAction.new_stateful(
            "show-hidden", None, GLib.Variant.new_boolean(hidden_accounts.shown())
        )
        self.show_hidden_action.connect("change-state", self._on_show_hidden)
        self.add_action(self.show_hidden_action)
        # Tabs and finding, with the shortcuts other tabbed applications use.
        for name, handler in (
            ("close-tab", self._on_close_tab),
            ("next-tab", lambda *_: self.step_tab(1)),
            ("previous-tab", lambda *_: self.step_tab(-1)),
            ("move-tab-right", lambda *_: self.shift_tab(1)),
            ("move-tab-left", lambda *_: self.shift_tab(-1)),
            ("find", self._on_find),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            self.add_action(action)
        application = self.get_application()
        if application is not None:
            application.set_accels_for_action("win.print-view", ["<Control>p"])
            application.set_accels_for_action("win.close-tab", ["<Control>w"])
            application.set_accels_for_action("win.next-tab", ["<Control>Page_Down"])
            application.set_accels_for_action("win.previous-tab", ["<Control>Page_Up"])
            # Moving a tab, the keyboard's way to do what dragging does (#390).
            application.set_accels_for_action("win.move-tab-right", ["<Shift><Control>Page_Down"])
            application.set_accels_for_action("win.move-tab-left", ["<Shift><Control>Page_Up"])
            application.set_accels_for_action("win.find", ["<Control>f"])

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

        # The current view's name in bold divides the icons that change view from
        # the current view's own commands (for example "Manage FSA claims" on the
        # FSA Dashboard), which follow it; rebuilt whenever the view changes
        # (#182, #361).
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

        # The book summary ends the toolbar, stacked on two lines so it takes one
        # short column; it shows only where the counts mean something (#361).
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

        self.busy_label: Gtk.Label | None = None
        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_vexpand(True)
        # Size to the page shown, so leaving a large view lets the window shrink.
        self.stack.set_hhomogeneous(False)
        self.stack.set_vhomogeneous(False)
        outer.append(self.stack)
        # The "register" page holds one register per register tab.
        self.register_stack = Gtk.Stack()
        self.register_stack.set_vexpand(True)
        self.register_stack.set_hhomogeneous(False)
        self.register_stack.set_vhomogeneous(False)
        self.stack.add_named(self.register_stack, "register")

    def show_busy(self, message: str | None) -> None:
        """Show ``message`` with a spinner in place of the views, or go back with None.

        Used while a book is upgraded on opening: the work blocks the main loop
        for seconds, and a window that says what it is doing does not look hung.
        """
        if message is None:
            if self.stack.get_visible_child_name() == "busy":
                if self.db is None:
                    self.stack.set_visible_child_name("empty")
                else:
                    self.show_category(CATEGORIES[0][0])
            return
        if self.busy_label is None:
            page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            page.set_valign(Gtk.Align.CENTER)
            page.set_halign(Gtk.Align.CENTER)
            # The start-up splash: the logo while the remembered book opens.
            page.append(logo_picture())
            page.append(Gtk.Spinner(spinning=True, width_request=32, height_request=32))
            self.busy_label = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
            self.busy_label.set_max_width_chars(60)
            page.append(self.busy_label)
            self.stack.add_named(page, "busy")
        self.busy_label.set_text(message)
        self.stack.set_visible_child_name("busy")

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

        empty.append(logo_picture(240))
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
        if self._prefill_source is not None:
            GLib.source_remove(self._prefill_source)
            self._prefill_source = None
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        if self.snapshot_reader is not None:
            self.snapshot_reader.close()
            self.snapshot_reader = None
        for view in self._all_views():
            view.set_db(None)
        # Tabs name accounts of this book; the next book starts with its own.
        for tab in list(self._tabs):
            self._remove_tab(tab)
        for register in list(self._registers):
            self._drop_register(register)
        for projection in list(self._projections):
            self._drop_projection(projection)
        for window, view in list(self._register_windows):
            view.set_db(None)
            window.destroy()
        self._register_windows.clear()
        self.db = None
        self.print_action.set_enabled(False)
        self.print_browser_action.set_enabled(False)
        self.category_action.set_enabled(False)
        for action in self.view_actions.values():
            action.set_enabled(False)

    def book_closing(self) -> None:
        """Detach from the current book before its database connection is closed."""
        self._detach_views()

    def _on_close_request(self, *_args) -> bool:
        remember_size(self, "main")
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

    def book_opened(self, db: OpenBook, path: str) -> None:
        self.db = db
        self.set_title(f"{path.rsplit('/', 1)[-1]} — {APP_NAME}")
        if isinstance(db, RemoteBook):
            # The host reads from its own snapshots; this one only waits for it.
            self.snapshot_reader = SnapshotReader(api=db.book_api)
        elif db.path and db.path != ":memory:":
            self.snapshot_reader = SnapshotReader(db.path)
        # The window hears of changes, as its views do, from the book server (#356).
        self._unsubscribe = book_server(db).subscribe(self._on_book_change)
        for view in self._all_views():
            view.set_db(db)
        self._refresh_status()
        self._on_undo_available(False)
        self._on_redo_available(False)
        self.category_action.set_enabled(True)
        for action in self.view_actions.values():
            action.set_enabled(True)
        # The remembered tabs, with the chosen one drawn; else the Dashboard.
        if not self._restore_tabs():
            self._restoring_tabs = True
            try:
                self.show_category(CATEGORIES[0][0])
            finally:
                self._restoring_tabs = False
        self._queue_prefill()
        if self._prompt_due_on_open:
            self._schedule_due_prompt()

    # ------------------------------------------------------- hidden tabs (#389)

    def displays(self, view) -> bool:
        """Whether ``view`` is the one on screen in this window."""
        shown = self.stack.get_visible_child()
        if shown is self.register_stack:
            return self.register_stack.get_visible_child() is view
        return shown is view

    def defers_refresh(self, view) -> bool:
        """A view attached to the book while hidden is drawn when shown, or in the
        background, rather than at once: drawing every remembered tab made opening
        a long book take half a minute (#389). A view this window does not hold
        (a register window, a dialog's own view) is drawn at once."""
        held = any(view is item for item in self._all_views())
        return held and not self.displays(view)

    def _queue_prefill(self) -> None:
        """Soon, redraw out-of-date hidden tabs in the background (after a change too)."""
        if self.snapshot_reader is None or self._prefill_source is not None:
            return
        self._prefill_source = GLib.timeout_add(250, self._prefill)

    def _prefill(self) -> bool:
        """Start background redraws of hidden tabs whose views can have one.

        Each is read from the snapshot reader in turn, so switching to such a tab
        shows it current instead of calculating it then. A tab whose view has
        never been built is built hidden first; that is cheap now that hidden
        views wait to be drawn.
        """
        self._prefill_source = None
        if self.db is None or self.snapshot_reader is None:
            return GLib.SOURCE_REMOVE
        for tab in list(self._tabs):
            view = tab.view
            if view is None:
                if tab.key not in self.BACKGROUND_VIEWS:
                    continue
                view = self._views.get(tab.key) or self._build_hidden(tab.key)
            if view is None or self.displays(view) or not hasattr(view, "refresh_in_background"):
                continue
            if view.is_current() or view.refreshing_in_background:
                continue
            view.refresh_in_background()
        return GLib.SOURCE_REMOVE

    #: Views whose calculation runs on the snapshot reader, worth building hidden.
    BACKGROUND_VIEWS = frozenset({"plan", "accounts"})

    def _build_hidden(self, key: str):
        view = self._build_view(key)
        if view is None:
            return None
        self._views[key] = view
        self.stack.add_named(view, key)
        if self.db is not None:
            view.set_db(self.db)
        return view

    def wait_for_background(self, timeout: float = 10.0) -> bool:
        """Finish queued prefills and background reads; deterministic support for tests."""
        if self._prefill_source is not None:
            GLib.source_remove(self._prefill_source)
            self._prefill()
        reader = self.snapshot_reader
        return reader is None or reader.wait(timeout)

    def queue_modal(self, show: Callable[[Callable[[], None]], bool]) -> None:
        """Show a modal dialog after this window is on screen and every earlier one closed.

        ``show(done)`` presents its dialog and returns True, calling ``done`` once
        the dialog is gone, or returns False when it has nothing to show. Two
        modal dialogs open at once over the same window let the later one take
        every input while the earlier one sits on top of it, so neither answers
        and the window looks frozen (#295 follow-up); the queue prevents that, and
        waiting for the window to be mapped keeps the window from covering them.
        """
        self._modal_queue.append(show)
        if not self._modal_active:
            self._modal_active = True
            self._modal_cancel = when_presented(self, self._show_next_modal)

    def _show_next_modal(self) -> None:
        self._modal_cancel = None
        while self._modal_queue:
            show = self._modal_queue.pop(0)
            finished = False

            def done() -> None:
                nonlocal finished
                if not finished:
                    finished = True
                    GLib.idle_add(self._show_next_modal_once)

            if show(done):
                return
        self._modal_active = False

    def _show_next_modal_once(self) -> bool:
        self._show_next_modal()
        return GLib.SOURCE_REMOVE

    @staticmethod
    def when_closed(dialog: Gtk.Window, done: Callable[[], None]) -> None:
        """Call ``done`` once ``dialog`` has closed, however it closes.

        GTK 4 does not emit ``hide`` when a window closes; it unmaps and
        unrealizes it, and destroys it later. ``unrealize`` is the dependable
        signal, and ``destroy`` covers a dialog destroyed before it was shown.
        """
        dialog.connect("unrealize", lambda *_: done())
        dialog.connect("destroy", lambda *_: done())

    def _schedule_due_prompt(self) -> None:
        """Queue the GnuCash change review, then the due review, for this book.

        Scheduling again (the book reopened, or opened twice before the window
        appeared) supersedes reviews queued earlier that have not been shown.
        """
        expected_db = self.db
        self._review_generation += 1
        generation = self._review_generation

        def review(prompt: Callable[[], Gtk.Window | None]):
            def show(done: Callable[[], None]) -> bool:
                if generation != self._review_generation:
                    return False
                if self.db is not expected_db or self.db is None:
                    return False
                dialog = prompt()
                if dialog is None:
                    return False
                self.when_closed(dialog, done)
                return True

            return show

        self.queue_modal(review(self.prompt_for_held_imports))
        self.queue_modal(review(self.prompt_for_due))

    def prompt_for_held_imports(self) -> Gtk.Window | None:
        """Ask about GnuCash changes held back from reconciled transactions."""
        if self.db is None:
            return None
        from ..api.local import book_server
        from .dialogs.import_review_dialog import ImportReviewDialog

        changes = list(book_server(self.db).held_imports())
        if not changes:
            return None
        dialog = ImportReviewDialog(self, self.db, changes)
        dialog.connect("close-request", lambda *_: (self._refresh_views(), False)[1])
        dialog.present()
        return dialog

    def prompt_for_due(self) -> Gtk.Window | None:
        """Ask about anything due (on opening the book, or from Post Scheduled
        Transactions); the dialog, or None when nothing is due."""
        if self.db is None:
            return None
        from ..api.local import book_server
        from .dialogs.due_dialog import DueDialog

        # Due and missed, plus what schedules enter or remind in advance (#352),
        # from the book server (#359).
        reviews = book_server(self.db).due_review()
        if not reviews:
            return None
        dialog = DueDialog(self, self.db, reviews)
        dialog.connect("close-request", lambda *_: (self._refresh_views(), False)[1])
        dialog.present()
        return dialog

    def _all_views(self) -> list[Gtk.Widget]:
        """Every view in this window, including every register tab's register."""
        views = [view for key, view in self._views.items() if key != "register"]
        return views + list(self._registers) + list(self._projections)

    def planning_views(self) -> list[Gtk.Widget]:
        """Plan and every Projection, for scenario notifications."""
        views = [self._views[key] for key in ("plan", "projection") if key in self._views]
        return views + list(self._projections)

    def refresh_views(self) -> None:
        """A book-wide setting changed: redraw the shown view, the rest when shown."""
        for view in self._all_views():
            view.invalidate()

    #: Kept for callers that used the private name.
    _refresh_views = refresh_views

    def _refresh_status(self) -> None:
        if self.db is None:
            return
        counts = book_server(self.db).book_counts()
        self.status.set_text(f"{counts.accounts} accounts\n{counts.transactions} transactions")

    def _on_book_change(self, change: Change) -> None:
        self._refresh_status()
        self._queue_prefill()
        label = change.label
        if label and change.undo_by:
            label = f"{label} ({change.undo_by})"
        self._on_undo_available(change.can_undo, label)
        self._on_redo_available(change.can_redo)

    def _on_undo_available(self, available: bool, message: str | None = None) -> None:
        application = self.get_application()
        if application is not None:
            application.set_action_enabled("undo", available)
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
        self.status.set_visible(key not in labels or key in SUMMARY_VIEWS)
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
        shown = self.stack.get_visible_child()
        if shown in self._projections and key == "projection":
            return shown  # a scenario tab acts on its own scenario
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
        # Showing a view redraws it only if the book changed (or the day turned)
        # since it was last drawn; the map handler usually did that already.
        view.ensure_current()
        self._enable_printing(view)
        self._focus_view(view)

    @staticmethod
    def _focus_view(view) -> None:
        """Put the keyboard in the shown view, wherever the switch came from (#331).

        A register takes it in the cell under its cursor; a tab or toolbar button
        clicked to get here would otherwise keep it.
        """
        focus = getattr(view, "focus_entry", None)
        if callable(focus):
            focus()

    def _enable_printing(self, view) -> None:
        printable = bool(self.db is not None and getattr(view, "PRINTABLE", False))
        self.print_action.set_enabled(printable)
        self.print_browser_action.set_enabled(printable)

    def _printable_view(self):
        """The shown report-capable view, once its applied state is current."""
        view = self.stack.get_visible_child()
        if view is None or not getattr(view, "PRINTABLE", False):
            return None
        view.flush_refresh()
        wait_for_background = getattr(view, "wait_for_background", None)
        if callable(wait_for_background) and not wait_for_background():
            raise TimeoutError("the current report is still calculating")
        return view

    def _report_error(self, message: str) -> None:
        reporter = getattr(self.get_application(), "_report", None)
        if reporter is not None:
            reporter(message)

    def _on_print_view(self, *_args) -> None:
        """Print the applied state of the current report through GTK printing."""
        try:
            view = self._printable_view()
            document = view.printable_report() if view is not None else None
        except Exception as exc:  # noqa: BLE001 - reported, never lost
            self._report_error(f"Could not prepare the report: {exc}")
            return
        if document is None:
            return
        from . import printing

        printing.print_report(self, document, on_error=self._report_error)

    def _on_print_in_browser(self, *_args) -> None:
        """Open the current report as a web page, for the browser's print dialog."""
        try:
            view = self._printable_view()
            document = view.printable_html() if view is not None else None
            if not document:
                return
            from . import printing

            printing.open_print_preview(document)
        except Exception as exc:  # noqa: BLE001 - opening the desktop handler may fail
            self._report_error(f"Could not open the print preview: {exc}")

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
            register = self._new_register(account_handle)
        self._activate_register(register)
        self.show_category("register")
        register.show_account(account_handle)
        self._update_tab_label(register)

    def open_register_tab(self, account_handle: str | None = None):
        """Open another register tab, even for an account that already has one (#228).

        Without an account it shows the first account, in the picker's order, that
        has no register tab yet (or the current register's account when every
        account has one); its account picker then moves it to any account.
        """
        if account_handle is None and self.db is not None:
            open_accounts = {view.account_handle for view in self._registers}
            candidates = sorted(
                (
                    record
                    for record in book_server(self.db).accounts()
                    if not record.account.is_root and not record.account.placeholder
                ),
                key=lambda record: record.full_name,
            )
            spare = next(
                (
                    record.account.handle
                    for record in candidates
                    if record.account.handle not in open_accounts
                ),
                None,
            )
            current = self._views.get("register")
            account_handle = spare or (current.account_handle if current is not None else None)
        register = self._new_register(account_handle)
        self._activate_register(register)
        self.show_category("register")
        if account_handle is not None:
            register.show_account(account_handle)
        self._update_tab_label(register)
        return register

    # ------------------------------------------------------------------- tabs

    def _new_register(self, account_handle: str | None = None):
        from .views.register import RegisterView

        register = RegisterView(self)
        # Set before attaching, so the register reads this account once rather
        # than first the picker's first account and then this one (#389).
        register.account_handle = account_handle
        self._registers.append(register)
        self.register_stack.add_child(register)
        register.account_picker.connect(
            "notify::selected", lambda *_a: self.tab_view_changed(register)
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

    def open_projection_tab(self, scenario_handle: str | None) -> Gtk.Widget:
        """Show a Projection of one scenario in a tab of its own.

        The main Projection tab follows the scenario chosen in Plan; these extra
        tabs keep theirs, so two futures can be looked at side by side. A scenario
        that already has a tab switches to it.
        """
        projection = next(
            (view for view in self._projections if view.scenario_handle == scenario_handle),
            None,
        )
        if projection is None:
            projection = self._new_projection(scenario_handle)
        self._show_projection(projection)
        return projection

    def _new_projection(self, scenario_handle: str | None):
        from .views.projection import ProjectionView

        projection = ProjectionView(self, pinned=True, scenario_handle=scenario_handle)
        self._projections.append(projection)
        self.stack.add_child(projection)
        if self.db is not None:
            projection.set_db(self.db)
        return projection

    def _show_projection(self, projection) -> None:
        self.stack.set_visible_child(projection)
        self._select_tab("projection", projection)
        self.category_action.set_state(GLib.Variant.new_string("projection"))
        self._show_view_tools("projection")
        projection.refresh()
        self._enable_printing(projection)

    def _drop_projection(self, projection) -> None:
        projection.set_db(None)
        if projection in self._projections:
            self._projections.remove(projection)
        if projection.get_parent() is self.stack:
            self.stack.remove(projection)

    def tab_view_changed(self, view) -> None:
        """A tab's register or scenario Projection now shows something else."""
        self._update_tab_label(view)
        self._remember_tabs()

    def _account_record(self, handle: str | None) -> AccountRecord | None:
        """An account with its full name, as the book server stores it, if it exists."""
        if self.db is None or not handle:
            return None
        try:
            return book_server(self.db).account(handle)
        except ApiError:
            return None

    def _scenario_exists(self, handle: str) -> bool:
        assert self.db is not None
        try:
            book_server(self.db).find_scenario(handle)
        except ApiError:
            return False
        return True

    def _tab_title(self, key: str, view) -> str:
        if view is None:
            return next((label for item, label, _icon in CATEGORIES if item == key), key)
        if key != "register":
            return view.tab_title
        record = self._account_record(view.account_handle)
        if record is None:
            return "Register"
        return record.account.name

    def _tab_for(self, key: str, view) -> _Tab | None:
        return next((tab for tab in self._tabs if tab.key == key and tab.view is view), None)

    def _add_tab(self, key: str, view) -> _Tab:
        """A tab for ``view`` (or the view ``key``) at the end of the bar, not chosen."""
        tab = _Tab(key, view, self._tab_title(key, view))
        tab.button.connect("clicked", lambda *_a: self._on_tab_clicked(tab))
        tab.close.connect("clicked", lambda *_a: self.close_tab(tab))
        self._make_draggable(tab)
        self._tabs.append(tab)
        self.tab_bar.append(tab.widget)
        self.tab_scroll.set_visible(True)
        return tab

    def _select_tab(self, key: str, view) -> None:
        """Mark the tab for what is shown, opening one if needed."""
        tab = self._tab_for(key, view) or self._add_tab(key, view)
        for item in self._tabs:
            item.set_current(item is tab)
        self._update_tab_label(view)
        self.tab_scroll.set_visible(True)
        self._remember_tabs()

    def _update_tab_label(self, view) -> None:
        if view is None:
            return
        tab = next((item for item in self._tabs if item.view is view), None)
        if tab is None:
            return
        title = self._tab_title(tab.key, view)
        full = ""
        if tab.key == "register":
            record = self._account_record(view.account_handle)
            full = record.full_name if record is not None else ""
        tab.set_title(title, full or title)

    def _on_tab_clicked(self, tab: _Tab) -> None:
        if tab.key == "register":
            self._activate_register(tab.view)
        elif tab.view is not None:
            self._show_projection(tab.view)
            return
        self.show_category(tab.key)

    def _current_tab(self) -> _Tab | None:
        return next((tab for tab in self._tabs if tab.current), None)

    def _on_close_tab(self, *_args) -> None:
        tab = self._current_tab()
        if tab is not None:
            self.close_tab(tab)

    def step_tab(self, step: int) -> None:
        """Show the tab ``step`` places from the current one, wrapping around."""
        if not self._tabs:
            return
        current = self._current_tab()
        index = self._tabs.index(current) if current is not None else 0
        self._on_tab_clicked(self._tabs[(index + step) % len(self._tabs)])

    def move_tab(self, tab: _Tab, index: int) -> None:
        """Put ``tab`` at ``index`` in the tab bar and remember the new order (#390)."""
        if tab not in self._tabs:
            return
        index = max(0, min(index, len(self._tabs) - 1))
        if self._tabs.index(tab) == index:
            return
        self._tabs.remove(tab)
        self._tabs.insert(index, tab)
        before = self._tabs[index - 1].widget if index > 0 else None
        self.tab_bar.reorder_child_after(tab.widget, before)
        self._remember_tabs()

    def shift_tab(self, step: int) -> None:
        """Move the current tab ``step`` places along the bar, stopping at either end."""
        current = self._current_tab()
        if current is not None:
            self.move_tab(current, self._tabs.index(current) + step)

    def _make_draggable(self, tab: _Tab) -> None:
        """Drag a tab onto another to put it there, as other tabbed applications do."""
        source = Gtk.DragSource(actions=Gdk.DragAction.MOVE)
        source.connect("prepare", lambda *_a: Gdk.ContentProvider.new_for_value(str(id(tab))))
        tab.widget.add_controller(source)
        target = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.MOVE)
        target.connect("drop", lambda _t, value, _x, _y: self._on_tab_drop(tab, value))
        tab.widget.add_controller(target)

    def _on_tab_drop(self, onto: _Tab, value: str) -> bool:
        dragged = next((tab for tab in self._tabs if str(id(tab)) == value), None)
        if dragged is None or dragged is onto or onto not in self._tabs:
            return False
        self.move_tab(dragged, self._tabs.index(onto))
        return True

    def _on_show_hidden(self, action, value) -> None:
        """List hidden accounts and make them usable everywhere, or put them away,
        leaving the shown view as it is (#397)."""
        action.set_state(value)
        hidden_accounts.set_shown(value.get_boolean())
        self.refresh_views()
        for _window, view in self._register_windows:
            view.invalidate()

    def _on_double_line(self, action, value) -> None:
        """Show or hide every register's notes line, and remember the choice."""
        action.set_state(value)
        application = self.get_application()
        settings = getattr(application, "view_settings", None)
        if settings is None:
            return
        settings.set("register", "double_line", value.get_boolean())
        settings.save()
        from .views.register import apply_register_display

        apply_register_display()
        changed = getattr(application, "preferences_changed", None)
        if callable(changed):
            changed()

    def shown_register(self):
        """The register tab on screen, or None when another view is shown."""
        if self.stack.get_visible_child() is self.register_stack:
            return self._views.get("register")
        return None

    def _on_find(self, *_args) -> None:
        """Ctrl+F: the shown register's filter, the one search a view has."""
        register = self.shown_register()
        entry = getattr(register, "filter_entry", None)
        if entry is not None:
            # A tab's own request to focus its cursor's cell is overtaken.
            register.cancel_focus_entry()
            entry.grab_focus()

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
        elif tab.view is not None:
            self._drop_projection(tab.view)
        if not was_current or self.db is None:
            self._remember_tabs()
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

    # ------------------------------------------------------ remembered tabs

    def _tab_settings(self) -> tuple[Settings, str] | None:
        """The interface-state file and this book's key in it, if any."""
        settings = getattr(self.get_application(), "view_settings", None)
        path = getattr(self.db, "path", None) if self.db is not None else None
        if settings is None or not path or path == ":memory:":
            return None
        digest = hashlib.sha256(str(Path(path).resolve()).encode("utf-8")).hexdigest()
        return settings, digest[:24]

    def _remember_tabs(self) -> None:
        """Keep this book's open tabs, so reopening it brings them back."""
        if self._restoring_tabs or not self._tabs:
            return
        place = self._tab_settings()
        if place is None:
            return
        settings, key = place
        entries = []
        for tab in self._tabs:
            if tab.key == "register":
                entries.append([tab.key, tab.view.account_handle or ""])
            elif tab.view is not None:
                entries.append([tab.key, tab.view.scenario_handle or "", "scenario"])
            else:
                entries.append([tab.key])
        current = next((i for i, tab in enumerate(self._tabs) if tab.current), 0)
        stored = json.dumps({"tabs": entries, "current": current}, separators=(",", ":"))
        if settings.get(TAB_SECTION, key) != stored:
            settings.set(TAB_SECTION, key, stored)
            settings.save()

    def _restore_tabs(self) -> bool:
        """Reopen the tabs this book had; anything since deleted is skipped.

        Only the chosen tab is drawn; the rest are drawn when chosen, or in the
        background (#389). Returns whether any tab came back.
        """
        place = self._tab_settings()
        if place is None or self.db is None:
            return False
        settings, key = place
        try:
            stored = json.loads(settings.get(TAB_SECTION, key) or "{}")
            entries = [list(entry) for entry in stored.get("tabs", [])]
            current = int(stored.get("current", 0))
        except (TypeError, ValueError, AttributeError):
            return False
        known = {item for item, _label, _icon in CATEGORIES}
        opened: list[_Tab | None] = []
        self._restoring_tabs = True
        try:
            for entry in entries:
                opened.append(self._reopen_tab(entry, known))
            wanted = opened[current] if 0 <= current < len(opened) else None
            restored = [tab for tab in opened if tab is not None]
            if restored:
                self._on_tab_clicked(wanted or restored[-1])
        finally:
            self._restoring_tabs = False
        if restored:
            self._remember_tabs()
        return bool(restored)

    def _reopen_tab(self, entry: list, known: set[str]) -> _Tab | None:
        if self.db is None or not entry or not isinstance(entry[0], str):
            return None
        if entry[0] not in known:
            return None
        key = entry[0]
        reference = entry[1] if len(entry) > 1 and isinstance(entry[1], str) else ""
        # A remembered tab comes back as a tab; what it shows is drawn when it is
        # chosen, or in the background (#389), not all at once as the book opens.
        if key == "register":
            if self._account_record(reference) is None:
                return None
            # Every remembered register tab comes back, even two on one account.
            return self._add_tab("register", self._new_register(reference))
        if len(entry) > 2 and key == "projection":
            if reference and not self._scenario_exists(reference):
                return None
            return self._add_tab(key, self._new_projection(reference or None))
        return self._tab_for(key, None) or self._add_tab(key, None)

    def open_register_window(self, account_handle: str) -> Gtk.Window | None:
        """Open an independently navigable register sharing the current book."""
        if self.db is None:
            return None
        from .views.register import RegisterView

        window = BoundedWindow(transient_for=self)
        window.set_destroy_with_parent(True)
        window.set_default_size(1050, 620)
        window.size_key = "register-window"
        window.always_remember_size = True
        register = RegisterView(self)
        register.account_handle = account_handle
        window.set_child(register)
        pair = (window, register)
        self._register_windows.append(pair)

        def update_title(*_args) -> None:
            record = self._account_record(register.account_handle)
            name = record.full_name if record is not None else ""
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
    """One tab: a view, one register, or one scenario's Projection (#183)."""

    def __init__(self, key: str, view, title: str) -> None:
        self.key = key
        #: The register or scenario Projection this tab owns; None for a view.
        self.view = view
        self.title = title
        self.current = False
        self.widget = Gtk.Box(spacing=0)
        self.widget.add_css_class("linked")
        self.button = Gtk.Button(label=title)
        self.button.set_has_frame(False)
        # Choosing a tab leaves the keyboard in the view it shows (#331).
        self.button.set_focus_on_click(False)
        self.button.get_child().set_ellipsize(Pango.EllipsizeMode.END)
        self.button.get_child().set_max_width_chars(28)
        self.close = Gtk.Button.new_from_icon_name("window-close-symbolic")
        self.close.set_has_frame(False)
        self.close.set_focus_on_click(False)
        self.close.set_tooltip_text(f"Close {title}")
        self.widget.append(self.button)
        self.widget.append(self.close)

    @property
    def register(self):
        """The register this tab owns, if it is a register tab."""
        return self.view if self.key == "register" else None

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
