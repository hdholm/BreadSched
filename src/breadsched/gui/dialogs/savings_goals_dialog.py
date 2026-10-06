"""Add, fund, close, and review savings goals through the shared goal service.

A goal sets aside a prorated share of each income received until its target
date, like a pending bill; extra money can be allocated at any time. The goal
never creates transactions. Every write is one undo step, and a rejected save
leaves the stored goal unchanged.
"""

from __future__ import annotations

from datetime import date

from ...gen.db.sqlite import DbSQLite
from ...gen.lib.money import Money
from ...gen.lib.savings_goal import SavingsGoal
from ...gen.services.savings_goals import (
    AllocateToGoal,
    SaveSavingsGoal,
    SavingsGoalReport,
    SetGoalOverride,
    allocate_to_goal,
    close_savings_goal,
    delete_savings_goal,
    goal_accounts,
    goal_overrides,
    purchase_accounts,
    query_savings_goals,
    reopen_savings_goal,
    save_savings_goal,
    set_goal_override,
)
from ...gen.utils.amount_input import parse_user_amount
from ...presentation import goal_override_text, goal_status_text, service_error_message
from ..gi_setup import Gtk
from ..widgets.bounded import BoundedWindow, scroll_body
from ..widgets.choice import bounded_dropdown
from ..widgets.help import help_row

__all__ = ["SavingsGoalsDialog"]


class SavingsGoalsDialog(BoundedWindow):
    """Goal list with each goal's progress, above the goal editor."""

    def __init__(self, parent: Gtk.Window | None, db: DbSQLite, today: date | None = None) -> None:
        super().__init__(title="Savings Goals", transient_for=parent, modal=True)
        self.set_destroy_with_parent(True)
        self.set_default_size(900, 640)
        self.db = db
        self.today = today or date.today()
        #: The goal being edited, or ``None`` when the form adds a new one.
        self.editing: str | None = None
        self.report: SavingsGoalReport | None = None

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(18)
        self.set_child(box)
        box.prepend(help_row("goals"))
        box.append(
            Gtk.Label(
                label=(
                    "From its start date, each income received sets aside a share of what a "
                    "goal still needs, so the whole target is set aside by its target date. "
                    "Extra money you allocate is set aside in full. Goal money is held out of "
                    "the Dashboard's Available, like a bill's reserve; nothing is spent on the "
                    "target date. Close a goal to release its money."
                ),
                xalign=0,
                wrap=True,
            )
        )
        self.show_closed = Gtk.CheckButton(label="Show closed goals")
        self.show_closed.connect("toggled", lambda _b: self.refresh())
        box.append(self.show_closed)
        scroller = Gtk.ScrolledWindow(min_content_height=140, vexpand=True)
        self.goal_rows = Gtk.Grid(column_spacing=14, row_spacing=4)
        scroller.set_child(self.goal_rows)
        box.append(scroller)
        self.totals = Gtk.Label(xalign=0, wrap=True)
        box.append(self.totals)

        box.append(Gtk.Label(label="Goal", xalign=0, css_classes=["heading"]))
        form = Gtk.Grid(column_spacing=10, row_spacing=6)
        self.name_entry = Gtk.Entry(hexpand=True, placeholder_text="New roof")
        self.account_picker = bounded_dropdown()
        self.target_entry = Gtk.Entry(placeholder_text="12000.00")
        self.start_entry = Gtk.Entry(placeholder_text="YYYY-MM-DD")
        self.target_date_entry = Gtk.Entry(placeholder_text="YYYY-MM-DD")
        self.description_entry = Gtk.Entry(placeholder_text="Optional")
        for row, (label, widget) in enumerate(
            (
                ("Name", self.name_entry),
                ("Held in", self.account_picker),
                ("Target amount", self.target_entry),
                ("Start saving", self.start_entry),
                ("Target date", self.target_date_entry),
                ("Description", self.description_entry),
            )
        ):
            form.attach(Gtk.Label(label=label, xalign=0), 0, row, 1, 1)
            form.attach(widget, 1, row, 1, 1)
        actions = Gtk.Box(spacing=8)
        self.save_button = Gtk.Button(label="Add goal")
        self.save_button.add_css_class("suggested-action")
        self.save_button.connect("clicked", lambda _b: self.save())
        new_button = Gtk.Button(label="New goal")
        new_button.connect("clicked", lambda _b: self.edit(None))
        actions.append(self.save_button)
        actions.append(new_button)
        form.attach(actions, 1, 6, 1, 1)
        box.append(form)

        allocate = Gtk.Box(spacing=8)
        allocate.append(Gtk.Label(label="Allocate extra"))
        self.allocate_amount = Gtk.Entry(placeholder_text="Amount")
        self.allocate_date = Gtk.Entry(text=self.today.isoformat())
        self.allocate_memo = Gtk.Entry(placeholder_text="Memo", hexpand=True)
        self.allocate_button = Gtk.Button(label="Allocate to goal")
        self.allocate_button.connect("clicked", lambda _b: self.allocate())
        for widget in (
            self.allocate_amount,
            self.allocate_date,
            self.allocate_memo,
            self.allocate_button,
        ):
            allocate.append(widget)
        box.append(allocate)

        # Goals apply to every scenario unless one changes them here.
        override = Gtk.Box(spacing=8)
        override.append(Gtk.Label(label="In scenario"))
        self.scenario_picker = bounded_dropdown()
        self.override_target = Gtk.Entry(placeholder_text="Target amount", width_chars=12)
        self.override_date = Gtk.Entry(placeholder_text="Target date", width_chars=12)
        self.override_excluded = Gtk.CheckButton(label="Leave out")
        # Model the purchase in that scenario: the target is spent on this date.
        self.purchase_on = Gtk.Entry(placeholder_text="Buy on", width_chars=12)
        self.purchase_on.set_tooltip_text(
            "In this scenario, spend the target on this date (on or after the target date)"
        )
        self.purchase_picker = bounded_dropdown()
        self.override_button = Gtk.Button(label="Apply to scenario")
        self.override_button.set_tooltip_text(
            "Leave every field empty and Leave out unchecked to follow the goal unchanged"
        )
        self.override_button.connect("clicked", lambda _b: self.apply_override())
        for widget in (
            self.scenario_picker,
            self.override_target,
            self.override_date,
            self.override_excluded,
            self.override_button,
        ):
            override.append(widget)
        box.append(override)
        purchase = Gtk.Box(spacing=8)
        purchase.append(Gtk.Label(label="Purchase in that scenario"))
        purchase.append(self.purchase_on)
        purchase.append(self.purchase_picker)
        box.append(purchase)

        self.status = Gtk.Label(xalign=0, wrap=True, selectable=True)
        box.append(self.status)
        # The list and three forms outgrow a laptop screen; the status stays put.
        scroll_body(self)
        self._accounts: list[str] = []
        self._scenarios: list[str] = []
        #: Accounts a purchase can go to; index 0 of the picker is "no purchase".
        self._purchase_accounts: list[str] = []
        self._load_accounts()
        self.edit(None)
        self.refresh()

    # ------------------------------------------------------------------ views

    def _load_accounts(self) -> None:
        choices = goal_accounts(self.db)
        self._accounts = [handle for handle, _name in choices]
        self.account_picker.set_model(Gtk.StringList.new([name for _handle, name in choices]))
        scenarios = sorted(self.db.iter_scenarios(), key=lambda item: item.name.casefold())
        self._scenarios = [scenario.handle for scenario in scenarios]
        self.scenario_picker.set_model(
            Gtk.StringList.new([scenario.name for scenario in scenarios])
        )
        purchases = purchase_accounts(self.db)
        self._purchase_accounts = [handle for handle, _name in purchases]
        self.purchase_picker.set_model(
            Gtk.StringList.new(["No purchase", *(f"Buy into {name}" for _h, name in purchases)])
        )

    def refresh(self) -> None:
        """Reload every goal's progress on today's date."""
        result = query_savings_goals(
            self.db, self.today, include_closed=self.show_closed.get_active()
        )
        self.report = report = result.value
        _clear(self.goal_rows)
        headings = (
            "Goal",
            "Held in",
            "Target date",
            "Target",
            "Set aside",
            "Remaining",
            "Status",
            "Scenario changes",
        )
        changes = goal_overrides(self.db)
        for column, heading in enumerate(headings):
            label = Gtk.Label(label=heading, xalign=1 if 3 <= column <= 5 else 0)
            label.add_css_class("dim")
            self.goal_rows.attach(label, column, 0, 1, 1)
        goals = report.goals if report is not None else ()
        if not goals:
            self.goal_rows.attach(Gtk.Label(label="No savings goals yet.", xalign=0), 0, 1, 8, 1)
        for row, item in enumerate(goals, start=1):
            cells = (
                (item.goal.name, False),
                (item.account_name, False),
                (item.goal.target_date.isoformat(), False),
                (item.target.format(), True),
                (item.set_aside.format(), True),
                (item.remaining.format(), True),
                (goal_status_text(item), False),
                (
                    "\n".join(
                        goal_override_text(scenario.name, override)
                        for scenario, override in changes.get(item.goal.handle, [])
                    )
                    or "—",
                    False,
                ),
            )
            for column, (text, numeric) in enumerate(cells):
                label = Gtk.Label(label=text, xalign=1 if numeric else 0, selectable=True)
                if numeric:
                    label.add_css_class("numeric")
                self.goal_rows.attach(label, column, row, 1, 1)
            handle = item.goal.handle
            edit = Gtk.Button(label="Edit")
            edit.connect("clicked", lambda _b, h=handle: self.edit(h))
            closed = item.goal.closed_on is not None
            toggle = Gtk.Button(label="Reopen" if closed else "Close")
            toggle.connect("clicked", lambda _b, h=handle, c=closed: self.toggle_closed(h, c))
            delete = Gtk.Button(label="Delete")
            delete.connect("clicked", lambda _b, h=handle: self.delete(h))
            for offset, button in enumerate((edit, toggle, delete)):
                self.goal_rows.attach(button, 8 + offset, row, 1, 1)
        if report is not None and goals:
            text = f"Set aside for goals: {report.set_aside.format()}"
            if report.held != report.set_aside:
                text += f"; held from spendable cash: {report.held.format()}"
            self.totals.set_text(text)
        else:
            self.totals.set_text("")

    def edit(self, handle: str | None) -> None:
        """Load a goal into the form, or clear it to add a new one."""
        goal = self.db.get_savings_goal(handle) if handle is not None else None
        self.editing = goal.handle if goal is not None else None
        self.name_entry.set_text(goal.name if goal is not None else "")
        if goal is not None and goal.account in self._accounts:
            self.account_picker.set_selected(self._accounts.index(goal.account))
        self.target_entry.set_text(str(goal.target_amount.to_decimal()) if goal else "")
        self.start_entry.set_text((goal.start_date if goal else self.today).isoformat())
        self.target_date_entry.set_text(goal.target_date.isoformat() if goal else "")
        self.description_entry.set_text(goal.description if goal is not None else "")
        self.save_button.set_label("Save changes" if goal is not None else "Add goal")
        self.allocate_button.set_sensitive(goal is not None)
        self.override_button.set_sensitive(goal is not None and bool(self._scenarios))

    # ----------------------------------------------------------------- writes

    def _date(self, entry: Gtk.Entry, label: str) -> date:
        try:
            return date.fromisoformat(entry.get_text().strip())
        except ValueError:
            raise ValueError(f"Enter the {label} as YYYY-MM-DD.") from None

    def _money(self, entry: Gtk.Entry, label: str) -> Money:
        try:
            return Money(parse_user_amount(entry.get_text().strip()))
        except (ValueError, ArithmeticError):
            raise ValueError(f"Enter a valid {label}.") from None

    def save(self) -> SavingsGoal | None:
        """Add or update the goal in the form; None when refused."""
        if not self._accounts:
            self._message("Add an asset account to hold the goal's money first.", True)
            return None
        try:
            request = SaveSavingsGoal(
                name=self.name_entry.get_text(),
                account=self._accounts[self.account_picker.get_selected()],
                target_amount=self._money(self.target_entry, "target amount"),
                start_date=self._date(self.start_entry, "start date"),
                target_date=self._date(self.target_date_entry, "target date"),
                description=self.description_entry.get_text(),
                handle=self.editing,
            )
        except ValueError as exc:
            self._message(str(exc), True)
            return None
        result = save_savings_goal(self.db, request)
        if result.value is None:
            self._message(service_error_message(result.errors[0]), True)
            return None
        self.edit(result.value.handle)
        self.refresh()
        self._message(f"Saved {result.value.name}.", False)
        return result.value

    def allocate(self) -> SavingsGoal | None:
        """Set extra money aside for the goal being edited."""
        if self.editing is None:
            self._message("Choose a goal to allocate to.", True)
            return None
        try:
            request = AllocateToGoal(
                self.editing,
                self._money(self.allocate_amount, "amount to allocate"),
                self._date(self.allocate_date, "allocation date"),
                self.allocate_memo.get_text().strip(),
            )
        except ValueError as exc:
            self._message(str(exc), True)
            return None
        result = allocate_to_goal(self.db, request)
        if result.value is None:
            self._message(service_error_message(result.errors[0]), True)
            return None
        self.allocate_amount.set_text("")
        self.allocate_memo.set_text("")
        self.refresh()
        self._message(f"Allocated {request.amount.format()} to {result.value.name}.", False)
        return result.value

    def apply_override(self) -> bool:
        """Change the goal being edited in the chosen scenario, or clear the change."""
        if self.editing is None or not self._scenarios:
            self._message("Choose a goal, and save a scenario first.", True)
            return False
        target_text = self.override_target.get_text().strip()
        date_text = self.override_date.get_text().strip()
        bought_text = self.purchase_on.get_text().strip()
        try:
            target = self._money(self.override_target, "target amount") if target_text else None
            when = self._date(self.override_date, "target date") if date_text else None
            bought = self._date(self.purchase_on, "purchase date") if bought_text else None
        except ValueError as exc:
            self._message(str(exc), True)
            return False
        scenario = self._scenarios[self.scenario_picker.get_selected()]
        picked = self.purchase_picker.get_selected()
        into = (
            self._purchase_accounts[picked - 1]
            if 0 < picked <= len(self._purchase_accounts)
            else None
        )
        result = set_goal_override(
            self.db,
            SetGoalOverride(
                scenario,
                self.editing,
                target,
                when,
                self.override_excluded.get_active(),
                purchase_on=bought,
                purchase_account=into,
            ),
        )
        if result.value is None:
            self._message(service_error_message(result.errors[0]), True)
            return False
        override = result.value.goal_overrides.get(self.editing)
        self.refresh()
        self._message(
            goal_override_text(result.value.name, override)
            if override is not None
            else f"{result.value.name} follows the goal unchanged.",
            False,
        )
        return True

    def toggle_closed(self, handle: str, closed: bool) -> SavingsGoal | None:
        result = (
            reopen_savings_goal(self.db, handle)
            if closed
            else close_savings_goal(self.db, handle, self.today)
        )
        if result.value is None:
            self._message(service_error_message(result.errors[0]), True)
            return None
        self.refresh()
        verb = "Reopened" if closed else "Closed"
        self._message(f"{verb} {result.value.name}.", False)
        return result.value

    def delete(self, handle: str) -> bool:
        goal = self.db.get_savings_goal(handle)
        result = delete_savings_goal(self.db, handle)
        if result.value is None:
            self._message(service_error_message(result.errors[0]), True)
            return False
        if self.editing == handle:
            self.edit(None)
        self.refresh()
        name = goal.name if goal is not None else "the goal"
        self._message(f"Deleted {name}. Edit → Undo restores it.", False)
        return True

    def _message(self, text: str, error: bool) -> None:
        self.status.set_text(text)
        if error:
            self.status.add_css_class("negative")
        else:
            self.status.remove_css_class("negative")


def _clear(grid: Gtk.Grid) -> None:
    child = grid.get_first_child()
    while child is not None:
        following = child.get_next_sibling()
        grid.remove(child)
        child = following
