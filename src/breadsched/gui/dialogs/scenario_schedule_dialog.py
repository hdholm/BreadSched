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
from ...gen.engine.entry_input import parse_amount_text
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
    ScheduledTransaction,
    ScheduleGrowthPolicy,
    WeekendAdjust,
    evaluate,
    scheduled_occurrence_preview,
)
from ...gen.services import (
    FixedScheduleInput,
    FixedSplitInput,
    FormulaScenarioScheduleInput,
    SaveFixedScenarioSchedule,
    build_fixed_scenario_schedule,
    build_formula_scenario_schedule,
    save_fixed_scenario_schedule,
    save_formula_scenario_schedule,
)
from ..gi_setup import Gtk
from ..widgets.bounded import BoundedWindow, scroll_body
from ..widgets.choice import bounded_dropdown
from ..widgets.date_entry import attach_date_keys, parse_date_text
from ..widgets.form_pages import FormPages
from ..widgets.frequency import FrequencyPicker
from ..widgets.help import help_row
from ..widgets.schedule_timeline import (
    DatedAmountListEditor,
    DateListEditor,
    MonthAmountListEditor,
    PlanningSplitListEditor,
)
from .schedule_dialog import FUTURE_CHANGES, update_schedule_pages

__all__ = ["ScenarioScheduleDialog", "ScenarioSchedulePickerDialog"]

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


class ScenarioScheduleDialog(BoundedWindow):
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

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(18)
        self.set_child(box)
        box.prepend(help_row("scheduled"))

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

        # The fields most estimates need first; the rest a tab away (#383).
        self.pages = FormPages()
        box.append(self.pages)
        basics = self.pages.page("Basics")
        splits = self.pages.page("Splits")
        changes = self.pages.page(FUTURE_CHANGES)
        advanced = self.pages.page("Advanced")

        self.name_entry = Gtk.Entry(placeholder_text="Groceries")
        self.name_entry.set_hexpand(True)
        self.name_entry.connect("changed", self._validate)
        basics.add("Name", self.name_entry)

        self.growth_policy = bounded_dropdown([label for label, _policy in _GROWTH_POLICIES])
        self.growth_policy.set_tooltip_text(
            "Automatic uses income growth when a schedule contains income, "
            "expense inflation for expense-only schedules, and no generic growth "
            "for formula-driven schedules."
        )
        advanced.add("Projection growth", self.growth_policy)

        self.category = bounded_dropdown(self._names)
        self.category.connect("notify::selected", self._validate)
        basics.add("Category / investment account", self.category)

        self.category_planning_flow = bounded_dropdown([label for label, _kind in _PLANNING_FLOWS])
        self.category_planning_flow.connect("notify::selected", self._validate)
        advanced.add("Category planning purpose", self.category_planning_flow)

        self.funding = bounded_dropdown(self._names)
        self.funding.connect("notify::selected", self._validate)
        if len(self._accounts) > 1:
            self.funding.set_selected(1)
        basics.add("Paid from / into", self.funding)

        self.planning_flow = bounded_dropdown([label for label, _kind in _PLANNING_FLOWS])
        advanced.add("Planning purpose override", self.planning_flow)

        self.investment_activity = bounded_dropdown(
            [label for label, _kind in _INVESTMENT_ACTIVITIES]
        )
        self.investment_activity.connect("notify::selected", self._validate)
        self.investment_activity_label = advanced.add(
            "Investment activity", self.investment_activity
        )

        self.amount_entry = Gtk.Entry(placeholder_text="0.00")
        self.amount_entry.connect("changed", self._validate)
        basics.add("Amount", self.amount_entry)

        self.additional_splits = PlanningSplitListEditor(
            self._validate,
            self._names,
            [label for label, _kind in _PLANNING_FLOWS],
            [label for label, _kind in _INVESTMENT_ACTIVITIES],
        )
        splits.add(
            "Additional splits",
            self.additional_splits,
            tooltip=(
                "Add fixed payroll deductions, retirement/FSA funding, debt principal, "
                "or other legs. The paid-from/into split is balanced automatically."
            ),
        )

        self.amount_changes_editor = DatedAmountListEditor(self._validate, "Add future amount")
        changes.add("Future amounts", self.amount_changes_editor)

        self.seasonal_amounts_editor = MonthAmountListEditor(self._validate)
        changes.add("Seasonal month amounts", self.seasonal_amounts_editor)

        self.skipped_editor = DateListEditor(
            self._validate, "Add skipped occurrence", self._occurrence_options
        )
        changes.add("Skip occurrences", self.skipped_editor)

        self.occurrence_adjustments_editor = DatedAmountListEditor(
            self._validate,
            "Add one-time amount",
            self._occurrence_options,
        )
        changes.add("One-time amounts", self.occurrence_adjustments_editor)

        # Every N days, weeks, months, or years, as in the schedule editor (#370).
        self.frequency = FrequencyPicker(PeriodType.MONTH, 1)
        self.frequency.connect_changed(self._recurrence_changed)
        basics.add("Frequency", self.frequency)

        self.start_entry = attach_date_keys(Gtk.Entry(text=date.today().replace(day=1).isoformat()))
        self.start_entry.connect("changed", self._recurrence_changed)
        basics.add("First occurrence", self.start_entry)

        self.ends = bounded_dropdown(["Never", "On date", "After occurrences"])
        self.ends.connect("notify::selected", self._recurrence_changed)
        basics.add("Ends", self.ends)

        self.end_entry = attach_date_keys(Gtk.Entry(placeholder_text="YYYY-MM-DD"))
        self.end_entry.connect("changed", self._recurrence_changed)
        basics.add("End date", self.end_entry)

        self.count_entry = Gtk.Entry(placeholder_text="12")
        self.count_entry.connect("changed", self._recurrence_changed)
        basics.add("Occurrences", self.count_entry)

        self.weekend = bounded_dropdown([item[0] for item in _WEEKEND])
        self.weekend.connect("notify::selected", self._recurrence_changed)
        advanced.add("If it falls on a weekend", self.weekend)

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
        # The form scrolls; its buttons stay on screen (dialog audit, #148).
        scroll_body(self)

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
        self.frequency.set_value(source.recurrence.period, source.recurrence.interval)
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
            start = parse_date_text(self.start_entry.get_text().strip())
        except ValueError:
            return None
        period, interval = self.frequency.value()
        end = None
        count = None
        if period is not PeriodType.ONCE:
            if self.ends.get_selected() == 1:
                try:
                    end = parse_date_text(self.end_entry.get_text().strip())
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
            amount = Money(parse_amount_text(text))
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
                when = parse_date_text(when_text)
                amount = Money(parse_amount_text(amount_text))
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
            skipped = [parse_date_text(raw) for raw in values]
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
                ScheduledMonthAmount(month, Money(parse_amount_text(raw_amount)))
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
                when = parse_date_text(when_text)
                amount = Money(parse_amount_text(amount_text))
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
        update_schedule_pages(self)
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
        period = self.frequency.value()[0]
        bounded = period is not PeriodType.ONCE
        self.ends.set_sensitive(bounded)
        self.end_entry.set_sensitive(bounded and self.ends.get_selected() == 1)
        self.count_entry.set_sensitive(bounded and self.ends.get_selected() == 2)
        if recurrence is None:
            problems.append("check the schedule dates/count")
        if not self._formula_mode:
            for (
                _account_index,
                raw_amount,
                _purpose_index,
                _activity_index,
                _memo,
                _direction,
            ) in self.additional_splits.values():
                try:
                    extra_amount = Money(parse_amount_text(raw_amount))
                except (ValueError, ArithmeticError):
                    problems.append("check additional split amounts")
                    break
                if extra_amount <= 0:
                    problems.append("additional split amounts must be greater than zero")
                    break
            if not problems:
                validation = build_fixed_scenario_schedule(self.db, self._fixed_request())
                if validation.value is None:
                    messages = {
                        "schedule.accounts.same": "choose two different accounts",
                        "schedule.accounts.duplicate": (
                            "each additional split needs a different account"
                        ),
                        "schedule.category.classification_conflict": (
                            "choose a category planning purpose or investment activity, not both"
                        ),
                        "schedule.category.role_required": (
                            "choose an income/expense category, planning purpose, "
                            "or investment activity"
                        ),
                    }
                    problems.append(
                        messages.get(validation.errors[0].code, validation.errors[0].code)
                    )

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

    def _formula_request(self) -> FormulaScenarioScheduleInput:
        recurrence = self._recurrence()
        variables = self._formula_variables()
        assert self.source is not None and recurrence is not None and variables is not None
        return FormulaScenarioScheduleInput(
            scenario_handle=self.scenario.handle,
            source_schedule=self.source.handle,
            name=self.name_entry.get_text().strip(),
            recurrence=recurrence,
            formulas={index: entry.get_text() for index, entry in self._formula_entries},
            variables=variables,
            growth_policy=_GROWTH_POLICIES[self.growth_policy.get_selected()][1],
            skipped=tuple(self._skipped(recurrence) or ()),
            source=self.source,
            current=self.current,
        )

    def _fixed_request(self) -> SaveFixedScenarioSchedule:
        recurrence = self._recurrence()
        amount = self._amount()
        assert recurrence is not None and amount is not None
        additional = tuple(
            FixedSplitInput(
                account=self._accounts[account_index].handle,
                amount=Money(parse_amount_text(raw_amount)),
                planning_flow=_PLANNING_FLOWS[purpose_index][1],
                investment_activity=_INVESTMENT_ACTIVITIES[activity_index][1],
                memo=memo,
                opposite_direction=direction_index == 1,
            )
            for (
                account_index,
                raw_amount,
                purpose_index,
                activity_index,
                memo,
                direction_index,
            ) in self.additional_splits.values()
        )
        placeholder = (
            self.current.placeholder
            if self.current is not None
            else self.source.placeholder
            if self.source is not None
            else True
        )
        evidence = (
            self.current.estimate_evidence
            if self.current is not None
            else self.source.estimate_evidence
            if self.source is not None
            else None
        )
        return SaveFixedScenarioSchedule(
            scenario_handle=self.scenario.handle,
            source_schedule=self.source.handle if self.source is not None else None,
            definition=FixedScheduleInput(
                name=self.name_entry.get_text().strip(),
                recurrence=recurrence,
                category=self._accounts[self.category.get_selected()].handle,
                funding=self._accounts[self.funding.get_selected()].handle,
                amount=amount,
                category_planning_flow=_PLANNING_FLOWS[self.category_planning_flow.get_selected()][
                    1
                ],
                funding_planning_flow=_PLANNING_FLOWS[self.planning_flow.get_selected()][1],
                investment_activity=_INVESTMENT_ACTIVITIES[self.investment_activity.get_selected()][
                    1
                ],
                category_ledger_direction=self._category_ledger_direction,
                additional_splits=additional,
                enabled=True,
                placeholder=placeholder,
                growth_policy=_GROWTH_POLICIES[self.growth_policy.get_selected()][1],
                amount_changes=tuple(self._amount_changes() or ()),
                seasonal_amounts=tuple(self._seasonal_amounts() or ()),
                skipped=tuple(self._skipped(recurrence) or ()),
                occurrence_adjustments=tuple(self._occurrence_adjustments(recurrence) or ()),
                estimate_evidence=evidence,
            ),
            source=self.source,
            current=self.current,
        )

    def build(self) -> ScenarioSchedule:
        """Build the scenario-owned recurring estimate described by the form."""
        recurrence = self._recurrence()
        assert recurrence is not None
        if self._formula_mode:
            result = build_formula_scenario_schedule(self.db, self._formula_request())
            assert result.value is not None
            return result.value

        result = build_fixed_scenario_schedule(self.db, self._fixed_request())
        assert result.value is not None
        return result.value

    def _on_save(self, _button) -> None:
        result = (
            save_formula_scenario_schedule(self.db, self._formula_request())
            if self._formula_mode
            else save_fixed_scenario_schedule(self.db, self._fixed_request())
        )
        if result.value is None:
            self.status.set_text(result.errors[0].code)
            return
        self.close()


class ScenarioSchedulePickerDialog(BoundedWindow):
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
        box.prepend(help_row("scheduled"))
        box.append(Gtk.Label(label="Baseline scheduled transaction", xalign=0))
        self.schedule = bounded_dropdown(
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
