"""Import dialog.

The file is sniffed as soon as it is chosen and the detected format is shown before
anything is written, because "this is a GnuCash SQLite book with 1,240
transactions" is the confirmation a person needs, and a file extension cannot
supply it: GnuCash names both of its container formats ``.gnucash``.

The import itself runs as one batch, so it is one undo step. Warnings are shown
rather than swallowed -- a book that imported with three imbalances corrected is a
different outcome from one that imported cleanly, and the user should know which
they got.
"""

from __future__ import annotations

from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

from ...gen.db.sqlite import DbSQLite  # noqa: E402
from ...gen.plug import (  # noqa: E402
    IMPORTER,
    PluginManager,
    remember_import_source,
    remembered_import_source,
)
from ...gen.utils import logs  # noqa: E402
from ...gen.utils.cancellation import OperationCancelled  # noqa: E402
from ...plugins.importer.gnucash_common import ImportResult  # noqa: E402
from ..background import BackgroundJob  # noqa: E402
from ..gi_setup import Gio, GLib, Gtk, Pango

__all__ = ["ImportDialog"]

LOG = logs.get_logger(__name__)

#: How many warnings the dialog shows before summarising the rest.
WARNING_LIMIT = 50


class ImportDialog(Gtk.Window):
    """Choose a GnuCash file, check what it is, then import it."""

    def __init__(self, parent: Gtk.Window | None, db: DbSQLite) -> None:
        super().__init__(title="Import financial data", transient_for=parent, modal=True)
        self.db = db
        self.path: str | None = None
        self._job: BackgroundJob[ImportResult, tuple[str, int, int]] | None = None
        self._close_when_done = False
        self.set_default_size(820, 640)
        self.connect("close-request", self._on_close_request)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(18)
        self.set_child(box)

        box.append(
            Gtk.Label(
                label=(
                    "Accounts and transactions are copied into this book. Supported import "
                    "files are opened read-only and never changed."
                ),
                xalign=0,
                wrap=True,
            )
        )

        chooser_row = Gtk.Box(spacing=8)
        self.path_label = Gtk.Label(label="No file chosen", xalign=0, hexpand=True)
        self.path_label.add_css_class("dim")
        # START, not END: the filename is the informative part of a long path.
        self.path_label.set_ellipsize(Pango.EllipsizeMode.START)
        chooser_row.append(self.path_label)
        self.choose_button = Gtk.Button(label="Choose file…")
        self.choose_button.connect("clicked", self._on_choose)
        chooser_row.append(self.choose_button)
        box.append(chooser_row)

        self.detected_label = Gtk.Label(xalign=0)
        box.append(self.detected_label)

        self.format_box = Gtk.Box(spacing=8)
        self.number_format = Gtk.DropDown.new_from_strings(
            ["Auto-detect number format", "Period decimal (1,234.56)", "Comma decimal (1.234,56)"]
        )
        self.date_format = Gtk.DropDown.new_from_strings(
            ["Auto-detect QIF date order", "Month first (MM/DD)", "Day first (DD/MM)"]
        )
        self.format_box.append(self.number_format)
        self.format_box.append(self.date_format)
        self.format_box.set_visible(False)
        box.append(self.format_box)

        self.scheduled_check = Gtk.CheckButton(
            label="Import scheduled transactions when supported", active=True
        )
        box.append(self.scheduled_check)

        self.debug_check = Gtk.CheckButton(
            label="Write a detailed log next to the file being imported"
        )
        self.debug_check.set_tooltip_text(
            "Records every account and transaction read, and why anything was "
            "skipped. Useful when an import does not produce what you expected."
        )
        box.append(self.debug_check)

        # A slow import looks like a hung application, so the meter appears the
        # moment work starts and names the stage it is in.
        self.progress = Gtk.ProgressBar(show_text=True)
        self.progress.set_visible(False)
        box.append(self.progress)

        self.result_view = Gtk.Label(xalign=0, wrap=True, selectable=True)
        self.result_view.add_css_class("dim")
        self.result_view.set_valign(Gtk.Align.START)
        scroller = Gtk.ScrolledWindow(child=self.result_view)
        scroller.set_vexpand(True)
        scroller.set_min_content_height(320)
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        box.append(scroller)

        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        self.close_button = Gtk.Button(label="Close")
        self.close_button.connect("clicked", self._on_close)
        buttons.append(self.close_button)
        self.cancel_button = Gtk.Button(label="Cancel import")
        self.cancel_button.connect("clicked", self._on_cancel)
        self.cancel_button.set_visible(False)
        buttons.append(self.cancel_button)
        self.import_button = Gtk.Button(label="Import")
        self.import_button.add_css_class("suggested-action")
        self.import_button.set_sensitive(False)
        self.import_button.connect("clicked", self._on_import)
        buttons.append(self.import_button)
        box.append(buttons)

        remembered = remembered_import_source(self.db)
        if remembered is not None and Path(remembered).is_file():
            self.set_source(remembered)

    # ---------------------------------------------------------------- choosing

    def _on_choose(self, _button) -> None:
        dialog = Gtk.FileDialog(title="Choose a financial-data file")
        remembered = remembered_import_source(self.db)
        if remembered is not None:
            previous = Path(remembered)
            if previous.is_file():
                dialog.set_initial_file(Gio.File.new_for_path(str(previous)))
            elif previous.parent.is_dir():
                dialog.set_initial_folder(Gio.File.new_for_path(str(previous.parent)))
        dialog.open(self, None, self._on_chosen)

    def _on_chosen(self, dialog, result) -> None:
        try:
            file = dialog.open_finish(result)
        except GLib.Error:
            return
        self.set_source(file.get_path())

    def set_source(self, path: str) -> None:
        """Point the dialog at a file, sniff it, and clear any previous outcome.

        Clearing matters: a result left over from the last attempt is worse than no
        result, because it looks like it describes the file now selected.
        """
        if self._job is not None and self._job.active:
            return
        self.path = path
        self.path_label.set_text(path)
        self.path_label.remove_css_class("dim")
        self._clear_result()

        plugin = PluginManager.instance().for_file(path, IMPORTER)
        self.detected_label.set_text(
            f"Detected: {plugin.name}" if plugin is not None else "Not recognised"
        )
        if plugin is None:
            self.detected_label.add_css_class("negative")
            self.import_button.set_sensitive(False)
        else:
            self.detected_label.remove_css_class("negative")
            self.import_button.set_sensitive(True)
        self._plugin = plugin
        if plugin is None:
            self.format_box.set_visible(False)
        else:
            is_qif = plugin.id == "qif"
            is_ofx = plugin.id == "ofx"
            self.format_box.set_visible(is_qif or is_ofx)
            self.number_format.set_visible(is_qif or is_ofx)
            self.date_format.set_visible(is_qif)

    def _on_progress(self, stage: str, done: int, total: int) -> None:
        """Show worker progress; BackgroundJob invokes this on GTK's main loop."""
        if total > 0:
            self.progress.set_fraction(min(1.0, done / total))
            self.progress.set_text(f"{stage} ({done:,} of {total:,})")
        else:
            # An XML book gives no count in advance; pulse rather than pretend.
            self.progress.pulse()
            self.progress.set_text(stage)

    def _clear_result(self) -> None:
        """Reset everything describing the previous attempt."""
        self.result_view.set_text("")
        self.progress.set_visible(False)
        self.progress.set_fraction(0.0)
        self.result_view.remove_css_class("negative")
        self.result_view.add_css_class("dim")
        self.detected_label.remove_css_class("negative")
        self.import_button.set_label("Import")

    # --------------------------------------------------------------- importing

    def _on_import(self, _button) -> None:
        if (
            self.path is None
            or self._plugin is None
            or (self._job is not None and self._job.active)
        ):
            return
        path = self.path
        plugin = self._plugin
        self._clear_result()
        self.import_button.set_sensitive(False)
        self.choose_button.set_sensitive(False)
        self.scheduled_check.set_sensitive(False)
        self.debug_check.set_sensitive(False)
        self.number_format.set_sensitive(False)
        self.date_format.set_sensitive(False)
        self.cancel_button.set_visible(True)
        self.result_view.set_text("Importing…")
        self.progress.set_visible(True)
        self.progress.set_fraction(0.0)
        self.progress.set_text("Starting")

        log_path = None
        if self.debug_check.get_active():
            log_path = logs.configure(
                verbosity=2,
                path=Path(path).with_suffix(".import-log.txt"),
                stream=False,
            )

        kwargs: dict[str, Any] = {
            "include_scheduled": self.scheduled_check.get_active(),
            "notify": False,
        }
        if plugin.id in {"qif", "ofx"}:
            kwargs["number_format"] = ("auto", "dot", "comma")[self.number_format.get_selected()]
        if plugin.id == "qif":
            kwargs["date_format"] = ("auto", "month-first", "day-first")[
                self.date_format.get_selected()
            ]

        def work(_cancel, report) -> ImportResult:
            kwargs["progress"] = lambda stage, done, total: report((stage, done, total))
            try:
                return plugin.run(self.db, path, **kwargs)
            finally:
                logs.configure(verbosity=0)

        self._job = BackgroundJob()
        self._job.start(
            work,
            lambda update: self._on_progress(*update),
            lambda result: self._import_succeeded(path, result, log_path),
            lambda exc: self._import_failed(path, exc, log_path),
        )

    def _import_succeeded(self, path: str, result: ImportResult, log_path: Path | None) -> None:
        remember_import_source(self.db, path)
        # The worker suppressed synchronous database callbacks: GTK must only be
        # notified after commit, here on its own main thread.
        self.db.emit("database-changed", (self.db,))
        self.db.emit("undo-available", (bool(self.db.undo_stack),))
        self.db.emit("redo-available", (False,))
        result.log_path = str(log_path) if log_path else None
        self.progress.set_fraction(1.0)
        self.progress.set_text("Finished")
        # Fifty rather than twenty: a book with a hundred oddities is exactly the
        # one whose warnings need reading, and truncating them at twenty hides the
        # pattern in what went wrong.
        self.result_view.set_text(
            result.detail(limit=WARNING_LIMIT) + "\n\nThis import is a single undo step (Ctrl+Z)."
        )
        self.import_button.set_label("Import again")
        self._finish_import()

    def _import_failed(self, path: str, exc: BaseException, log_path: Path | None) -> None:
        cancelled = isinstance(exc, OperationCancelled)
        if not cancelled:
            LOG.error(
                "import of %s failed",
                path,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
        lines = [
            "Import cancelled." if cancelled else f"Import failed: {exc}",
            "",
            "Nothing was written to the book.",
        ]
        if log_path:
            lines += ["", f"A detailed log is at {log_path}"]
        elif not cancelled:
            lines += [
                "",
                "Tick the log option above and try again to record what "
                "the importer was reading when it stopped.",
            ]
        self.result_view.set_text("\n".join(lines))
        self.result_view.remove_css_class("dim")
        if not cancelled:
            self.result_view.add_css_class("negative")
        self.progress.set_visible(False)
        self._finish_import()

    def _finish_import(self) -> None:
        self.import_button.set_sensitive(True)
        self.choose_button.set_sensitive(True)
        self.scheduled_check.set_sensitive(True)
        self.debug_check.set_sensitive(True)
        self.number_format.set_sensitive(True)
        self.date_format.set_sensitive(True)
        self.cancel_button.set_visible(False)
        if self._close_when_done:
            self._close_when_done = False
            self.close()

    def _on_cancel(self, _button) -> None:
        if self._job is not None:
            self._job.cancel()
            self.cancel_button.set_sensitive(False)
            self.progress.set_text("Cancelling…")

    def _on_close(self, _button) -> None:
        if self._job is not None and self._job.active:
            self._close_when_done = True
            self._on_cancel(_button)
            return
        self.close()

    def _on_close_request(self, _window) -> bool:
        if self._job is None or not self._job.active:
            return False
        self._close_when_done = True
        self._on_cancel(None)
        return True

    def wait_for_background(self, timeout: float = 10.0) -> bool:
        """Wait for the current import; deterministic support for GUI tests."""
        return self._job is None or self._job.wait(timeout)
