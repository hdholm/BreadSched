"""A desktop-portal backend that answers file and print dialogs from a script.

A sandboxed GTK application never shows its own file chooser or print dialog: it
asks ``org.freedesktop.portal.Desktop``, whose backend (GNOME's, KDE's, ...) shows
the dialog on the host. For CI this module is that backend. The real portal
frontend, ``xdg-desktop-portal``, still sits between it and the application: it
checks the caller, exports each chosen file through the document portal, and
passes the printed document on. This backend only stands in for the person:

- **Open** and **Save** return the host path listed for the dialog's title in the
  answers file; a title with no answer is cancelled.
- **Print** accepts the print dialog with PDF output, then stores each document
  it is asked to print in ``print_dir``.

Every request is appended to the log as one JSON object per line.

It runs on the host with the system's PyGObject, before the frontend starts, and
needs a ``.portal`` file naming it (see ``flatpak_desktop_checks.sh``).

Usage: python3 portal_test_backend.py ANSWERS.json LOG.jsonl
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

BUS_NAME = "org.freedesktop.impl.portal.desktop.breadschedtest"
OBJECT_PATH = "/org/freedesktop/portal/desktop"

INTERFACES = """
<node>
  <interface name="org.freedesktop.impl.portal.FileChooser">
    <method name="OpenFile">
      <arg type="o" name="handle" direction="in"/>
      <arg type="s" name="app_id" direction="in"/>
      <arg type="s" name="parent_window" direction="in"/>
      <arg type="s" name="title" direction="in"/>
      <arg type="a{sv}" name="options" direction="in"/>
      <arg type="u" name="response" direction="out"/>
      <arg type="a{sv}" name="results" direction="out"/>
    </method>
    <method name="SaveFile">
      <arg type="o" name="handle" direction="in"/>
      <arg type="s" name="app_id" direction="in"/>
      <arg type="s" name="parent_window" direction="in"/>
      <arg type="s" name="title" direction="in"/>
      <arg type="a{sv}" name="options" direction="in"/>
      <arg type="u" name="response" direction="out"/>
      <arg type="a{sv}" name="results" direction="out"/>
    </method>
  </interface>
  <interface name="org.freedesktop.impl.portal.Print">
    <method name="PreparePrint">
      <arg type="o" name="handle" direction="in"/>
      <arg type="s" name="app_id" direction="in"/>
      <arg type="s" name="parent_window" direction="in"/>
      <arg type="s" name="title" direction="in"/>
      <arg type="a{sv}" name="settings" direction="in"/>
      <arg type="a{sv}" name="page_setup" direction="in"/>
      <arg type="a{sv}" name="options" direction="in"/>
      <arg type="u" name="response" direction="out"/>
      <arg type="a{sv}" name="results" direction="out"/>
    </method>
    <method name="Print">
      <arg type="o" name="handle" direction="in"/>
      <arg type="s" name="app_id" direction="in"/>
      <arg type="s" name="parent_window" direction="in"/>
      <arg type="s" name="title" direction="in"/>
      <arg type="h" name="fd" direction="in"/>
      <arg type="a{sv}" name="options" direction="in"/>
      <arg type="u" name="response" direction="out"/>
      <arg type="a{sv}" name="results" direction="out"/>
    </method>
  </interface>
</node>
"""

#: Portal response codes.
SUCCESS, CANCELLED = 0, 1

#: A4 landscape, in the portal's page-setup vocabulary (millimetres).
PAGE_SETUP = {
    "PPDName": GLib.Variant("s", ""),
    "Name": GLib.Variant("s", "iso_a4"),
    "DisplayName": GLib.Variant("s", "A4"),
    "Width": GLib.Variant("d", 210.0),
    "Height": GLib.Variant("d", 297.0),
    "MarginTop": GLib.Variant("d", 12.0),
    "MarginBottom": GLib.Variant("d", 12.0),
    "MarginLeft": GLib.Variant("d", 12.0),
    "MarginRight": GLib.Variant("d", 12.0),
    "Orientation": GLib.Variant("s", "landscape"),
}


class Backend:
    def __init__(self, answers: dict, log: Path) -> None:
        self.answers = answers
        self.log = log
        self.print_dir = Path(answers["print_dir"])
        self.print_dir.mkdir(parents=True, exist_ok=True)
        self.printed = 0

    def record(self, **entry: object) -> None:
        with self.log.open("a", encoding="utf-8") as output:
            output.write(json.dumps(entry) + "\n")

    def choose(self, kind: str, app_id: str, title: str) -> tuple[int, dict]:
        answer = self.answers.get(kind, {}).get(title)
        self.record(method=kind, app_id=app_id, title=title, answer=answer)
        if answer is None:
            return CANCELLED, {}
        uri = Path(answer).absolute().as_uri()
        return SUCCESS, {"uris": GLib.Variant("as", [uri])}

    def prepare_print(self, app_id: str, title: str) -> tuple[int, dict]:
        self.record(method="PreparePrint", app_id=app_id, title=title)
        settings = {
            "output-file-format": GLib.Variant("s", "pdf"),
            "n-copies": GLib.Variant("s", "1"),
        }
        return SUCCESS, {
            "settings": GLib.Variant("a{sv}", settings),
            "page-setup": GLib.Variant("a{sv}", PAGE_SETUP),
            "token": GLib.Variant("u", 1),
        }

    def print_file(self, app_id: str, title: str, fd: int) -> tuple[int, dict]:
        self.printed += 1
        target = self.print_dir / f"printed-{self.printed}.out"
        with os.fdopen(fd, "rb") as source:
            source.seek(0)
            data = source.read()
        target.write_bytes(data)
        self.record(method="Print", app_id=app_id, title=title, path=str(target), bytes=len(data))
        return SUCCESS, {}

    def call(self, _connection, _sender, _path, _interface, method, parameters, invocation):
        args = parameters.unpack()
        try:
            if method in ("OpenFile", "SaveFile"):
                _handle, app_id, _parent, title, _options = args
                kind = "open" if method == "OpenFile" else "save"
                response, results = self.choose(kind, app_id, title)
            elif method == "PreparePrint":
                _handle, app_id, _parent, title, _settings, _setup, _options = args
                response, results = self.prepare_print(app_id, title)
            elif method == "Print":
                _handle, app_id, _parent, title, index, _options = args
                fds = invocation.get_message().get_unix_fd_list()
                response, results = self.print_file(app_id, title, fds.get(index))
            else:
                invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)
                return
        except Exception as exc:  # noqa: BLE001 - reported to the caller and the log
            self.record(method=method, error=repr(exc))
            invocation.return_dbus_error("org.freedesktop.DBus.Error.Failed", repr(exc))
            return
        invocation.return_value(GLib.Variant("(ua{sv})", (response, results)))


def main(answers_path: str, log_path: str) -> int:
    answers = json.loads(Path(answers_path).read_text(encoding="utf-8"))
    backend = Backend(answers, Path(log_path))
    connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    for interface in Gio.DBusNodeInfo.new_for_xml(INTERFACES).interfaces:
        connection.register_object(OBJECT_PATH, interface, backend.call, None, None)
    loop = GLib.MainLoop()

    def acquired(*_args) -> None:
        backend.record(method="ready")

    def lost(*_args) -> None:
        backend.record(method="name-lost")
        loop.quit()

    Gio.bus_own_name_on_connection(connection, BUS_NAME, Gio.BusNameOwnerFlags.NONE, acquired, lost)
    loop.run()
    return 1


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
