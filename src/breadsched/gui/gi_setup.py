"""The one place PyGObject is imported.

Every GUI module imports its GTK names from here rather than reaching for
``gi.repository`` itself. Two reasons, both learned the hard way.

**Version pinning has to happen before the first import, everywhere.** Repeating
``gi.require_version`` in seventeen modules means one of them will eventually be
missed, and the symptom is a ``PyGIWarning`` from whichever module happened to
import first — a warning about the wrong file, in a run that still passes.

**PyGObject warns during its own import.** While installing deprecation
descriptors on a namespace, ``gi/overrides/__init__.py`` calls ``getattr`` on an
attribute it has already wrapped, which fires the descriptor::

    PyGIDeprecationWarning: GLib.unix_signal_add_full is deprecated;
    use GLibUnix.signal_add_full instead

Nothing in this project references that function; the warning is raised inside
PyGObject, by PyGObject, on ``from gi.repository import GLib``. It cannot be fixed
here and it is not actionable, so it is suppressed at the single point where the
import happens. Suppressing it here rather than through a ``filterwarnings`` entry
in the test configuration means it is silenced for anyone embedding this code too,
and that the suppression is narrow enough to leave every other warning — including
GTK deprecations we do need to act on — fully visible.
"""

from __future__ import annotations

import warnings

import gi

__all__ = ["Gdk", "Gio", "GLib", "GObject", "Gtk", "Pango", "UPSTREAM_NOISE"]

# Pin every namespace before anything imports it. A namespace left unpinned loads
# whichever version is installed, silently, with a warning aimed at the wrong file.
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Pango", "1.0")

#: Message patterns raised by PyGObject itself that no change here can prevent.
#: Matched as regular expressions against the start of the warning text.
UPSTREAM_NOISE = (
    r"GLib\.unix_signal_add_full is deprecated",
)


def _import_repository():
    """Import the gi namespaces with PyGObject's own import noise suppressed."""
    with warnings.catch_warnings():
        for pattern in UPSTREAM_NOISE:
            warnings.filterwarnings(
                "ignore", message=pattern, category=DeprecationWarning
            )
        from gi.repository import Gdk, Gio, GLib, GObject, Gtk, Pango

        return Gdk, Gio, GLib, GObject, Gtk, Pango


Gdk, Gio, GLib, GObject, Gtk, Pango = _import_repository()
