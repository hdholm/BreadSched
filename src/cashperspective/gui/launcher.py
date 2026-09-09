"""Launcher for the graphical interface.

This module deliberately does **not** import ``gi`` at the top level. It is the
last thing that runs before the GTK stack is touched, so it is the only place that
can turn "PyGObject is not installed" into an instruction rather than a traceback.
Argument handling and ``--help`` therefore work on a machine with no GTK at all,
which is exactly the machine whose user needs the help text.
"""

from __future__ import annotations

import sys
from pathlib import Path

__all__ = ["main", "USAGE"]

USAGE = """\
Usage: breadsched-gtk [BOOK]

Start the BreadSched graphical interface, optionally opening BOOK
(a .breadsched file). Without BOOK, the window opens empty and you can
create or open a book from the toolbar.

Options:
  -h, --help     show this message and exit
  --version      show the version and exit

Equivalent commands:
  breadsched-gtk household.breadsched
  breadsched gui household.breadsched
  python -m cashperspective.gui household.breadsched
"""

#: Shown when the GTK stack is missing. Package names differ per platform, and a
#: bare "install PyGObject" sends people to a pip build that fails on the C
#: headers, so the actual system packages are named.
INSTALL_HELP = """\
BreadSched's graphical interface needs GTK 4 and PyGObject, which are not
installed.

  Debian/Ubuntu:  sudo apt install python3-gi python3-gi-cairo \\
                                   gir1.2-gtk-4.0 libgtk-4-1
  Fedora:         sudo dnf install python3-gobject python3-cairo gtk4
  Arch:           sudo pacman -S python-gobject gtk4
  macOS:          brew install pygobject3 gtk4 py3cairo
  Windows:        use MSYS2: pacman -S mingw-w64-ucrt-x86_64-python-gobject \\
                                       mingw-w64-ucrt-x86_64-gtk4

Then reinstall BreadSched with the gui extra:  pip install -e ".[gui]"

The command line interface needs none of this and works now:  breadsched --help\
"""


def main(argv: list[str] | None = None) -> int:
    """Start the GUI, or explain clearly why it cannot start."""
    args = list(sys.argv[1:] if argv is None else argv)

    if any(flag in args for flag in ("-h", "--help")):
        print(USAGE, end="")
        return 0

    if "--version" in args:
        from .. import __version__

        print(f"breadsched-gtk {__version__}")
        return 0

    positional = [a for a in args if not a.startswith("-")]
    if len(positional) > 1:
        print("breadsched-gtk: open one book at a time", file=sys.stderr)
        return 2

    book: str | None = None
    if positional:
        candidate = Path(positional[0]).expanduser()
        if not candidate.exists():
            print(
                f"breadsched-gtk: no book at {candidate}\n"
                f"Create one with: breadsched init {candidate}",
                file=sys.stderr,
            )
            return 2
        book = str(candidate.resolve())

    try:
        from .app import CashPerspectiveApplication
    except (ImportError, ValueError) as exc:
        # ImportError: PyGObject absent. ValueError: PyGObject present but the
        # GTK 4 typelib is not, which is what gi.require_version raises.
        print(INSTALL_HELP, file=sys.stderr)
        print(f"\n(underlying error: {exc})", file=sys.stderr)
        return 3

    application = CashPerspectiveApplication()
    # Gio parses trailing arguments as files to open, which drives do_open.
    return application.run([sys.argv[0]] + ([book] if book else []))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
