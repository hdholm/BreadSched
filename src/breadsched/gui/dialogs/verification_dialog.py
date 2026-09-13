"""Read-only, background book verification results."""

from __future__ import annotations

from ...gen.db.sqlite import DbSQLite
from ...gen.db.verification import BookVerification
from ...gen.utils.cancellation import OperationCancelled
from ..background import BackgroundJob
from ..gi_setup import Gtk

__all__ = ["VerificationDialog"]


class VerificationDialog(Gtk.Window):
    """Verify one native book without freezing or modifying its open writer."""

    def __init__(self, parent: Gtk.Window | None, path: str) -> None:
        super().__init__(title="Verify book", transient_for=parent, modal=True)
        self.path = path
        self._job: BackgroundJob[BookVerification, None] = BackgroundJob()
        self._close_when_done = False
        self.set_default_size(700, 480)
        self.connect("close-request", self._on_close_request)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(18)
        self.set_child(box)

        self.heading = Gtk.Label(label="Checking SQLite and financial relationships…", xalign=0)
        self.heading.add_css_class("category-title")
        box.append(self.heading)
        self.spinner = Gtk.Spinner(spinning=True)
        self.spinner.set_halign(Gtk.Align.START)
        box.append(self.spinner)

        self.details = Gtk.Label(xalign=0, yalign=0, wrap=True, selectable=True)
        self.details.set_text(path)
        scroller = Gtk.ScrolledWindow(child=self.details, vexpand=True)
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        box.append(scroller)

        self.close_button = Gtk.Button(label="Cancel", halign=Gtk.Align.END)
        self.close_button.connect("clicked", self._on_close)
        box.append(self.close_button)

        def work(cancel, _report) -> BookVerification:
            if cancel.is_set():
                raise OperationCancelled()
            report = DbSQLite.verify_path(path)
            if cancel.is_set():
                raise OperationCancelled()
            return report

        self._job.start(work, lambda _update: None, self._succeeded, self._failed)

    def _succeeded(self, report: BookVerification) -> None:
        self.spinner.stop()
        self.spinner.set_visible(False)
        self.close_button.set_label("Close")
        if report.ok:
            self.heading.set_text("No problems found")
            self.heading.add_css_class("success")
            self.details.set_text(
                "SQLite integrity and BreadSched's financial-object relationships are clean."
            )
        else:
            self.heading.set_text("Book verification found problems")
            self.heading.add_css_class("negative")
            lines = [f"SQLite: {problem}" for problem in report.sqlite]
            lines.extend(f"{issue.code}: {issue.message}" for issue in report.issues)
            self.details.set_text("\n".join(lines))
        self._finish()

    def _failed(self, exc: BaseException) -> None:
        self.spinner.stop()
        self.spinner.set_visible(False)
        self.close_button.set_label("Close")
        if isinstance(exc, OperationCancelled):
            self.heading.set_text("Verification cancelled")
            self.details.set_text("The book was not changed.")
        else:
            self.heading.set_text("The book could not be verified")
            self.heading.add_css_class("negative")
            self.details.set_text(str(exc))
        self._finish()

    def _finish(self) -> None:
        if self._close_when_done:
            self._close_when_done = False
            self.close()

    def _on_close(self, _button) -> None:
        if self._job.active:
            self._close_when_done = True
            self._job.cancel()
            self.close_button.set_sensitive(False)
            self.heading.set_text("Cancelling verification…")
            return
        self.close()

    def _on_close_request(self, _window) -> bool:
        if not self._job.active:
            return False
        self._on_close(None)
        return True

    def wait_for_background(self, timeout: float = 10.0) -> bool:
        """Wait for completion; deterministic support for GUI tests."""
        return self._job.wait(timeout)
