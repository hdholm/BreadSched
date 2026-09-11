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
    PlanningFlowKind,
    Recurrence,
    Scenario,
    ScenarioSchedule,
    ScheduledAmountChange,
    ScheduledOccurrenceAdjustment,
    ScheduledSplit,
    ScheduledTransaction,
    WeekendAdjust,
    scheduled_occurrence_preview,
)
from ..gi_setup import Gtk
from ..widgets.schedule_timeline import (
    DatedAmountListEditor,
    DateListEditor,
    PlanningSplitListEditor,
)

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

_PLANNING_FLOWS = [
    ("Ordinary transfer", None),
    ("Retirement saving", PlanningFlowKind.RETIREMENT_SAVING),
    ("Benefit / FSA funding", PlanningFlowKind.BENEFIT_FUNDING),
    ("Debt principal", PlanningFlowKind.DEBT_PRINCIPAL),
    ("Retirement distribution", PlanningFlowKind.RETIREMENT_INCOME),
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
        self._constructing = True
        initial = current or source
        self._formula_mode = bool(
            initial is not None and any(split.formula for split in initial.splits)
        )
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

        self.planning_flow = Gtk.DropDown.new_from_strings(
            [label for label, _kind in _PLANNING_FLOWS]
        )
        grid.attach(Gtk.Label(label="Planning purpose override", xalign=0), 0, row, 1, 1)
        grid.attach(self.planning_flow, 1, row, 1, 1)
        row += 1

        self.amount_entry = Gtk.Entry(placeholder_text="0.00")
        self.amount_entry.connect("changed", self._validate)
        grid.attach(Gtk.Label(label="Amount", xalign=0), 0, row, 1, 1)
        grid.attach(self.amount_entry, 1, row, 1, 1)
        row += 1

        self.additional_splits = PlanningSplitListEditor(
            self._validate,
            self._names,
            [label for label, _kind in _PLANNING_FLOWS],
        )
        label = Gtk.Label(label="Additional splits", xalign=0, valign=Gtk.Align.START)
        label.set_tooltip_text(
            "Add fixed payroll deductions, retirement/FSA funding, debt principal, "
            "or other legs. The paid-from/into split is balanced automatically."
        )
        grid.attach(label, 0, row, 1, 1)
        grid.attach(self.additional_splits, 1, row, 1, 1)
        row += 1

        self.amount_changes_editor = DatedAmountListEditor(
            self._validate, "Add future amount"
        )
        label = Gtk.Label(label="Future amounts", xalign=0, valign=Gtk.Align.START)
        grid.attach(label, 0, row, 1, 1)
        grid.attach(self.amount_changes_editor, 1, row, 1, 1)
        row += 1

        self.skipped_editor = DateListEditor(
            self._validate, "Add skipped occurrence", self._occurrence_options
        )
        label = Gtk.Label(label="Skip occurrences", xalign=0, valign=Gtk.Align.START)
        grid.attach(label, 0, row, 1, 1)
        grid.attach(self.skipped_editor, 1, row, 1, 1)
        row += 1

        self.occurrence_adjustments_editor = DatedAmountListEditor(
            self._validate,
            "Add one-time amount",
            self._occurrence_options,
        )
        label = Gtk.Label(label="One-time amounts", xalign=0, valign=Gtk.Align.START)
        grid.attach(label, 0, row, 1, 1)
        grid.attach(self.occurrence_adjustments_editor, 1, row, 1, 1)
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
        self.frequency.connect("notify::selected", self._recurrence_changed)
        grid.attach(Gtk.Label(label="Frequency", xalign=0), 0, row, 1, 1)
        grid.attach(self.frequency, 1, row, 1, 1)
        row += 1

        self.start_entry = Gtk.Entry(text=date.today().replace(day=1).isoformat())
        self.start_entry.connect("changed", self._recurrence_changed)
        grid.attach(Gtk.Label(label="First occurrence", xalign=0), 0, row, 1, 1)
        grid.attach(self.start_entry, 1, row, 1, 1)
        row += 1

        self.ends = Gtk.DropDown.new_from_strings(
            ["Never", "On date", "After occurrences"]
        )
        self.ends.connect("notify::selected", self._recurrence_changed)
        grid.attach(Gtk.Label(label="Ends", xalign=0), 0, row, 1, 1)
        grid.attach(self.ends, 1, row, 1, 1)
        row += 1

        self.end_entry = Gtk.Entry(placeholder_text="YYYY-MM-DD")
        self.end_entry.connect("changed", self._recurrence_changed)
        grid.attach(Gtk.Label(label="End date", xalign=0), 0, row, 1, 1)
        grid.attach(self.end_entry, 1, row, 1, 1)
        row += 1

        self.count_entry = Gtk.Entry(placeholder_text="12")
        self.count_entry.connect("changed", self._recurrence_changed)
        grid.attach(Gtk.Label(label="Occurrences", xalign=0), 0, row, 1, 1)
        grid.attach(self.count_entry, 1, row, 1, 1)
        row += 1

        self.weekend = Gtk.DropDown.new_from_strings([item[0] for item in _WEEKEND])
        self.weekend.connect("notify::selected", self._recurrence_changed)
        grid.attach(Gtk.Label(label="If it falls on a weekend", xalign=0), 0, row, 1, 1)
        grid.attach(self.weekend, 1, row, 1, 1)

        self.protected_details = Gtk.Label(xalign=0, wrap=True, selectable=True)
        self.protected_details.add_css_class("dim")
        self.protected_details.set_visible(False)
        box.append(self.protected_details)

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
        self._constructing = False
        self._validate()

    def _account_index(self, handle: str) -> int | None:
        return next(
            (index for index, account in enumerate(self._accounts) if account.handle == handle),
            None,
        )

    def _load_source(self, source: ScheduledTransaction | ScenarioSchedule) -> None:
        self.name_entry.set_text(source.name)
        self.start_entry.set_text(source.recurrence.start.isoformat())
        if source.recurrence.end is not None:
            self.ends.set_selected(1)
            self.end_entry.set_text(source.recurrence.end.isoformat())
        elif source.recurrence.count is not None:
            self.ends.set_selected(2)
            self.count_entry.set_text(str(source.recurrence.count))
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

        if self._formula_mode:
            self.skipped_editor.set_values(source.skipped)
            self._protect_formula_fields(source)
            return

        flow_split = None
        for split in source.splits:
            account = self.db.get_account(split.account)
            if account is not None and account.account_class in (
                AccountClass.INCOME,
                AccountClass.EXPENSE,
            ):
                flow_split = split
                break
        if flow_split is None or any(split.formula for split in source.splits):
            self.status.set_text(
                "This schedule uses formulas or has no income/expense anchor. "
                "Suppression is supported, but this fixed-split editor cannot rewrite it."
            )
            self.status.add_css_class("negative")
            self.save_button.set_sensitive(False)
            return
        others = [split for split in source.splits if split is not flow_split]
        funding_split = next(
            (
                split
                for split in others
                if (account := self.db.get_account(split.account)) is not None
                and account.account_class not in (AccountClass.INCOME, AccountClass.EXPENSE)
                and split.planning_flow is None
            ),
            others[-1] if others else None,
        )
        if funding_split is None:
            self.status.set_text("This schedule has no identifiable funding/net-cash split.")
            self.save_button.set_sensitive(False)
            return

        category_index = self._account_index(flow_split.account)
        funding_index = self._account_index(funding_split.account)
        if category_index is not None:
            self.category.set_selected(category_index)
        if funding_index is not None:
            self.funding.set_selected(funding_index)
        self.planning_flow.set_selected(
            next(
                (
                    index
                    for index, (_label, kind) in enumerate(_PLANNING_FLOWS)
                    if kind is funding_split.planning_flow
                ),
                0,
            )
        )
        category = self.db.get_account(flow_split.account)
        amount = flow_split.resolve(source.variables)
        if category is not None:
            amount = amount * category.sign()
        self.amount_entry.set_text(abs(amount).format())
        extra_values = []
        for split in others:
            if split is funding_split:
                continue
            account = self.db.get_account(split.account)
            account_index = self._account_index(split.account)
            if account is None or account_index is None:
                continue
            purpose_index = next(
                (
                    index
                    for index, (_label, kind) in enumerate(_PLANNING_FLOWS)
                    if kind is split.planning_flow
                ),
                0,
            )
            resolved = split.resolve(source.variables)
            normal_amount = (
                split.planning_flow.plan_amount(resolved)
                if split.planning_flow is not None
                else resolved * account.sign()
            )
            direction_index = (
                1 if split.planning_flow is None and normal_amount < 0 else 0
            )
            extra_values.append(
                (
                    account_index,
                    str(abs(normal_amount).to_decimal()),
                    purpose_index,
                    split.memo or "",
                    direction_index,
                )
            )
        self.additional_splits.set_values(extra_values)
        self.amount_changes_editor.set_values(
            (item.start, str(item.amount.to_decimal())) for item in source.amount_changes
        )
        self.skipped_editor.set_values(source.skipped)
        self.occurrence_adjustments_editor.set_values(
            (item.when, str(item.amount.to_decimal()))
            for item in source.occurrence_adjustments
        )


    def _protect_formula_fields(
        self, source: ScheduledTransaction | ScenarioSchedule
    ) -> None:
        """Expose scenario metadata edits without rewriting formula-owned values."""
        protected = (
            self.category,
            self.funding,
            self.planning_flow,
            self.amount_entry,
            self.additional_splits,
            self.amount_changes_editor,
            self.occurrence_adjustments_editor,
        )
        for widget in protected:
            widget.set_sensitive(False)
        lines = [
            "Formula expressions, variables, split accounts, and formula-derived "
            "amounts are protected. Name, recurrence, and skipped occurrences may "
            "be edited without changing them.",
            "",
            "Protected splits:",
        ]
        for index, split in enumerate(source.splits, 1):
            account = self.db.get_account(split.account)
            account_name = self.db.full_name(account) if account is not None else split.account
            value = f"formula {split.formula!r}" if split.formula else str(split.amount or Money(0))
            lines.append(f"  {index}. {account_name}: {value}")
        if source.variables:
            lines.extend(["", "Formula variables:"])
            lines.extend(
                f"  {key} = {value}" for key, value in sorted(source.variables.items())
            )
        self.protected_details.set_text("\n".join(lines))
        self.protected_details.set_visible(True)
        self.preview.set_visible(False)

    def _recurrence(self) -> Recurrence | None:
        try:
            start = date.fromisoformat(self.start_entry.get_text().strip())
        except ValueError:
            return None
        _label, period, interval = self._frequency_options[self.frequency.get_selected()]
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
        day_of_month = None
        second_day_of_month = None
        initial = self.current or self.source
        if (
            initial is not None
            and period is initial.recurrence.period
            and interval == initial.recurrence.interval
        ):
            day_of_month = initial.recurrence.day_of_month
            second_day_of_month = initial.recurrence.second_day_of_month
        return Recurrence(
            period=period,
            interval=interval,
            start=start,
            end=end,
            count=count,
            day_of_month=day_of_month,
            second_day_of_month=second_day_of_month,
            weekend_adjust=_WEEKEND[self.weekend.get_selected()][1],
        )

    def _occurrence_options(self) -> list[date]:
        recurrence = self._recurrence()
        if recurrence is None:
            return []
        horizon = date(min(recurrence.start.year + 10, 9999), 12, 31)
        return recurrence.occurrences(horizon)[:500]

    def _recurrence_changed(self, *_args) -> None:
        self.skipped_editor.refresh_date_choices()
        self.occurrence_adjustments_editor.refresh_date_choices()
        self._validate()

    def _amount(self) -> Money | None:
        text = self.amount_entry.get_text().strip()
        if not text:
            return None
        try:
            amount = Money(text)
        except (ValueError, ArithmeticError):
            return None
        return abs(amount) if amount else None


    def _amount_changes(self) -> list[ScheduledAmountChange] | None:
        values = self.amount_changes_editor.values()
        if not values:
            return []
        changes = []
        try:
            for when_text, amount_text in values:
                when = date.fromisoformat(when_text)
                amount = Money(amount_text)
                if amount <= 0:
                    return None
                changes.append(ScheduledAmountChange(when, amount))
        except (ValueError, ArithmeticError):
            return None
        if len({item.start for item in changes}) != len(changes):
            return None
        return sorted(changes, key=lambda item: item.start)

    def _skipped(self, recurrence: Recurrence | None) -> list[date] | None:
        values = self.skipped_editor.values()
        if not values:
            return []
        if recurrence is None:
            return None
        try:
            skipped = [date.fromisoformat(raw) for raw in values]
        except ValueError:
            return None
        if len(set(skipped)) != len(skipped):
            return None
        if any(when not in recurrence.occurrences(when, since=when) for when in skipped):
            return None
        return sorted(skipped)

    def _occurrence_adjustments(
        self, recurrence: Recurrence | None
    ) -> list[ScheduledOccurrenceAdjustment] | None:
        values = self.occurrence_adjustments_editor.values()
        if not values:
            return []
        if recurrence is None:
            return None
        changes = []
        try:
            for when_text, amount_text in values:
                when = date.fromisoformat(when_text)
                amount = Money(amount_text)
                if amount <= 0:
                    return None
                changes.append(ScheduledOccurrenceAdjustment(when, amount))
        except (ValueError, ArithmeticError):
            return None
        if len({item.when for item in changes}) != len(changes):
            return None
        if any(
            item.when not in recurrence.occurrences(item.when, since=item.when)
            for item in changes
        ):
            return None
        return sorted(changes, key=lambda item: item.when)

    def _validate(self, *_args) -> None:
        if self._constructing:
            return
        problems = []
        if not self.name_entry.get_text().strip():
            problems.append("give it a name")
        amount = self._amount()
        amount_changes = self._amount_changes()
        if not self._formula_mode:
            if amount is None:
                problems.append("enter an amount")
            if amount_changes is None:
                problems.append("check future amounts")
        recurrence = self._recurrence()
        skipped = self._skipped(recurrence)
        if skipped is None:
            problems.append("check skipped occurrence dates")
        adjustments = self._occurrence_adjustments(recurrence)
        if not self._formula_mode:
            if adjustments is None:
                problems.append("check one-time amounts")
            if skipped is not None and adjustments is not None:
                if set(skipped) & {item.when for item in adjustments}:
                    problems.append("an occurrence cannot be both skipped and overridden")
        period = self._frequency_options[self.frequency.get_selected()][1]
        bounded = period is not PeriodType.ONCE
        self.ends.set_sensitive(bounded)
        self.end_entry.set_sensitive(bounded and self.ends.get_selected() == 1)
        self.count_entry.set_sensitive(bounded and self.ends.get_selected() == 2)
        if recurrence is None:
            problems.append("check the schedule dates/count")
        if not self._formula_mode:
            if self.category.get_selected() == self.funding.get_selected():
                problems.append("choose two different accounts")
            category = self._accounts[self.category.get_selected()]
            if category.account_class not in (AccountClass.INCOME, AccountClass.EXPENSE):
                problems.append("choose an income or expense category")
            selected_accounts = {self.category.get_selected(), self.funding.get_selected()}
            for (
                account_index, raw_amount, _purpose_index, _memo, _direction
            ) in self.additional_splits.values():
                if account_index in selected_accounts:
                    problems.append("each additional split needs a different account")
                    break
                selected_accounts.add(account_index)
                try:
                    extra_amount = Money(raw_amount)
                except (ValueError, ArithmeticError):
                    problems.append("check additional split amounts")
                    break
                if extra_amount <= 0:
                    problems.append("additional split amounts must be greater than zero")
                    break

        self.save_button.set_sensitive(not problems)
        self.status.set_text("; ".join(problems).capitalize() if problems else "")
        if problems or self._formula_mode:
            self.preview.set_text("")
        elif recurrence is not None:
            assert amount is not None
            rows = scheduled_occurrence_preview(
                recurrence,
                amount,
                amount_changes or [],
                skipped or [],
                adjustments or [],
            )
            self.preview.set_text(
                "Upcoming:\n"
                + "\n".join(
                    f"{when.isoformat()}   {value.format()}   {status}"
                    for when, value, status in rows
                )
            )

    def build(self) -> ScenarioSchedule:
        """Build the scenario-owned recurring estimate described by the form."""
        recurrence = self._recurrence()
        assert recurrence is not None
        if self._formula_mode:
            initial = self.current or self.source
            assert initial is not None
            if isinstance(initial, ScenarioSchedule):
                change = ScenarioSchedule.from_dict(initial.serialize())
            else:
                change = ScenarioSchedule.from_scheduled(initial)
            old_name = change.name
            change.name = self.name_entry.get_text().strip()
            if change.description == old_name:
                change.description = change.name
            change.recurrence = recurrence
            change.skipped = self._skipped(recurrence) or []
            if self.source is not None:
                change.source_schedule = self.source.handle
            return change

        amount = self._amount()
        assert amount is not None
        category = self._accounts[self.category.get_selected()]
        funding = self._accounts[self.funding.get_selected()]
        signed = amount * category.sign()
        extra_splits = []
        extra_total = Money(0)
        for (
            account_index, raw_amount, purpose_index, memo, direction_index
        ) in self.additional_splits.values():
            account = self._accounts[account_index]
            extra_amount = Money(raw_amount)
            purpose = _PLANNING_FLOWS[purpose_index][1]
            value = (
                purpose.ledger_amount(extra_amount)
                if purpose is not None
                else extra_amount * account.sign() * (-1 if direction_index == 1 else 1)
            )
            extra_total = extra_total + value
            extra_splits.append(
                ScheduledSplit(account.handle, value, memo=memo, planning_flow=purpose)
            )
        funding_value = -(signed + extra_total)
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
                *extra_splits,
                ScheduledSplit(
                    funding.handle,
                    funding_value,
                    planning_flow=_PLANNING_FLOWS[
                        self.planning_flow.get_selected()
                    ][1],
                ),
            ],
            source_schedule=self.source.handle if self.source is not None else None,
            enabled=True,
            placeholder=placeholder,
            amount_changes=self._amount_changes() or [],
            seasonal_amounts=list(
                self.current.seasonal_amounts
                if self.current is not None
                else self.source.seasonal_amounts if self.source is not None else []
            ),
            skipped=self._skipped(recurrence) or [],
            occurrence_adjustments=self._occurrence_adjustments(recurrence) or [],
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
