"""Manage saved planning scenarios, base assumptions, and dated overrides."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation

from ...gen.db.sqlite import DbSQLite
from ...gen.lib import AccountClass, AssumptionPeriod, Scenario
from ...gen.lib.base import create_handle
from ..gi_setup import Gtk
from ..planning_context import (
    baseline_scenario,
    notify_planning_scenario_changed,
    persist_baseline_assumptions,
)

__all__ = ["ScenarioManagerDialog"]


_ASSUMPTIONS = (
    ("Income growth", "income_growth"),
    ("Expense inflation", "expense_inflation"),
    ("Investment return", "investment_return"),
    ("Cash interest", "cash_interest"),
    ("Liability interest", "liability_interest"),
)


class ScenarioManagerDialog(Gtk.Window):
    """Rename, duplicate, delete, and edit a saved scenario's base assumptions."""

    def __init__(self, parent: Gtk.Window | None, db: DbSQLite, manager) -> None:
        super().__init__(title="Manage scenarios", transient_for=parent, modal=True)
        self.db = db
        self.manager = manager
        self._baseline = baseline_scenario(manager, db)
        self._scenarios: list[Scenario] = []
        self._loading = False
        self._account_rates: dict[str, Decimal] = {}
        self.set_default_size(620, 520)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(18)
        self.set_child(box)

        box.append(
            Gtk.Label(
                label=(
                    "Base scenario assumptions apply to the default plan. Saved scenarios can "
                    "override those assumptions and future estimated activity without changing "
                    "the Base scenario."
                ),
                xalign=0,
                wrap=True,
            )
        )

        picker_row = Gtk.Box(spacing=8)
        picker_row.append(Gtk.Label(label="Scenario"))
        self.picker = Gtk.DropDown()
        self.picker.set_hexpand(True)
        self.picker.connect("notify::selected", self._on_selected)
        picker_row.append(self.picker)
        self.duplicate_button = Gtk.Button(label="Duplicate…")
        self.duplicate_button.connect("clicked", self._on_duplicate)
        picker_row.append(self.duplicate_button)
        self.delete_button = Gtk.Button(label="Delete…")
        self.delete_button.add_css_class("destructive-action")
        self.delete_button.connect("clicked", self._on_delete)
        picker_row.append(self.delete_button)
        box.append(picker_row)

        self.editor = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.append(self.editor)

        self.editor.append(Gtk.Label(label="Name", xalign=0))
        self.name_entry = Gtk.Entry()
        self.editor.append(self.name_entry)

        self.editor.append(Gtk.Label(label="Description", xalign=0))
        self.description_entry = Gtk.Entry()
        self.editor.append(self.description_entry)

        self.editor.append(Gtk.Label(label="Base annual assumptions", xalign=0))
        rates = Gtk.Grid(column_spacing=12, row_spacing=8)
        self.rate_controls: dict[str, Gtk.SpinButton] = {}
        for row, (label, attribute) in enumerate(_ASSUMPTIONS):
            rates.attach(Gtk.Label(label=label, xalign=0), 0, row, 1, 1)
            control = Gtk.SpinButton.new_with_range(-100.0, 100.0, 0.1)
            control.set_digits(2)
            control.set_tooltip_text("Annual percentage; 6.00 means 6% per year")
            rates.attach(control, 1, row, 1, 1)
            rates.attach(Gtk.Label(label="%", xalign=0), 2, row, 1, 1)
            self.rate_controls[attribute] = control
        self.editor.append(rates)

        account_row = Gtk.Box(spacing=8)
        self.account_summary = Gtk.Label(xalign=0, wrap=True)
        self.account_summary.add_css_class("dim")
        self.account_summary.set_hexpand(True)
        account_row.append(self.account_summary)
        self.account_button = Gtk.Button(label="Edit account-specific rates…")
        self.account_button.connect("clicked", self._on_edit_account_rates)
        account_row.append(self.account_button)
        self.editor.append(account_row)

        timeline_row = Gtk.Box(spacing=8)
        self.timeline_summary = Gtk.Label(xalign=0, wrap=True)
        self.timeline_summary.add_css_class("dim")
        self.timeline_summary.set_hexpand(True)
        timeline_row.append(self.timeline_summary)
        self.timeline_button = Gtk.Button(label="Edit dated assumptions…")
        self.timeline_button.connect("clicked", self._on_edit_timeline)
        timeline_row.append(self.timeline_button)
        self.editor.append(timeline_row)

        self.event_summary = Gtk.Label(xalign=0, wrap=True)
        self.event_summary.add_css_class("dim")
        self.editor.append(self.event_summary)

        self.status = Gtk.Label(xalign=0, wrap=True)
        box.append(self.status)

        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        close = Gtk.Button(label="Close")
        close.connect("clicked", lambda *_: self.close())
        buttons.append(close)
        self.save_button = Gtk.Button(label="Save changes")
        self.save_button.add_css_class("suggested-action")
        self.save_button.connect("clicked", self._on_save)
        buttons.append(self.save_button)
        box.append(buttons)

        self._reload()

    def _reload(self, select_handle: str | None = None) -> None:
        self._scenarios = list(self.db.iter_scenarios())
        model = Gtk.StringList()
        model.append("Base scenario")
        selected = 0
        for index, scenario in enumerate(self._scenarios, 1):
            model.append(scenario.name)
            if scenario.handle == select_handle:
                selected = index
        self._loading = True
        try:
            self.picker.set_model(model)
            self.picker.set_selected(selected)
        finally:
            self._loading = False
        self._load_selected()

    def _base_selected(self) -> bool:
        return self.picker.get_selected() == 0

    def _selected(self) -> Scenario | None:
        selected = self.picker.get_selected()
        if selected == 0:
            return self._baseline
        if selected > len(self._scenarios):
            return None
        return self._scenarios[selected - 1]

    def _on_selected(self, *_args) -> None:
        if not self._loading:
            self._load_selected()

    def _load_selected(self) -> None:
        scenario = self._selected()
        enabled = scenario is not None
        base = enabled and self._base_selected()
        self.editor.set_sensitive(enabled)
        self.save_button.set_sensitive(enabled)
        self.duplicate_button.set_sensitive(enabled)
        self.delete_button.set_sensitive(enabled and not base)
        self.timeline_button.set_sensitive(enabled and not base)
        self.name_entry.set_sensitive(enabled and not base)
        self.description_entry.set_sensitive(enabled and not base)
        if scenario is None:
            self.name_entry.set_text("")
            self.description_entry.set_text("")
            self.timeline_summary.set_text("No saved scenarios yet.")
            self.account_summary.set_text("")
            self.event_summary.set_text("")
            return

        self.name_entry.set_text("Base scenario" if base else scenario.name)
        self.description_entry.set_text("" if base else scenario.description)
        for _label, attribute in _ASSUMPTIONS:
            value = getattr(scenario.assumptions, attribute) * Decimal("100")
            self.rate_controls[attribute].set_value(float(value))
        self._account_rates = dict(scenario.assumptions.per_account)
        self._update_account_summary()
        periods = len(scenario.assumption_periods)
        changes = len(scenario.schedule_overrides)
        if base:
            self.timeline_summary.set_text(
                "Dated assumption periods belong to saved scenarios."
            )
            self.event_summary.set_text(
                "Base scenario uses the book's baseline scheduled and estimated activity."
            )
        else:
            self.timeline_summary.set_text(f"{periods} dated assumption period(s).")
            self.event_summary.set_text(
                f"{changes} scenario-specific recurring event change(s) are preserved here."
            )
        self.status.set_text("")
        self.status.remove_css_class("negative")

    def _on_save(self, _button) -> None:
        scenario = self._selected()
        if scenario is None:
            return
        if self._base_selected():
            for _label, attribute in _ASSUMPTIONS:
                value = Decimal(str(self.rate_controls[attribute].get_value())) / Decimal("100")
                setattr(self._baseline.assumptions, attribute, value)
            self._baseline.assumptions.per_account = dict(self._account_rates)
            persist_baseline_assumptions(self.manager, self.db)
            notify_planning_scenario_changed(self.manager)
            self.status.set_text("Base scenario assumptions saved in this book.")
            self.status.remove_css_class("negative")
            return
        name = self.name_entry.get_text().strip()
        if not name:
            self._error("Give the scenario a name first.")
            return
        duplicate = self.db.get_scenario_by_name(name)
        if duplicate is not None and duplicate.handle != scenario.handle:
            self._error(f'A scenario named "{name}" already exists.')
            return

        scenario.name = name
        scenario.description = self.description_entry.get_text().strip()
        for _label, attribute in _ASSUMPTIONS:
            value = Decimal(str(self.rate_controls[attribute].get_value())) / Decimal("100")
            setattr(scenario.assumptions, attribute, value)
        scenario.assumptions.per_account = dict(self._account_rates)
        with self.db.transaction(f"Update scenario {scenario.name}") as txn:
            self.db.commit_scenario(scenario, txn)
        self._reload(scenario.handle)
        notify_planning_scenario_changed(self.manager)
        self.status.set_text("Scenario saved.")
        self.status.remove_css_class("negative")

    def _projection_accounts(self):
        accounts = []
        for account in self.db.iter_accounts():
            if account.account_class is AccountClass.LIABILITY or (
                account.account_class is AccountClass.ASSET
                and account.atype.is_investment
            ):
                accounts.append(account)
        return sorted(accounts, key=lambda item: self.db.full_name(item).casefold())

    def _update_account_summary(self) -> None:
        count = len(self._account_rates)
        self.account_summary.set_text(
            f"{count} account-specific projection rate override(s)."
            if count
            else "No account-specific projection rate overrides."
        )

    def _on_edit_account_rates(self, _button) -> None:
        AccountAssumptionsDialog(
            self, self.db, self._projection_accounts(), self._account_rates,
            self._account_rates_saved,
        ).present()

    def _account_rates_saved(self, values: dict[str, Decimal]) -> None:
        self._account_rates = dict(values)
        self._update_account_summary()

    def _on_duplicate(self, _button) -> None:
        scenario = self._selected()
        if scenario is None:
            return
        clone = Scenario.from_dict(scenario.serialize())
        clone.handle = create_handle()
        clone.gid = ""
        clone.change = 0
        clone.name = self._unique_copy_name(scenario.name)
        with self.db.transaction(f"Duplicate scenario {scenario.name}") as txn:
            self.db.add_scenario(clone, txn)
        self._reload(clone.handle)
        self.status.set_text(f'Created "{clone.name}". Rename it or adjust assumptions as needed.')
        self.status.remove_css_class("negative")

    def _unique_copy_name(self, name: str) -> str:
        base = f"{name} copy"
        candidate = base
        number = 2
        while self.db.get_scenario_by_name(candidate) is not None:
            candidate = f"{base} {number}"
            number += 1
        return candidate

    def _on_edit_timeline(self, _button) -> None:
        scenario = self._selected()
        if scenario is None or self._base_selected():
            return
        AssumptionTimelineDialog(self, self.db, scenario, self._timeline_saved).present()

    def _timeline_saved(self, handle: str) -> None:
        self._reload(handle)
        self.status.set_text("Dated assumptions saved.")
        self.status.remove_css_class("negative")

    def _on_delete(self, _button) -> None:
        scenario = self._selected()
        if scenario is None or self._base_selected():
            return
        ScenarioDeleteDialog(self, self.db, scenario, self._after_delete).present()

    def _after_delete(self) -> None:
        self._reload()
        self.status.set_text("Scenario deleted. Base scenario was not changed.")
        self.status.remove_css_class("negative")

    def _error(self, message: str) -> None:
        self.status.set_text(message)
        self.status.add_css_class("negative")


class AccountAssumptionsDialog(Gtk.Window):
    """Edit optional annual Projection rates for individual accounts."""

    def __init__(self, parent, db, accounts, values, callback) -> None:
        super().__init__(
            title="Account-specific projection rates", transient_for=parent, modal=True
        )
        self.db = db
        self.accounts = accounts
        self.callback = callback
        self.entries: dict[str, Gtk.Entry] = {}
        self.set_default_size(620, 480)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for side in ("top", "bottom", "start", "end"):
            getattr(outer, f"set_margin_{side}")(16)
        self.set_child(outer)
        outer.append(Gtk.Label(
            label=(
                "Blank values inherit the account's own annual rate when present, then "
                "the scenario's investment or liability default."
            ),
            xalign=0, wrap=True,
        ))
        grid = Gtk.Grid(column_spacing=12, row_spacing=6)
        for row, account in enumerate(accounts):
            grid.attach(Gtk.Label(label=db.full_name(account), xalign=0), 0, row, 1, 1)
            entry = Gtk.Entry(placeholder_text="inherit")
            if account.handle in values:
                entry.set_text(str(values[account.handle] * Decimal("100")))
            grid.attach(entry, 1, row, 1, 1)
            grid.attach(Gtk.Label(label="%", xalign=0), 2, row, 1, 1)
            self.entries[account.handle] = entry
        scroll = Gtk.ScrolledWindow(child=grid)
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        outer.append(scroll)
        self.status = Gtk.Label(xalign=0, wrap=True)
        outer.append(self.status)
        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        buttons.append(cancel)
        save = Gtk.Button(label="Use these rates")
        save.add_css_class("suggested-action")
        save.connect("clicked", self._on_save)
        buttons.append(save)
        outer.append(buttons)

    def _on_save(self, _button) -> None:
        values: dict[str, Decimal] = {}
        try:
            for handle, entry in self.entries.items():
                raw = entry.get_text().strip()
                if not raw:
                    continue
                rate = Decimal(raw) / Decimal("100")
                if rate < Decimal("-1") or rate > Decimal("1"):
                    raise ValueError("Rates must be between -100% and 100%.")
                values[handle] = rate
        except (InvalidOperation, ValueError) as error:
            self.status.set_text(str(error) or "Enter valid annual percentages.")
            self.status.add_css_class("negative")
            return
        self.callback(values)
        self.close()


class AssumptionTimelineDialog(Gtk.Window):
    """List and edit dated overrides for one saved scenario."""

    def __init__(
        self,
        parent: Gtk.Window,
        db: DbSQLite,
        scenario: Scenario,
        saved_callback,
    ) -> None:
        super().__init__(
            title=f"Dated assumptions — {scenario.name}",
            transient_for=parent,
            modal=True,
        )
        self.db = db
        self.scenario = scenario
        self.saved_callback = saved_callback
        self._periods: list[AssumptionPeriod] = []
        self.set_default_size(720, 420)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(18)
        self.set_child(box)
        box.append(
            Gtk.Label(
                label=(
                    "Dated values override the scenario's base annual assumptions only for "
                    "their date range. Leave a rate blank to inherit the value already in force."
                ),
                xalign=0,
                wrap=True,
            )
        )

        row = Gtk.Box(spacing=8)
        self.picker = Gtk.DropDown()
        self.picker.set_hexpand(True)
        row.append(self.picker)
        add = Gtk.Button(label="Add…")
        add.connect("clicked", self._on_add)
        row.append(add)
        self.edit_button = Gtk.Button(label="Edit…")
        self.edit_button.connect("clicked", self._on_edit)
        row.append(self.edit_button)
        self.delete_button = Gtk.Button(label="Delete")
        self.delete_button.add_css_class("destructive-action")
        self.delete_button.connect("clicked", self._on_delete)
        row.append(self.delete_button)
        box.append(row)

        self.summary = Gtk.Label(xalign=0, wrap=True)
        self.summary.add_css_class("dim")
        box.append(self.summary)
        close = Gtk.Button(label="Close", halign=Gtk.Align.END)
        close.connect("clicked", lambda *_: self.close())
        box.append(close)
        self._reload()

    def _reload(self, selected: int = 0) -> None:
        self._periods = sorted(
            self.scenario.assumption_periods,
            key=lambda period: (period.start, period.end or date.max),
        )
        model = Gtk.StringList()
        for period in self._periods:
            through = period.end.isoformat() if period.end is not None else "onward"
            detail = period.description.strip() or self._changed_rates(period)
            model.append(f"{period.start.isoformat()} — {through}: {detail}")
        self.picker.set_model(model)
        if self._periods:
            self.picker.set_selected(min(selected, len(self._periods) - 1))
        enabled = bool(self._periods)
        self.edit_button.set_sensitive(enabled)
        self.delete_button.set_sensitive(enabled)
        self.summary.set_text(
            f"{len(self._periods)} dated assumption period(s). Later-starting overlapping "
            "periods win for values they override."
        )

    @staticmethod
    def _changed_rates(period: AssumptionPeriod) -> str:
        names = [
            label
            for label, attribute in _ASSUMPTIONS
            if getattr(period, attribute) is not None
        ]
        return ", ".join(names) if names else "no rate overrides"

    def _selected_index(self) -> int | None:
        index = self.picker.get_selected()
        return index if index < len(self._periods) else None

    def _on_add(self, _button) -> None:
        AssumptionPeriodDialog(self, self.db, None, self._save_new).present()

    def _on_edit(self, _button) -> None:
        index = self._selected_index()
        if index is None:
            return
        AssumptionPeriodDialog(self, self.db, self._periods[index], self._save_edit).present()

    def _save_new(self, period: AssumptionPeriod) -> None:
        self.scenario.assumption_periods.append(period)
        self._commit()
        self._reload(len(self.scenario.assumption_periods) - 1)

    def _save_edit(self, period: AssumptionPeriod) -> None:
        index = self._selected_index()
        if index is None:
            return
        old = self._periods[index]
        original_index = self.scenario.assumption_periods.index(old)
        self.scenario.assumption_periods[original_index] = period
        self._commit()
        self._reload(index)

    def _on_delete(self, _button) -> None:
        index = self._selected_index()
        if index is None:
            return
        self.scenario.assumption_periods.remove(self._periods[index])
        self._commit()
        self._reload(max(0, index - 1))

    def _commit(self) -> None:
        with self.db.transaction(f"Update scenario {self.scenario.name}") as txn:
            self.db.commit_scenario(self.scenario, txn)
        self.saved_callback(self.scenario.handle)


class AssumptionPeriodDialog(Gtk.Window):
    """Edit one dated set of optional annual-rate overrides."""

    def __init__(
        self, parent: Gtk.Window, db: DbSQLite, period: AssumptionPeriod | None, callback
    ) -> None:
        super().__init__(
            title="Edit dated assumptions" if period is not None else "Add dated assumptions",
            transient_for=parent,
            modal=True,
        )
        self.db = db
        self.period = period
        self.callback = callback
        self._account_rates = dict(period.per_account) if period is not None else {}
        self.set_default_size(520, -1)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(18)
        self.set_child(box)

        grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        box.append(grid)
        grid.attach(Gtk.Label(label="Start (YYYY-MM-DD)", xalign=0), 0, 0, 1, 1)
        self.start_entry = Gtk.Entry()
        self.start_entry.set_text(period.start.isoformat() if period else date.today().isoformat())
        grid.attach(self.start_entry, 1, 0, 1, 1)
        grid.attach(Gtk.Label(label="Through (blank = onward)", xalign=0), 0, 1, 1, 1)
        self.end_entry = Gtk.Entry()
        if period is not None and period.end is not None:
            self.end_entry.set_text(period.end.isoformat())
        grid.attach(self.end_entry, 1, 1, 1, 1)
        grid.attach(Gtk.Label(label="Description", xalign=0), 0, 2, 1, 1)
        self.description_entry = Gtk.Entry()
        self.description_entry.set_text(period.description if period else "")
        grid.attach(self.description_entry, 1, 2, 1, 1)

        self.rate_entries: dict[str, Gtk.Entry] = {}
        for row, (label, attribute) in enumerate(_ASSUMPTIONS, 3):
            grid.attach(Gtk.Label(label=f"{label} %", xalign=0), 0, row, 1, 1)
            entry = Gtk.Entry(placeholder_text="inherit")
            value = getattr(period, attribute) if period is not None else None
            if value is not None:
                entry.set_text(str(value * Decimal("100")))
            grid.attach(entry, 1, row, 1, 1)
            self.rate_entries[attribute] = entry

        account_row = Gtk.Box(spacing=8)
        self.account_summary = Gtk.Label(xalign=0, wrap=True)
        self.account_summary.add_css_class("dim")
        self.account_summary.set_hexpand(True)
        account_row.append(self.account_summary)
        account_button = Gtk.Button(label="Edit dated account rates…")
        account_button.connect("clicked", self._on_edit_account_rates)
        account_row.append(account_button)
        box.append(account_row)
        self._update_account_summary()

        self.status = Gtk.Label(xalign=0, wrap=True)
        box.append(self.status)
        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        buttons.append(cancel)
        save = Gtk.Button(label="Save")
        save.add_css_class("suggested-action")
        save.connect("clicked", self._on_save)
        buttons.append(save)
        box.append(buttons)

    def _projection_accounts(self):
        accounts = []
        for account in self.db.iter_accounts():
            if account.account_class is AccountClass.LIABILITY or (
                account.account_class is AccountClass.ASSET
                and account.atype.is_investment
            ):
                accounts.append(account)
        return sorted(accounts, key=lambda item: self.db.full_name(item).casefold())

    def _update_account_summary(self) -> None:
        count = len(self._account_rates)
        self.account_summary.set_text(
            f"{count} dated account-rate override(s)."
            if count
            else "No dated account-rate overrides."
        )

    def _on_edit_account_rates(self, _button) -> None:
        AccountAssumptionsDialog(
            self, self.db, self._projection_accounts(), self._account_rates,
            self._account_rates_saved,
        ).present()

    def _account_rates_saved(self, values: dict[str, Decimal]) -> None:
        self._account_rates = dict(values)
        self._update_account_summary()

    def _on_save(self, _button) -> None:
        try:
            start = date.fromisoformat(self.start_entry.get_text().strip())
            end_text = self.end_entry.get_text().strip()
            end = date.fromisoformat(end_text) if end_text else None
            values: dict[str, Decimal | None] = {}
            for attribute, entry in self.rate_entries.items():
                text = entry.get_text().strip()
                values[attribute] = (
                    None if not text else Decimal(text) / Decimal("100")
                )
            period = AssumptionPeriod(
                start,
                end,
                description=self.description_entry.get_text().strip(),
                income_growth=values["income_growth"],
                expense_inflation=values["expense_inflation"],
                investment_return=values["investment_return"],
                cash_interest=values["cash_interest"],
                liability_interest=values["liability_interest"],
                per_account=dict(self._account_rates),
            )
        except (ValueError, InvalidOperation) as exc:
            self.status.set_text(f"Check the dates and percentages: {exc}")
            self.status.add_css_class("negative")
            return
        self.callback(period)
        self.close()


class ScenarioDeleteDialog(Gtk.Window):
    """Confirm destructive removal of one saved scenario."""

    def __init__(
        self,
        parent: Gtk.Window,
        db: DbSQLite,
        scenario: Scenario,
        deleted_callback,
    ) -> None:
        super().__init__(title="Delete scenario", transient_for=parent, modal=True)
        self.db = db
        self.scenario = scenario
        self.deleted_callback = deleted_callback

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(18)
        self.set_child(box)
        box.append(
            Gtk.Label(
                label=(
                    f'Delete scenario "{scenario.name}"? This removes its assumptions and '
                    f"{len(scenario.schedule_overrides)} scenario-specific recurring change(s). "
                    "Baseline transactions and schedules are not modified."
                ),
                xalign=0,
                wrap=True,
            )
        )
        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        buttons.append(cancel)
        delete = Gtk.Button(label="Delete scenario")
        delete.add_css_class("destructive-action")
        delete.connect("clicked", self._confirm)
        buttons.append(delete)
        box.append(buttons)

    def _confirm(self, _button) -> None:
        with self.db.transaction(f"Delete scenario {self.scenario.name}") as txn:
            self.db.remove_scenario(self.scenario.handle, txn)
        self.close()
        self.deleted_callback()
