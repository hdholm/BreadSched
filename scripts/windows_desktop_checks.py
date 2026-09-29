"""Drive the native Windows file chooser and the print path in an installed copy.

``packaging/windows/test-installer.ps1`` runs this with the installer's own Python
after the GTK smoke. It goes through the application's own actions, not a test
double:

- **Open** shows the native "Open book" dialog; the check types another book's
  path into its file-name box and presses Open, then requires the application
  to have opened that book.
- **Export transactions** does the same through the native save dialog and
  requires the CSV to be written.
- **Print** runs the Print action on every printable view and requires the
  report to be written, a default handler for it to exist, and opening it to
  succeed.

Usage: python windows_desktop_checks.py BOOK OTHER_BOOK WORK_DIR REPORT
"""

from __future__ import annotations

import ctypes
import json
import sys
import time
from collections.abc import Callable
from ctypes import wintypes
from pathlib import Path
from typing import Any

TIMEOUT = 60.0
WM_SETTEXT = 0x000C
BM_CLICK = 0x00F5
IDOK = 1


def _user32() -> Any:
    user32 = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
    user32.FindWindowW.restype = wintypes.HWND
    user32.FindWindowW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR)
    user32.GetDlgItem.restype = wintypes.HWND
    user32.GetDlgItem.argtypes = (wintypes.HWND, ctypes.c_int)
    user32.SendMessageW.restype = ctypes.c_ssize_t
    user32.SendMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPCWSTR)
    user32.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    user32.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    return user32


def _descendants(user32: Any, parent: int) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)  # type: ignore[attr-defined]

    def visit(hwnd: int, _param: int) -> bool:
        name = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, name, 256)
        found.append((hwnd, name.value))
        return True

    user32.EnumChildWindows(parent, callback_type(visit), 0)
    return found


def main(book: str, other_book: str, work_dir: str, report_path: str) -> int:
    from breadsched import APP_ID
    from breadsched.gui import printing
    from breadsched.gui.app import BreadSchedApplication
    from breadsched.gui.gi_setup import Gio, GLib
    from breadsched.gui.viewmanager import CATEGORIES, ViewManager

    user32 = _user32()
    context = GLib.MainContext.default()
    work = Path(work_dir)

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
        raise AssertionError(f"timed out waiting for {what}")

    def choose(title: str, path: Path) -> None:
        """Type ``path`` into the native dialog titled ``title`` and accept it."""
        dialog = wait_for(lambda: user32.FindWindowW("#32770", title), f'the "{title}" dialog')
        # The file-name box is the dialog's first Edit control, in Open and Save alike.
        controls = wait_for(
            lambda: [hwnd for hwnd, name in _descendants(user32, dialog) if name == "Edit"],
            f'the file-name box in "{title}"',
        )
        user32.SendMessageW(controls[0], WM_SETTEXT, 0, str(path))
        button = user32.GetDlgItem(dialog, IDOK)
        assert button, f'no default button in "{title}": {_descendants(user32, dialog)}'
        user32.PostMessageW(button, BM_CLICK, 0, 0)
        wait_for(lambda: not user32.FindWindowW("#32770", title), f'"{title}" to close')

    app = BreadSchedApplication(application_id=f"{APP_ID}.WindowsChecks", unique=False)
    app.register()
    app.do_startup()
    reported: list[str] = []
    app._report = reported.append  # type: ignore[method-assign]
    window = ViewManager(app, prompt_due_on_open=False)
    app.open_book(book)
    window.present()
    settle()

    # Open another book through the native file chooser.
    target = Path(other_book).resolve()
    app.on_open()
    choose("Open book", target)
    wait_for(
        lambda: app.book_path is not None and Path(app.book_path).resolve() == target,
        "the chosen book to open",
    )

    # Save through the native save dialog.
    export = work / "exported-transactions.csv"
    export.unlink(missing_ok=True)
    app.on_export()
    choose("Export transactions", export)
    wait_for(lambda: export.is_file() and export.stat().st_size > 0, "the exported CSV")
    header = export.read_text(encoding="utf-8").splitlines()[0]

    # Print every printable view through the application's Print action.
    printed: dict[str, dict[str, Any]] = {}
    current = {"view": ""}
    real_open = printing.open_print_preview

    def recording_open(document: str) -> Path:
        path = printing.write_print_preview(document)
        content_type, _uncertain = Gio.content_type_guess(str(path), None)
        handler = Gio.AppInfo.get_default_for_type(content_type, False)
        assert handler is not None, f"no default application for {content_type}"
        opened = Gio.AppInfo.launch_default_for_uri(path.as_uri(), None)
        assert opened, f"could not open {path}"
        printed[current["view"]] = {
            "bytes": path.stat().st_size,
            "handler": handler.get_name(),
        }
        return path

    printing.open_print_preview = recording_open
    try:
        for key, *_rest in CATEGORIES:
            window.show_category(key)
            settle()
            view = window.stack.get_visible_child()
            if not getattr(view, "PRINTABLE", False):
                continue
            current["view"] = key
            # The Print action writes and opens the report before it returns.
            window.print_action.activate(None)
            settle()
            assert key in printed, (key, reported)
    finally:
        printing.open_print_preview = real_open
    assert not reported, reported
    assert printed, "no printable view was printed"

    report = {
        "opened": str(app.book_path),
        "export_header": header,
        "printed": printed,
    }
    window.destroy()
    if app.db is not None:
        app.db.close()
    with open(report_path, "w", encoding="utf-8") as output:
        json.dump(report, output, indent=2)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 5:
        print(__doc__, file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main(*sys.argv[1:]))
