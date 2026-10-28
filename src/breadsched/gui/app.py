"""GTK4 application shell.

The GUI is a strict consumer of :mod:`breadsched.gen`: it holds no financial logic of
its own, and every number it shows comes from an engine function.  That boundary is
what lets the whole model be tested without a display server, and it is why this
module is the only place ``gi`` is imported at start-up.
"""

from __future__ import annotations

import sys
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING

from .. import APP_ID, APP_NAME, __version__  # noqa: E402
from ..gen.db.sqlite import SCHEMA_VERSION, DbSQLite, stored_schema_version  # noqa: E402
from ..gen.utils.logs import get_logger  # noqa: E402
from ..gen.utils.settings import Settings  # noqa: E402
from ..presentation import (  # noqa: E402
    book_open_notice,
    book_opening_progress,
    book_upgrade_progress,
)
from ..user_guide import help_target
from .gi_setup import Gdk, Gio, GLib, Gtk
from .user_guide import UserGuideWindow
from .view_catalog import CATEGORIES as MENU_CATEGORIES  # noqa: E402
from .view_catalog import VIEW_ACTIONS, view_action_name  # noqa: E402
from .viewmanager import ViewManager  # noqa: E402
from .widgets.brand import install_icons, logo_texture
from .widgets.presented import when_presented

if TYPE_CHECKING:
    from .book_sharing import SharedBook

__all__ = ["BreadSchedApplication", "main"]

LOG = get_logger(__name__)

STYLE_RESOURCE = resources.files("breadsched.gui").joinpath("resources/style.css")


def show_scrollbars() -> bool:
    """Give every scrolled area a scrollbar that is always shown (#318).

    GTK 4's overlay scrollbars are a thin indicator that appears only while
    scrolling or under the pointer, so a list such as the due review gives no
    sign that more of it is out of view. Returns whether the setting exists.
    """
    settings = Gtk.Settings.get_default()
    if settings is None or settings.find_property("gtk-overlay-scrolling") is None:
        return False
    settings.set_property("gtk-overlay-scrolling", False)
    return True


class BreadSchedApplication(Gtk.Application):
    """Owns the open book and the windows looking at it."""

    def __init__(
        self,
        application_id: str = APP_ID,
        unique: bool = True,
        settings_directory: Path | None = None,
    ) -> None:
        """Create the application.

        ``application_id`` and ``unique`` exist because GApplication exports itself
        on the session bus at a path derived from its id, and a second export at
        the same path fails outright. Two instances in one process therefore need
        distinct ids, which is exactly what the test suite does. Normal use never
        passes either argument.
        """
        flags = Gio.ApplicationFlags.HANDLES_OPEN
        if not unique:
            flags |= Gio.ApplicationFlags.NON_UNIQUE
        super().__init__(application_id=application_id, flags=flags)
        self.db: DbSQLite | None = None
        self.book_path: str | None = None
        #: The open book shared with other commands, when that is on (#359).
        self.shared_book: SharedBook | None = None
        #: Deliberate preferences, and remembered interface state. Kept apart so a
        #: user can delete the second without losing the first.
        self.settings = Settings("settings", directory=settings_directory)
        self.view_settings = Settings("views", directory=settings_directory)
        self.actions: dict[str, Gio.SimpleAction] = {}

    # ------------------------------------------------------------- life cycle

    def do_startup(self) -> None:
        Gtk.Application.do_startup(self)
        provider = Gtk.CssProvider()
        stylesheet = STYLE_RESOURCE.read_text(encoding="utf-8")
        if hasattr(provider, "load_from_string"):  # GTK 4.12+
            provider.load_from_string(stylesheet)
        else:
            provider.load_from_data(stylesheet.encode("utf-8"))
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
        show_scrollbars()
        # The installed desktop entry, AppStream metadata, and icon share the
        # application id, so windows use the themed icon wherever it is installed.
        # A checkout or pip install has no installed icon: the package carries it.
        install_icons(display)
        Gtk.Window.set_default_icon_name(APP_ID)
        self._install_actions()
        #: The Actions menu follows the active window's current view (#182).
        self.actions_menu = ActionsMenu()
        self.set_menubar(build_menu_model(self.actions_menu))

    def show_view_actions(self, key: str | None) -> None:
        """Put ``key``'s commands first in the Actions menu, other views' after."""
        self.actions_menu.show_view(key)

    def do_activate(self) -> None:
        window = self.props.active_window or ViewManager(self)
        # Reopen the previous book behind an "Opening…" page: the window comes up
        # at once and says what it is doing, rather than nothing appearing for the
        # seconds a large book takes to open (#389), and the empty-book chooser is
        # never flashed. An upgrade says so on the same page (#295).
        if self.db is None:
            remembered = self.settings.get("general", "last_book_path")
            if remembered and Path(remembered).exists() and isinstance(window, ViewManager):
                self.open_book_announced(remembered, window=window, failure=None, always=True)
            else:
                self.reopen_last_book()
        window.present()

    @staticmethod
    def _needs_upgrade(path: str) -> bool:
        schema = stored_schema_version(path)
        return schema is not None and schema < SCHEMA_VERSION

    def open_book_announced(
        self,
        path: str,
        *,
        window: ViewManager | None = None,
        failure: str | None,
        always: bool = False,
    ) -> None:
        """Open ``path``; a book that needs upgrading first says so on screen.

        Upgrading backs the book up, verifies the copy, and migrates it, which
        blocks the main loop for seconds on a large book. The window therefore
        shows what is happening and is presented before the upgrade starts, and
        the notice afterwards comes up over a window that is already on screen.
        ``failure`` prefixes the message reported if opening fails; ``None`` logs
        the failure instead, as for the remembered book at start-up. ``always``
        shows an "Opening…" page for a current book too, as start-up does (#389).
        """
        if window is None:
            active = self.props.active_window
            window = active if isinstance(active, ViewManager) else None
        schema = stored_schema_version(path)
        upgrading = schema is not None and schema < SCHEMA_VERSION
        if window is None or not (upgrading or always):
            self._open_or_report(path, failure)
            return
        window.show_busy(
            book_upgrade_progress(path, schema)
            if upgrading and schema is not None
            else book_opening_progress(path)
        )
        window.present()

        def upgrade() -> None:
            try:
                self._open_or_report(path, failure)
            finally:
                window.show_busy(None)

        when_presented(window, upgrade)

    def _open_or_report(self, path: str, failure: str | None) -> None:
        try:
            self.open_book(path)
        except Exception as exc:  # noqa: BLE001 - surfaced or logged below
            if failure is None:
                LOG.exception("could not reopen %s", path)
            else:
                self._report(f"{failure}: {exc}")

    def reopen_last_book(self) -> bool:
        """Reopen the book from last time, if it is still there.

        A book that has been moved or deleted is forgotten rather than reported:
        the user did not ask for it this time, and an error dialog on start-up
        about a file they deliberately removed is noise.
        """
        remembered = self.settings.get("general", "last_book_path")
        if not remembered:
            return False

        if not Path(remembered).exists():
            self.settings.remove("general", "last_book_path")
            self.settings.save()
            return False
        try:
            self.open_book(remembered)
            return True
        except Exception:  # noqa: BLE001 - a damaged book must not block start-up
            LOG.exception("could not reopen %s", remembered)
            return False

    def do_open(self, files, n_files, hint) -> None:
        self.do_activate()
        if files:
            self.open_book_announced(files[0].get_path(), failure="Could not open the book")

    def _install_actions(self) -> None:
        for name, handler, accel in (
            ("open", self.on_open, "<Control>o"),
            ("new", self.on_new, "<Control>n"),
            ("import", self.on_import, "<Control>i"),
            ("import-new", self.on_import_new, None),
            ("import-csv", self.on_import_csv, None),
            ("rules", self.on_rules, None),
            ("receivables", self.on_receivables, None),
            ("savings-goals", self.on_savings_goals, None),
            ("find-transactions", self.on_find_transactions, "<Control><Shift>f"),
            ("fsa-claims", self.on_fsa_claims, None),
            ("gnucash-writeback", self.on_gnucash_writeback, None),
            ("open-default", self.on_open_default, None),
            ("undo", self.on_undo, "<Control>z"),
            ("redo", self.on_redo, "<Control><Shift>z"),
            ("export", self.on_export, None),
            ("backup", self.on_backup, None),
            ("restore", self.on_restore, None),
            ("verify", self.on_verify, None),
            ("post-scheduled", self.on_post_scheduled, None),
            ("new-transaction", self.on_new_transaction, "<Control>t"),
            ("preferences", self.on_preferences, "<Control>comma"),
            ("user-guide", self.on_user_guide, "F1"),
            ("about", self.on_about, None),
            ("quit", self.on_quit, "<Control>q"),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            self.add_action(action)
            self.actions[name] = action
            if accel:
                self.set_accels_for_action(f"app.{name}", [accel])
        # A widget bound to an action takes its sensitivity from that action and
        # ignores set_sensitive(), so undo/redo availability must be expressed here
        # rather than on the button.
        for name in (
            "undo",
            "redo",
            "import",
            "import-csv",
            "rules",
            "receivables",
            "savings-goals",
            "find-transactions",
            "fsa-claims",
            "gnucash-writeback",
            "export",
            "backup",
            "verify",
            "post-scheduled",
            "new-transaction",
        ):
            self.actions[name].set_enabled(False)

    # ------------------------------------------------------------------- book

    def open_book(self, path: str) -> None:
        """Close whatever is open and load ``path``, then tell the windows."""
        if self.db is not None:
            # Views own signal handlers and deferred GLib callbacks tied to this
            # connection. Detach them before closing it so no stale repaint can
            # later run against a dead book.
            for window in self.get_windows():
                if isinstance(window, ViewManager):
                    window.book_closing()
            self.stop_sharing()
            self.db.close()
        self.db = DbSQLite()
        self.db.load(path)
        self.book_path = path
        self.update_sharing()
        # Remembered so the next start reopens it. Written immediately rather than
        # at shutdown: a crash should not cost the setting.
        self.settings.set("general", "last_book_path", str(Path(path).resolve()))
        self.settings.save()
        for name in (
            "import",
            "import-csv",
            "rules",
            "receivables",
            "savings-goals",
            "find-transactions",
            "fsa-claims",
            "gnucash-writeback",
            "export",
            "backup",
            "verify",
            "post-scheduled",
            "new-transaction",
        ):
            self.set_action_enabled(name, True)
        # The notice says what opening did (an upgrade, where backups go), so it
        # is queued before the reviews book_opened queues behind it.
        notice = book_open_notice(path, migration_backup=self.db.migration_backup)
        if notice is not None:
            self._report(notice)
        for window in self.get_windows():
            if isinstance(window, ViewManager):
                window.book_opened(self.db, path)

    # --------------------------------------------------------------- sharing

    def update_sharing(self) -> None:
        """Share the open book with other commands exactly when the preference is on."""
        from .book_sharing import SETTING, share_book

        wanted = self.settings.get_bool(*SETTING) and self.db is not None
        if wanted and self.shared_book is None and self.book_path and self.db is not None:
            self.shared_book = share_book(self.db, self.book_path)
        elif not wanted:
            self.stop_sharing()

    def stop_sharing(self) -> None:
        if self.shared_book is not None:
            self.shared_book.stop()
            self.shared_book = None

    def do_shutdown(self) -> None:
        self.stop_sharing()
        Gtk.Application.do_shutdown(self)

    def require_db(self) -> DbSQLite | None:
        return self.db

    def set_action_enabled(self, name: str, enabled: bool) -> None:
        action = self.actions.get(name)
        if action is not None:
            action.set_enabled(enabled)

    # ---------------------------------------------------------------- actions

    def on_new(self, *_args) -> None:
        dialog = Gtk.FileDialog(title="New book", initial_name="household.breadsched")
        dialog.save(self.props.active_window, None, self._on_new_chosen)

    def _on_new_chosen(self, dialog, result) -> None:
        try:
            file = dialog.save_finish(result)
        except GLib.Error:
            return
        from argparse import Namespace

        from ..cli.book_commands import cmd_init

        target = Path(file.get_path())
        try:
            if target.exists():
                # The chooser already asked whether to replace, and the user said
                # yes; cmd_init refuses to overwrite. Honour the answer by clearing
                # the old book first, including SQLite's write-ahead log and
                # shared-memory files -- left behind, those reattach to the new
                # database and carry the old book's pages into it.
                self._remove_book(target)
            cmd_init(Namespace(book=str(target), json=False))
        except Exception as exc:  # noqa: BLE001 - surfaced to the user below
            self._report(f"Could not create the book: {exc}")
            return
        self.open_book(str(target))

    @staticmethod
    def _remove_book(target: Path) -> None:
        """Delete a book and the sidecar files SQLite keeps beside it."""
        for suffix in ("", "-wal", "-shm", "-journal"):
            companion = Path(str(target) + suffix)
            if companion.exists():
                companion.unlink()

    def on_open(self, *_args) -> None:
        dialog = Gtk.FileDialog(title="Open book")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        book_filter = Gtk.FileFilter()
        book_filter.set_name("BreadSched books")
        book_filter.add_pattern("*.breadsched")
        filters.append(book_filter)
        dialog.set_filters(filters)
        dialog.open(self.props.active_window, None, self._on_open_chosen)

    def _on_open_chosen(self, dialog, result) -> None:
        try:
            file = dialog.open_finish(result)
        except GLib.Error:
            return
        self.open_book_announced(file.get_path(), failure="Could not open the book")

    @staticmethod
    def _materialize_starter_book(target: Path) -> None:
        """Create starter content in a new user-owned book at ``target``.

        The destination must not already exist.  This boundary is intentionally
        strict so a future packaged example/template can be copied or imported
        here without ever opening that example as the user's live book.
        """
        if target.exists():
            raise FileExistsError(f"book already exists: {target}")

        from argparse import Namespace

        from ..cli.book_commands import cmd_init

        target.parent.mkdir(parents=True, exist_ok=True)
        cmd_init(Namespace(book=str(target), json=True))

    def on_open_default(self, *_args) -> None:
        """Open the user's default book, creating it only when absent.

        Existing user data is never replaced by this action.  Starter/example
        content is materialized only when the destination does not yet exist, and
        only the resulting user-owned book is opened and remembered.
        """
        from .paths import default_book_path

        target = default_book_path()
        try:
            if not target.exists():
                self._materialize_starter_book(target)
            self.open_book(str(target))
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            self._report(f"Could not open the default book: {exc}")

    def on_import_new(self, *_args) -> None:
        """Create a book and import a GnuCash file straight into it.

        Two steps that are always taken together for a first-time user, so the
        start screen offers them as one: choose the GnuCash file, then choose
        where the new book should live.
        """
        dialog = Gtk.FileDialog(title="GnuCash book to import")
        dialog.open(self.props.active_window, None, self._on_import_source_chosen)

    def _on_import_source_chosen(self, dialog, result) -> None:
        try:
            source = dialog.open_finish(result)
        except GLib.Error:
            return
        source_path = source.get_path()

        from .paths import BOOK_SUFFIX

        suggested = Path(source_path).stem + BOOK_SUFFIX
        save = Gtk.FileDialog(title="New book to import into", initial_name=suggested)
        save.save(
            self.props.active_window,
            None,
            lambda d, r: self._on_import_target_chosen(d, r, source_path),
        )

    def _on_import_target_chosen(self, dialog, result, source_path: str) -> None:
        try:
            file = dialog.save_finish(result)
        except GLib.Error:
            return

        from argparse import Namespace

        from ..cli.book_commands import cmd_init

        target = Path(file.get_path())
        try:
            if target.exists():
                self._remove_book(target)
            cmd_init(Namespace(book=str(target), json=True))
            self.open_book(str(target))
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            self._report(f"Could not create the book: {exc}")
            return

        from .dialogs.import_dialog import ImportDialog

        if self.db is None:
            return
        window = ImportDialog(self.props.active_window, self.db)
        window.set_source(source_path)
        window.present()

    def on_import(self, *_args) -> None:
        if self.db is None:
            self._report("Open a book before importing into it.")
            return
        from .dialogs.import_dialog import ImportDialog

        ImportDialog(self.props.active_window, self.db).present()

    def on_import_csv(self, *_args):
        """Open the CSV statement mapping dialog for the current book."""
        if self.db is None:
            self._report("Open a book before importing into it.")
            return None
        from .dialogs.csv_import_dialog import CsvImportDialog

        dialog = CsvImportDialog(self.props.active_window, self.db)
        dialog.present()
        return dialog

    def on_rules(self, *_args):
        """Open categorization rules and their proposals for the current book."""
        if self.db is None:
            self._report("Open a book before managing categorization rules.")
            return None
        from .dialogs.rules_dialog import RulesDialog

        dialog = RulesDialog(self.props.active_window, self.db)
        dialog.present()
        return dialog

    def on_receivables(
        self,
        *_args,
        expense: tuple[str, str] | None = None,
        parent: Gtk.Window | None = None,
    ):
        """Open reimbursable expenses, optionally to track one expense split.

        ``parent`` is the window the command came from; otherwise the active one.
        """
        if self.db is None:
            self._report("Open a book before tracking reimbursable expenses.")
            return None
        from .dialogs.receivables_dialog import ReceivablesDialog

        dialog = ReceivablesDialog(parent or self.props.active_window, self.db, expense=expense)
        dialog.present()
        return dialog

    def on_find_transactions(self, *_args):
        """Search every register; sort, group, and total what is found (#382)."""
        if self.db is None:
            self._report("Open a book before finding transactions.")
            return None
        from .dialogs.find_dialog import FindDialog

        window = self.props.active_window
        manager = window if isinstance(window, ViewManager) else None
        dialog = FindDialog(
            window, self.db, open_account=manager.open_register if manager is not None else None
        )
        dialog.present()
        return dialog

    def on_savings_goals(self, *_args):
        """Open savings goals: add, fund, close, and review their progress."""
        if self.db is None:
            self._report("Open a book before managing savings goals.")
            return None
        from .dialogs.savings_goals_dialog import SavingsGoalsDialog

        dialog = SavingsGoalsDialog(self.props.active_window, self.db)
        dialog.present()
        return dialog

    def on_fsa_claims(self, *_args, claim: str | None = None):
        """Manage FSA claims from any view; the open claims window is brought forward."""
        if self.db is None:
            self._report("Open a book before managing FSA claims.")
            return None
        from .dialogs.fsa_claims_dialog import FsaClaimsDialog

        return FsaClaimsDialog.present_for(self.props.active_window, self.db, claim)

    def on_gnucash_writeback(self, *_args):
        """Preview, then write chosen changes back to the imported GnuCash book."""
        if self.db is None:
            self._report("Open a book before writing changes to GnuCash.")
            return None
        from .dialogs.gnucash_writeback_dialog import GnuCashWritebackDialog

        dialog = GnuCashWritebackDialog(self.props.active_window, self.db)
        dialog.present()
        return dialog

    def on_undo(self, *_args) -> None:
        if self.db is not None:
            self.db.undo()

    def on_redo(self, *_args) -> None:
        if self.db is not None:
            self.db.redo()

    def on_export(self, *_args) -> None:
        if self.db is None:
            return
        dialog = Gtk.FileDialog(title="Export transactions", initial_name="transactions.csv")

        def on_saved(file_dialog, result) -> None:
            try:
                file = file_dialog.save_finish(result)
            except GLib.Error:
                return
            from ..plugins.export.csv_export import export_transactions

            if self.db is not None:
                export_transactions(self.db, file.get_path())

        dialog.save(self.props.active_window, None, on_saved)

    def on_backup(self, *_args) -> None:
        """Create a consistent snapshot of the current open book."""
        if self.db is None or self.book_path is None:
            return
        dialog = Gtk.FileDialog(
            title="Back up book",
            initial_name=f"{Path(self.book_path).name}.backup",
        )

        def on_saved(file_dialog, result) -> None:
            try:
                file = file_dialog.save_finish(result)
            except GLib.Error:
                return
            try:
                assert self.db is not None
                destination = self.db.backup_to(file.get_path(), overwrite=True)
            except Exception as exc:  # noqa: BLE001 - surfaced to the user
                self._report(f"Could not back up the book: {exc}")
                return
            self._report(f"Backup created at {destination}")

        dialog.save(self.props.active_window, None, on_saved)

    def on_restore(self, *_args) -> None:
        """Restore a verified backup as a book, never over the currently open file."""
        choose = Gtk.FileDialog(title="Choose a BreadSched backup")
        choose.open(self.props.active_window, None, self._on_restore_source_chosen)

    def _on_restore_source_chosen(self, dialog, result) -> None:
        try:
            source = dialog.open_finish(result).get_path()
        except GLib.Error:
            return
        save = Gtk.FileDialog(title="Restore backup as", initial_name="restored.breadsched")
        save.save(
            self.props.active_window,
            None,
            lambda d, r: self._on_restore_target_chosen(d, r, source),
        )

    def _on_restore_target_chosen(self, dialog, result, source: str) -> None:
        try:
            target = dialog.save_finish(result).get_path()
        except GLib.Error:
            return
        if self.book_path is not None and Path(target).resolve() == Path(self.book_path).resolve():
            self._report(
                "Restore the backup as a different file. The currently open book "
                "cannot be replaced underneath its live database connection."
            )
            return
        try:
            restored = DbSQLite.restore_backup(source, target, overwrite=Path(target).exists())
            self.open_book(restored)
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            self._report(f"Could not restore the backup: {exc}")

    def on_verify(self, *_args) -> None:
        """Open the read-only full-book verification workflow."""
        if self.book_path is None:
            return
        from .dialogs.verification_dialog import VerificationDialog

        VerificationDialog(self.props.active_window, self.book_path).present()

    def on_post_scheduled(self, *_args) -> None:
        """Review what is due, as on opening a book, rather than only showing it (#326)."""
        window = self.props.active_window
        if not isinstance(window, ViewManager):
            window = next((w for w in self.get_windows() if isinstance(w, ViewManager)), None)
        if window is None or self.db is None:
            return
        if window.prompt_for_due() is None:
            self._report("No scheduled transactions are due to post.")

    def on_new_transaction(self, *_args):
        """The transaction editor, opened in the shown register's account if any."""
        if self.db is None:
            return None
        # One New Transaction command (#336): in a register it starts in that
        # register's account, as the register's own button does.
        window = self.props.active_window or next(
            (w for w in self.get_windows() if isinstance(w, ViewManager)), None
        )
        shown = getattr(window, "shown_register", None)
        register = shown() if callable(shown) else None
        dialog = register._on_add_clicked() if register is not None else None
        if dialog is not None:
            return dialog
        from .dialogs.transaction_dialog import TransactionDialog

        dialog = TransactionDialog(window, self.db)
        dialog.present()
        return dialog

    def on_quit(self, *_args) -> None:
        """File → Quit: remember each window's size (#349), then quit.

        Quitting ends the application without asking its windows to close, so
        their own close handlers would not record the size.
        """
        from .widgets.window_sizes import remember_size

        for window in self.get_windows():
            if isinstance(window, ViewManager):
                remember_size(window, "main")
        self.quit()

    def on_preferences(self, *_args):
        """Edit → Preferences (#333): the settings that are not day-to-day work."""
        from .dialogs.preferences_dialog import PreferencesDialog

        dialog = PreferencesDialog(self.props.active_window, self)
        dialog.present()
        return dialog

    def preferences_changed(self) -> None:
        """Apply saved preferences: menus (View → Double Line) and book sharing."""
        self.update_sharing()
        double_line = self.view_settings.get_bool("register", "double_line")
        for window in self.get_windows():
            action = getattr(window, "double_line_action", None)
            if action is not None:
                action.set_state(GLib.Variant.new_boolean(double_line))

    def on_about(self, *_args) -> None:
        about = Gtk.AboutDialog(
            transient_for=self.props.active_window,
            modal=True,
            program_name=APP_NAME,
            version=__version__,
            comments="Plan cash flow from scheduled financial events and compare "
            "the plan with actual activity.",
            license_type=Gtk.License.AGPL_3_0,
            logo=logo_texture(),
        )
        about.present()

    def on_user_guide(self, *_args) -> None:
        """Present the packaged guide, reusing its window when it is already open."""
        self.show_guide()

    def show_guide(
        self, topic: str | None = None, parent: Gtk.Window | None = None
    ) -> UserGuideWindow:
        """Present the guide; with ``topic``, at that workflow's desktop section.

        ``parent`` is the window that asked (a dialog's Help button); the guide
        stays above it.
        """
        guide = next(
            (window for window in self.get_windows() if isinstance(window, UserGuideWindow)),
            None,
        )
        parent = parent or self.props.active_window
        if guide is None:
            guide = UserGuideWindow(self, parent)
        elif parent is not None and parent is not guide:
            guide.set_transient_for(parent)
        if topic is not None:
            guide.show_part(*help_target(topic, "desktop"))
        guide.present()
        return guide

    def _report(self, message: str) -> None:
        """Tell the user ``message`` in a modal alert over the main window.

        The alert waits until its parent is on screen: shown earlier (a notice
        from opening the remembered book at start-up), the parent could appear
        on top of it and then refuse input while the hidden alert waits (#295).
        """
        window = self.props.active_window
        if window is None:
            print(message, file=sys.stderr)
            return
        if isinstance(window, ViewManager):
            # Over the main window, alerts take their turn with its other modal
            # dialogs, so an alert and a review never wait on each other.
            def show(done) -> bool:
                self._show_alert(window, message, done)
                return True

            window.queue_modal(show)
            return
        when_presented(window, lambda: self._show_alert(window, message, lambda: None))

    @staticmethod
    def _show_alert(window: Gtk.Window, message: str, done) -> None:
        """Show ``message`` modally over ``window``; ``done`` runs when it is dismissed."""
        alert = Gtk.AlertDialog(message=message, modal=True, buttons=["OK"])

        def dismissed(dialog, result) -> None:
            try:
                dialog.choose_finish(result)
            except GLib.Error:
                pass  # Escape or closing the alert dismisses it too.
            done()

        alert.choose(window, None, dismissed)


class ActionsMenu:
    """The Actions menu, arranged around the current view (#182).

    The current view's commands come first, under that view's name. Commands that
    work anywhere follow, and every other view's commands sit under **Other
    Views**, so each command still has exactly one menu item. The menu model is
    rebuilt in place, so the menu bar that shows it never changes.
    """

    def __init__(self) -> None:
        self.menu = Gio.Menu()
        self.current: str | None = None
        self._built = False
        self.show_view(None)

    def show_view(self, key: str | None) -> None:
        # Rebuild only for another view. Opening a submenu moves focus to its
        # popover, which toggles the window's is-active and asks again for the
        # same view; rebuilding then destroyed the open submenu under the
        # pointer, and GTK crashed (#229).
        if self._built and key == self.current:
            return
        self._built = True
        self.current = key
        labels = {item_key: label for item_key, label, _icon in MENU_CATEGORIES}
        self.menu.remove_all()

        def commands(view: str) -> list:
            # Toggles are on the View menu and reports on the Reports menu.
            return [item for item in VIEW_ACTIONS.get(view, ()) if not (item.toggle or item.report)]

        if key is not None and commands(key):
            here = Gio.Menu()
            for item in commands(key):
                here.append(item.label, f"win.{view_action_name(key, item)}")
            self.menu.append_section(labels.get(key, key), here)
        general = Gio.Menu()
        general.append("New _Transaction…", "app.new-transaction")
        general.append("Review _Due Transactions…", "app.post-scheduled")
        general.append("Categorization _Rules…", "app.rules")
        general.append("Rei_mbursable Expenses…", "app.receivables")
        general.append("Savings _Goals…", "app.savings-goals")
        # Claims are not tied to the FSA Dashboard: they are managed beside any
        # register (#366). That view lists the command first already.
        if key != "fsa-dashboard":
            general.append("Manage _FSA Claims…", "app.fsa-claims")
        self.menu.append_section(None, general)
        others = Gio.Menu()
        for view in VIEW_ACTIONS:
            if view == key or not commands(view):
                continue
            submenu = Gio.Menu()
            for item in commands(view):
                submenu.append(item.label, f"win.{view_action_name(view, item)}")
            others.append_submenu(labels[view], submenu)
        section = Gio.Menu()
        section.append_submenu("Other _Views", others)
        self.menu.append_section(None, section)


def build_menu_model(actions_menu: ActionsMenu | None = None) -> Gio.Menu:
    """The application menu bar: File, Edit, View, Actions, Reports, Help.

    A conventional menu bar rather than a hamburger, because this is an accounting
    application whose users are coming from GnuCash and Gramps, and discoverability
    of a rarely-used command matters more here than chrome does.
    """
    menubar = Gio.Menu()

    file_menu = Gio.Menu()
    new_section = Gio.Menu()
    new_section.append("_New Book…", "app.new")
    new_section.append("_Open Book…", "app.open")
    file_menu.append_section(None, new_section)
    transfer = Gio.Menu()
    transfer.append("Import GnuCash Book into _New Book…", "app.import-new")
    transfer.append("Import GnuCash Book into _Current Book…", "app.import")
    transfer.append("Import CSV _Statement…", "app.import-csv")
    transfer.append("_Write Changes to GnuCash…", "app.gnucash-writeback")
    transfer.append("_Export Transactions…", "app.export")
    file_menu.append_section(None, transfer)
    safety = Gio.Menu()
    safety.append("_Back Up Current Book…", "app.backup")
    safety.append("_Restore Backup as New Book…", "app.restore")
    safety.append("_Verify Current Book…", "app.verify")
    file_menu.append_section(None, safety)
    quit_section = Gio.Menu()
    quit_section.append("_Print Current View…", "win.print-view")
    if sys.platform == "darwin":
        # GTK's macOS print dialog is not yet supported; until native macOS
        # printing is, the browser's print dialog stands in for it there.
        quit_section.append("Print in _Browser…", "win.print-browser")
    quit_section.append("_Quit", "app.quit")
    file_menu.append_section(None, quit_section)
    menubar.append_submenu("_File", file_menu)

    edit_menu = Gio.Menu()
    edit_menu.append("_Undo", "app.undo")
    edit_menu.append("_Redo", "app.redo")
    finding = Gio.Menu()
    finding.append("_Find in Register", "win.find")
    finding.append("Find _Transactions…", "app.find-transactions")
    edit_menu.append_section(None, finding)
    preferences = Gio.Menu()
    preferences.append("_Preferences…", "app.preferences")
    edit_menu.append_section(None, preferences)
    menubar.append_submenu("_Edit", edit_menu)

    view_menu = Gio.Menu()
    views = Gio.Menu()
    for key, label, _icon in MENU_CATEGORIES:
        views.append(label, f"win.show-category::{key}")
    view_menu.append_section(None, views)
    toggles = Gio.Menu()
    toggles.append("_Double Line", "win.double-line")
    for key, actions in VIEW_ACTIONS.items():
        for item in actions:
            if item.toggle:
                toggles.append(item.label, f"win.{view_action_name(key, item)}")
    view_menu.append_section(None, toggles)
    tabs = Gio.Menu()
    tabs.append("_Previous Tab", "win.previous-tab")
    tabs.append("Ne_xt Tab", "win.next-tab")
    tabs.append("Move Tab _Left", "win.move-tab-left")
    tabs.append("Move Tab _Right", "win.move-tab-right")
    tabs.append("_Close Tab", "win.close-tab")
    view_menu.append_section(None, tabs)
    menubar.append_submenu("_View", view_menu)

    # Every view-level command also has a menu item (#156), the current view's
    # first (#182).
    menubar.append_submenu("_Actions", (actions_menu or ActionsMenu()).menu)

    # Every report in one place (#336), whichever view it is computed from.
    reports_menu = Gio.Menu()
    for key, actions in VIEW_ACTIONS.items():
        for item in actions:
            if item.report:
                reports_menu.append(item.label, f"win.{view_action_name(key, item)}")
    menubar.append_submenu("_Reports", reports_menu)

    help_menu = Gio.Menu()
    help_menu.append("_User Guide", "app.user-guide")
    help_menu.append("_About BreadSched", "app.about")
    menubar.append_submenu("_Help", help_menu)
    return menubar


def main(argv: list[str] | None = None) -> int:
    """Run the application directly.

    Prefer :mod:`breadsched.gui.launcher`, which validates arguments and reports a
    missing GTK stack without a traceback. This entry point assumes GTK is present,
    because by the time this module imported successfully, it was.
    """
    return BreadSchedApplication().run(list(sys.argv if argv is None else argv))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
