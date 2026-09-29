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
import os
import sys
import time
from collections.abc import Callable
from ctypes import wintypes
from pathlib import Path
from typing import Any

TIMEOUT = 60.0
WM_SETTEXT = 0x000C
WM_GETTEXT = 0x000D
WM_COMMAND = 0x0111
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
VK_RETURN = 0x0D
BM_CLICK = 0x00F5
IDOK = 1


class _Win32:
    """The few user32 calls needed to find and fill a native file dialog."""

    def __init__(self) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
        prototype = ctypes.WINFUNCTYPE  # type: ignore[attr-defined]
        lresult = ctypes.c_ssize_t
        hwnd, uint = wintypes.HWND, wintypes.UINT
        self.find = prototype(hwnd, wintypes.LPCWSTR, wintypes.LPCWSTR)(("FindWindowW", user32))
        self.parent = prototype(hwnd, hwnd)(("GetParent", user32))
        self.visible = prototype(wintypes.BOOL, hwnd)(("IsWindowVisible", user32))
        self.control_id = prototype(ctypes.c_int, hwnd)(("GetDlgCtrlID", user32))
        self.item = prototype(hwnd, hwnd, ctypes.c_int)(("GetDlgItem", user32))
        self._class = prototype(ctypes.c_int, hwnd, wintypes.LPWSTR, ctypes.c_int)(
            ("GetClassNameW", user32)
        )
        self._set = prototype(lresult, hwnd, uint, wintypes.WPARAM, wintypes.LPCWSTR)(
            ("SendMessageW", user32)
        )
        self._get = prototype(lresult, hwnd, uint, wintypes.WPARAM, wintypes.LPWSTR)(
            ("SendMessageW", user32)
        )
        self.post = prototype(wintypes.BOOL, hwnd, uint, wintypes.WPARAM, wintypes.LPARAM)(
            ("PostMessageW", user32)
        )
        self._enum_proc = prototype(wintypes.BOOL, hwnd, wintypes.LPARAM)
        self._enum_top = prototype(wintypes.BOOL, self._enum_proc, wintypes.LPARAM)(
            ("EnumWindows", user32)
        )
        self._owner = prototype(wintypes.DWORD, hwnd, ctypes.POINTER(wintypes.DWORD))(
            ("GetWindowThreadProcessId", user32)
        )
        self._enum = prototype(wintypes.BOOL, hwnd, self._enum_proc, wintypes.LPARAM)(
            ("EnumChildWindows", user32)
        )

    def class_name(self, window: int) -> str:
        name = ctypes.create_unicode_buffer(256)
        self._class(window, name, 256)
        return name.value

    def text(self, window: int) -> str:
        buffer = ctypes.create_unicode_buffer(1024)
        self._get(window, WM_GETTEXT, 1024, buffer)
        return buffer.value

    def set_text(self, window: int, value: str) -> None:
        self._set(window, WM_SETTEXT, 0, value)

    def descendants(self, parent: int) -> list[int]:
        found: list[int] = []

        def visit(window: int, _param: int) -> bool:
            found.append(window)
            return True

        self._enum(parent, self._enum_proc(visit), 0)
        return found

    def file_name_box(self, dialog: int) -> int | None:
        """The visible Edit of the file-name combo box (Open and Save alike)."""
        edits = [
            window
            for window in self.descendants(dialog)
            if self.class_name(window) == "Edit" and self.visible(window)
        ]
        for window in edits:
            if self.class_name(self.parent(window)) == "ComboBox":
                return window
        return edits[0] if edits else None

    def accept(self, dialog: int, box: int, attempt: int) -> str:
        """Press the dialog's default button one of three ways; return which."""
        way = ("command", "enter", "click")[attempt % 3]
        if way == "command":
            self.post(dialog, WM_COMMAND, IDOK, 0)
        elif way == "enter":
            self.post(box, WM_KEYDOWN, VK_RETURN, 0x001C0001)
            self.post(box, WM_KEYUP, VK_RETURN, 0xC01C0001)
        else:
            self.post(self.item(dialog, IDOK), BM_CLICK, 0, 0)
        return way

    def own_windows(self) -> list[str]:
        """This process's visible top-level windows, e.g. a rejection message box."""
        found: list[str] = []

        def visit(window: int, _param: int) -> bool:
            pid = wintypes.DWORD()
            self._owner(window, ctypes.byref(pid))
            if pid.value == os.getpid() and self.visible(window):
                texts = [self.text(child) for child in self.descendants(window)]
                found.append(f"{self.class_name(window)} {self.text(window)!r}: {texts[:8]}")
            return True

        self._enum_top(self._enum_proc(visit), 0)
        return found

    def describe(self, dialog: int) -> list[str]:
        return [
            f"{self.class_name(window)}#{self.control_id(window)}"
            f"{'' if self.visible(window) else ' (hidden)'}: {self.text(window)!r}"
            for window in self.descendants(dialog)
        ]


def main(book: str, other_book: str, work_dir: str, report_path: str) -> int:
    from breadsched import APP_ID
    from breadsched.gui import printing
    from breadsched.gui.app import BreadSchedApplication
    from breadsched.gui.gi_setup import Gio, GLib
    from breadsched.gui.viewmanager import CATEGORIES, ViewManager

    win32 = _Win32()
    accepted: dict[str, list[str]] = {}
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
        """Type ``path`` into the native dialog titled ``title`` and accept it.

        The dialog may still be initializing when it first appears, so the name
        is set and accepted again until the dialog closes. Accepting rotates
        through ``WM_COMMAND(IDOK)`` to the dialog (which, unlike a button
        click, does not need an active window), Enter in the file-name box, and
        a click on the default button; the report records which ones it took.
        """
        dialog = wait_for(lambda: win32.find("#32770", title), f'the "{title}" dialog')
        # MSYS2's Python joins paths with "/" when MSYSTEM is set; the native
        # dialog rejects such a name, so give it the Windows form.
        name = str(path).replace("/", "\\")
        attempts: list[str] = []
        deadline = time.monotonic() + TIMEOUT
        while time.monotonic() < deadline:
            if not win32.find("#32770", title):
                accepted[title] = attempts
                return
            box = win32.file_name_box(dialog)
            if box and win32.find("#32770", title):
                win32.set_text(box, name)
                if win32.text(box) == name:
                    attempts.append(win32.accept(dialog, box, len(attempts)))
            pause_until = time.monotonic() + 2.0
            while time.monotonic() < pause_until and win32.find("#32770", title):
                settle()
                time.sleep(0.1)
        controls = "\n  ".join(win32.describe(dialog))
        windows = "\n  ".join(win32.own_windows())
        raise AssertionError(
            f'"{title}" did not accept {name} after {attempts}; controls:\n  {controls}'
            f"\nwindows of this process:\n  {windows}"
        )

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
        "dialogs": accepted,
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
