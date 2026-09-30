"""Grant a Flatpak application one file through the document portal, as Open or Save do.

A file chooser hands a sandboxed application its choice by adding the file to the
document portal and granting the application access. This does the same through
the portal's D-Bus API (``flatpak document-export`` can disagree with the portal
service about reply formats when the two come from different releases).

Prints the document id; the application sees the file as
``/run/user/<uid>/doc/<id>/<name>``. ``--new`` adds a file that does not exist
yet, the way Save does.

Usage: python3 portal_export.py APP_ID PATH [--new]
"""

from __future__ import annotations

import os
import sys

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

NAME = "org.freedesktop.portal.Documents"
PATH = "/org/freedesktop/portal/documents"


def export(app_id: str, path: str, *, new: bool = False) -> str:
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    fds = Gio.UnixFDList()
    target = os.path.abspath(path)
    if new:
        parent = os.open(os.path.dirname(target), os.O_PATH | os.O_CLOEXEC)
        filename = os.path.basename(target).encode() + b"\0"
        method = "AddNamed"
        parameters = GLib.Variant("(haybb)", (fds.append(parent), filename, True, True))
        os.close(parent)
    else:
        handle = os.open(target, os.O_PATH | os.O_CLOEXEC)
        method = "Add"
        parameters = GLib.Variant("(hbb)", (fds.append(handle), True, True))
        os.close(handle)
    result, _fds = bus.call_with_unix_fd_list_sync(
        NAME,
        PATH,
        NAME,
        method,
        parameters,
        GLib.VariantType("(s)"),
        Gio.DBusCallFlags.NONE,
        -1,
        fds,
        None,
    )
    (document,) = result.unpack()
    bus.call_sync(
        NAME,
        PATH,
        NAME,
        "GrantPermissions",
        GLib.Variant("(ssas)", (document, app_id, ["read", "write"])),
        None,
        Gio.DBusCallFlags.NONE,
        -1,
        None,
    )
    return document


if __name__ == "__main__":
    arguments = [item for item in sys.argv[1:] if item != "--new"]
    if len(arguments) != 2:
        print(__doc__, file=sys.stderr)
        raise SystemExit(2)
    print(export(arguments[0], arguments[1], new="--new" in sys.argv[1:]))
