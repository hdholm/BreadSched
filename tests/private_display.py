"""Run the GTK tests on a private virtual X display.

A plain ``pytest`` (or ``pytest -n auto``) on a desktop otherwise opens every
test window on the real compositor: windows flash on screen, steal focus, and a
dozen workers creating and destroying hundreds of windows at once can crash GTK's
Wayland backend inside the compositor's event handling (seen on Fedora 44 with
GNOME). CI and the ``make`` targets already use Xvfb; this makes every run do the
same. Each pytest process (each xdist worker) gets its own Xvfb, started before
GTK is imported and stopped when the process exits.

Set ``GUI_VISIBLE=1`` to watch the tests on the current display instead. Without
an ``Xvfb`` executable (Windows, macOS, or a Linux host without it) the current
display is used, as before.
"""

from __future__ import annotations

import atexit
import os
import select
import shutil
import subprocess
from collections.abc import MutableMapping

__all__ = ["start_private_display"]


def start_private_display(
    environ: MutableMapping[str, str] = os.environ, timeout: float = 10.0
) -> subprocess.Popen | None:
    """Start Xvfb and point GTK at it; return the server, or None if not used."""
    if environ.get("GUI_VISIBLE"):
        return None
    xvfb = shutil.which("Xvfb")
    if xvfb is None:
        return None
    read_end, write_end = os.pipe()
    try:
        server = subprocess.Popen(
            [
                xvfb,
                "-displayfd",
                str(write_end),
                "-screen",
                "0",
                "1280x1024x24",
                "-nolisten",
                "tcp",
            ],
            pass_fds=(write_end,),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        os.close(read_end)
        os.close(write_end)
        return None
    os.close(write_end)
    # Xvfb writes the display number it chose, then a newline, once it is ready.
    number = b""
    try:
        while not number.endswith(b"\n"):
            ready, _, _ = select.select([read_end], [], [], timeout)
            chunk = os.read(read_end, 16) if ready else b""
            if not chunk:
                break
            number += chunk
    finally:
        os.close(read_end)
    if not number.strip().isdigit():
        server.kill()
        server.wait()
        return None

    def stop() -> None:
        server.terminate()
        try:
            server.wait(5)
        except subprocess.TimeoutExpired:
            server.kill()

    atexit.register(stop)
    display = f":{number.strip().decode()}"
    environ["DISPLAY"] = display
    environ["GDK_BACKEND"] = "x11"
    environ.pop("WAYLAND_DISPLAY", None)
    environ["BREADSCHED_PRIVATE_DISPLAY"] = display
    return server
