"""What is due, asked once, when a book is opened.

Posting scheduled transactions automatically is convenient right up to the first
time it is wrong, and a wrong posting is discovered weeks later as a balance that
does not match the bank. A single "post everything due" button has the same problem
in one click.

So each occurrence is decided on its own, with three answers that mean genuinely
different things:

* **Post now** — it happened; write it to the ledger.
* **Remind me later** — undecided; ask again next time. This is the default,
  because the safe answer to a question the user has not read is to ask again.
* **Never** — it did not happen, or it was recorded some other way. The occurrence
  is marked dealt with without posting anything, and is not raised again.

"Never" applies to that one date, not the schedule: skipping March must not also
dismiss February or silence April.
"""

from __future__ import annotations

from ...gen.db.sqlite import DbSQLite
from ...gen.engine import schedule as schedule_engine
from ..gi_setup import Gtk

__all__ = ["DueDialog"]

POST, LATER, NEVER = 0, 1, 2

_CHOICES = ["Post now", "Remind me later", "Never (mark as done)"]


class DueDialog(Gtk.Window):
    """One row per due occurrence, each with its own decision."""

    def __init__(self, parent: Gtk.Window | None, db: DbSQLite, occurrences) -> None:
        super().__init__(title="Scheduled transactions due", transient_for=parent, modal=True)
        self.set_destroy_with_parent(True)
        self.db = db
        self.occurrences = list(occurrences)
        self.choosers: list[Gtk.DropDown] = []
        self.set_default_size(700, 460)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(18)
        self.set_child(box)

        box.append(
            Gtk.Label(
                label=(
                    f"{len(self.occurrences)} scheduled transaction(s) are due. "
                    "Choose what to do with each."
                ),
                xalign=0,
                wrap=True,
            )
        )

        bulk = Gtk.Box(spacing=8)
        for label, choice in (
            ("Post all", POST),
            ("Remind me later for all", LATER),
            ("Never, all", NEVER),
        ):
            button = Gtk.Button(label=label)
            button.connect("clicked", lambda _b, value=choice: self.set_all(value))
            bulk.append(button)
        box.append(bulk)

        grid = Gtk.Grid(column_spacing=14, row_spacing=6)
        for position, heading in enumerate(("Due", "Schedule", "Amount", "Action")):
            label = Gtk.Label(label=heading, xalign=1 if position == 2 else 0)
            label.add_css_class("summary-label")
            grid.attach(label, position, 0, 1, 1)

        for index, occurrence in enumerate(self.occurrences, start=1):
            due = Gtk.Label(label=occurrence.when.isoformat(), xalign=0)
            if occurrence.when < _today():
                due.add_css_class("negative")
                due.set_text(f"{occurrence.when.isoformat()}  (overdue)")
            grid.attach(due, 0, index, 1, 1)
            grid.attach(Gtk.Label(label=occurrence.name, xalign=0), 1, index, 1, 1)

            amount = Gtk.Label(label=occurrence.amount.format(), xalign=1)
            amount.add_css_class("numeric")
            grid.attach(amount, 2, index, 1, 1)

            chooser = Gtk.DropDown.new_from_strings(_CHOICES)
            # Undecided by default: the safe answer to an unread question is to
            # ask again, not to write to the ledger.
            chooser.set_selected(LATER)
            self.choosers.append(chooser)
            grid.attach(chooser, 3, index, 1, 1)

        scroller = Gtk.ScrolledWindow(child=grid)
        scroller.set_vexpand(True)
        box.append(scroller)

        self.status = Gtk.Label(xalign=0)
        self.status.add_css_class("dim")
        box.append(self.status)

        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        close = Gtk.Button(label="Decide later")
        close.connect("clicked", lambda *_: self.close())
        buttons.append(close)
        apply_button = Gtk.Button(label="Apply")
        apply_button.add_css_class("suggested-action")
        apply_button.connect("clicked", self._on_apply)
        buttons.append(apply_button)
        box.append(buttons)

    # ------------------------------------------------------------------ choices

    def set_all(self, choice: int) -> None:
        for chooser in self.choosers:
            chooser.set_selected(choice)

    def selection(self, choice: int) -> list:
        return [
            occurrence
            for occurrence, chooser in zip(self.occurrences, self.choosers, strict=False)
            if chooser.get_selected() == choice
        ]

    def apply(self) -> tuple[int, int]:
        """Carry out the decisions. Returns (posted, skipped)."""
        posted = schedule_engine.post_occurrences(
            self.db, self.selection(POST), message="Post scheduled transactions"
        )
        skipped = schedule_engine.skip_occurrences(
            self.db, self.selection(NEVER), message="Skip scheduled transactions"
        )
        return len(posted), skipped

    def _on_apply(self, _button) -> None:
        posted, skipped = self.apply()
        self.close()
        if posted or skipped:
            parts = []
            if posted:
                parts.append(f"posted {posted}")
            if skipped:
                parts.append(f"marked {skipped} as done")
            alert = Gtk.AlertDialog(
                message=f"Scheduled transactions: {', '.join(parts)}.",
                detail="Undo with Ctrl+Z if that was not what you wanted.",
            )
            alert.show(self.get_transient_for())


def _today():
    from datetime import date

    return date.today()
