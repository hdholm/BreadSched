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

from ...gen.db.sqlite import DbSQLite  # noqa: E402
from ...gen.plug import IMPORTER, PluginManager  # noqa: E402
from ...gen.utils import logs  # noqa: E402
from ..gi_setup import GLib, Gtk, Pango

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
        self.set_default_size(820, 640)

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
        choose_button = Gtk.Button(label="Choose file…")
        choose_button.connect("clicked", self._on_choose)
        chooser_row.append(choose_button)
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
        close_button = Gtk.Button(label="Close")
        close_button.connect("clicked", lambda *_: self.close())
        buttons.append(close_button)
        self.import_button = Gtk.Button(label="Import")
        self.import_button.add_css_class("suggested-action")
        self.import_button.set_sensitive(False)
        self.import_button.connect("clicked", self._on_import)
        buttons.append(self.import_button)
        box.append(buttons)

    # ---------------------------------------------------------------- choosing

    def _on_choose(self, _button) -> None:
        dialog = Gtk.FileDialog(title="Choose a financial-data file")
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
        """Show how far the import has got, and keep the window responsive.

        The import runs on the main thread, so the loop is pumped here rather than
        moved to a worker: a background thread would be writing to the database
        while the interface reads it, and the meter is not worth that.
        """
        if total > 0:
            self.progress.set_fraction(min(1.0, done / total))
            self.progress.set_text(f"{stage} ({done:,} of {total:,})")
        else:
            # An XML book gives no count in advance; pulse rather than pretend.
            self.progress.pulse()
            self.progress.set_text(stage)
        self._pump()

    @staticmethod
    def _pump() -> None:
        context = GLib.MainContext.default()
        # Bounded: an unbounded drain could process a click that starts a second
        # import on top of the one already running.
        for _ in range(20):
            if not context.pending():
                break
            context.iteration(False)

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
        if self.path is None or self._plugin is None:
            return
        self._clear_result()
        self.import_button.set_sensitive(False)
        self.result_view.set_text("Importing…")
        self.progress.set_visible(True)
        self.progress.set_fraction(0.0)
        self.progress.set_text("Starting")
        self._pump()

        log_path = None
        if self.debug_check.get_active():
            log_path = logs.configure(
                verbosity=2,
                path=Path(self.path).with_suffix(".import-log.txt"),
                stream=False,
            )

        try:
            kwargs = {
                "include_scheduled": self.scheduled_check.get_active(),
                "progress": self._on_progress,
            }
            if self._plugin.id in {"qif", "ofx"}:
                kwargs["number_format"] = ("auto", "dot", "comma")[
                    self.number_format.get_selected()
                ]
            if self._plugin.id == "qif":
                kwargs["date_format"] = ("auto", "month-first", "day-first")[
                    self.date_format.get_selected()
                ]
            result = self._plugin.run(self.db, self.path, **kwargs)
        except Exception as exc:  # noqa: BLE001 - shown to the user, and logged
            LOG.exception("import of %s failed", self.path)
            lines = [f"Import failed: {exc}", "", "Nothing was written to the book."]
            if log_path:
                lines += ["", f"A detailed log is at {log_path}"]
            else:
                lines += [
                    "",
                    "Tick the log option above and try again to record what "
                    "the importer was reading when it stopped.",
                ]
            self.result_view.set_text("\n".join(lines))
            self.result_view.remove_css_class("dim")
            self.result_view.add_css_class("negative")
            self.import_button.set_sensitive(True)
            self.progress.set_visible(False)
            return
        finally:
            logs.configure(verbosity=0)

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
        self.import_button.set_sensitive(True)
