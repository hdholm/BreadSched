"""The parts every transaction and schedule editor is built from.

A transaction and a schedule are entered the same way: their splits on the
register's own grid (:class:`SplitsPanel`, over a ``split_sheet.SplitSheet``) and
their tags and linked documents on a page of their own (:class:`DocumentsPage`).
The transaction editor (``widgets/transaction_editor``) and the schedule editor
(``dialogs/schedule_dialog``) each hold one of each, so neither has a copy.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from ...api import ApiError
from ...gen.engine import attachments as attachment_engine
from ..gi_setup import Gio, GLib, Gtk
from ..split_sheet import SplitSheet
from .bounded import keeps_enter
from .form_pages import FormGrid
from .register_grid import RegisterGrid

__all__ = ["DocumentsPage", "SplitsPanel"]

#: Split lines the grid shows at its smallest; it grows with the window.
GRID_LINES = 4


class SplitsPanel(Gtk.Box):
    """A heading with **Planning detail**, and the split grid beneath it."""

    def __init__(self, sheet: SplitSheet, on_change: Callable[[], None]) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.sheet = sheet
        caption = Gtk.Box(spacing=8)
        label = Gtk.Label(label="Splits", xalign=0)
        label.add_css_class("total-row")
        label.set_hexpand(True)
        caption.append(label)
        self.detail_toggle = Gtk.ToggleButton(label="Planning detail")
        self.detail_toggle.set_tooltip_text(
            "Show each split's planning purpose, and investment activity for "
            "investment and retirement accounts"
        )
        self.detail_toggle.connect("toggled", self._on_detail_toggled)
        caption.append(self.detail_toggle)
        self.append(caption)
        self.grid = RegisterGrid(on_change=on_change)
        self.grid.set_banding("lines")
        self.grid.set_size_request(-1, 26 * (GRID_LINES + 1))
        self.grid.set_vexpand(True)
        if any(line.purpose or line.activity for line in sheet.lines):
            # Detail already set is never hidden.
            self.detail_toggle.set_active(True)
        self.grid.set_sheet(sheet)
        self.append(self.grid)

    def _on_detail_toggled(self, button: Gtk.ToggleButton) -> None:
        self.sheet.set_detail(button.get_active())
        self.grid.sync(focus=False)

    #: A split line's cells by the names :meth:`type_line` takes.
    CELLS = {
        "memo": "description",
        "account": "transfer",
        "increase": "increase",
        "decrease": "decrease",
        "shares": "shares",
        "price": "price",
        "purpose": "purpose",
        "activity": "activity",
    }

    def type_line(self, index: int, **cells: str) -> None:
        """Type into split line ``index`` as the keyboard would: each named cell
        (``memo``, ``account``, ``increase``, ``decrease``, ``shares``, ``price``,
        ``purpose``, ``activity``) in turn, each read as it is left."""
        sheet = self.sheet
        sheet.move_to(index)
        for name, text in cells.items():
            sheet.column = self.CELLS[name]
            sheet.set_text(text)
            sheet.settle()
        self.grid.sync(focus=False)


class DocumentsPage:
    """Tags and linked documents on an editor's page, for a transaction or a schedule.

    Tags are read with the holder when the editor saves (:meth:`tags`). Documents
    change at once through the book server (each change its own undoable step) and
    only on a holder already saved; a missing file is marked, never removed. A
    schedule's documents are linked to every transaction it posts.
    """

    def __init__(
        self,
        page: FormGrid,
        book,
        kind: str,
        holder: Any,
        *,
        folder: Path | None,
        on_count: Callable[[], None],
        on_error: Callable[[str], object],
        on_changed: Callable[[Any], None],
    ) -> None:
        self.book = book
        self.kind = kind
        #: The stored transaction or schedule, or None before it is first saved.
        self.holder = holder
        self.folder = folder
        self.on_count = on_count
        self.on_error = on_error
        self.on_changed = on_changed
        self.tags_entry = Gtk.Entry(placeholder_text="Comma-separated, e.g. Tax, Home repair")
        if holder is not None:
            self.tags_entry.set_text(", ".join(holder.tags))
        self.tags_entry.set_tooltip_text(
            "BreadSched's own labels; kept when a GnuCash book is imported again"
            + ("; every posting carries them" if kind == "schedule" else "")
        )
        self.tags_entry.connect("changed", lambda *_: self.on_count())
        page.add("Tags", self.tags_entry)
        self.documents = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        page.add("Documents", self.documents)
        #: (status, row) for each linked document shown, in order.
        self.document_rows: list[tuple[attachment_engine.AttachmentStatus, Gtk.Widget]] = []
        self.link_entry: Gtk.Entry | None = None
        self.show()

    def tags(self) -> tuple[str, ...]:
        return tuple(self.tags_entry.get_text().split(","))

    def count(self) -> int:
        """What the page holds, for its tab: each tag and each document."""
        tags = [part for part in self.tags_entry.get_text().split(",") if part.strip()]
        return len(tags) + len(self.document_rows)

    # ----------------------------------------------------------------- showing

    def show(self) -> None:
        while (child := self.documents.get_first_child()) is not None:
            self.documents.remove(child)
        self.document_rows = []
        self.link_entry = None
        if self.holder is None:
            what = "schedule" if self.kind == "schedule" else "transaction"
            note = Gtk.Label(label=f"Save the {what} to link documents.", xalign=0)
            note.add_css_class("dim")
            self.documents.append(note)
            return
        report = (
            self.book.attachment_report(schedule=self.holder.handle)
            if self.kind == "schedule"
            else self.book.attachment_report(transaction=self.holder.handle)
        )
        for item in report.attachments:
            row = Gtk.Box(spacing=6)
            label = Gtk.Label(
                label=item.location + (" — missing" if item.missing else ""),
                xalign=0,
                selectable=True,
                wrap=True,
            )
            label.set_hexpand(True)
            if item.missing:
                label.add_css_class("negative")
                label.set_tooltip_text(
                    f"Not found at {item.path}. Restore the file, or relink it."
                    if item.path is not None
                    else "Not found"
                )
            elif item.owner == "source":
                label.set_tooltip_text("Linked in GnuCash; refreshed on re-import")
            if item.owner == "source":
                label.add_css_class("dim")
            row.append(label)
            open_button = Gtk.Button(label="Open")
            open_button.set_sensitive(not item.missing)
            open_button.connect("clicked", lambda _b, status=item: self.open_document(status))
            row.append(open_button)
            if item.owner == "breadsched":
                again = Gtk.Button(label="Relink…")
                again.connect("clicked", lambda _b, status=item: self._choose_relink(status))
                row.append(again)
                remove = Gtk.Button(icon_name="list-remove-symbolic")
                remove.set_tooltip_text("Unlink; the file itself is kept")
                remove.connect(
                    "clicked", lambda _b, status=item: self.remove_document(status.location)
                )
                row.append(remove)
            self.documents.append(row)
            self.document_rows.append((item, row))
        folder = report.folder
        controls = Gtk.Box(spacing=6)
        attach = Gtk.Button(label="Attach file…")
        attach.set_tooltip_text(
            f"Copy a file into {folder} and link it" if folder else "Link a file where it is"
        )
        attach.connect("clicked", self._choose_attachment)
        controls.append(attach)
        address = Gtk.Entry(placeholder_text="https://…")
        address.set_hexpand(True)
        keeps_enter(address)
        address.connect("activate", lambda entry: self.link_location(entry.get_text()))
        controls.append(address)
        self.link_entry = address
        link = Gtk.Button(label="Link address")
        link.connect("clicked", lambda *_: self.link_location(address.get_text()))
        controls.append(link)
        self.documents.append(controls)
        self.on_count()

    # ----------------------------------------------------------------- changing

    def _call(self, operation: Callable[[str], Any]) -> bool:
        """Run one book-server document operation on the stored holder's handle."""
        if self.holder is None:
            return False
        try:
            changed = operation(self.holder.handle)
        except ApiError as error:
            self.on_error(str(error))
            return False
        # Later saves build on the stored holder, including its documents.
        self.holder = changed
        self.on_changed(changed)
        self.show()
        return True

    def attach_path(self, path: Path) -> bool:
        """Copy a file into the attachment folder (or link it in place) and link it."""
        if self.holder is None:
            return False
        book, schedule = self.book, self.kind == "schedule"
        report = (
            book.attachment_report(schedule=self.holder.handle)
            if schedule
            else book.attachment_report(transaction=self.holder.handle)
        )
        copy = report.folder is not None
        return self._call(
            lambda handle: (
                book.attach_schedule_file(handle, str(path), copy)
                if schedule
                else book.attach_file(handle, str(path), copy)
            )
        )

    def link_location(self, location: str) -> bool:
        book, schedule = self.book, self.kind == "schedule"
        return self._call(
            lambda handle: (
                book.link_schedule_attachment(handle, location)
                if schedule
                else book.link_attachment(handle, location)
            )
        )

    def remove_document(self, location: str) -> bool:
        book, schedule = self.book, self.kind == "schedule"
        return self._call(
            lambda handle: (
                book.unlink_schedule_attachment(handle, location)
                if schedule
                else book.unlink_attachment(handle, location)
            )
        )

    def relink_document(self, location: str, path: Path) -> bool:
        book, schedule = self.book, self.kind == "schedule"
        moved = attachment_engine.location_in(self.folder, path)
        return self._call(
            lambda handle: (
                book.relink_schedule_attachment(handle, location, moved)
                if schedule
                else book.relink_attachment(handle, location, moved)
            )
        )

    def open_document(self, item: attachment_engine.AttachmentStatus) -> str | None:
        """Open a document with the desktop's default application."""
        if item.kind == "web":
            uri = item.location
        elif item.path is not None and item.path.is_file():
            uri = item.path.as_uri()
        else:
            self.on_error(f"{item.location} cannot be found")
            return None
        try:
            Gio.AppInfo.launch_default_for_uri(uri, None)
        except GLib.Error as exc:
            self.on_error(f"Could not open {item.location}: {exc.message}")
        return uri

    def _choose_attachment(self, button) -> None:
        dialog = Gtk.FileDialog(title="Attach a document")

        def chosen(dialog, result) -> None:
            try:
                file = dialog.open_finish(result)
            except GLib.Error:
                return
            if file.get_path():
                self.attach_path(Path(file.get_path()))

        dialog.open(button.get_root() if button is not None else None, None, chosen)

    def _choose_relink(self, item: attachment_engine.AttachmentStatus) -> None:
        dialog = Gtk.FileDialog(title=f"Where is {item.location} now?")

        def chosen(dialog, result) -> None:
            try:
                file = dialog.open_finish(result)
            except GLib.Error:
                return
            if file.get_path():
                self.relink_document(item.location, Path(file.get_path()))

        dialog.open(self.documents.get_root(), None, chosen)
