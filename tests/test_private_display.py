"""The GTK tests' private display (tests/private_display.py)."""

from __future__ import annotations

import os
import shutil

import pytest
from private_display import start_private_display


def test_gui_visible_keeps_the_current_display():
    environ = {"GUI_VISIBLE": "1", "DISPLAY": ":5", "WAYLAND_DISPLAY": "wayland-0"}
    assert start_private_display(environ) is None
    assert environ == {"GUI_VISIBLE": "1", "DISPLAY": ":5", "WAYLAND_DISPLAY": "wayland-0"}


def test_without_xvfb_the_current_display_is_used(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    environ = {"WAYLAND_DISPLAY": "wayland-0"}
    assert start_private_display(environ) is None
    assert environ == {"WAYLAND_DISPLAY": "wayland-0"}


def test_a_private_xvfb_replaces_the_desktop_display():
    if shutil.which("Xvfb") is None:
        pytest.skip("Xvfb is not installed (xvfb on Debian/Ubuntu, xorg-x11-server-Xvfb on Fedora)")
    environ = {"DISPLAY": ":99999", "WAYLAND_DISPLAY": "wayland-0"}
    server = start_private_display(environ)
    try:
        assert server is not None and server.poll() is None
        assert environ["DISPLAY"].startswith(":") and environ["DISPLAY"] != ":99999"
        assert environ["GDK_BACKEND"] == "x11" and "WAYLAND_DISPLAY" not in environ
        assert environ["BREADSCHED_PRIVATE_DISPLAY"] == environ["DISPLAY"]
        # The server answers on the display it reported.
        socket = f"/tmp/.X11-unix/X{environ['DISPLAY'][1:]}"
        assert os.path.exists(socket)
    finally:
        if server is not None:
            server.terminate()
            server.wait(5)


def test_this_run_uses_a_private_display_unless_visible():
    """The suite itself runs on Xvfb wherever Xvfb is installed."""
    if os.environ.get("GUI_VISIBLE") or shutil.which("Xvfb") is None:
        pytest.skip("GUI_VISIBLE is set or Xvfb is not installed; the current display is used")
    display = os.environ.get("BREADSCHED_PRIVATE_DISPLAY")
    assert display and os.environ["DISPLAY"] == display
    assert os.environ["GDK_BACKEND"] == "x11" and "WAYLAND_DISPLAY" not in os.environ
