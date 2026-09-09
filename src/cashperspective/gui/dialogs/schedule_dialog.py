"""Create a scheduled transaction, or a budget-only estimate.

The two are the same object with one flag between them, and the dialog says so
plainly rather than hiding it: a *commitment* is posted to the ledger when it falls
due, while an *estimate* shapes budgets and forecasts and is never posted. Choosing
wrongly is the difference between a forecast and a fabricated ledger, so the choice
is made explicitly here rather than inferred.

The recurrence controls are the ones that change which month a payment lands in --
frequency, interval, start date, and weekend adjustment. Those are the settings a
cash-flow forecast is actually sensitive to.
"""

from __future__ import annotations

from datetime import date

from ...gen.db.sqlite import DbSQLite
from ...gen.lib import (
    Money,
    PeriodType,
    Recurrence,
    ScheduledSplit,
    ScheduledTransaction,
    WeekendAdjust,
)
from ..gi_setup import Gtk

__all__ = ["ScheduleDialog"]

_FREQUENCIES = [
    ("Weekly", PeriodType.WEEK, 1),
    ("Fortnightly", PeriodType.WEEK, 2),
    ("Twice a month", PeriodType.SEMI_MONTH, 1),
    ("Monthly", PeriodType.MONTH, 1),
    ("Quarterly", PeriodType.MONTH, 3),
    ("Twice a year", PeriodType.MONTH, 6),
    ("Yearly", PeriodType.YEAR, 1),
    ("One off", PeriodType.ONCE, 1),
]

_WEEKEND = [
    ("Leave on the day", WeekendAdjust.NONE),
    ("Move to the Friday before", WeekendAdjust.PREVIOUS),
    ("Move to the Monday after", WeekendAdjust.NEXT),
]


class ScheduleDialog(Gtk.Window):
    """Enter a recurring transaction."""

    def __init__(self, parent: Gtk.Window | None, db: DbSQLite) -> None:
        super().__init__(
            title="New scheduled transaction", transient_for=parent, modal=True
        )
        self.db = db
        self.set_default_size(560, 520)
        self._accounts = sorted(
            (a for a in db.iter_accounts() if not a.is_root and not a.placeholder),
            key=db.full_name,
        )
        self._names = [db.full_name(a) for a in self._accounts]

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(18)
        self.set_child(box)

        grid = Gtk.Grid(column_spacing=10, row_spacing=8)
        box.append(grid)
        row = 0

        self.name_entry = Gtk.Entry(placeholder_text="Rent")
        self.name_entry.set_hexpand(True)
        self.name_entry.connect("changed", self._validate)
        grid.attach(Gtk.Label(label="Name", xalign=0), 0, row, 1, 1)
        grid.attach(self.name_entry, 1, row, 1, 1)
        row += 1

        self.kind = Gtk.DropDown.new_from_strings(
            ["Commitment - posted to the ledger when due",
             "Estimate - shapes budgets only, never posted"]
        )
        grid.attach(Gtk.Label(label="Kind", xalign=0), 0, row, 1, 1)
        grid.attach(self.kind, 1, row, 1, 1)
        row += 1

        self.category = Gtk.DropDown.new_from_strings(self._names)
        grid.attach(Gtk.Label(label="Category", xalign=0), 0, row, 1, 1)
        grid.attach(self.category, 1, row, 1, 1)
        row += 1

        self.funding = Gtk.DropDown.new_from_strings(self._names)
        if len(self._accounts) > 1:
            self.funding.set_selected(1)
        grid.attach(Gtk.Label(label="Paid from / into", xalign=0), 0, row, 1, 1)
        grid.attach(self.funding, 1, row, 1, 1)
        row += 1

        self.amount_entry = Gtk.Entry(placeholder_text="0.00")
        self.amount_entry.connect("changed", self._validate)
        grid.attach(Gtk.Label(label="Amount", xalign=0), 0, row, 1, 1)
        grid.attach(self.amount_entry, 1, row, 1, 1)
        row += 1

        self.frequency = Gtk.DropDown.new_from_strings([f[0] for f in _FREQUENCIES])
        self.frequency.set_selected(3)
        self.frequency.connect("notify::selected", self._validate)
        grid.attach(Gtk.Label(label="Frequency", xalign=0), 0, row, 1, 1)
        grid.attach(self.frequency, 1, row, 1, 1)
        row += 1

        self.start_entry = Gtk.Entry(text=date.today().replace(day=1).isoformat())
        self.start_entry.connect("changed", self._validate)
        grid.attach(Gtk.Label(label="First due", xalign=0), 0, row, 1, 1)
        grid.attach(self.start_entry, 1, row, 1, 1)
        row += 1

        self.weekend = Gtk.DropDown.new_from_strings([w[0] for w in _WEEKEND])
        self.weekend.set_tooltip_text(
            "A payment moved off a weekend can land in a different month, which "
            "changes the forecast for both."
        )
        grid.attach(Gtk.Label(label="If it falls on a weekend", xalign=0), 0, row, 1, 1)
        grid.attach(self.weekend, 1, row, 1, 1)
        row += 1

        self.auto_check = Gtk.CheckButton(
            label="Post automatically once the date arrives"
        )
        grid.attach(self.auto_check, 1, row, 1, 1)

        self.preview = Gtk.Label(xalign=0, wrap=True)
        self.preview.add_css_class("dim")
        box.append(self.preview)

        self.status = Gtk.Label(xalign=0)
        box.append(self.status)

        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        buttons.append(cancel)
        self.save_button = Gtk.Button(label="Save")
        self.save_button.add_css_class("suggested-action")
        self.save_button.set_sensitive(False)
        self.save_button.connect("clicked", self._on_save)
        buttons.append(self.save_button)
        box.append(buttons)

        self._validate()

    # ------------------------------------------------------------- validation

    def _recurrence(self) -> Recurrence | None:
        try:
            start = date.fromisoformat(self.start_entry.get_text().strip())
        except ValueError:
            return None
        _label, period, interval = _FREQUENCIES[self.frequency.get_selected()]
        return Recurrence(
            period=period,
            interval=interval,
            start=start,
            weekend_adjust=_WEEKEND[self.weekend.get_selected()][1],
        )

    def _amount(self) -> Money | None:
        text = self.amount_entry.get_text().strip()
        if not text:
            return None
        try:
            value = Money(text)
        except (ValueError, ArithmeticError):
            return None
        return value if value else None

    def _validate(self, *_args) -> None:
        problems = []
        if not self.name_entry.get_text().strip():
            problems.append("give it a name")
        if self._amount() is None:
            problems.append("enter an amount")
        recurrence = self._recurrence()
        if recurrence is None:
            problems.append("check the date (YYYY-MM-DD)")
        if self.category.get_selected() == self.funding.get_selected():
            problems.append("choose two different accounts")

        self.save_button.set_sensitive(not problems)
        self.status.set_text("; ".join(problems).capitalize() if problems else "")

        if recurrence is not None and not problems:
            upcoming = recurrence.occurrences(
                date(recurrence.start.year + 1, recurrence.start.month, 1)
            )[:4]
            self.preview.set_text(
                "Next: " + ", ".join(d.isoformat() for d in upcoming)
                if upcoming else ""
            )
        else:
            self.preview.set_text("")

    # ----------------------------------------------------------------- saving

    def build(self) -> ScheduledTransaction:
        """The schedule the current form describes."""
        amount = self._amount()
        assert amount is not None
        category = self._accounts[self.category.get_selected()]
        funding = self._accounts[self.funding.get_selected()]
        recurrence = self._recurrence()
        assert recurrence is not None

        schedule = ScheduledTransaction(
            name=self.name_entry.get_text().strip(),
            recurrence=recurrence,
            splits=[
                ScheduledSplit(category.handle, amount * category.sign()),
                ScheduledSplit(funding.handle, -(amount * category.sign())),
            ],
            auto_create=self.auto_check.get_active(),
        )
        schedule.placeholder = self.kind.get_selected() == 1
        return schedule

    def _on_save(self, _button) -> None:
        schedule = self.build()
        with self.db.transaction(f"Add scheduled {schedule.name}") as txn:
            self.db.add_scheduled(schedule, txn)
        self.close()
