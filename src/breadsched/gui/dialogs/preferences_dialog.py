"""Edit → Preferences: the settings that are not part of day-to-day work (#333).

Everything here was already a setting, kept in the user's settings files rather
than in a book; this window gathers them so they are neither hidden nor scattered
over toolbars:

* **Book** – the book opened at start (``general.last_book_path``), which can be
  changed or forgotten.
* **Registers** – Double Line (also **View → Double Line**) and row shading
  (``register.double_line``, ``register.banding`` in the view settings), applied
  to every open register when saved.
* **Online quotes** – the Alpha Vantage API key, the same stored key the Online
  Quotes window edits.
* **Dashboard** – **Configure Dashboard Groups…**, which opens that window for the
  open book (groups belong to the book, not to these settings).
* **Windows** – forget the remembered window sizes (#349).

**Save** writes them all; **Cancel** (or Escape) changes nothing.
"""

from __future__ import annotations

from ...plugins.quotes import alphavantage_key, save_alphavantage_key
from ..gi_setup import Gio, GLib, Gtk
from ..views.register import BANDING_CHOICES, apply_register_display
from ..widgets.bounded import BoundedWindow
from ..widgets.choice import bounded_dropdown
from ..widgets.help import help_row
from ..widgets.window_sizes import forget_sizes

__all__ = ["PreferencesDialog"]

_REGISTER = "register"


class PreferencesDialog(BoundedWindow):
    """The user's settings, in one window."""

    def __init__(self, parent: Gtk.Window | None, application) -> None:
        super().__init__(title="Preferences", transient_for=parent, modal=True)
        self.set_destroy_with_parent(True)
        self.application = application
        self.settings = application.settings
        self.view_settings = application.view_settings
        self.set_default_size(560, -1)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(18)
        self.set_child(box)
        box.append(help_row("preferences"))

        grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        box.append(grid)
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

        heading("Online quotes")
        self.api_key = Gtk.PasswordEntry(show_peek_icon=True, hexpand=True)
        self.api_key.set_text(alphavantage_key(self.settings))
        self.api_key.update_property([Gtk.AccessibleProperty.LABEL], ["Alpha Vantage API key"])
        grid.attach(Gtk.Label(label="Alpha Vantage API key", xalign=0), 0, row, 1, 1)
        grid.attach(self.api_key, 1, row, 1, 1)
        row += 1

        heading("Dashboard")
        groups = Gtk.Button(label="Configure Dashboard Groups…", halign=Gtk.Align.START)
        groups.set_tooltip_text("Arrange the open book's Dashboard groups")
        groups.set_sensitive(getattr(application, "db", None) is not None)
        groups.connect("clicked", self._on_dashboard_groups)
        grid.attach(groups, 0, row, 2, 1)
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

        self.status = Gtk.Label(xalign=0, wrap=True)
        self.status.add_css_class("dim")
        box.append(self.status)

        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        buttons.append(cancel)
        save = Gtk.Button(label="Save")
        save.add_css_class("suggested-action")
        save.connect("clicked", lambda *_: self.save() and self.close())
        buttons.append(save)
        box.append(buttons)

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

    def _on_dashboard_groups(self, _button) -> None:
        parent = self.get_transient_for()
        if parent is not None:
            parent.activate_action("win.dashboard-configure-groups", None)

    # ----------------------------------------------------------------- saving

    def save(self) -> bool:
        """Write every setting; False, with the reason shown, if one could not be saved."""
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
