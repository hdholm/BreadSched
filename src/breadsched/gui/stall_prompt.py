"""Ask whether to wait for, or quit, a BreadSched that has stopped responding.

Run by the watchdog (``python -m breadsched.gui.stall_prompt SECONDS LOG [DOING]``) as a
process of its own, because the stuck one cannot draw anything. It exits with
``PROMPT_QUIT`` for **Quit BreadSched**, and 0 for **Wait** or when closed; the
watchdog ends it by itself if BreadSched starts responding again.
"""

from __future__ import annotations

import sys

from .gi_setup import GLib, Gtk
from .watchdog import PROMPT_QUIT
from .widgets.bounded import action_row

__all__ = ["StallPrompt", "main"]


class StallPrompt(Gtk.Window):
    """The question, with **Wait** (the default) and **Quit BreadSched**."""

    def __init__(self, seconds: str, log: str, on_answer, doing: str = "") -> None:
        super().__init__(title="BreadSched is not responding")
        self.set_default_size(520, -1)
        self._on_answer = on_answer
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(18)
        heading = Gtk.Label(label="BreadSched is not responding", xalign=0)
        heading.add_css_class("title-3")
        box.append(heading)
        if doing:
            intro = (
                f"BreadSched has been {doing} for {seconds} seconds, longer than "
                "expected. It may still be working, or it may be stuck. Waiting is "
                "safest while it is still working."
            )
        else:
            intro = (
                f"BreadSched has not responded for {seconds} seconds. It may still be "
                "working (opening or upgrading a large book takes a while), or it may "
                "be stuck."
            )
        self.message = Gtk.Label(
            label=(
                f"{intro}\n\n"
                "Quit ends it now. Your book keeps everything saved before it stopped "
                "responding; only typing not yet entered is lost. When you start "
                "BreadSched again, it offers to report the problem.\n\n"
                f"Where it is stuck has been written to:\n{log}"
            ),
            xalign=0,
            wrap=True,
            selectable=True,
        )
        box.append(self.message)
        self.quit_button = Gtk.Button(label="Quit BreadSched")
        self.quit_button.add_css_class("destructive-action")
        self.quit_button.connect("clicked", lambda *_: self.answer(True))
        self.wait_button = Gtk.Button(label="Wait")
        self.wait_button.connect("clicked", lambda *_: self.answer(False))
        box.append(action_row(destructive=self.quit_button, primary=self.wait_button))
        self.set_child(box)
        self.set_default_widget(self.wait_button)
        self.connect("close-request", self._on_close)

    def _on_close(self, *_args) -> bool:
        self.answer(False)  # closing it means Wait
        return False

    def answer(self, quit_: bool) -> None:
        on_answer, self._on_answer = self._on_answer, None
        if on_answer is not None:
            on_answer(quit_)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    seconds = args[0] if args else "?"
    log = args[1] if len(args) > 1 else ""
    doing = args[2] if len(args) > 2 else ""
    loop = GLib.MainLoop()
    outcome = {"quit": False}

    def answered(quit_: bool) -> None:
        outcome["quit"] = quit_
        loop.quit()

    window = StallPrompt(seconds, log, answered, doing)
    window.present()
    window.wait_button.grab_focus()
    # The message is selectable (to copy the log's path) but starts unselected.
    GLib.idle_add(lambda: window.message.select_region(0, 0) or False)
    loop.run()
    return PROMPT_QUIT if outcome["quit"] else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
