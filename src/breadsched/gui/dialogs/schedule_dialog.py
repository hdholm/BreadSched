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

    def __init__(
        self,
        parent: Gtk.Window | None,
        db: DbSQLite,
        source: ScheduledTransaction | None = None,
    ) -> None:
        super().__init__(
            title=(
                "Edit scheduled transaction" if source else "New scheduled transaction"
            ),
            transient_for=parent,
            modal=True,
        )
        self.db = db
        self.source = source
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

        self.ends = Gtk.DropDown.new_from_strings(
            ["Never", "On date", "After occurrences"]
        )
        self.ends.connect("notify::selected", self._validate)
        grid.attach(Gtk.Label(label="Ends", xalign=0), 0, row, 1, 1)
        grid.attach(self.ends, 1, row, 1, 1)
        row += 1

        self.end_entry = Gtk.Entry(placeholder_text="YYYY-MM-DD")
        self.end_entry.connect("changed", self._validate)
        grid.attach(Gtk.Label(label="End date", xalign=0), 0, row, 1, 1)
        grid.attach(self.end_entry, 1, row, 1, 1)
        row += 1

        self.count_entry = Gtk.Entry(placeholder_text="12")
        self.count_entry.connect("changed", self._validate)
        grid.attach(Gtk.Label(label="Occurrences", xalign=0), 0, row, 1, 1)
        grid.attach(self.count_entry, 1, row, 1, 1)
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

        if source is not None:
            self._load_source(source)
        self._validate()

    def _load_source(self, source: ScheduledTransaction) -> None:
        """Populate the simple editor from an existing two-split schedule."""
        self.name_entry.set_text(source.name)
        self.kind.set_selected(1 if source.placeholder else 0)
        parts = []
        for split in source.splits:
            account = self.db.get_account(split.account)
            if account is None or split.formula:
                continue
            parts.append((account, split))
        flow = next(
            (
                item
                for item in parts
                if item[0].account_class.value in {"income", "expense"}
            ),
            None,
        )
        if flow is not None:
            other = next((item for item in parts if item is not flow), None)
            if other is not None:
                flow_account, flow_split = flow
                category_index = next(
                    index
                    for index, account in enumerate(self._accounts)
                    if account.handle == flow_account.handle
                )
                funding_index = next(
                    index
                    for index, account in enumerate(self._accounts)
                    if account.handle == other[0].handle
                )
                self.category.set_selected(category_index)
                self.funding.set_selected(funding_index)
                amount = abs(flow_split.resolve(source.variables) * flow_account.sign())
                self.amount_entry.set_text(str(amount.to_decimal()))

        for index, (_label, period, interval) in enumerate(_FREQUENCIES):
            if (
                source.recurrence.period is period
                and source.recurrence.interval == interval
            ):
                self.frequency.set_selected(index)
                break
        self.start_entry.set_text(source.recurrence.start.isoformat())
        if source.recurrence.end is not None:
            self.ends.set_selected(1)
            self.end_entry.set_text(source.recurrence.end.isoformat())
        elif source.recurrence.count is not None:
            self.ends.set_selected(2)
            self.count_entry.set_text(str(source.recurrence.count))
        for index, (_label, adjustment) in enumerate(_WEEKEND):
            if source.recurrence.weekend_adjust is adjustment:
                self.weekend.set_selected(index)
                break
        self.auto_check.set_active(source.auto_create)

    # ------------------------------------------------------------- validation

    def _recurrence(self) -> Recurrence | None:
        try:
            start = date.fromisoformat(self.start_entry.get_text().strip())
        except ValueError:
            return None
        _label, period, interval = _FREQUENCIES[self.frequency.get_selected()]
        end = None
        count = None
        if period is not PeriodType.ONCE:
            if self.ends.get_selected() == 1:
                try:
                    end = date.fromisoformat(self.end_entry.get_text().strip())
                except ValueError:
                    return None
                if end < start:
                    return None
            elif self.ends.get_selected() == 2:
                try:
                    count = int(self.count_entry.get_text().strip())
                except ValueError:
                    return None
                if count < 1:
                    return None
        return Recurrence(
            period=period,
            interval=interval,
            start=start,
            end=end,
            count=count,
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
        period = _FREQUENCIES[self.frequency.get_selected()][1]
        bounded = period is not PeriodType.ONCE
        self.ends.set_sensitive(bounded)
        self.end_entry.set_sensitive(bounded and self.ends.get_selected() == 1)
        self.count_entry.set_sensitive(bounded and self.ends.get_selected() == 2)
        if recurrence is None:
            problems.append("check the schedule dates/count")
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

        if self.source is None:
            schedule = ScheduledTransaction()
        else:
            schedule = ScheduledTransaction.from_dict(self.source.serialize())
        old_name = schedule.name
        schedule.name = self.name_entry.get_text().strip()
        if self.source is None or schedule.description == old_name:
            schedule.description = schedule.name
        schedule.recurrence = recurrence
        schedule.splits = [
            ScheduledSplit(category.handle, amount * category.sign()),
            ScheduledSplit(funding.handle, -(amount * category.sign())),
        ]
        schedule.auto_create = self.auto_check.get_active()
        schedule.placeholder = self.kind.get_selected() == 1
        return schedule

    def _on_save(self, _button) -> None:
        schedule = self.build()
        action = "Update" if self.source is not None else "Add"
        with self.db.transaction(f"{action} scheduled {schedule.name}") as txn:
            if self.source is None:
                self.db.add_scheduled(schedule, txn)
            else:
                self.db.commit_scheduled(schedule, txn)
        self.close()
