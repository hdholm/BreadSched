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

Occurrences of one schedule are grouped under a heading with their count and
total. A schedule with several missed dates also gets a chooser that sets every
one of its dates at once, while each date keeps its own answer.

Apply goes through the shared due-review service. It rechecks every chosen date
against the book, so a date posted or skipped elsewhere since this window opened
is refused rather than posted twice, and the whole batch is one undo step.
"""

from __future__ import annotations

from ...gen.db.sqlite import DbSQLite
from ...gen.lib.money import Money
from ...gen.services import DueDecision, ResolveDue, resolve_due
from ...presentation import service_error_message
from ..gi_setup import Gtk
from ..widgets.bounded import BoundedWindow
from ..widgets.choice import bounded_dropdown
from ..widgets.help import help_row

__all__ = ["DueDialog"]

POST, LATER, NEVER = 0, 1, 2

_CHOICES = ["Post now", "Remind me later", "Never (mark as done)"]
_DECISIONS = {POST: DueDecision.POST, LATER: DueDecision.DEFER, NEVER: DueDecision.SKIP}
_GROUP_CHOICES = ["Choose each date", "Post all", "Remind me later for all", "Never, all"]


class DueDialog(BoundedWindow):
    """One row per due occurrence, each with its own decision."""

    def __init__(self, parent: Gtk.Window | None, db: DbSQLite, occurrences) -> None:
        super().__init__(title="Scheduled transactions due", transient_for=parent, modal=True)
        self.set_destroy_with_parent(True)
        self.db = db
        groups: dict[str, list] = {}
        for occurrence in occurrences:
            groups.setdefault(occurrence.schedule.handle, []).append(occurrence)
        ordered = sorted(groups.values(), key=lambda items: min(o.when for o in items))
        self.occurrences = [o for items in ordered for o in sorted(items, key=lambda o: o.when)]
        self.choosers: list[Gtk.DropDown] = []
        #: Per-schedule "set every date" choosers, keyed by schedule handle.
        self.group_choosers: dict[str, Gtk.DropDown] = {}
        self.set_default_size(700, 460)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(18)
        self.set_child(box)
        box.prepend(help_row("due-review"))

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

        row = 1
        index = 0
        for items in ordered:
            items = sorted(items, key=lambda o: o.when)
            first = items[0]
            total = sum((o.amount for o in items), Money(0))
            heading = Gtk.Label(
                label=f"{first.name}: {len(items)} due, {total.format()}",
                xalign=0,
            )
            heading.add_css_class("summary-label")
            grid.attach(heading, 0, row, 3, 1)
            if len(items) > 1:
                group = bounded_dropdown(_GROUP_CHOICES)
                group.set_selected(0)
                size = len(items)
                group.connect(
                    "notify::selected",
                    lambda chooser, _pspec, start=index, count=size: self._set_group(
                        chooser, start, count
                    ),
                )
                self.group_choosers[first.schedule.handle] = group
                grid.attach(group, 3, row, 1, 1)
            row += 1
            for occurrence in items:
                due = Gtk.Label(label=occurrence.when.isoformat(), xalign=0)
                if occurrence.when < _today():
                    due.add_css_class("negative")
                    due.set_text(f"{occurrence.when.isoformat()}  (overdue)")
                due.set_margin_start(18)
                grid.attach(due, 0, row, 1, 1)
                grid.attach(Gtk.Label(label=occurrence.name, xalign=0), 1, row, 1, 1)

                amount = Gtk.Label(label=occurrence.amount.format(), xalign=1)
                amount.add_css_class("numeric")
                grid.attach(amount, 2, row, 1, 1)

                chooser = bounded_dropdown(_CHOICES)
                # Undecided by default: the safe answer to an unread question is to
                # ask again, not to write to the ledger.
                chooser.set_selected(LATER)
                self.choosers.append(chooser)
                grid.attach(chooser, 3, row, 1, 1)
                row += 1
                index += 1

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

    def _set_group(self, group: Gtk.DropDown, start: int, count: int) -> None:
        selected = group.get_selected()
        if selected == 0:
            return
        choice = (POST, LATER, NEVER)[selected - 1]
        for chooser in self.choosers[start : start + count]:
            chooser.set_selected(choice)

    def selection(self, choice: int) -> list:
        return [
            occurrence
            for occurrence, chooser in zip(self.occurrences, self.choosers, strict=False)
            if chooser.get_selected() == choice
        ]

    def apply(self) -> tuple[int, int] | None:
        """Carry out the decisions. Returns (posted, skipped), or None if refused."""
        decisions = tuple(
            (occurrence.schedule.handle, occurrence.when, _DECISIONS[chooser.get_selected()])
            for occurrence, chooser in zip(self.occurrences, self.choosers, strict=True)
            if chooser.get_selected() != LATER
        )
        if not decisions:
            return 0, 0
        result = resolve_due(self.db, ResolveDue(decisions, as_of=_today()))
        if not result.ok:
            self.status.set_text(service_error_message(result.errors[0]))
            self.status.add_css_class("negative")
            return None
        assert result.value is not None
        return result.value.posted, result.value.skipped

    def _on_apply(self, _button) -> None:
        outcome = self.apply()
        if outcome is None:
            return
        posted, skipped = outcome
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
