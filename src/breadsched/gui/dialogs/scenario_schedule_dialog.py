"""Edit recurring estimates that belong to one saved scenario.

Scenario schedules never mutate the baseline book schedule they replace.  They are
forecast inputs only: the resolved scenario event stream is consumed by both Plan
and Projection, while actual ledger posting continues to use the baseline book.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date

from ...gen.db.sqlite import DbSQLite
from ...gen.engine import estimates, investment
from ...gen.lib import (
    AccountClass,
    FormulaError,
    InvestmentActivityKind,
    Money,
    PeriodType,
    PlanningFlowKind,
    Recurrence,
    Scenario,
    ScenarioSchedule,
    ScheduledAmountChange,
    ScheduledMonthAmount,
    ScheduledOccurrenceAdjustment,
    ScheduledSplit,
    ScheduledTransaction,
    ScheduleGrowthPolicy,
    WeekendAdjust,
    evaluate,
    scheduled_occurrence_preview,
)
from ...gen.utils.amount_input import parse_user_amount
from ..gi_setup import Gtk
from ..widgets.schedule_timeline import (
    DatedAmountListEditor,
    DateListEditor,
    MonthAmountListEditor,
    PlanningSplitListEditor,
)

__all__ = ["ScenarioScheduleDialog", "ScenarioSchedulePickerDialog"]

_FREQUENCIES = [
    ("Weekly", PeriodType.WEEK, 1),
    ("Fortnightly", PeriodType.WEEK, 2),
    ("Twice a month", PeriodType.SEMI_MONTH, 1),
    ("Monthly", PeriodType.MONTH, 1),
    ("Monthly — nth weekday", PeriodType.NTH_WEEKDAY, 1),
    ("Monthly — last weekday", PeriodType.LAST_WEEKDAY, 1),
    ("Quarterly", PeriodType.MONTH, 3),
    ("Twice a year", PeriodType.MONTH, 6),
    ("Yearly", PeriodType.YEAR, 1),
    ("Once", PeriodType.ONCE, 1),
]

_WEEKEND = [
    ("Leave on the day", WeekendAdjust.NONE),
    ("Move to the Friday before", WeekendAdjust.PREVIOUS),
    ("Move to the Monday after", WeekendAdjust.NEXT),
]

_GROWTH_POLICIES = [
    ("Automatic from schedule contents", ScheduleGrowthPolicy.AUTO),
    ("No growth - fixed nominal amount", ScheduleGrowthPolicy.NONE),
    ("Income growth", ScheduleGrowthPolicy.INCOME),
    ("Expense inflation", ScheduleGrowthPolicy.INFLATION),
]

_PLANNING_FLOWS = [
    ("Ordinary transfer", None),
    ("Retirement saving", PlanningFlowKind.RETIREMENT_SAVING),
    ("Benefit / FSA funding", PlanningFlowKind.BENEFIT_FUNDING),
    ("Debt principal", PlanningFlowKind.DEBT_PRINCIPAL),
    ("Retirement distribution", PlanningFlowKind.RETIREMENT_INCOME),
]

_INVESTMENT_ACTIVITIES = [
    ("Ordinary investment activity", None),
    *[(kind.label, kind) for kind in InvestmentActivityKind],
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
        self._formula_entries: list[tuple[int, Gtk.Entry]] = []
        self._formula_originals: list[str] = []
        self._formula_variables_original = ""
        self._category_ledger_direction: int | None = None
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

        self.growth_policy = Gtk.DropDown.new_from_strings(
            [label for label, _policy in _GROWTH_POLICIES]
        )
        self.growth_policy.set_tooltip_text(
            "Automatic uses income growth when a schedule contains income, "
            "expense inflation for expense-only schedules, and no generic growth "
            "for formula-driven schedules."
        )
        grid.attach(Gtk.Label(label="Projection growth", xalign=0), 0, row, 1, 1)
        grid.attach(self.growth_policy, 1, row, 1, 1)
        row += 1

        self.category = Gtk.DropDown.new_from_strings(self._names)
        self.category.connect("notify::selected", self._validate)
        grid.attach(Gtk.Label(label="Category / investment account", xalign=0), 0, row, 1, 1)
        grid.attach(self.category, 1, row, 1, 1)
        row += 1

        self.category_planning_flow = Gtk.DropDown.new_from_strings(
            [label for label, _kind in _PLANNING_FLOWS]
        )
        self.category_planning_flow.connect("notify::selected", self._validate)
        grid.attach(Gtk.Label(label="Category planning purpose", xalign=0), 0, row, 1, 1)
        grid.attach(self.category_planning_flow, 1, row, 1, 1)
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

        self.investment_activity = Gtk.DropDown.new_from_strings(
            [label for label, _kind in _INVESTMENT_ACTIVITIES]
        )
        self.investment_activity.connect("notify::selected", self._validate)
        grid.attach(Gtk.Label(label="Investment activity", xalign=0), 0, row, 1, 1)
        grid.attach(self.investment_activity, 1, row, 1, 1)
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
            [label for label, _kind in _INVESTMENT_ACTIVITIES],
        )
        label = Gtk.Label(label="Additional splits", xalign=0, valign=Gtk.Align.START)
        label.set_tooltip_text(
            "Add fixed payroll deductions, retirement/FSA funding, debt principal, "
            "or other legs. The paid-from/into split is balanced automatically."
        )
        grid.attach(label, 0, row, 1, 1)
        grid.attach(self.additional_splits, 1, row, 1, 1)
        row += 1

        self.amount_changes_editor = DatedAmountListEditor(self._validate, "Add future amount")
        label = Gtk.Label(label="Future amounts", xalign=0, valign=Gtk.Align.START)
        grid.attach(label, 0, row, 1, 1)
        grid.attach(self.amount_changes_editor, 1, row, 1, 1)
        row += 1

        self.seasonal_amounts_editor = MonthAmountListEditor(self._validate)
        label = Gtk.Label(label="Seasonal month amounts", xalign=0, valign=Gtk.Align.START)
        grid.attach(label, 0, row, 1, 1)
        grid.attach(self.seasonal_amounts_editor, 1, row, 1, 1)
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

        self.ends = Gtk.DropDown.new_from_strings(["Never", "On date", "After occurrences"])
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

        self.formula_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.formula_box.set_visible(False)
        box.append(self.formula_box)

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
        self.growth_policy.set_selected(
            next(
                index
                for index, (_label, policy) in enumerate(_GROWTH_POLICIES)
                if policy is source.growth_policy
            )
        )
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

        investment_flows = [
            split for split in source.splits if split.investment_activity is not None
        ]
        flow_split = investment_flows[0] if len(source.splits) == 2 and investment_flows else None
        if flow_split is None:
            for split in source.splits:
                account = self.db.get_account(split.account)
                if account is not None and account.account_class in (
                    AccountClass.INCOME,
                    AccountClass.EXPENSE,
                ):
                    flow_split = split
                    break
        if flow_split is None and investment_flows:
            flow_split = investment_flows[0]
        if flow_split is None or any(split.formula for split in source.splits):
            self.status.set_text(
                "This schedule uses formulas or has no editable category/activity anchor. "
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
        self.category_planning_flow.set_selected(
            next(
                (
                    index
                    for index, (_label, kind) in enumerate(_PLANNING_FLOWS)
                    if kind is flow_split.planning_flow
                ),
                0,
            )
        )
        self.investment_activity.set_selected(
            next(
                (
                    index
                    for index, (_label, kind) in enumerate(_INVESTMENT_ACTIVITIES)
                    if kind is flow_split.investment_activity
                ),
                0,
            )
        )
        category = self.db.get_account(flow_split.account)
        resolved = flow_split.resolve(source.variables)
        amount = resolved
        if flow_split.investment_activity is not None:
            amount = abs(resolved)
            if flow_split.investment_activity.direction == 0:
                self._category_ledger_direction = 1 if resolved >= 0 else -1
        elif category is not None:
            amount = resolved * category.sign()
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
            activity_index = next(
                (
                    index
                    for index, (_label, kind) in enumerate(_INVESTMENT_ACTIVITIES)
                    if kind is split.investment_activity
                ),
                0,
            )
            resolved = split.resolve(source.variables)
            normal_amount = (
                split.planning_flow.plan_amount(resolved)
                if split.planning_flow is not None
                else resolved * account.sign()
            )
            direction_index = 1 if split.planning_flow is None and normal_amount < 0 else 0
            extra_values.append(
                (
                    account_index,
                    str(abs(normal_amount).to_decimal()),
                    purpose_index,
                    activity_index,
                    split.memo or "",
                    direction_index,
                )
            )
        self.additional_splits.set_values(extra_values)
        self.amount_changes_editor.set_values(
            (item.start, str(item.amount.to_decimal())) for item in source.amount_changes
        )
        self.seasonal_amounts_editor.set_values(
            (item.month, str(item.amount.to_decimal())) for item in source.seasonal_amounts
        )
        self.skipped_editor.set_values(source.skipped)
        self.occurrence_adjustments_editor.set_values(
            (item.when, str(item.amount.to_decimal())) for item in source.occurrence_adjustments
        )

    def _protect_formula_fields(self, source: ScheduledTransaction | ScenarioSchedule) -> None:
        """Protect formula-owned structure while allowing validated formula inputs."""
        protected = (
            self.category,
            self.category_planning_flow,
            self.funding,
            self.planning_flow,
            self.investment_activity,
            self.amount_entry,
            self.additional_splits,
            self.amount_changes_editor,
            self.seasonal_amounts_editor,
            self.occurrence_adjustments_editor,
        )
        for widget in protected:
            widget.set_sensitive(False)
        self._build_formula_editor(source)
        lines = [
            "Split accounts and formula-derived amount timelines remain protected. "
            "Formula expressions and named variables may be edited when the safe "
            "formula evaluator can validate the revised expressions.",
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
            lines.extend(f"  {key} = {value}" for key, value in sorted(source.variables.items()))
        self.protected_details.set_text("\n".join(lines))
        self.protected_details.set_visible(True)
        self.preview.set_visible(False)

    def _build_formula_editor(self, source: ScheduledTransaction | ScenarioSchedule) -> None:
        """Expose formula text and variables without making split accounts editable."""
        self.formula_box.set_visible(True)
        heading = Gtk.Label(label="Formula inputs", xalign=0)
        heading.add_css_class("heading")
        self.formula_box.append(heading)
        self._formula_entries = []
        self._formula_originals = []
        for index, split in enumerate(source.splits):
            if not split.formula:
                continue
            account = self.db.get_account(split.account)
            account_name = self.db.full_name(account) if account is not None else split.account
            row = Gtk.Box(spacing=8)
            label = Gtk.Label(label=account_name, xalign=0)
            label.set_hexpand(True)
            entry = Gtk.Entry(text=split.formula)
            entry.set_hexpand(True)
            entry.connect("changed", self._validate)
            row.append(label)
            row.append(entry)
            self.formula_box.append(row)
            self._formula_entries.append((index, entry))
            self._formula_originals.append(split.formula)
        self.formula_variables_entry = Gtk.Entry()
        self.formula_variables_entry.set_placeholder_text("name=value; other=value")
        variables_text = "; ".join(
            f"{key}={value}" for key, value in sorted(source.variables.items())
        )
        self.formula_variables_entry.set_text(variables_text)
        self.formula_variables_entry.connect("changed", self._validate)
        self._formula_variables_original = variables_text
        variables_row = Gtk.Box(spacing=8)
        variables_label = Gtk.Label(label="Variables", xalign=0)
        variables_label.set_hexpand(True)
        variables_row.append(variables_label)
        variables_row.append(self.formula_variables_entry)
        self.formula_box.append(variables_row)

    def _formula_variables(self) -> dict[str, str] | None:
        """Parse the compact ``name=value`` formula-variable editor."""
        text = self.formula_variables_entry.get_text().strip()
        if not text:
            return {}
        variables: dict[str, str] = {}
        for item in text.split(";"):
            if "=" not in item:
                return None
            name, value = (part.strip() for part in item.split("=", 1))
            if not re.fullmatch(r"[A-Za-z_]\w*", name) or not value:
                return None
            try:
                evaluate(value, {})
            except FormulaError:
                return None
            variables[name] = value
        return variables

    def _formula_inputs_changed(self) -> bool:
        formulas = [entry.get_text() for _index, entry in self._formula_entries]
        return (
            formulas != self._formula_originals
            or self.formula_variables_entry.get_text().strip() != self._formula_variables_original
        )

    def _validate_formula_inputs(self) -> str | None:
        variables = self._formula_variables()
        if variables is None:
            return "check formula variables"
        context: dict[str, str | int] = dict(variables)
        context.update({"period": 1, "i": 1})
        for _index, entry in self._formula_entries:
            formula = entry.get_text().strip()
            if not formula:
                return "formula expressions cannot be blank"
            try:
                evaluate(formula, context)
            except (FormulaError, ValueError, ArithmeticError):
                return "check formula expressions and variables"
        return None

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
            amount = Money(parse_user_amount(text))
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
                amount = Money(parse_user_amount(amount_text))
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

    def _seasonal_amounts(self) -> list[ScheduledMonthAmount] | None:
        values = self.seasonal_amounts_editor.values()
        if len({month for month, _amount in values}) != len(values):
            return None
        try:
            amounts = [
                ScheduledMonthAmount(month, Money(parse_user_amount(raw_amount)))
                for month, raw_amount in values
            ]
        except (ValueError, ArithmeticError):
            return None
        if any(item.amount <= 0 for item in amounts):
            return None
        return sorted(amounts, key=lambda item: item.month)

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
                amount = Money(parse_user_amount(amount_text))
                if amount <= 0:
                    return None
                changes.append(ScheduledOccurrenceAdjustment(when, amount))
        except (ValueError, ArithmeticError):
            return None
        if len({item.when for item in changes}) != len(changes):
            return None
        if any(
            item.when not in recurrence.occurrences(item.when, since=item.when) for item in changes
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
            if self._seasonal_amounts() is None:
                problems.append("check seasonal month amounts")
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
        elif self._formula_inputs_changed():
            formula_problem = self._validate_formula_inputs()
            if formula_problem:
                problems.append(formula_problem)
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
            investment_activity = _INVESTMENT_ACTIVITIES[self.investment_activity.get_selected()][1]
            category_planning_flow = _PLANNING_FLOWS[self.category_planning_flow.get_selected()][1]
            if category_planning_flow is not None and investment_activity is not None:
                problems.append(
                    "choose a category planning purpose or investment activity, not both"
                )
            if (
                category.account_class not in (AccountClass.INCOME, AccountClass.EXPENSE)
                and investment_activity is None
                and category_planning_flow is None
            ):
                problems.append("choose an income/expense category or an investment activity")
            selected_accounts = {self.category.get_selected(), self.funding.get_selected()}
            for (
                account_index,
                raw_amount,
                _purpose_index,
                _activity_index,
                _memo,
                _direction,
            ) in self.additional_splits.values():
                if account_index in selected_accounts:
                    problems.append("each additional split needs a different account")
                    break
                selected_accounts.add(account_index)
                try:
                    extra_amount = Money(parse_user_amount(raw_amount))
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
            change.growth_policy = _GROWTH_POLICIES[self.growth_policy.get_selected()][1]
            change.skipped = self._skipped(recurrence) or []
            if self._formula_inputs_changed():
                variables = self._formula_variables()
                assert variables is not None
                for index, entry in self._formula_entries:
                    change.splits[index].formula = entry.get_text().strip()
                change.variables = variables
            if self.source is not None:
                change.source_schedule = self.source.handle
            return change

        amount = self._amount()
        assert amount is not None
        category = self._accounts[self.category.get_selected()]
        funding = self._accounts[self.funding.get_selected()]
        investment_activity = _INVESTMENT_ACTIVITIES[self.investment_activity.get_selected()][1]
        category_planning_flow = _PLANNING_FLOWS[self.category_planning_flow.get_selected()][1]
        signed = (
            category_planning_flow.ledger_amount(amount)
            if category_planning_flow is not None
            else amount * investment_activity.direction
            if investment_activity is not None and investment_activity.direction
            else amount * self._category_ledger_direction
            if self._category_ledger_direction is not None
            else amount * category.sign()
        )
        extra_splits = []
        extra_total = Money(0)
        for (
            account_index,
            raw_amount,
            purpose_index,
            activity_index,
            memo,
            direction_index,
        ) in self.additional_splits.values():
            account = self._accounts[account_index]
            extra_amount = Money(parse_user_amount(raw_amount))
            purpose = _PLANNING_FLOWS[purpose_index][1]
            investment_activity = _INVESTMENT_ACTIVITIES[activity_index][1]
            value = (
                extra_amount * investment_activity.direction
                if investment_activity is not None and investment_activity.direction
                else purpose.ledger_amount(extra_amount)
                if purpose is not None
                else extra_amount * account.sign() * (-1 if direction_index == 1 else 1)
            )
            extra_total = extra_total + value
            extra_splits.append(
                ScheduledSplit(
                    account.handle,
                    value,
                    memo=memo,
                    planning_flow=purpose,
                    investment_activity=investment_activity,
                )
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
                ScheduledSplit(
                    category.handle,
                    signed,
                    planning_flow=category_planning_flow,
                    investment_activity=investment_activity,
                ),
                *extra_splits,
                ScheduledSplit(
                    funding.handle,
                    funding_value,
                    planning_flow=_PLANNING_FLOWS[self.planning_flow.get_selected()][1],
                    investment_activity=(
                        InvestmentActivityKind.ROLLOVER
                        if investment_activity is InvestmentActivityKind.ROLLOVER
                        else None
                    ),
                ),
            ],
            source_schedule=self.source.handle if self.source is not None else None,
            enabled=True,
            placeholder=placeholder,
            growth_policy=_GROWTH_POLICIES[self.growth_policy.get_selected()][1],
            amount_changes=self._amount_changes() or [],
            seasonal_amounts=self._seasonal_amounts() or [],
            skipped=self._skipped(recurrence) or [],
            occurrence_adjustments=self._occurrence_adjustments(recurrence) or [],
            estimate_evidence=(
                self.current.estimate_evidence
                if self.current is not None
                else self.source.estimate_evidence
                if self.source is not None
                else None
            ),
        )

    def _on_save(self, _button) -> None:
        change = self.build()
        try:
            estimates.validate_historical_estimate_adjustment(self.db, change)
        except ValueError as exc:
            self.status.set_text(str(exc))
            return
        problems = investment.scheduled_activity_problems(self.db, change)
        if problems:
            self.status.set_text("; ".join(problems))
            return
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
