"""GTK4 application shell.

The GUI is a strict consumer of :mod:`cashperspective.gen`: it holds no financial logic of
its own, and every number it shows comes from an engine function.  That boundary is
what lets the whole model be tested without a display server, and it is why this
module is the only place ``gi`` is imported at start-up.
"""

from __future__ import annotations

import sys
from importlib import resources
from pathlib import Path

from .. import APP_ID, APP_NAME, __version__  # noqa: E402
from ..gen.db.sqlite import DbSQLite  # noqa: E402
from ..gen.utils.logs import get_logger  # noqa: E402
from ..gen.utils.settings import Settings  # noqa: E402
from .gi_setup import Gdk, Gio, GLib, Gtk
from .viewmanager import CATEGORIES as MENU_CATEGORIES  # noqa: E402
from .viewmanager import ViewManager  # noqa: E402

__all__ = ["CashPerspectiveApplication", "main"]

LOG = get_logger(__name__)

STYLE_RESOURCE = resources.files("cashperspective.gui").joinpath("resources/style.css")


class CashPerspectiveApplication(Gtk.Application):
    """Owns the open book and the windows looking at it."""

    def __init__(self, application_id: str = APP_ID, unique: bool = True) -> None:
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
        #: Deliberate preferences, and remembered interface state. Kept apart so a
        #: user can delete the second without losing the first.
        self.settings = Settings("settings")
        self.view_settings = Settings("views")
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
        self._install_actions()
        self.set_menubar(build_menu_model())

    def do_activate(self) -> None:
        window = self.props.active_window or ViewManager(self)
        # Restore the previous book before presenting the window.  If we present
        # first, the empty-book chooser is briefly visible even when there is a
        # perfectly good remembered book to reopen.
        if self.db is None:
            self.reopen_last_book()
        window.present()

    def reopen_last_book(self) -> bool:
        """Reopen the book from last time, if it is still there.

        A book that has been moved or deleted is forgotten rather than reported:
        the user did not ask for it this time, and an error dialog on start-up
        about a file they deliberately removed is noise.
        """
        remembered = self.settings.get("general", "last_book")
        if not remembered:
            return False

        if not Path(remembered).exists():
            self.settings.remove("general", "last_book")
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
            self.open_book(files[0].get_path())

    def _install_actions(self) -> None:
        for name, handler, accel in (
            ("open", self.on_open, "<Control>o"),
            ("new", self.on_new, "<Control>n"),
            ("import", self.on_import, "<Control>i"),
            ("import-new", self.on_import_new, None),
            ("open-default", self.on_open_default, None),
            ("undo", self.on_undo, "<Control>z"),
            ("redo", self.on_redo, "<Control><Shift>z"),
            ("export", self.on_export, None),
            ("post-scheduled", self.on_post_scheduled, None),
            ("new-transaction", self.on_new_transaction, "<Control>t"),
            ("new-budget", self.on_new_budget, "<Control>b"),
            ("about", self.on_about, None),
            ("quit", lambda *_: self.quit(), "<Control>q"),
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
        for name in ("undo", "redo", "import", "export", "post-scheduled",
                     "new-transaction", "new-budget"):
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
            self.db.close()
        self.db = DbSQLite()
        self.db.load(path)
        self.book_path = path
        # Remembered so the next start reopens it. Written immediately rather than
        # at shutdown: a crash should not cost the setting.
        self.settings.set("general", "last_book", str(Path(path).resolve()))
        # ``last_book_explicit`` existed briefly while startup behavior was being
        # migrated.  Book identity is sufficient: a user book at the default path
        # is no different from any other user-owned book.
        self.settings.remove("general", "last_book_explicit")
        self.settings.save()
        for name in ("import", "export", "post-scheduled", "new-transaction",
                     "new-budget"):
            self.set_action_enabled(name, True)
        for window in self.get_windows():
            if isinstance(window, ViewManager):
                window.book_opened(self.db, path)

    def require_db(self) -> DbSQLite | None:
        return self.db

    def set_action_enabled(self, name: str, enabled: bool) -> None:
        action = self.actions.get(name)
        if action is not None:
            action.set_enabled(enabled)

    # ---------------------------------------------------------------- actions

    def on_new(self, *_args) -> None:
        dialog = Gtk.FileDialog(title="New book", initial_name="household.cashperspective")
        dialog.save(self.props.active_window, None, self._on_new_chosen)

    def _on_new_chosen(self, dialog, result) -> None:
        try:
            file = dialog.save_finish(result)
        except GLib.Error:
            return
        from argparse import Namespace

        from ..cli.main import cmd_init

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
        book_filter.set_name("CashPerspective books")
        book_filter.add_pattern("*.cashperspective")
        filters.append(book_filter)
        dialog.set_filters(filters)
        dialog.open(self.props.active_window, None, self._on_open_chosen)

    def _on_open_chosen(self, dialog, result) -> None:
        try:
            file = dialog.open_finish(result)
        except GLib.Error:
            return
        try:
            self.open_book(file.get_path())
        except Exception as exc:  # noqa: BLE001
            self._report(f"Could not open the book: {exc}")

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

        from ..cli.main import cmd_init

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

        from ..cli.main import cmd_init

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

        window = ImportDialog(self.props.active_window, self.db)
        window.set_source(source_path)
        window.present()

    def on_import(self, *_args) -> None:
        if self.db is None:
            self._report("Open a book before importing into it.")
            return
        from .dialogs.import_dialog import ImportDialog

        ImportDialog(self.props.active_window, self.db).present()

    def on_undo(self, *_args) -> None:
        if self.db is not None:
            self.db.undo()

    def on_redo(self, *_args) -> None:
        if self.db is not None:
            self.db.redo()

    def on_export(self, *_args) -> None:
        if self.db is None:
            return
        dialog = Gtk.FileDialog(title="Export transactions",
                                initial_name="transactions.csv")

        def on_saved(file_dialog, result) -> None:
            try:
                file = file_dialog.save_finish(result)
            except GLib.Error:
                return
            from ..plugins.export.csv_export import export_transactions

            export_transactions(self.db, file.get_path())

        dialog.save(self.props.active_window, None, on_saved)

    def on_post_scheduled(self, *_args) -> None:
        window = self.props.active_window
        if window is not None:
            window.show_category("scheduled")

    def on_new_transaction(self, *_args) -> None:
        if self.db is None:
            return
        from .dialogs.transaction_dialog import TransactionDialog

        TransactionDialog(self.props.active_window, self.db).present()

    def on_new_budget(self, *_args) -> None:
        if self.db is None:
            return
        from .dialogs.budget_dialog import NewBudgetDialog

        NewBudgetDialog(self.props.active_window, self.db).present()

    def on_about(self, *_args) -> None:
        about = Gtk.AboutDialog(
            transient_for=self.props.active_window,
            modal=True,
            program_name=APP_NAME,
            version=__version__,
            comments="Track income and expenses against a cash-flow budget, "
                     "and project them forward.",
            license_type=Gtk.License.AGPL_3_0,
        )
        about.present()

    def _report(self, message: str) -> None:
        window = self.props.active_window
        if window is None:
            print(message, file=sys.stderr)
            return
        alert = Gtk.AlertDialog(message=message)
        alert.show(window)


def build_menu_model() -> Gio.Menu:
    """The application menu bar: File, Edit, View, Actions, Help.

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
    transfer.append("_Export Transactions…", "app.export")
    file_menu.append_section(None, transfer)
    quit_section = Gio.Menu()
    quit_section.append("_Quit", "app.quit")
    file_menu.append_section(None, quit_section)
    menubar.append_submenu("_File", file_menu)

    edit_menu = Gio.Menu()
    edit_menu.append("_Undo", "app.undo")
    edit_menu.append("_Redo", "app.redo")
    menubar.append_submenu("_Edit", edit_menu)

    view_menu = Gio.Menu()
    for key, label, _icon in MENU_CATEGORIES:
        view_menu.append(label, f"win.show-category::{key}")
    menubar.append_submenu("_View", view_menu)

    actions_menu = Gio.Menu()
    actions_menu.append("New _Transaction…", "app.new-transaction")
    actions_menu.append("New _Budget…", "app.new-budget")
    actions_menu.append("_Post Scheduled Transactions", "app.post-scheduled")
    menubar.append_submenu("_Actions", actions_menu)

    help_menu = Gio.Menu()
    help_menu.append("_About CashPerspective", "app.about")
    menubar.append_submenu("_Help", help_menu)
    return menubar


def main(argv: list[str] | None = None) -> int:
    """Run the application directly.

    Prefer :mod:`cashperspective.gui.launcher`, which validates arguments and reports a
    missing GTK stack without a traceback. This entry point assumes GTK is present,
    because by the time this module imported successfully, it was.
    """
    return CashPerspectiveApplication().run(list(sys.argv if argv is None else argv))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
