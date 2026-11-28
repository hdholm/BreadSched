"""Edit → Preferences: the settings that are not part of day-to-day work (#333).

Everything here was already a setting, kept in the user's settings files rather
than in a book; this window gathers them so they are neither hidden nor scattered
over toolbars:

* **Book** – the book opened at start (``general.last_book_path``), which can be
  changed or forgotten.
* **Dates** – how every date is shown (``general.date_format``): as the computer's
  locale writes dates (the default), or ISO, month first, or day first; it is also
  the order a typed ``3/4`` is read in.
* **Registers** – Double Line (also **View → Double Line**) and row shading
  (``register.double_line``, ``register.banding`` in the view settings), applied
  to every open register when saved.
* **Online quotes** – the Alpha Vantage API key, the same stored key the Online
  Quotes window edits.
* **Windows** – forget the remembered window sizes (#349).
* **Sharing** – let other BreadSched commands use the open book through this window
  (``general.share_book``, #359).
* **Users** – **Users and devices…**: your password, the other people who may use
  this computer's books, and your devices. Used alone,
  nothing here needs changing.

Those are the **General** tab. The **Dashboard** tab holds every Dashboard
setting of the open book (:class:`~.dashboard_settings.DashboardSettings`): its
horizons, the items shown, the account groups, and each table's columns.

**Save** writes them all; **Cancel** (or Escape) changes nothing. Either asks
first when an account was chosen in a Dashboard group's picker but not added.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ...api.local import book_server
from ...api.users import UserDirectory
from ...gen.utils import date_format
from ...plugins.quotes import alphavantage_key, save_alphavantage_key
from ..book_sharing import SETTING as SHARE_SETTING
from ..gi_setup import Gio, GLib, Gtk
from ..views.register import BANDING_CHOICES, apply_register_display
from ..widgets.bounded import BoundedWindow, scroll_body
from ..widgets.choice import bounded_dropdown
from ..widgets.date_entry import SETTING as DATE_SETTING
from ..widgets.window_sizes import forget_sizes
from .dashboard_settings import DashboardSettings
from .users_dialog import UsersDialog

__all__ = ["PreferencesDialog"]

_REGISTER = "register"


class PreferencesDialog(BoundedWindow):
    """The user's settings, in one window."""

    def __init__(self, parent: Gtk.Window | None, application) -> None:
        super().__init__(title="Preferences", transient_for=parent)
        self.set_destroy_with_parent(True)
        self.application = application
        self.settings = application.settings
        self.view_settings = application.view_settings
        self.set_default_size(560, -1)

        box = self.frame("preferences")

        self.notebook = Gtk.Notebook()
        self.notebook.set_vexpand(True)
        box.append(self.notebook)
        grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        for side in ("top", "bottom", "start", "end"):
            getattr(grid, f"set_margin_{side}")(12)
        self.notebook.append_page(grid, Gtk.Label(label="General"))
        self.notebook.append_page(self._dashboard_page(), Gtk.Label(label="Dashboard"))
        row = 0

        def heading(text: str) -> None:
            nonlocal row
            label = Gtk.Label(label=text, xalign=0)
            label.add_css_class("summary-label")
            grid.attach(label, 0, row, 2, 1)
            row += 1

        heading("Book")
        self.book_path = self.settings.get("general", "last_book_path") or ""
        self.book_label = Gtk.Label(xalign=0, wrap=True, hexpand=True)
        self.book_label.set_selectable(True)
        grid.attach(Gtk.Label(label="Open at start", xalign=0), 0, row, 1, 1)
        grid.attach(self.book_label, 1, row, 1, 1)
        row += 1
        book_buttons = Gtk.Box(spacing=8)
        choose = Gtk.Button(label="Choose…")
        choose.set_tooltip_text("Choose the book BreadSched opens when it starts")
        choose.connect("clicked", self._on_choose_book)
        book_buttons.append(choose)
        forget = Gtk.Button(label="Forget")
        forget.set_tooltip_text("Start without opening a book")
        forget.connect("clicked", lambda *_: self.set_book_path(""))
        book_buttons.append(forget)
        grid.attach(book_buttons, 1, row, 1, 1)
        row += 1
        self.set_book_path(self.book_path)

        heading("Registers")
        self.double_line = Gtk.CheckButton(
            label="Show a notes line under each transaction (Double Line)"
        )
        self.double_line.set_active(self.view_settings.get_bool(_REGISTER, "double_line"))
        grid.attach(self.double_line, 0, row, 2, 1)
        row += 1
        self.banding = bounded_dropdown([label for _key, label in BANDING_CHOICES])
        self.banding.update_property([Gtk.AccessibleProperty.LABEL], ["Row shading"])
        keys = [key for key, _label in BANDING_CHOICES]
        chosen = self.view_settings.get(_REGISTER, "banding") or "transactions"
        self.banding.set_selected(keys.index(chosen) if chosen in keys else 0)
        grid.attach(Gtk.Label(label="Row shading", xalign=0), 0, row, 1, 1)
        grid.attach(self.banding, 1, row, 1, 1)
        row += 1

        heading("Dates")
        self.date_styles = list(date_format.STYLES)
        self.date_format = bounded_dropdown(
            [date_format.style_label(style) for style in self.date_styles]
        )
        self.date_format.update_property([Gtk.AccessibleProperty.LABEL], ["Show dates as"])
        self.date_format.set_tooltip_text(
            "How every date is shown, and the order a typed 3/4 is read in "
            "(YYYY-MM-DD is always accepted)"
        )
        stored = self.settings.get(*DATE_SETTING) or date_format.DEFAULT_STYLE
        self.date_format.set_selected(
            self.date_styles.index(stored) if stored in self.date_styles else 0
        )
        grid.attach(Gtk.Label(label="Show dates as", xalign=0), 0, row, 1, 1)
        grid.attach(self.date_format, 1, row, 1, 1)
        row += 1

        heading("Online quotes")
        self.api_key = Gtk.PasswordEntry(show_peek_icon=True, hexpand=True)
        self.api_key.set_text(alphavantage_key(self.settings))
        self.api_key.update_property([Gtk.AccessibleProperty.LABEL], ["Alpha Vantage API key"])
        grid.attach(Gtk.Label(label="Alpha Vantage API key", xalign=0), 0, row, 1, 1)
        grid.attach(self.api_key, 1, row, 1, 1)
        row += 1

        heading("Windows")
        self.forget_sizes = Gtk.CheckButton(
            label="Forget remembered window sizes (windows open at their own sizes again)"
        )
        self.forget_sizes.set_tooltip_text(
            "BreadSched remembers the main window's size, and each window you resize"
        )
        grid.attach(self.forget_sizes, 0, row, 2, 1)
        row += 1

        heading("Sharing")
        self.share_book = Gtk.CheckButton(label="Let other BreadSched commands use the open book")
        self.share_book.set_tooltip_text(
            "Commands such as breadsched due-review work through this window "
            "instead of finding the book already open"
        )
        self.share_book.set_active(self.settings.get_bool(*SHARE_SETTING))
        grid.attach(self.share_book, 0, row, 2, 1)
        row += 1

        heading("Users")
        self.users_summary = Gtk.Label(xalign=0, wrap=True, hexpand=True)
        grid.attach(self.users_summary, 0, row, 1, 1)
        users = Gtk.Button(label="Users and devices…")
        users.set_tooltip_text(
            "Your password, the people who may use this computer's books, and your devices"
        )
        users.connect("clicked", self._on_users)
        grid.attach(users, 1, row, 1, 1)
        row += 1
        self._show_users_summary()

        self.status = Gtk.Label(xalign=0, wrap=True)
        self.status.add_css_class("dim")
        box.append(self.status)

        save = Gtk.Button(label="Save")
        save.connect("clicked", lambda *_: self.save_and_close())
        self.actions(box, dismiss="Cancel", primary=save)
        # With the Dashboard tab the window is tall: the tabs scroll on a small
        # screen while the status line and buttons stay in view.
        scroll_body(self, keep=2)
        #: Set once the user agreed to close with an account chosen but not added.
        self._closing = False
        #: The question asked about such accounts, as (message, buttons), for tests.
        self.unadded_question: tuple[str, list[str]] | None = None
        self.connect("close-request", self._on_close_request)

    # --------------------------------------------------------------- the book

    def set_book_path(self, path: str) -> None:
        self.book_path = path
        self.book_label.set_text(path or "None: BreadSched starts without a book")

    def _on_choose_book(self, _button) -> None:
        dialog = Gtk.FileDialog(title="Book to open at start")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        books = Gtk.FileFilter()
        books.set_name("BreadSched books")
        books.add_pattern("*.breadsched")
        filters.append(books)
        dialog.set_filters(filters)

        def chosen(dialog, result) -> None:
            try:
                file = dialog.open_finish(result)
            except GLib.Error:
                return
            self.set_book_path(file.get_path() or "")

        dialog.open(self, None, chosen)

    # -------------------------------------------------------------- dashboard

    def _dashboard_view(self):
        """The open book's Dashboard view (the view a book opens on), or None."""
        views = getattr(self.get_transient_for(), "_views", None)
        return views.get("dashboard") if views is not None else None

    def _dashboard_page(self) -> Gtk.Widget:
        """The open book's Dashboard settings, or why there are none."""
        db = getattr(self.application, "db", None)
        #: The Dashboard tab's settings; None without an open book.
        self.dashboard: DashboardSettings | None = None
        if db is None:
            note = Gtk.Label(
                label="Open a book to choose what its Dashboard shows.", xalign=0, wrap=True
            )
            for side in ("top", "bottom", "start", "end"):
                getattr(note, f"set_margin_{side}")(12)
            return note
        view = self._dashboard_view()
        tables = view.column_tables() if view is not None else ()
        self.dashboard = DashboardSettings(db, tables=tables, settings=self.view_settings)
        for side in ("top", "bottom", "start", "end"):
            getattr(self.dashboard, f"set_margin_{side}")(12)
        return self.dashboard

    # ------------------------------------------------- accounts not yet added

    def _unadded_text(self) -> str:
        assert self.dashboard is not None
        return "\n".join(f"{item.account} in {item.group}" for item in self.dashboard.unadded())

    def save_and_close(self) -> None:
        """Save, then close; first ask about an account chosen but not added."""
        if self.dashboard is not None and self.dashboard.unadded():
            self.ask_unadded(saving=True)
            return
        if self.save():
            self._closing = True
            self.close()

    def _on_close_request(self, *_args) -> bool:
        """Cancel, Escape, or the title bar: ask first about an unadded account."""
        if self._closing or self.dashboard is None or not self.dashboard.unadded():
            return False
        self.ask_unadded(saving=False)
        return True

    def ask_unadded(self, *, saving: bool) -> None:
        """An account is chosen in a group's picker but **Add** was not chosen."""
        if saving:
            message = "Add the chosen account before saving?"
            buttons = ["Go back", "Save without it", "Add and save"]
        else:
            message = "Close without adding the chosen account?"
            buttons = ["Go back", "Close without saving"]
        detail = (
            "An account is chosen in a Dashboard group but was not added with its "
            "Add button:\n" + self._unadded_text()
        )
        self.unadded_question = (message, buttons)
        alert = Gtk.AlertDialog(
            message=message,
            detail=detail,
            buttons=buttons,
            cancel_button=0,
            default_button=len(buttons) - 1,
        )

        def finished(dialog, result) -> None:
            try:
                choice = dialog.choose_finish(result)
            except GLib.Error:
                choice = 0
            # Already answered (or the window closed meanwhile): nothing to do.
            if self.unadded_question is not None:
                self.settle_unadded(choice, saving=saving)

        alert.choose(self, None, finished)

    def settle_unadded(self, choice: int, *, saving: bool) -> None:
        """Act on the answer to :meth:`ask_unadded` (its button's index)."""
        self.unadded_question = None
        if choice == 0:
            # Go back: show the group with the account still chosen.
            self.notebook.set_current_page(1)
            return
        if not saving:
            self._closing = True
            self.close()
            return
        assert self.dashboard is not None
        if choice == 2:
            self.dashboard.add_unadded()
        if self.save():
            self._closing = True
            self.close()

    # ------------------------------------------------------------------ users

    def _show_users_summary(self) -> None:
        try:
            others = [user for user in UserDirectory().users() if not user.host_account]
        except (OSError, sqlite3.Error):
            self.users_summary.set_text("The list of users could not be read.")
            return
        self.users_summary.set_text(
            "Only you"
            if not others
            else f"You and {len(others)} other {'person' if len(others) == 1 else 'people'}"
        )

    def _on_users(self, _button) -> UsersDialog:
        # With a book open, the window also says who may use it.
        db = getattr(self.application, "db", None)
        book_id = book_server(db).book_id() if db is not None else None
        path = getattr(self.application, "book_path", "") or ""
        dialog = UsersDialog(self, book_id=book_id, book_name=Path(path).name if path else "")

        def closed(*_args) -> bool:
            self._show_users_summary()
            return False

        dialog.connect("close-request", closed)
        dialog.present()
        return dialog

    # ----------------------------------------------------------------- saving

    def save(self) -> bool:
        """Write every setting; False, with the reason shown, if one could not be saved.

        The Dashboard tab is saved first: when the book refuses it, nothing else is
        written and the tab is shown with the reason.
        """
        if self.dashboard is not None:
            if not self.dashboard.save():
                self.notebook.set_current_page(1)
                return False
            view = self._dashboard_view()
            if view is not None:
                view.invalidate()
        self.settings.set(*SHARE_SETTING, self.share_book.get_active())
        self.settings.set(*DATE_SETTING, self.date_styles[self.date_format.get_selected()])
        if self.book_path:
            self.settings.set("general", "last_book_path", self.book_path)
        else:
            self.settings.remove("general", "last_book_path")
        # As in Online Quotes, a key read from the environment is stored only if
        # it was changed here.
        if alphavantage_key(self.settings) != self.api_key.get_text().strip():
            saved = save_alphavantage_key(self.settings, self.api_key.get_text())
        else:
            saved = self.settings.save()
        if not saved:
            self.status.set_text("Could not save the settings file.")
            return False
        self.view_settings.set(_REGISTER, "double_line", self.double_line.get_active())
        self.view_settings.set(
            _REGISTER, "banding", BANDING_CHOICES[self.banding.get_selected()][0]
        )
        if self.forget_sizes.get_active():
            forget_sizes(self.view_settings)
        if not self.view_settings.save():
            self.status.set_text("Could not save the view settings file.")
            return False
        apply_register_display()
        changed = getattr(self.application, "preferences_changed", None)
        if callable(changed):
            changed()
        return True
