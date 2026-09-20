"""Launcher tests.

These run on a machine with no GTK installed — which is the point. The launcher's
whole job is to behave well in exactly that situation, so the container without
PyGObject is the correct place to test it, not an obstacle to doing so.
"""

from __future__ import annotations

import importlib.util
import sys

import pytest

from breadsched.cli.main import main as cli_main
from breadsched.gui import launcher

_HAS_PYGOBJECT = importlib.util.find_spec("gi") is not None


def _has_gtk4_namespace() -> bool:
    """Distinguish an installed binding from an importable GTK4 runtime."""
    if not _HAS_PYGOBJECT:
        return False
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk  # noqa: F401
    except (ImportError, ValueError):
        return False
    return True


_HAS_GTK4 = _has_gtk4_namespace()


@pytest.fixture
def book(tmp_path, capsys):
    path = tmp_path / "household.breadsched"
    cli_main(["init", str(path)])
    capsys.readouterr()
    return path


class TestHelpAndVersion:
    def test_help_works_without_gtk(self, capsys):
        assert launcher.main(["--help"]) == 0
        out = capsys.readouterr().out
        assert "breadsched-gtk [BOOK]" in out

    def test_short_help_flag(self, capsys):
        assert launcher.main(["-h"]) == 0
        assert "Usage:" in capsys.readouterr().out

    def test_version_works_without_gtk(self, capsys):
        from breadsched import __version__

        assert launcher.main(["--version"]) == 0
        assert __version__ in capsys.readouterr().out

    def test_help_lists_the_equivalent_commands(self, capsys):
        launcher.main(["--help"])
        out = capsys.readouterr().out
        for form in ("breadsched-gtk", "breadsched gui", "python -m breadsched.gui"):
            assert form in out


class TestArgumentChecking:
    def test_a_missing_book_is_reported_before_gtk_is_touched(self, tmp_path, capsys):
        code = launcher.main([str(tmp_path / "nope.breadsched")])
        assert code == 2
        assert "no book at" in capsys.readouterr().err

    def test_the_error_suggests_how_to_create_one(self, tmp_path, capsys):
        launcher.main([str(tmp_path / "nope.breadsched")])
        assert "breadsched init" in capsys.readouterr().err

    def test_two_books_are_refused(self, book, capsys):
        assert launcher.main([str(book), str(book)]) == 2
        assert "one book at a time" in capsys.readouterr().err


@pytest.fixture
def no_gtk(monkeypatch):
    """Simulate a machine with no GTK stack, whatever this machine has.

    Setting the entry to None makes the import statement raise ImportError, which
    is what PyGObject's absence looks like. Deriving the test from the environment
    instead would make it pass vacuously on a build machine without GTK, and start
    a real main loop on one with it.
    """
    monkeypatch.setitem(sys.modules, "breadsched.gui.app", None)


@pytest.fixture
def fake_gtk(monkeypatch):
    """Let the launcher reach the application, without entering a main loop."""
    from breadsched.gui import app as app_module

    calls = []
    monkeypatch.setattr(
        app_module.BreadSchedApplication,
        "run",
        lambda self, argv: calls.append(argv) or 0,
    )
    return calls


class TestMissingGtk:
    """The situation a first-time user on a fresh machine actually meets."""

    def test_a_missing_gtk_stack_is_explained_not_traced(self, book, capsys, no_gtk):
        code = launcher.main([str(book)])
        # 3 distinguishes "environment is not ready" from "you typed it wrong" (2).
        assert code == 3
        error = capsys.readouterr().err
        assert "Traceback" not in error
        assert "GTK 4 and PyGObject" in error

    def test_the_message_names_real_packages_per_platform(self, book, capsys, no_gtk):
        launcher.main([str(book)])
        error = capsys.readouterr().err
        for hint in ("apt install", "dnf install", "pacman -S", "brew install"):
            assert hint in error

    def test_the_message_points_at_the_working_cli(self, book, capsys, no_gtk):
        launcher.main([str(book)])
        assert "breadsched --help" in capsys.readouterr().err

    def test_the_underlying_error_is_still_shown(self, book, capsys, no_gtk):
        launcher.main([str(book)])
        assert "underlying error" in capsys.readouterr().err

    @pytest.mark.skipif(
        not _HAS_PYGOBJECT or _HAS_GTK4,
        reason="requires PyGObject installed without an importable GTK4 namespace",
    )
    def test_installed_pygobject_without_gtk4_is_explained(self, book, capsys):
        code = launcher.main([str(book)])
        assert code == 3
        error = capsys.readouterr().err
        assert "Traceback" not in error
        assert "GTK 4 and PyGObject" in error


class TestCliSubcommand:
    def test_breadsched_gui_delegates_to_the_launcher(self, book, capsys, no_gtk):
        assert cli_main(["gui", str(book)]) == 3
        assert "GTK 4 and PyGObject" in capsys.readouterr().err

    def test_breadsched_gui_accepts_no_book(self, capsys, no_gtk):
        assert cli_main(["gui"]) == 3

    @pytest.mark.skipif(not _HAS_GTK4, reason="GTK4 is not importable")
    def test_breadsched_gui_starts_the_application(self, book, fake_gtk):
        assert cli_main(["gui", str(book)]) == 0
        assert str(book) in fake_gtk[0]

    def test_the_subcommand_appears_in_help(self, capsys):
        with pytest.raises(SystemExit):
            cli_main(["--help"])
        assert "gui" in capsys.readouterr().out


@pytest.mark.skipif(sys.version_info < (3, 11), reason="tomllib needs 3.11")
@pytest.mark.skipif(not _HAS_GTK4, reason="GTK4 is not importable")
class TestStartingSuccessfully:
    """Requires PyGObject importable; the run() call itself is stubbed out."""

    def test_a_book_is_passed_through_for_gio_to_open(self, book, fake_gtk):
        assert launcher.main([str(book)]) == 0
        assert fake_gtk[0][-1] == str(book.resolve())

    def test_no_book_starts_with_an_empty_window(self, fake_gtk):
        assert launcher.main([]) == 0
        assert len(fake_gtk[0]) == 1

    def test_a_relative_path_is_resolved(self, book, fake_gtk, monkeypatch):
        monkeypatch.chdir(book.parent)
        assert launcher.main([book.name]) == 0
        assert fake_gtk[0][-1] == str(book.resolve())


class TestEntryPoints:
    def test_the_gui_script_points_at_the_launcher(self):
        """A console script bound to app:main would traceback without GTK."""
        from pathlib import Path

        try:
            import tomllib
        except ModuleNotFoundError:
            import tomli as tomllib

        root = Path(__file__).resolve().parent.parent
        config = tomllib.loads((root / "pyproject.toml").read_text())
        assert config["project"]["gui-scripts"]["breadsched-gtk"] == (
            "breadsched.gui.launcher:main"
        )

    def test_the_module_form_is_runnable(self):
        import importlib.util

        spec = importlib.util.find_spec("breadsched.gui.__main__")
        assert spec is not None

    def test_the_cli_script_is_breadsched(self):
        from pathlib import Path

        try:
            import tomllib
        except ModuleNotFoundError:
            import tomli as tomllib

        root = Path(__file__).resolve().parent.parent
        config = tomllib.loads((root / "pyproject.toml").read_text())
        assert config["project"]["scripts"]["breadsched"] == "breadsched.cli.main:main"
