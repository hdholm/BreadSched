"""Preview and write changes back to the imported GnuCash book, SQLite or XML (#174).

The dialog shows exactly what each transaction's write would change and every
local difference that cannot be written, with its reason. Nothing is written until
changes are ticked and **Write selected** is pressed; the shared service then backs
the book up, writes atomically, and reads back to confirm.
"""

from __future__ import annotations

from ...gen.db.sqlite import DbSQLite
from ...gen.services.gnucash_writeback import (
    ApplyWriteback,
    WritebackPlan,
    apply_writeback,
    preview_writeback,
    set_writeback_keep_backups,
    writeback_keep_backups,
)
from ...presentation import service_error_message
from ..gi_setup import Gtk

__all__ = ["GnuCashWritebackDialog"]


class GnuCashWritebackDialog(Gtk.Window):
    """One row per writable transaction, each chosen explicitly."""

    def __init__(self, parent: Gtk.Window | None, db: DbSQLite) -> None:
        super().__init__(title="Write changes to GnuCash", transient_for=parent, modal=True)
        self.set_destroy_with_parent(True)
        self.set_default_size(820, 520)
        self.db = db
        self.plan: WritebackPlan | None = None
        self.checks: list[tuple[str, Gtk.CheckButton]] = []

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(outer, f"set_margin_{side}")(18)
        self.set_child(outer)

        self.summary = Gtk.Label(xalign=0, wrap=True)
        outer.append(self.summary)
        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        scroller = Gtk.ScrolledWindow(child=self.content)
        scroller.set_vexpand(True)
        scroller.set_min_content_height(120)
        outer.append(scroller)

        settings = Gtk.Box(spacing=8)
        settings.append(Gtk.Label(label="Backups to keep", xalign=0))
        self.keep = Gtk.SpinButton.new_with_range(1, 1000, 1)
        self.keep.set_value(writeback_keep_backups(db))
        self.keep.connect("value-changed", self._on_keep_changed)
        settings.append(self.keep)
        outer.append(settings)

        self.status = Gtk.Label(xalign=0, wrap=True)
        self.status.add_css_class("dim")
        outer.append(self.status)

        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        close = Gtk.Button(label="Close")
        close.connect("clicked", lambda *_: self.close())
        buttons.append(close)
        self.write_button = Gtk.Button(label="Write selected")
        self.write_button.add_css_class("suggested-action")
        self.write_button.connect("clicked", lambda *_: self.write())
        buttons.append(self.write_button)
        outer.append(buttons)
        self.refresh()

    # ------------------------------------------------------------------ preview

    def refresh(self) -> None:
        while child := self.content.get_first_child():
            self.content.remove(child)
        self.checks = []
        previewed = preview_writeback(self.db)
        if previewed.value is None:
            self.plan = None
            self.summary.set_text(service_error_message(previewed.errors[0]))
            self.write_button.set_sensitive(False)
            return
        self.plan = previewed.value
        self.summary.set_text(
            f"GnuCash book: {self.plan.source}\n"
            "Tick the transactions to write. Nothing is written until you press "
            "Write selected; the book is backed up first. Close the book in GnuCash."
        )
        if not self.plan.changes:
            self.content.append(Gtk.Label(label="Nothing to write.", xalign=0))
        for change in self.plan.changes:
            check = Gtk.CheckButton(
                label=f"{change.post_date.isoformat()}  {change.description}  "
                f"({', '.join(change.kinds)})"
            )
            self.checks.append((change.transaction, check))
            self.content.append(check)
            detail = Gtk.Label(label="\n".join(change.details), xalign=0, wrap=True)
            detail.set_margin_start(28)
            detail.add_css_class("dim")
            detail.set_selectable(True)
            self.content.append(detail)
        if self.plan.unsupported:
            heading = Gtk.Label(label="Not written", xalign=0)
            heading.add_css_class("heading")
            self.content.append(heading)
            for item in self.plan.unsupported:
                self.content.append(
                    Gtk.Label(
                        label=f"{item.post_date.isoformat()}  {item.description}: {item.reason}",
                        xalign=0,
                        wrap=True,
                    )
                )
        self.write_button.set_sensitive(bool(self.plan.changes))

    def select_all(self, active: bool = True) -> None:
        for _handle, check in self.checks:
            check.set_active(active)

    def chosen(self) -> tuple[str, ...]:
        return tuple(handle for handle, check in self.checks if check.get_active())

    # -------------------------------------------------------------------- write

    def _message(self, text: str, error: bool) -> None:
        self.status.set_text(text)
        if error:
            self.status.add_css_class("negative")
        else:
            self.status.remove_css_class("negative")

    def _on_keep_changed(self, spin: Gtk.SpinButton) -> None:
        result = set_writeback_keep_backups(self.db, int(spin.get_value()))
        if result.value is None:
            self._message(service_error_message(result.errors[0]), True)

    def write(self) -> int | None:
        """Write the ticked transactions; the count written, or None if refused."""
        chosen = self.chosen()
        if not chosen:
            self._message("Tick one or more transactions to write.", True)
            return None
        result = apply_writeback(self.db, ApplyWriteback(chosen))
        if result.value is None:
            self._message(service_error_message(result.errors[0]), True)
            self.refresh()
            return None
        written = len(result.value.written)
        self.refresh()
        self._message(
            f"Wrote {written} transaction(s) to GnuCash and re-imported to confirm. "
            f"Backup: {result.value.backup}",
            False,
        )
        return written
