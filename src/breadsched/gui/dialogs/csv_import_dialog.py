"""Map, preview, and import a CSV bank or card statement.

CSV layouts differ by institution, so the dialog reads the columns first, suggests a
mapping from the header names, and shows every row's status before anything is
written. Reading, validation, classification, and the single-batch write all go
through ``gen.services.csv_import``, the same rules as the web interface and
``breadsched import-csv``.
"""

from __future__ import annotations

import re

from ...gen.db.sqlite import DbSQLite
from ...gen.lib.account import AccountClass
from ...gen.services.csv_import import (
    CsvImportRequest,
    CsvMapping,
    CsvPreview,
    InspectCsv,
    import_csv,
    inspect_csv,
    preview_csv_import,
)
from ...plugins.importer.gnucash_common import ImportResult
from ...presentation import service_error_message
from ..gi_setup import GLib, Gtk

__all__ = ["CsvImportDialog"]

_STATUS_LABELS = {
    "new": "New",
    "imported": "Already imported",
    "possible_duplicate": "Possible duplicate",
    "invalid": "Invalid",
}
_FIELDS = ("date", "amount", "debit", "credit", "description", "memo")
_GUESSES = {
    "date": (r"date", r"datum"),
    "amount": (r"^amount$", r"betrag", r"amount"),
    "debit": (r"debit", r"withdraw"),
    "credit": (r"credit", r"deposit"),
    "description": (r"desc", r"payee", r"name", r"merchant"),
    "memo": (r"memo", r"note", r"reference"),
}


def _guess(columns: tuple[str, ...], patterns: tuple[str, ...]) -> str | None:
    for pattern in patterns:
        for column in columns:
            if re.search(pattern, column, re.IGNORECASE):
                return column
    return None


class CsvImportDialog(Gtk.Window):
    """Choose a CSV statement, map its columns, preview, then import."""

    DATE_FORMATS = ("auto", "iso", "month-first", "day-first")
    NUMBER_FORMATS = ("auto", "dot", "comma")

    def __init__(self, parent: Gtk.Window | None, db: DbSQLite) -> None:
        super().__init__(title="Import CSV statement", transient_for=parent, modal=True)
        self.set_destroy_with_parent(True)
        self.db = db
        self.path: str | None = None
        self.columns: tuple[str, ...] = ()
        self.set_default_size(900, 680)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(18)
        self.set_child(box)

        intro = Gtk.Label(
            label=(
                "Choose the statement and its account, check the suggested columns, and "
                "preview. Nothing is written until you import. Re-importing the same rows "
                "adds nothing and keeps any category you chose. A row matching a "
                "transaction already in the account on the same date and amount is held "
                "back unless you include possible duplicates."
            ),
            xalign=0,
            wrap=True,
        )
        intro.add_css_class("dim")
        box.append(intro)

        source_row = Gtk.Box(spacing=8)
        self.path_label = Gtk.Label(label="No file chosen", xalign=0, hexpand=True)
        self.path_label.add_css_class("dim")
        choose = Gtk.Button(label="Choose CSV file…")
        choose.connect("clicked", self._on_choose)
        self.header = Gtk.CheckButton(label="First row is a header", active=True)
        self.header.connect("toggled", lambda *_: self.read_columns())
        source_row.append(self.path_label)
        source_row.append(self.header)
        source_row.append(choose)
        box.append(source_row)

        grid = Gtk.Grid(column_spacing=10, row_spacing=6)
        box.append(grid)
        self.accounts = sorted(
            (
                account
                for account in db.iter_accounts()
                if not account.placeholder
                and account.account_class in (AccountClass.ASSET, AccountClass.LIABILITY)
            ),
            key=lambda account: db.full_name(account).casefold(),
        )
        self.account = Gtk.DropDown.new_from_strings(
            [db.full_name(account) for account in self.accounts]
        )
        grid.attach(Gtk.Label(label="Account", xalign=0), 0, 0, 1, 1)
        grid.attach(self.account, 1, 0, 1, 1)

        self.pickers: dict[str, Gtk.DropDown] = {}
        for index, field in enumerate(_FIELDS):
            picker = Gtk.DropDown.new_from_strings(["(none)"])
            self.pickers[field] = picker
            row, column = divmod(index, 2)
            grid.attach(
                Gtk.Label(label=f"{field.capitalize()} column", xalign=0), column * 2, row + 1, 1, 1
            )
            grid.attach(picker, column * 2 + 1, row + 1, 1, 1)

        self.date_format = Gtk.DropDown.new_from_strings(
            ["Detect date order", "Year first", "Month first", "Day first"]
        )
        self.number_format = Gtk.DropDown.new_from_strings(
            ["Detect decimal separator", "Period decimal (1,234.56)", "Comma decimal (1.234,56)"]
        )
        grid.attach(Gtk.Label(label="Date order", xalign=0), 0, 4, 1, 1)
        grid.attach(self.date_format, 1, 4, 1, 1)
        grid.attach(Gtk.Label(label="Number format", xalign=0), 2, 4, 1, 1)
        grid.attach(self.number_format, 3, 4, 1, 1)
        self.invert = Gtk.CheckButton(label="Money out is shown positive")
        self.include_duplicates = Gtk.CheckButton(label="Include possible duplicates")
        grid.attach(self.invert, 0, 5, 2, 1)
        grid.attach(self.include_duplicates, 2, 5, 2, 1)

        self.layout_label = Gtk.Label(xalign=0, wrap=True)
        self.layout_label.add_css_class("dim")
        box.append(self.layout_label)

        scroller = Gtk.ScrolledWindow(vexpand=True)
        self.rows_box = Gtk.Grid(column_spacing=14, row_spacing=4)
        scroller.set_child(self.rows_box)
        box.append(scroller)

        self.status = Gtk.Label(xalign=0, wrap=True)
        box.append(self.status)

        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        close = Gtk.Button(label="Close")
        close.connect("clicked", lambda *_: self.close())
        self.preview_button = Gtk.Button(label="Preview")
        self.preview_button.connect("clicked", lambda *_: self.preview())
        self.import_button = Gtk.Button(label="Import")
        self.import_button.add_css_class("suggested-action")
        self.import_button.connect("clicked", lambda *_: self.import_rows())
        for button in (close, self.preview_button, self.import_button):
            buttons.append(button)
        box.append(buttons)
        self._set_ready(False)
        if not self.accounts:
            self._message("Add a bank, cash, or card account before importing a statement.", True)

    # --------------------------------------------------------------- source

    def _on_choose(self, _button) -> None:
        dialog = Gtk.FileDialog(title="Choose a CSV statement")
        dialog.open(self, None, self._on_chosen)

    def _on_chosen(self, dialog, result) -> None:
        try:
            file = dialog.open_finish(result)
        except GLib.Error:
            return
        self.set_source(file.get_path())

    def set_source(self, path: str) -> None:
        """Point the dialog at a file and read its columns."""
        self.path = path
        self.path_label.set_text(path)
        self.path_label.remove_css_class("dim")
        self.read_columns()

    def read_columns(self) -> bool:
        """Show the detected layout and first rows, and suggest a mapping."""
        self._clear_rows()
        if self.path is None:
            return False
        result = inspect_csv(InspectCsv(self.path, header=self.header.get_active()))
        if result.value is None:
            self._message(service_error_message(result.errors[0]), True)
            self._set_ready(False)
            return False
        inspection = result.value
        self.columns = inspection.columns
        options = ["(none)", *self.columns]
        for field, picker in self.pickers.items():
            picker.set_model(Gtk.StringList.new(options))
            guess = _guess(self.columns, _GUESSES[field])
            picker.set_selected(options.index(guess) if guess else 0)
        # One signed amount column wins over separate debit and credit guesses.
        if self._selected("amount"):
            self.pickers["debit"].set_selected(0)
            self.pickers["credit"].set_selected(0)
        self.layout_label.set_text(
            f"Read as {inspection.encoding} with delimiter {inspection.delimiter!r}. "
            "Check the suggested columns, then preview."
        )
        self._show_rows(list(self.columns), [list(row) for row in inspection.sample])
        self._message("", False)
        self._set_ready(True)
        return True

    # -------------------------------------------------------------- mapping

    def _selected(self, field: str) -> str | None:
        index = self.pickers[field].get_selected()
        return self.columns[index - 1] if 0 < index <= len(self.columns) else None

    def mapping(self) -> CsvMapping:
        return CsvMapping(
            date=self._selected("date") or "",
            amount=self._selected("amount"),
            debit=self._selected("debit"),
            credit=self._selected("credit"),
            description=self._selected("description"),
            memo=self._selected("memo"),
            date_format=self.DATE_FORMATS[self.date_format.get_selected()],  # type: ignore[arg-type]
            number_format=self.NUMBER_FORMATS[self.number_format.get_selected()],  # type: ignore[arg-type]
            header=self.header.get_active(),
            invert=self.invert.get_active(),
        )

    def _request(self) -> CsvImportRequest | None:
        if self.path is None or not self.accounts:
            self._message("Choose a CSV file and an account first.", True)
            return None
        return CsvImportRequest(
            source=self.path,
            account=self.accounts[self.account.get_selected()].handle,
            mapping=self.mapping(),
            include_duplicates=self.include_duplicates.get_active(),
        )

    # ---------------------------------------------------- preview and import

    def preview(self) -> CsvPreview | None:
        """Classify every row with the current mapping; write nothing."""
        request = self._request()
        if request is None:
            return None
        result = preview_csv_import(self.db, request)
        if result.value is None:
            self._clear_rows()
            self._message(service_error_message(result.errors[0]), True)
            return None
        preview = result.value
        counts = " · ".join(
            f"{label}: {preview.count(status)}"  # type: ignore[arg-type]
            for status, label in _STATUS_LABELS.items()
        )
        self.layout_label.set_text(
            f"{preview.encoding}, delimiter {preview.delimiter!r}, {preview.date_format} "
            f"dates, {preview.number_format} decimals. {counts}"
        )
        self._show_rows(
            ["Line", "Date", "Amount", "Description", "Status", "Reason"],
            [
                [
                    str(row.line),
                    row.when.isoformat() if row.when else "",
                    row.amount.format(parens_negative=True) if row.amount is not None else "",
                    row.description,
                    _STATUS_LABELS[row.status],
                    row.reason,
                ]
                for row in preview.rows
            ],
            numeric={2},
        )
        self._message("", False)
        return preview

    def import_rows(self) -> ImportResult | None:
        """Import the previewed rows as one undo step; None when refused."""
        request = self._request()
        if request is None:
            return None
        result = import_csv(self.db, request)
        if result.value is None:
            self._message(service_error_message(result.errors[0]), True)
            return None
        imported = result.value
        outcome = imported.result
        held = imported.preview.count("possible_duplicate")
        self.preview()
        self._message(
            f"Imported {outcome.transactions_new} new; {outcome.transactions_unchanged} "
            f"already imported; {held} possible duplicate(s) "
            f"{'included' if request.include_duplicates else 'held back'}; "
            f"{outcome.skipped} skipped.",
            False,
        )
        return outcome

    # ------------------------------------------------------------- display

    def _set_ready(self, ready: bool) -> None:
        self.preview_button.set_sensitive(ready)
        self.import_button.set_sensitive(ready)

    def _clear_rows(self) -> None:
        child = self.rows_box.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self.rows_box.remove(child)
            child = following

    def _show_rows(
        self, headers: list[str], rows: list[list[str]], numeric: set[int] | None = None
    ) -> None:
        self._clear_rows()
        numeric = numeric or set()
        for column, text in enumerate(headers):
            heading = Gtk.Label(label=text, xalign=1 if column in numeric else 0)
            heading.add_css_class("dim")
            self.rows_box.attach(heading, column, 0, 1, 1)
        for row_index, values in enumerate(rows, start=1):
            for column, text in enumerate(values):
                label = Gtk.Label(label=text, xalign=1 if column in numeric else 0, selectable=True)
                self.rows_box.attach(label, column, row_index, 1, 1)

    def _message(self, text: str, error: bool) -> None:
        self.status.set_text(text)
        if error:
            self.status.add_css_class("negative")
        else:
            self.status.remove_css_class("negative")
