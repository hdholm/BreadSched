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
one of its dates at once, while each date keeps its own answer. Those dates are
listed under an expander and built only when it is opened (#389): a book with
hundreds of missed dates opened slowly when each had its own drop-down at once.

The rows come from the book server's due review (``book.due_review``, #359), each
marked with why it is listed, and Apply goes through the same server
(``book.resolve_due``). It rechecks every chosen date against the book, so a date
posted or skipped elsewhere since this window opened is refused rather than posted
twice, and the whole batch is one undo step. What it posted then opens in
``PostedTransactionsDialog`` to check, edit, or delete (#328).
"""

from __future__ import annotations

from ...api import ApiError
from ...api.local import book_server
from ...gen.db.sqlite import DbSQLite
from ...gen.services import DueDecision, DueItem, DueScheduleReview, ResolveDue
from ..gi_setup import Gtk
from ..widgets.bounded import BoundedWindow
from ..widgets.choice import bounded_dropdown
from ..widgets.help import help_row

__all__ = ["DueDialog"]


class _Choice:
    """One date's answer, with the drop-down that shows it once it is built.

    Behaves like the drop-down for reading and setting the answer, so a date can
    be decided (by its schedule's chooser or "for all") before its row exists.
    """

    def __init__(self, value: int) -> None:
        self.value = value
        self.widget: Gtk.DropDown | None = None

    def get_selected(self) -> int:
        return self.widget.get_selected() if self.widget is not None else self.value

    def set_selected(self, value: int) -> None:
        self.value = value
        if self.widget is not None:
            self.widget.set_selected(value)

    def bind(self, widget: Gtk.DropDown) -> None:
        widget.set_selected(self.value)
        widget.connect("notify::selected", lambda w, _p: setattr(self, "value", w.get_selected()))
        self.widget = widget


POST, LATER, NEVER = 0, 1, 2

_CHOICES = ["Post now", "Remind me later", "Never (mark as done)"]
_DECISIONS = {POST: DueDecision.POST, LATER: DueDecision.DEFER, NEVER: DueDecision.SKIP}
_GROUP_CHOICES = ["Choose each date", "Post all", "Remind me later for all", "Never, all"]
#: How a row says why it is listed, beside its date (#352); a date due today says
#: nothing more.
_STAGE_NOTES = {"overdue": "overdue", "advance": "in advance", "reminder": "reminder"}


class DueDialog(BoundedWindow):
    """One row per due occurrence, each with its own decision."""

    def __init__(
        self, parent: Gtk.Window | None, db: DbSQLite, reviews: list[DueScheduleReview]
    ) -> None:
        super().__init__(title="Scheduled transactions due", transient_for=parent, modal=True)
        self.set_destroy_with_parent(True)
        self.db = db
        self.book = book_server(db)
        ordered = sorted(
            (review for review in reviews if review.items),
            key=lambda review: min(item.when for item in review.items),
        )
        #: Every listed date, in the order of :attr:`choosers`.
        self.items: list[DueItem] = [
            item for review in ordered for item in sorted(review.items, key=lambda i: i.when)
        ]
        self.choosers: list[_Choice] = []
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
                    f"{len(self.items)} scheduled transaction(s) are due, or are "
                    "entered or reminded in advance. Choose what to do with each."
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
        #: Per-schedule expanders of dates, keyed by schedule handle.
        self.expanders: dict[str, Gtk.Expander] = {}
        for review in ordered:
            items = sorted(review.items, key=lambda item: item.when)
            heading = Gtk.Label(
                label=f"{review.name}: {len(items)} to review, {review.total.format()}",
                xalign=0,
            )
            heading.add_css_class("summary-label")
            grid.attach(heading, 0, row, 3, 1)
            choices = [_Choice(LATER) for _item in items]
            self.choosers.extend(choices)
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
                self.group_choosers[review.schedule] = group
                grid.attach(group, 3, row, 1, 1)
                row += 1
                # The dates themselves only when asked for (#389).
                expander = Gtk.Expander(label=f"Show the {len(items)} dates")
                expander.set_margin_start(18)
                dates = Gtk.Grid(column_spacing=14, row_spacing=6)
                expander.set_child(dates)
                expander.connect(
                    "notify::expanded",
                    lambda widget, _pspec, fill=(dates, review, items, choices): (
                        self._fill_dates(*fill) if widget.get_expanded() else None
                    ),
                )
                self.expanders[review.schedule] = expander
                grid.attach(expander, 0, row, 4, 1)
                row += 1
            else:
                row += 1
                self._date_row(grid, row, review, items[0], choices[0])
                row += 1
            index += len(items)

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

    def _date_row(
        self, grid: Gtk.Grid, row: int, review: DueScheduleReview, item: DueItem, choice: _Choice
    ) -> None:
        due = Gtk.Label(label=item.when.isoformat(), xalign=0)
        if item.stage == "overdue":
            due.add_css_class("negative")
        note = _STAGE_NOTES.get(item.stage)
        if note:
            due.set_text(f"{item.when.isoformat()}  ({note})")
        due.set_margin_start(18)
        grid.attach(due, 0, row, 1, 1)
        grid.attach(Gtk.Label(label=review.name, xalign=0), 1, row, 1, 1)
        amount = Gtk.Label(label=item.amount.format(), xalign=1)
        amount.add_css_class("numeric")
        grid.attach(amount, 2, row, 1, 1)
        # Undecided by default: the safe answer to an unread question is to ask
        # again, not to write to the ledger.
        chooser = bounded_dropdown(_CHOICES)
        choice.bind(chooser)
        grid.attach(chooser, 3, row, 1, 1)

    def _fill_dates(
        self, grid: Gtk.Grid, review: DueScheduleReview, items: list, choices: list[_Choice]
    ) -> None:
        if grid.get_first_child() is not None:
            return
        for row, (item, choice) in enumerate(zip(items, choices, strict=True)):
            self._date_row(grid, row, review, item, choice)

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

    def selection(self, choice: int) -> list[DueItem]:
        return [
            item
            for item, chooser in zip(self.items, self.choosers, strict=False)
            if chooser.get_selected() == choice
        ]

    def apply(self) -> tuple[int, int] | None:
        """Carry out the decisions. Returns (posted, skipped), or None if refused.

        The handles of the transactions posted are kept in ``posted_transactions``.
        """
        decisions = tuple(
            (item.schedule, item.when, _DECISIONS[chooser.get_selected()])
            for item, chooser in zip(self.items, self.choosers, strict=True)
            if chooser.get_selected() != LATER
        )
        self.posted_transactions: tuple[str, ...] = ()
        if not decisions:
            return 0, 0
        try:
            outcome = self.book.resolve_due(ResolveDue(decisions, as_of=_today()))
        except ApiError as error:
            self.status.set_text(str(error))
            self.status.add_css_class("negative")
            return None
        self.posted_transactions = outcome.transactions
        return outcome.posted, outcome.skipped

    def _on_apply(self, _button) -> None:
        outcome = self.apply()
        if outcome is None:
            return
        posted, skipped = outcome
        parent = self.get_transient_for()
        self.close()
        if posted:
            # What was just posted, split by split, to check and correct (#328).
            from .posted_dialog import PostedTransactionsDialog

            self.review = PostedTransactionsDialog(
                parent, self.db, self.posted_transactions, skipped=skipped
            )
            self.review.present()
        elif skipped:
            alert = Gtk.AlertDialog(
                message=f"Scheduled transactions: marked {skipped} as done.",
                detail="Undo with Ctrl+Z if that was not what you wanted.",
            )
            alert.show(parent)


def _today():
    from datetime import date

    return date.today()
