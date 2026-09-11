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
    PlanningFlowKind,
    Recurrence,
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

_PLANNING_FLOWS = [
    ("Ordinary transfer", None),
    ("Retirement saving", PlanningFlowKind.RETIREMENT_SAVING),
    ("Benefit / FSA funding", PlanningFlowKind.BENEFIT_FUNDING),
    ("Debt principal", PlanningFlowKind.DEBT_PRINCIPAL),
    ("Retirement distribution", PlanningFlowKind.RETIREMENT_INCOME),
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
            "Add payroll deductions, retirement/FSA funding, debt principal, or "
            "other fixed legs. Amounts are entered in the account's normal direction; "
            "the paid-from/into split is adjusted automatically to keep the transaction balanced."
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

        self.frequency = Gtk.DropDown.new_from_strings([f[0] for f in _FREQUENCIES])
        self.frequency.set_selected(3)
        self.frequency.connect("notify::selected", self._recurrence_changed)
        grid.attach(Gtk.Label(label="Frequency", xalign=0), 0, row, 1, 1)
        grid.attach(self.frequency, 1, row, 1, 1)
        row += 1

        self.start_entry = Gtk.Entry(text=date.today().replace(day=1).isoformat())
        self.start_entry.connect("changed", self._recurrence_changed)
        grid.attach(Gtk.Label(label="First due", xalign=0), 0, row, 1, 1)
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

        self.weekend = Gtk.DropDown.new_from_strings([w[0] for w in _WEEKEND])
        self.weekend.connect("notify::selected", self._recurrence_changed)
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
            flow_account, flow_split = flow
            others = [item for item in parts if item is not flow]
            funding_item = next(
                (
                    item
                    for item in others
                    if item[0].account_class.value not in {"income", "expense"}
                    and item[1].planning_flow is None
                ),
                others[-1] if others else None,
            )
            if funding_item is not None:
                category_index = next(
                    index
                    for index, account in enumerate(self._accounts)
                    if account.handle == flow_account.handle
                )
                funding_index = next(
                    index
                    for index, account in enumerate(self._accounts)
                    if account.handle == funding_item[0].handle
                )
                self.category.set_selected(category_index)
                self.funding.set_selected(funding_index)
                planning_kind = funding_item[1].planning_flow
                self.planning_flow.set_selected(
                    next(
                        (
                            index
                            for index, (_label, kind) in enumerate(_PLANNING_FLOWS)
                            if kind is planning_kind
                        ),
                        0,
                    )
                )
                amount = abs(flow_split.resolve(source.variables) * flow_account.sign())
                self.amount_entry.set_text(str(amount.to_decimal()))
                extra_values = []
                for account, split in others:
                    if split is funding_item:
                        continue
                    account_index = next(
                        index
                        for index, candidate in enumerate(self._accounts)
                        if candidate.handle == account.handle
                    )
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
                    extra_values.append(
                        (account_index, str(normal_amount.to_decimal()), purpose_index)
                    )
                self.additional_splits.set_values(extra_values)

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
        self.amount_changes_editor.set_values(
            (item.start, str(item.amount.to_decimal())) for item in source.amount_changes
        )
        self.skipped_editor.set_values(source.skipped)
        self.occurrence_adjustments_editor.set_values(
            (item.when, str(item.amount.to_decimal()))
            for item in source.occurrence_adjustments
        )

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
            value = Money(text)
        except (ValueError, ArithmeticError):
            return None
        return value if value else None


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
        problems = []
        if not self.name_entry.get_text().strip():
            problems.append("give it a name")
        if self._amount() is None:
            problems.append("enter an amount")
        amount_changes = self._amount_changes()
        if amount_changes is None:
            problems.append("check future amounts")
        recurrence = self._recurrence()
        skipped = self._skipped(recurrence)
        if skipped is None:
            problems.append("check skipped occurrence dates")
        adjustments = self._occurrence_adjustments(recurrence)
        if adjustments is None:
            problems.append("check one-time amounts")
        if skipped is not None and adjustments is not None:
            if set(skipped) & {item.when for item in adjustments}:
                problems.append("an occurrence cannot be both skipped and overridden")
        period = _FREQUENCIES[self.frequency.get_selected()][1]
        bounded = period is not PeriodType.ONCE
        self.ends.set_sensitive(bounded)
        self.end_entry.set_sensitive(bounded and self.ends.get_selected() == 1)
        self.count_entry.set_sensitive(bounded and self.ends.get_selected() == 2)
        if recurrence is None:
            problems.append("check the schedule dates/count")
        if self.category.get_selected() == self.funding.get_selected():
            problems.append("choose two different accounts")
        selected_accounts = {self.category.get_selected(), self.funding.get_selected()}
        for account_index, raw_amount, _purpose_index in self.additional_splits.values():
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

        if recurrence is not None and not problems:
            amount = self._amount()
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
        planning_kind = _PLANNING_FLOWS[self.planning_flow.get_selected()][1]
        category_value = amount * category.sign()
        extra_splits = []
        extra_total = Money(0)
        for account_index, raw_amount, purpose_index in self.additional_splits.values():
            account = self._accounts[account_index]
            extra_amount = Money(raw_amount)
            purpose = _PLANNING_FLOWS[purpose_index][1]
            value = (
                purpose.ledger_amount(extra_amount)
                if purpose is not None
                else extra_amount * account.sign()
            )
            extra_total = extra_total + value
            extra_splits.append(
                ScheduledSplit(account.handle, value, planning_flow=purpose)
            )
        funding_value = -(category_value + extra_total)
        schedule.splits = [
            ScheduledSplit(category.handle, category_value),
            *extra_splits,
            ScheduledSplit(
                funding.handle,
                funding_value,
                planning_flow=planning_kind,
            ),
        ]
        schedule.auto_create = self.auto_check.get_active()
        schedule.placeholder = self.kind.get_selected() == 1
        schedule.amount_changes = self._amount_changes() or []
        schedule.skipped = self._skipped(recurrence) or []
        schedule.occurrence_adjustments = (
            self._occurrence_adjustments(recurrence) or []
        )
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
