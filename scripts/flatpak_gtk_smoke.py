"""Drive the installed GTK interface once inside the Flatpak sandbox.

CI runs this with ``flatpak run --command=python3`` under a virtual display and
with the network unshared. It proves the offline desktop path that command-line
checks cannot: the GTK runtime starts, the installed themed icon resolves, a book
under Documents opens and every view renders, the packaged User Guide loads, and
settings persist in the sandbox's own configuration directory.

Usage: python3 flatpak_gtk_smoke.py BOOK REPORT
"""

from __future__ import annotations

import json
import sys


def main(book: str, report_path: str) -> int:
    from breadsched import APP_ID
    from breadsched.gui.app import BreadSchedApplication
    from breadsched.gui.gi_setup import Gdk, GLib, Gtk
    from breadsched.gui.user_guide import UserGuideWindow, read_user_guide
    from breadsched.gui.viewmanager import CATEGORIES, ViewManager

    context = GLib.MainContext.default()

    def settle() -> None:
        while context.pending():
            context.iteration(False)

    app = BreadSchedApplication(application_id=f"{APP_ID}.Smoke", unique=False)
    app.register()
    app.do_startup()
    window = ViewManager(app, prompt_due_on_open=False)
    app.open_book(book)
    window.present()
    settle()

    rendered = []
    for key, *_rest in CATEGORIES:
        window.show_category(key)
        settle()
        assert window.stack.get_visible_child_name() == key, key
        rendered.append(key)

    guide = UserGuideWindow(app, window)
    guide.present()
    settle()
    text = guide.text_view.get_buffer().get_property("text")
    assert "BreadSched" in text and len(text) > 1000, "User Guide did not load"
    assert read_user_guide()

    display = Gdk.Display.get_default()
    assert display is not None, "no display inside the sandbox"
    icon = Gtk.IconTheme.get_for_display(display).has_icon(APP_ID)
    assert icon, f"installed icon {APP_ID} does not resolve"
    assert Gtk.Window.get_default_icon_name() == APP_ID

    settings_path = str(app.settings.path)
    report = {
        "views": rendered,
        "guide_characters": len(text),
        "icon": icon,
        "settings_path": settings_path,
        "last_book_path": app.settings.get("general", "last_book_path"),
    }
    guide.destroy()
    window.destroy()
    if app.db is not None:
        app.db.close()
    with open(report_path, "w", encoding="utf-8") as output:
        json.dump(report, output, indent=2)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
