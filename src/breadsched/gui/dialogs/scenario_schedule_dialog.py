"""Edit recurring estimates that belong to one saved scenario.

Scenario schedules never mutate the baseline book schedule they replace.  They are
forecast inputs only: the resolved scenario event stream is consumed by both Plan
and Projection, while actual ledger posting continues to use the baseline book.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

from ...gen.db.sqlite import DbSQLite
from ...gen.lib import (
    AccountClass,
    Money,
    PeriodType,
    Recurrence,
    Scenario,
    ScenarioSchedule,
    ScheduledSplit,
    ScheduledTransaction,
    WeekendAdjust,
)
from ..gi_setup import Gtk

__all__ = ["ScenarioScheduleDialog", "ScenarioSchedulePickerDialog"]

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


class ScenarioScheduleDialog(Gtk.Window):
    """Create a scenario estimate or replace one baseline schedule."""

    def __init__(
        self,
        parent: Gtk.Window | None,
        db: DbSQLite,
        scenario: Scenario,
        source: ScheduledTransaction | None = None,
        current: ScenarioSchedule | None = None,
    ) -> None:
        title = "Alternate scheduled transaction" if source else "New scenario estimate"
        super().__init__(title=title, transient_for=parent, modal=True)
        self.db = db
        self.scenario = scenario
        self.source = source
        self.current = current
        initial = current or source
        self.set_default_size(580, 500)
        self._accounts = sorted(
            (
                account
                for account in db.iter_accounts()
                if not account.is_root and not account.placeholder
            ),
            key=db.full_name,
        )
        self._names = [db.full_name(account) for account in self._accounts]
        self._frequency_options = list(_FREQUENCIES)
        if initial is not None:
            recurrence = initial.recurrence
            key = (recurrence.period, recurrence.interval)
            if not any((period, interval) == key for _label, period, interval in _FREQUENCIES):
                self._frequency_options.insert(
                    0,
                    (recurrence.describe(), recurrence.period, recurrence.interval),
                )

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(18)
        self.set_child(box)

        explanation = Gtk.Label(xalign=0, wrap=True)
        explanation.add_css_class("dim")
        if source is None:
            explanation.set_text(
                f"This recurring estimate belongs only to scenario “{scenario.name}”. "
                "It affects Plan and Projection but is never posted to the ledger."
            )
        else:
            explanation.set_text(
                f"This replaces “{source.name}” only while scenario “{scenario.name}” "
                "is selected. The baseline schedule is not changed."
            )
        box.append(explanation)

        grid = Gtk.Grid(column_spacing=10, row_spacing=8)
        box.append(grid)
        row = 0

        self.name_entry = Gtk.Entry(placeholder_text="Groceries")
        self.name_entry.set_hexpand(True)
        self.name_entry.connect("changed", self._validate)
        grid.attach(Gtk.Label(label="Name", xalign=0), 0, row, 1, 1)
        grid.attach(self.name_entry, 1, row, 1, 1)
        row += 1

        self.category = Gtk.DropDown.new_from_strings(self._names)
        self.category.connect("notify::selected", self._validate)
        grid.attach(Gtk.Label(label="Income / expense category", xalign=0), 0, row, 1, 1)
        grid.attach(self.category, 1, row, 1, 1)
        row += 1

        self.funding = Gtk.DropDown.new_from_strings(self._names)
        self.funding.connect("notify::selected", self._validate)
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

        self.frequency = Gtk.DropDown.new_from_strings(
            [item[0] for item in self._frequency_options]
        )
        default_frequency = next(
            (
                index
                for index, (_label, period, interval) in enumerate(self._frequency_options)
                if period is PeriodType.MONTH and interval == 1
            ),
            0,
        )
        self.frequency.set_selected(default_frequency)
        self.frequency.connect("notify::selected", self._validate)
        grid.attach(Gtk.Label(label="Frequency", xalign=0), 0, row, 1, 1)
        grid.attach(self.frequency, 1, row, 1, 1)
        row += 1

        self.start_entry = Gtk.Entry(text=date.today().replace(day=1).isoformat())
        self.start_entry.connect("changed", self._validate)
        grid.attach(Gtk.Label(label="First occurrence", xalign=0), 0, row, 1, 1)
        grid.attach(self.start_entry, 1, row, 1, 1)
        row += 1

        self.weekend = Gtk.DropDown.new_from_strings([item[0] for item in _WEEKEND])
        grid.attach(Gtk.Label(label="If it falls on a weekend", xalign=0), 0, row, 1, 1)
        grid.attach(self.weekend, 1, row, 1, 1)

        self.preview = Gtk.Label(xalign=0, wrap=True)
        self.preview.add_css_class("dim")
        box.append(self.preview)
        self.status = Gtk.Label(xalign=0)
        box.append(self.status)

        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        buttons.append(cancel)
        self.save_button = Gtk.Button(label="Save scenario change")
        self.save_button.add_css_class("suggested-action")
        self.save_button.connect("clicked", self._on_save)
        buttons.append(self.save_button)
        box.append(buttons)

        if initial is not None:
            self._load_source(initial)
        self._validate()

    def _account_index(self, handle: str) -> int | None:
        return next(
            (index for index, account in enumerate(self._accounts) if account.handle == handle),
            None,
        )

    def _load_source(self, source: ScheduledTransaction | ScenarioSchedule) -> None:
        self.name_entry.set_text(source.name)
        self.start_entry.set_text(source.recurrence.start.isoformat())
        frequency_index = next(
            (
                index
                for index, (_label, period, interval) in enumerate(self._frequency_options)
                if period is source.recurrence.period and interval == source.recurrence.interval
            ),
            0,
        )
        self.frequency.set_selected(frequency_index)
        weekend_index = next(
            (
                index
                for index, (_label, adjust) in enumerate(_WEEKEND)
                if adjust is source.recurrence.weekend_adjust
            ),
            0,
        )
        self.weekend.set_selected(weekend_index)

        flow_split = None
        other_split = None
        for split in source.splits:
            account = self.db.get_account(split.account)
            if account is not None and account.account_class in (
                AccountClass.INCOME,
                AccountClass.EXPENSE,
            ):
                flow_split = split
                break
        if flow_split is not None:
            other_split = next(
                (split for split in source.splits if split is not flow_split),
                None,
            )
        if (
            flow_split is None
            or other_split is None
            or len(source.splits) != 2
            or any(split.formula for split in source.splits)
        ):
            self.status.set_text(
                "This schedule has a complex split structure. Suppression is supported, "
                "but the simple alternate editor currently requires exactly one income or "
                "expense split and one funding split."
            )
            self.status.add_css_class("negative")
            self.save_button.set_sensitive(False)
            return

        category_index = self._account_index(flow_split.account)
        funding_index = self._account_index(other_split.account)
        if category_index is not None:
            self.category.set_selected(category_index)
        if funding_index is not None:
            self.funding.set_selected(funding_index)
        category = self.db.get_account(flow_split.account)
        amount = flow_split.resolve(source.variables)
        if category is not None:
            amount = amount * category.sign()
        self.amount_entry.set_text(abs(amount).format(grouping=False))

    def _recurrence(self) -> Recurrence | None:
        try:
            start = date.fromisoformat(self.start_entry.get_text().strip())
        except ValueError:
            return None
        _label, period, interval = self._frequency_options[self.frequency.get_selected()]
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
            amount = Money(text)
        except (ValueError, ArithmeticError):
            return None
        return abs(amount) if amount else None

    def _validate(self, *_args) -> None:
        problems = []
        if not self.name_entry.get_text().strip():
            problems.append("give it a name")
        amount = self._amount()
        if amount is None:
            problems.append("enter an amount")
        recurrence = self._recurrence()
        if recurrence is None:
            problems.append("check the date (YYYY-MM-DD)")
        if self.category.get_selected() == self.funding.get_selected():
            problems.append("choose two different accounts")
        category = self._accounts[self.category.get_selected()]
        if category.account_class not in (AccountClass.INCOME, AccountClass.EXPENSE):
            problems.append("choose an income or expense category")
        initial = self.current or self.source
        if initial is not None and (
            len(initial.splits) != 2 or any(split.formula for split in initial.splits)
        ):
            problems.append("complex schedules can only be suppressed for now")

        self.save_button.set_sensitive(not problems)
        self.status.set_text("; ".join(problems).capitalize() if problems else "")
        if problems:
            self.preview.set_text("")
        elif recurrence is not None:
            end = date(recurrence.start.year + 1, recurrence.start.month, 1)
            upcoming = recurrence.occurrences(end)[:4]
            self.preview.set_text("Next: " + ", ".join(item.isoformat() for item in upcoming))

    def build(self) -> ScenarioSchedule:
        """Build the scenario-owned recurring estimate described by the form."""
        amount = self._amount()
        recurrence = self._recurrence()
        assert amount is not None and recurrence is not None
        category = self._accounts[self.category.get_selected()]
        funding = self._accounts[self.funding.get_selected()]
        signed = amount * category.sign()
        placeholder = True
        if self.current is not None:
            placeholder = self.current.placeholder
        elif self.source is not None:
            placeholder = self.source.placeholder
        return ScenarioSchedule(
            name=self.name_entry.get_text().strip(),
            recurrence=recurrence,
            splits=[
                ScheduledSplit(category.handle, signed),
                ScheduledSplit(funding.handle, -signed),
            ],
            source_schedule=self.source.handle if self.source is not None else None,
            enabled=True,
            placeholder=placeholder,
        )

    def _on_save(self, _button) -> None:
        change = self.build()
        if change.source_schedule is not None:
            self.scenario.schedule_overrides = [
                existing
                for existing in self.scenario.schedule_overrides
                if existing.source_schedule != change.source_schedule
            ]
        self.scenario.schedule_overrides.append(change)
        with self.db.transaction(f"Update scenario {self.scenario.name}") as txn:
            self.db.commit_scenario(self.scenario, txn)
        self.close()


class ScenarioSchedulePickerDialog(Gtk.Window):
    """Choose a baseline schedule before altering or suppressing it."""

    def __init__(
        self,
        parent: Gtk.Window | None,
        db: DbSQLite,
        title: str,
        action_label: str,
        callback: Callable[[ScheduledTransaction], None],
    ) -> None:
        super().__init__(title=title, transient_for=parent, modal=True)
        self._callback = callback
        self._schedules = list(db.iter_scheduled())
        self.set_default_size(460, 180)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(18)
        self.set_child(box)
        box.append(Gtk.Label(label="Baseline scheduled transaction", xalign=0))
        self.schedule = Gtk.DropDown.new_from_strings(
            [item.name for item in self._schedules] or ["(none available)"]
        )
        box.append(self.schedule)

        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        buttons.append(cancel)
        apply = Gtk.Button(label=action_label)
        apply.add_css_class("suggested-action")
        apply.set_sensitive(bool(self._schedules))
        apply.connect("clicked", self._on_apply)
        buttons.append(apply)
        box.append(buttons)

    def _on_apply(self, _button) -> None:
        if not self._schedules:
            return
        selected = self._schedules[self.schedule.get_selected()]
        self.close()
        self._callback(selected)
