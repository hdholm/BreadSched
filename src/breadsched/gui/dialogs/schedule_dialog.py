"""Create a scheduled transaction, or a planning-only estimate.

The two are the same object with one flag between them, and the dialog says so
plainly rather than hiding it: a *commitment* is posted to the ledger when it falls
due, while an *estimate* shapes plans and forecasts and is never posted. Choosing
wrongly is the difference between a forecast and a fabricated ledger, so the choice
is made explicitly here rather than inferred.

The recurrence controls are the ones that change which month a payment lands in --
frequency, interval, start date, and weekend adjustment. Those are the settings a
cash-flow forecast is actually sensitive to.
"""

from __future__ import annotations

import re
from datetime import date

from ...gen.db.sqlite import DbSQLite
from ...gen.lib import (
    FormulaError,
    Money,
    PeriodType,
    PlanningFlowKind,
    Recurrence,
    ScheduledAmountChange,
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


class ScheduleDialog(Gtk.Window):
    """Enter a recurring transaction."""

    def __init__(
        self,
        parent: Gtk.Window | None,
        db: DbSQLite,
        source: ScheduledTransaction | None = None,
        read_only_reason: str | None = None,
    ) -> None:
        super().__init__(
            title=(
                "Scheduled transaction details"
                if source is not None and read_only_reason
                else "Edit scheduled transaction"
                if source
                else "New scheduled transaction"
            ),
            transient_for=parent,
            modal=True,
        )
        self.db = db
        self.source = source
        self.read_only_reason = read_only_reason
        self._formula_mode = bool(
            source is not None
            and read_only_reason is None
            and any(split.formula for split in source.splits)
        )
        self._formula_entries: list[tuple[int, Gtk.Entry]] = []
        self._formula_originals: list[str] = []
        self._formula_variables_original = ""
        self._category_planning_flow: PlanningFlowKind | None = None
        self._category_ledger_direction: int | None = None
        self._frequencies = list(_FREQUENCIES)
        if source is not None and not any(
            period is source.recurrence.period and interval == source.recurrence.interval
            for _label, period, interval in self._frequencies
        ):
            self._frequencies.append(
                (
                    f"{source.recurrence.describe()} (imported rule)",
                    source.recurrence.period,
                    source.recurrence.interval,
                )
            )
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
            [
                "Commitment - posted to the ledger when due",
                "Estimate - shapes plans only, never posted",
            ]
        )
        grid.attach(Gtk.Label(label="Kind", xalign=0), 0, row, 1, 1)
        grid.attach(self.kind, 1, row, 1, 1)
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

        self.category_memo_entry = Gtk.Entry(placeholder_text="Optional memo")
        grid.attach(Gtk.Label(label="Category memo", xalign=0), 0, row, 1, 1)
        grid.attach(self.category_memo_entry, 1, row, 1, 1)
        row += 1

        self.funding_memo_entry = Gtk.Entry(placeholder_text="Optional memo")
        grid.attach(Gtk.Label(label="Funding memo", xalign=0), 0, row, 1, 1)
        grid.attach(self.funding_memo_entry, 1, row, 1, 1)
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

        self.amount_changes_editor = DatedAmountListEditor(self._validate, "Add future amount")
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

        self.frequency = Gtk.DropDown.new_from_strings([item[0] for item in self._frequencies])
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

        self.weekend = Gtk.DropDown.new_from_strings([w[0] for w in _WEEKEND])
        self.weekend.connect("notify::selected", self._recurrence_changed)
        self.weekend.set_tooltip_text(
            "A payment moved off a weekend can land in a different month, which "
            "changes the forecast for both."
        )
        grid.attach(Gtk.Label(label="If it falls on a weekend", xalign=0), 0, row, 1, 1)
        grid.attach(self.weekend, 1, row, 1, 1)
        row += 1

        self.enabled_check = Gtk.CheckButton(label="Active — include in upcoming and plans")
        self.enabled_check.set_active(True)
        grid.attach(self.enabled_check, 1, row, 1, 1)
        row += 1

        self.auto_check = Gtk.CheckButton(label="Post automatically once the date arrives")
        grid.attach(self.auto_check, 1, row, 1, 1)

        self.formula_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.formula_box.set_visible(False)
        box.append(self.formula_box)

        self.details = Gtk.Label(xalign=0, yalign=0, wrap=True, selectable=True)
        self.details.set_visible(False)
        box.append(self.details)

        self.preview = Gtk.Label(xalign=0, wrap=True)
        self.preview.add_css_class("dim")
        box.append(self.preview)

        self.status = Gtk.Label(xalign=0)
        box.append(self.status)

        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Close" if read_only_reason else "Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        buttons.append(cancel)
        self.save_button = Gtk.Button(label="Save")
        self.save_button.add_css_class("suggested-action")
        self.save_button.set_sensitive(False)
        self.save_button.connect("clicked", self._on_save)
        buttons.append(self.save_button)
        box.append(buttons)

        if source is not None and read_only_reason:
            grid.set_visible(False)
            self.preview.set_visible(False)
            self.details.set_text(self._detail_text(source, read_only_reason))
            self.details.set_visible(True)
            self.save_button.set_visible(False)
            self.status.set_text(
                "Read-only: this schedule is preserved exactly as imported/stored."
            )
        else:
            if source is not None:
                if self._formula_mode:
                    self._load_formula_source(source)
                    self._protect_formula_fields(source)
                else:
                    self._load_source(source)
            self._validate()

    def _detail_text(self, source: ScheduledTransaction, reason: str) -> str:
        """Return a lossless human-readable summary for an unsupported schedule."""
        recurrence = source.recurrence
        lines = [
            reason,
            "",
            f"Name: {source.name}",
            f"Description: {source.description}",
            f"Kind: {'Estimate' if source.placeholder else 'Commitment'}",
            f"Enabled: {'yes' if source.enabled else 'no'}",
            f"Automatic posting: {'yes' if source.auto_create else 'no'}",
            f"Advance notice: {source.advance_days} day(s)",
            f"Currency: {source.currency or '(book/default)'}",
            f"Recurrence: {recurrence.describe()}",
            f"First due: {recurrence.start.isoformat()}",
            f"End date: {recurrence.end.isoformat() if recurrence.end else '(none)'}",
            (f"Occurrence limit: {recurrence.count if recurrence.count is not None else '(none)'}"),
            (
                "Day of month: "
                f"{recurrence.day_of_month if recurrence.day_of_month is not None else '(default)'}"
            ),
            (
                "Second day of month: "
                + (
                    str(recurrence.second_day_of_month)
                    if recurrence.second_day_of_month is not None
                    else "(none)"
                )
            ),
            f"Weekend adjustment: {recurrence.weekend_adjust.value}",
            (f"Last posted: {source.last_posted.isoformat() if source.last_posted else '(none)'}"),
            "",
            "Splits:",
        ]
        for index, split in enumerate(source.splits, 1):
            account = self.db.get_account(split.account)
            account_name = self.db.full_name(account) if account is not None else split.account
            value = f"formula {split.formula!r}" if split.formula else str(split.amount or Money(0))
            purpose = split.planning_flow.label if split.planning_flow is not None else "(none)"
            memo = split.memo or "(none)"
            lines.append(
                f"  {index}. {account_name}: {value}; planning purpose {purpose}; memo {memo}"
            )
        if source.variables:
            lines.extend(["", "Formula variables:"])
            lines.extend(f"  {key} = {value}" for key, value in sorted(source.variables.items()))
        if source.amount_changes:
            lines.extend(["", "Future amount changes:"])
            lines.extend(
                f"  {item.start.isoformat()}: {item.amount}" for item in source.amount_changes
            )
        if source.seasonal_amounts:
            lines.extend(["", "Seasonal month amounts:"])
            lines.extend(f"  month {item.month}: {item.amount}" for item in source.seasonal_amounts)
        if source.skipped:
            lines.extend(["", "Skipped occurrences:"])
            lines.extend(f"  {when.isoformat()}" for when in source.skipped)
        if source.occurrence_adjustments:
            lines.extend(["", "One-time occurrence amounts:"])
            lines.extend(
                f"  {item.when.isoformat()}: {item.amount}"
                for item in source.occurrence_adjustments
            )
        return "\n".join(lines)

    def _load_formula_source(self, source: ScheduledTransaction) -> None:
        """Load editable schedule metadata while preserving formula-owned values."""
        self.name_entry.set_text(source.name)
        self.kind.set_selected(1 if source.placeholder else 0)
        self.growth_policy.set_selected(
            next(
                index
                for index, (_label, policy) in enumerate(_GROWTH_POLICIES)
                if policy is source.growth_policy
            )
        )
        for index, (_label, period, interval) in enumerate(self._frequencies):
            if source.recurrence.period is period and source.recurrence.interval == interval:
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
        self.enabled_check.set_active(source.enabled)
        self.auto_check.set_active(source.auto_create)
        self.skipped_editor.set_values(source.skipped)

    def _protect_formula_fields(self, source: ScheduledTransaction) -> None:
        """Protect formula accounts/amount mechanics while allowing validated formulas."""
        protected = (
            self.category,
            self.funding,
            self.planning_flow,
            self.amount_entry,
            self.category_memo_entry,
            self.funding_memo_entry,
            self.additional_splits,
            self.amount_changes_editor,
            self.occurrence_adjustments_editor,
        )
        for widget in protected:
            widget.set_sensitive(False)
        self.preview.set_visible(False)
        self._build_formula_editor(source)
        self.details.set_text(
            self._detail_text(
                source,
                "Split accounts and formula-derived amount timelines remain protected. "
                "Formula expressions and named variables may be edited when the safe "
                "formula evaluator can validate the revised expressions.",
            )
        )
        self.details.set_visible(True)

    def _build_formula_editor(self, source: ScheduledTransaction) -> None:
        """Expose formula text and named variables without making split accounts editable."""
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

    def _load_source(self, source: ScheduledTransaction) -> None:
        """Populate the simple editor from an existing two-split schedule."""
        self.name_entry.set_text(source.name)
        self.kind.set_selected(1 if source.placeholder else 0)
        self.growth_policy.set_selected(
            next(
                index
                for index, (_label, policy) in enumerate(_GROWTH_POLICIES)
                if policy is source.growth_policy
            )
        )
        parts = []
        for split in source.splits:
            account = self.db.get_account(split.account)
            if account is None or split.formula:
                continue
            parts.append((account, split))
        flow = next(
            (item for item in parts if item[0].account_class.value in {"income", "expense"}),
            None,
        )
        if flow is None:
            planning_flows = [item for item in parts if item[1].planning_flow is not None]
            if planning_flows:
                flow = planning_flows[0]
        if flow is None:
            ordinary_balance_splits = [
                item
                for item in parts
                if item[0].account_class.value in {"asset", "liability"}
                and item[1].planning_flow is None
            ]
            if len(ordinary_balance_splits) == len(parts):
                positives = [
                    item
                    for item in ordinary_balance_splits
                    if item[1].resolve(source.variables) > 0
                ]
                negatives = [
                    item
                    for item in ordinary_balance_splits
                    if item[1].resolve(source.variables) < 0
                ]
                if len(positives) == 1 and negatives:
                    flow = positives[0]
                    self._category_ledger_direction = 1
        if flow is not None:
            flow_account, flow_split = flow
            others = [item for item in parts if item is not flow]
            funding_options = [
                item
                for item in others
                if item[0].account_class.value not in {"income", "expense"}
                and item[1].planning_flow is None
            ]
            abnormal_funding = [
                item
                for item in funding_options
                if item[1].resolve(source.variables) * item[0].sign() <= 0
            ]
            funding_item = (
                abnormal_funding[0]
                if len(abnormal_funding) == 1
                else funding_options[-1]
                if funding_options
                else others[-1]
                if others
                else None
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
                resolved_flow = flow_split.resolve(source.variables)
                if flow_split.planning_flow is not None:
                    self._category_planning_flow = flow_split.planning_flow
                    amount = flow_split.planning_flow.plan_amount(resolved_flow)
                else:
                    amount = abs(resolved_flow * flow_account.sign())
                self.amount_entry.set_text(str(amount.to_decimal()))
                self.category_memo_entry.set_text(flow_split.memo or "")
                self.funding_memo_entry.set_text(funding_item[1].memo or "")
                extra_values = []
                for account, split in others:
                    if split is funding_item[1]:
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
                    direction_index = 1 if split.planning_flow is None and normal_amount < 0 else 0
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

        for index, (_label, period, interval) in enumerate(self._frequencies):
            if source.recurrence.period is period and source.recurrence.interval == interval:
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
        self.enabled_check.set_active(source.enabled)
        self.auto_check.set_active(source.auto_create)
        self.amount_changes_editor.set_values(
            (item.start, str(item.amount.to_decimal())) for item in source.amount_changes
        )
        self.skipped_editor.set_values(source.skipped)
        self.occurrence_adjustments_editor.set_values(
            (item.when, str(item.amount.to_decimal())) for item in source.occurrence_adjustments
        )

    # ------------------------------------------------------------- validation

    def _recurrence(self) -> Recurrence | None:
        try:
            start = date.fromisoformat(self.start_entry.get_text().strip())
        except ValueError:
            return None
        _label, period, interval = self._frequencies[self.frequency.get_selected()]
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
        if (
            self.source is not None
            and period is self.source.recurrence.period
            and interval == self.source.recurrence.interval
        ):
            # Imported GnuCash rules can carry recurrence details that are not
            # separate controls in the simple editor (notably end-of-month and
            # semi-month firing days).  Preserve them whenever the user keeps the
            # same recurrence kind so editing another field is lossless.
            day_of_month = self.source.recurrence.day_of_month
            second_day_of_month = self.source.recurrence.second_day_of_month
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
            value = Money(parse_user_amount(text))
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
        problems = []
        if not self.name_entry.get_text().strip():
            problems.append("give it a name")
        recurrence = self._recurrence()
        period = self._frequencies[self.frequency.get_selected()][1]
        bounded = period is not PeriodType.ONCE
        self.ends.set_sensitive(bounded)
        self.end_entry.set_sensitive(bounded and self.ends.get_selected() == 1)
        self.count_entry.set_sensitive(bounded and self.ends.get_selected() == 2)
        if recurrence is None:
            problems.append("check the schedule dates/count")
        skipped = self._skipped(recurrence)
        if skipped is None:
            problems.append("check skipped occurrence dates")
        if self._formula_mode:
            if self._formula_inputs_changed():
                formula_problem = self._validate_formula_inputs()
                if formula_problem:
                    problems.append(formula_problem)
            self.save_button.set_sensitive(not problems)
            self.status.set_text("; ".join(problems).capitalize() if problems else "")
            return
        if self._amount() is None:
            problems.append("enter an amount")
        amount_changes = self._amount_changes()
        if amount_changes is None:
            problems.append("check future amounts")
        adjustments = self._occurrence_adjustments(recurrence)
        if adjustments is None:
            problems.append("check one-time amounts")
        if skipped is not None and adjustments is not None:
            if set(skipped) & {item.when for item in adjustments}:
                problems.append("an occurrence cannot be both skipped and overridden")
        if self.category.get_selected() == self.funding.get_selected():
            problems.append("choose two different accounts")
        for (
            _account_index,
            raw_amount,
            _purpose_index,
            _memo,
            _direction,
        ) in self.additional_splits.values():
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
        recurrence = self._recurrence()
        assert recurrence is not None
        if self._formula_mode:
            assert self.source is not None
            schedule = ScheduledTransaction.from_dict(self.source.serialize())
            old_name = schedule.name
            schedule.name = self.name_entry.get_text().strip()
            if schedule.description == old_name:
                schedule.description = schedule.name
            schedule.recurrence = recurrence
            schedule.enabled = self.enabled_check.get_active()
            schedule.auto_create = self.auto_check.get_active()
            schedule.placeholder = self.kind.get_selected() == 1
            schedule.growth_policy = _GROWTH_POLICIES[self.growth_policy.get_selected()][1]
            schedule.skipped = self._skipped(recurrence) or []
            if self._formula_inputs_changed():
                variables = self._formula_variables()
                assert variables is not None
                schedule.variables = variables
                for split_index, entry in self._formula_entries:
                    schedule.splits[split_index].formula = entry.get_text().strip()
            return schedule

        amount = self._amount()
        assert amount is not None
        category = self._accounts[self.category.get_selected()]
        funding = self._accounts[self.funding.get_selected()]

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
        if self._category_planning_flow is not None:
            category_value = self._category_planning_flow.ledger_amount(amount)
        elif self._category_ledger_direction is not None:
            category_value = amount * self._category_ledger_direction
        else:
            category_value = amount * category.sign()
        extra_splits = []
        extra_total = Money(0)
        for (
            account_index,
            raw_amount,
            purpose_index,
            memo,
            direction_index,
        ) in self.additional_splits.values():
            account = self._accounts[account_index]
            extra_amount = Money(parse_user_amount(raw_amount))
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
        funding_value = -(category_value + extra_total)
        schedule.splits = [
            ScheduledSplit(
                category.handle,
                category_value,
                memo=self.category_memo_entry.get_text().strip(),
                planning_flow=self._category_planning_flow,
            ),
            *extra_splits,
            ScheduledSplit(
                funding.handle,
                funding_value,
                memo=self.funding_memo_entry.get_text().strip(),
                planning_flow=planning_kind,
            ),
        ]
        schedule.enabled = self.enabled_check.get_active()
        schedule.auto_create = self.auto_check.get_active()
        schedule.placeholder = self.kind.get_selected() == 1
        schedule.growth_policy = _GROWTH_POLICIES[self.growth_policy.get_selected()][1]
        schedule.amount_changes = self._amount_changes() or []
        schedule.skipped = self._skipped(recurrence) or []
        schedule.occurrence_adjustments = self._occurrence_adjustments(recurrence) or []
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
