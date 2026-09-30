"""Drive the portal file chooser and print dialog from the installed Flatpak.

``flatpak_desktop_checks.sh`` runs this inside the sandbox, with the real
``xdg-desktop-portal`` frontend and document portal running on the host and
``portal_test_backend.py`` standing in for the person at each dialog. It goes
through the application's own actions, not a test double:

- **Open** asks the portal for a book; the backend picks one outside Documents,
  which the sandbox cannot see directly. The application must open it through
  the document portal path the portal hands back.
- **Export transactions** and **Back up book** ask the portal where to save;
  both files must be written where the portal says, outside Documents.
- **Print** on every printable view goes through the print portal: the backend
  accepts the dialog, and the application must send it one printed document per
  report. The portal's dialog cannot show the Plan's **Report** tab, so the
  application asks first whether to include the category detail; the check
  answers that it should.

Usage: python3 flatpak_desktop_checks.py BOOK PRINT_DIR REPORT
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

TIMEOUT = 60.0


def main(book: str, print_dir: str, report_path: str) -> int:
    from breadsched import APP_ID
    from breadsched.gen.utils.user_paths import portal_document_id
    from breadsched.gui import printing
    from breadsched.gui.app import BreadSchedApplication
    from breadsched.gui.gi_setup import GLib, Gtk
    from breadsched.gui.viewmanager import CATEGORIES, ViewManager

    finished: dict[str, str | None] = {}

    def recording(kind: str, real: Callable[..., Any]) -> Callable[..., Any]:
        def finish(dialog: Any, result: Any) -> Any:
            try:
                chosen = real(dialog, result)
            except GLib.Error as exc:
                finished[kind] = f"error: {exc.message}"
                raise
            finished[kind] = chosen.get_path() if chosen is not None else None
            return chosen

        return finish

    Gtk.FileDialog.open_finish = recording("open", Gtk.FileDialog.open_finish)
    Gtk.FileDialog.save_finish = recording("save", Gtk.FileDialog.save_finish)
    context = GLib.MainContext.default()
    printed_dir = Path(print_dir)

    def settle() -> None:
        while context.pending():
            context.iteration(False)

    def wait_for(condition: Callable[[], Any], what: str) -> Any:
        deadline = time.monotonic() + TIMEOUT
        while time.monotonic() < deadline:
            settle()
            value = condition()
            if value:
                return value
            time.sleep(0.1)
        raise AssertionError(f"timed out waiting for {what}; results {finished}")

    app = BreadSchedApplication(application_id=f"{APP_ID}.PortalChecks", unique=False)
    app.register()
    app.do_startup()
    reported: list[str] = []
    app._report = reported.append  # type: ignore[method-assign]
    window = ViewManager(app, prompt_due_on_open=False)
    app.open_book(book)
    window.present()
    settle()

    report: dict[str, Any] = {}

    app.on_open()
    opened = wait_for(lambda: finished.get("open"), "the portal's Open result")
    assert not opened.startswith("error:"), opened
    wait_for(lambda: app.book_path == opened, "the chosen book to open")
    assert app.db is not None and app.db.is_open
    report["opened"] = opened
    report["opened_through_portal"] = portal_document_id(opened) is not None
    report["open_notice"] = list(reported)
    reported.clear()

    def saved_through_portal(action: Callable[[], None], what: str) -> Path:
        finished.pop("save", None)
        started = time.time()
        action()
        saved = wait_for(lambda: finished.get("save"), f"the portal's {what} result")
        assert not saved.startswith("error:"), saved
        target = Path(saved)

        def written() -> bool:
            if not target.is_file():
                return False
            stat = target.stat()
            return stat.st_size > 0 and stat.st_mtime >= started - 2

        wait_for(written, f"the {what} at {target}")
        return target

    exported = saved_through_portal(app.on_export, "export")
    header = exported.read_text(encoding="utf-8").splitlines()[0]
    assert header.startswith("date,"), header
    report["exported"] = str(exported)
    backup = saved_through_portal(app.on_backup, "backup")
    assert backup.read_bytes().startswith(b"SQLite format 3"), backup
    report["backup"] = str(backup)
    report["backup_notice"] = list(reported)

    def buttons(widget: Any) -> Any:
        child = widget.get_first_child()
        while child is not None:
            if isinstance(child, Gtk.Button):
                yield child
            yield from buttons(child)
            child = child.get_next_sibling()

    def press(label: str) -> bool:
        """Click the visible button labelled ``label`` in any window, once."""
        for toplevel in Gtk.Window.list_toplevels():
            if toplevel is window or not toplevel.get_visible():
                continue
            for button in buttons(toplevel):
                if button.get_label() == label:
                    button.emit("clicked")
                    return True
        return False

    printed: dict[str, int] = {}
    asked: list[str] = []
    for key, *_rest in CATEGORIES:
        window.show_category(key)
        settle()
        view = window.stack.get_visible_child()
        if not getattr(view, "PRINTABLE", False):
            continue
        before = len(list(printed_dir.glob("printed-*")))
        # The same applied state the Print action prints.
        document = window._printable_view().printable_report()
        assert document is not None, key
        window.print_action.activate(None)
        if document.has_optional:
            # The portal's print dialog has no Report tab, so BreadSched asks
            # first; ask for the optional section.
            wait_for(
                lambda document=document: press(printing.optional_choice_label(document)),
                f"the {key} report's print choice",
            )
            asked.append(key)
        wait_for(
            lambda before=before: len(list(printed_dir.glob("printed-*"))) > before,
            f"the {key} report to reach the print portal",
        )
        printed[key] = before + 1
    assert printed, "no printable views"
    report["printed"] = printed
    report["asked"] = asked
    report["errors"] = [message for message in reported if "Could not" in message]

    window.destroy()
    if app.db is not None:
        app.db.close()
    Path(report_path).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print(__doc__, file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1], sys.argv[2], sys.argv[3]))
