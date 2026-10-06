"""Paychecks, pay changes, and payroll templates through the shared payroll service.

The dialog reads any schedule shaped like a paycheck (gross pay into an income
account, a bank or cash deposit, and taxes, deductions, savings, or repayments
between) and shows its lines as of a date. **Pay change** saves a raise or other
change from a date as per-leg future amounts, so earlier paychecks keep their
amounts. **Templates** keeps reusable paycheck descriptions, with each line a fixed
amount or a percentage of gross, and creates new paycheck schedules from them.
Every write is one undo step, and a rejected request leaves the book unchanged.
"""

from __future__ import annotations

from datetime import date

from ...gen.db.sqlite import DbSQLite
from ...gen.engine.payroll import (
    PayChangePlan,
    PaycheckBreakdown,
    PayLegKind,
    PayrollError,
    PayrollLine,
    PayrollTemplate,
    leg_kind,
    parse_percent,
    paycheck_breakdown,
)
from ...gen.lib.account import AccountClass
from ...gen.lib.money import Money
from ...gen.lib.recurrence import PeriodType, Recurrence
from ...gen.services.contracts import ServiceError, ServiceResult
from ...gen.services.payroll import (
    CreatePaycheck,
    PayChange,
    SavePayrollTemplate,
    apply_pay_change,
    create_paycheck_schedule,
    delete_payroll_template,
    list_payroll_templates,
    paychecks,
    preview_pay_change,
    save_payroll_template,
    template_from_schedule,
)
from ...gen.utils.amount_input import parse_user_amount
from ...presentation import service_error_message
from ..gi_setup import Gtk
from ..widgets.bounded import BoundedWindow
from ..widgets.choice import bounded_dropdown
from ..widgets.help import help_row

__all__ = ["PAY_PERIODS", "PayrollDialog"]

#: (label, period, interval) for a new paycheck's pay period.
PAY_PERIODS = (
    ("Every 2 weeks", PeriodType.WEEK, 2),
    ("Weekly", PeriodType.WEEK, 1),
    ("Twice a month (15th and last day)", PeriodType.SEMI_MONTH, 1),
    ("Monthly", PeriodType.MONTH, 1),
)
#: How a line changes with a pay change.
CHANGE_HOW = ("Keep", "Scale with gross", "Set to")


def _recurrence(period: PeriodType, interval: int, start: date) -> Recurrence:
    if period is PeriodType.SEMI_MONTH:
        return Recurrence(period, start=start, day_of_month=15, second_day_of_month=-1)
    return Recurrence(period, interval=interval, start=start)


def _clear(grid: Gtk.Grid) -> None:
    child = grid.get_first_child()
    while child is not None:
        following = child.get_next_sibling()
        grid.remove(child)
        child = following


def _heading(grid: Gtk.Grid, headings: tuple[str, ...]) -> None:
    for column, text in enumerate(headings):
        label = Gtk.Label(label=text, xalign=0)
        label.add_css_class("dim")
        grid.attach(label, column, 0, 1, 1)


def _amount_label(value: Money) -> Gtk.Label:
    label = Gtk.Label(label=value.format(), xalign=1)
    label.add_css_class("numeric")
    return label


class PayrollDialog(BoundedWindow):
    """A paycheck's lines, its pay changes, and reusable payroll templates."""

    def __init__(
        self, parent: Gtk.Window | None, db: DbSQLite, schedule: str | None = None
    ) -> None:
        super().__init__(title="Payroll", transient_for=parent, modal=True)
        self.set_destroy_with_parent(True)
        self.set_default_size(900, 680)
        self.db = db
        self.paychecks: list[PaycheckBreakdown] = []
        self._after_labels: dict[str, Gtk.Label] = {}
        self.templates: list[PayrollTemplate] = []
        self.change_rows: dict[str, tuple[Gtk.DropDown, Gtk.Entry, Gtk.Label]] = {}
        self.template_rows: list[tuple[Gtk.DropDown, Gtk.Entry]] = []
        self._loaded_template: str | None = None
        self.last_plan: PayChangePlan | None = None

        self.income_accounts = self._accounts(
            lambda account: account.account_class is AccountClass.INCOME
        )
        self.deposit_accounts = self._accounts(lambda account: account.is_spendable_cash)
        self.line_accounts = self._accounts(
            lambda account: leg_kind(account, db.full_name(account) or "") is not None
        )

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(18)
        self.set_child(box)
        box.prepend(help_row("payroll"))

        top = Gtk.Box(spacing=8)
        top.append(Gtk.Label(label="Paycheck"))
        self.paycheck_picker = bounded_dropdown(["(no paychecks)"])
        self.paycheck_picker.set_hexpand(True)
        self.paycheck_picker.connect("notify::selected", lambda *_args: self._paycheck_changed())
        top.append(self.paycheck_picker)
        box.append(top)

        self.notebook = Gtk.Notebook(vexpand=True)
        self.notebook.append_page(self._breakdown_page(), Gtk.Label(label="Breakdown"))
        self.notebook.append_page(self._change_page(), Gtk.Label(label="Pay change"))
        self.notebook.append_page(self._templates_page(), Gtk.Label(label="Templates"))
        box.append(self.notebook)

        self.status = Gtk.Label(xalign=0, wrap=True, selectable=True)
        box.append(self.status)
        self.refresh(select=schedule)

    def _accounts(self, wanted) -> list:
        return sorted(
            (
                account
                for account in self.db.iter_accounts()
                if not account.placeholder and not account.is_root and wanted(account)
            ),
            key=lambda account: (self.db.full_name(account) or "").casefold(),
        )

    def _name(self, handle: str) -> str:
        return self.db.full_name(handle) or handle

    def _message(self, text: str, error: bool = False) -> None:
        self.status.set_text(text)
        if error:
            self.status.add_css_class("error")
        else:
            self.status.remove_css_class("error")

    def _refused(self, result: ServiceResult) -> None:
        self._message(
            "; ".join(service_error_message(error) for error in result.errors) + ".", True
        )

    # ------------------------------------------------------------------- pages

    def _page(self) -> Gtk.Box:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for side in ("top", "bottom", "start", "end"):
            getattr(page, f"set_margin_{side}")(10)
        return page

    def _breakdown_page(self) -> Gtk.Widget:
        page = self._page()
        row = Gtk.Box(spacing=8)
        row.append(Gtk.Label(label="As of"))
        self.as_of_entry = Gtk.Entry(text=date.today().isoformat(), width_chars=12)
        self.as_of_entry.connect("activate", lambda _entry: self.show_breakdown())
        row.append(self.as_of_entry)
        show = Gtk.Button(label="Show")
        show.connect("clicked", lambda _b: self.show_breakdown())
        row.append(show)
        page.append(row)
        self.summary = Gtk.Label(xalign=0, wrap=True)
        page.append(self.summary)
        scroller = Gtk.ScrolledWindow(vexpand=True, min_content_height=200)
        self.breakdown_rows = Gtk.Grid(column_spacing=14, row_spacing=4)
        scroller.set_child(self.breakdown_rows)
        page.append(scroller)
        return page

    def _change_page(self) -> Gtk.Widget:
        page = self._page()
        page.append(
            Gtk.Label(
                label=(
                    "A pay change applies from its date onward; earlier paychecks keep "
                    "their amounts. Taxes scale with gross by default, other lines stay "
                    "the same, and the net deposit takes the difference."
                ),
                xalign=0,
                wrap=True,
            )
        )
        form = Gtk.Box(spacing=8)
        form.append(Gtk.Label(label="From"))
        self.change_start_entry = Gtk.Entry(width_chars=12, placeholder_text="YYYY-MM-DD")
        form.append(self.change_start_entry)
        form.append(Gtk.Label(label="New gross"))
        self.change_gross_entry = Gtk.Entry(width_chars=12, placeholder_text="0.00")
        form.append(self.change_gross_entry)
        page.append(form)
        scroller = Gtk.ScrolledWindow(vexpand=True, min_content_height=180)
        self.change_rows_grid = Gtk.Grid(column_spacing=14, row_spacing=4)
        scroller.set_child(self.change_rows_grid)
        page.append(scroller)
        buttons = Gtk.Box(spacing=8)
        preview = Gtk.Button(label="Preview")
        preview.connect("clicked", lambda _b: self.preview_change())
        buttons.append(preview)
        self.save_change_button = Gtk.Button(label="Save pay change")
        self.save_change_button.add_css_class("suggested-action")
        self.save_change_button.connect("clicked", lambda _b: self.save_change())
        buttons.append(self.save_change_button)
        page.append(buttons)
        return page

    def _templates_page(self) -> Gtk.Widget:
        page = self._page()
        pick = Gtk.Box(spacing=8)
        pick.append(Gtk.Label(label="Template"))
        self.template_picker = bounded_dropdown(["New template"])
        self.template_picker.set_hexpand(True)
        self.template_picker.connect("notify::selected", lambda *_args: self._template_changed())
        pick.append(self.template_picker)
        fill = Gtk.Button(label="Fill from paycheck")
        fill.set_tooltip_text("Describe the selected paycheck as a template; nothing is saved yet")
        fill.connect("clicked", lambda _b: self.fill_from_paycheck())
        pick.append(fill)
        page.append(pick)

        grid = Gtk.Grid(column_spacing=8, row_spacing=6)
        self.template_name_entry = Gtk.Entry(hexpand=True)
        self.income_picker = bounded_dropdown(
            [self._name(a.handle) for a in self.income_accounts] or ["(no income accounts)"]
        )
        self.deposit_picker = bounded_dropdown(
            [self._name(a.handle) for a in self.deposit_accounts] or ["(no bank accounts)"]
        )
        self.template_gross_entry = Gtk.Entry(placeholder_text="0.00")
        for row, (text, widget) in enumerate(
            (
                ("Name", self.template_name_entry),
                ("Gross pay into", self.income_picker),
                ("Net deposit to", self.deposit_picker),
                ("Usual gross", self.template_gross_entry),
            )
        ):
            grid.attach(Gtk.Label(label=text, xalign=0), 0, row, 1, 1)
            grid.attach(widget, 1, row, 1, 1)
        page.append(grid)

        page.append(
            Gtk.Label(
                label="Lines out of gross: an amount such as 85.50, or a percentage such as 6.2%",
                xalign=0,
                wrap=True,
            )
        )
        scroller = Gtk.ScrolledWindow(vexpand=True, min_content_height=140)
        self.template_lines_grid = Gtk.Grid(column_spacing=8, row_spacing=4)
        scroller.set_child(self.template_lines_grid)
        page.append(scroller)
        buttons = Gtk.Box(spacing=8)
        add_line = Gtk.Button(label="Add line")
        add_line.connect("clicked", lambda _b: self.add_template_line())
        buttons.append(add_line)
        save = Gtk.Button(label="Save template")
        save.add_css_class("suggested-action")
        save.connect("clicked", lambda _b: self.save_template())
        buttons.append(save)
        self.delete_template_button = Gtk.Button(label="Delete template")
        self.delete_template_button.add_css_class("destructive-action")
        self.delete_template_button.connect("clicked", lambda _b: self.delete_template())
        buttons.append(self.delete_template_button)
        page.append(buttons)

        page.append(
            Gtk.Label(label="New paycheck from this template", xalign=0, css_classes=["heading"])
        )
        create = Gtk.Box(spacing=8)
        self.new_name_entry = Gtk.Entry(placeholder_text="Schedule name", hexpand=True)
        self.new_start_entry = Gtk.Entry(
            text=date.today().isoformat(), width_chars=12, placeholder_text="First payday"
        )
        self.new_period_picker = bounded_dropdown([label for label, *_rest in PAY_PERIODS])
        self.new_gross_entry = Gtk.Entry(width_chars=12, placeholder_text="Gross (optional)")
        self.create_button = Gtk.Button(label="Create paycheck")
        self.create_button.connect("clicked", lambda _b: self.create_paycheck())
        for widget in (
            self.new_name_entry,
            self.new_start_entry,
            self.new_period_picker,
            self.new_gross_entry,
            self.create_button,
        ):
            create.append(widget)
        page.append(create)
        return page

    # ----------------------------------------------------------------- refresh

    def refresh(self, select: str | None = None) -> None:
        """Reload paychecks and templates, keeping or choosing a selection."""
        current = self.selected_paycheck()
        wanted = select or (current.schedule if current is not None else None)
        self.paychecks = paychecks(self.db)
        names = [item.name for item in self.paychecks] or ["(no paychecks)"]
        self.paycheck_picker.set_model(Gtk.StringList.new(names))
        index = next((i for i, item in enumerate(self.paychecks) if item.schedule == wanted), 0)
        self.paycheck_picker.set_selected(index)
        self._paycheck_changed()
        self._reload_templates(self._loaded_template)

    def selected_paycheck(self) -> PaycheckBreakdown | None:
        index = self.paycheck_picker.get_selected()
        return self.paychecks[index] if 0 <= index < len(self.paychecks) else None

    def select_paycheck(self, handle: str) -> None:
        index = next(i for i, item in enumerate(self.paychecks) if item.schedule == handle)
        self.paycheck_picker.set_selected(index)

    def _paycheck_changed(self) -> None:
        self.show_breakdown()
        self._build_change_rows()

    def _schedule(self):
        selected = self.selected_paycheck()
        return self.db.get_scheduled(selected.schedule) if selected is not None else None

    # --------------------------------------------------------------- breakdown

    def show_breakdown(self) -> PaycheckBreakdown | None:
        _clear(self.breakdown_rows)
        schedule = self._schedule()
        if schedule is None:
            self.summary.set_text(
                "No schedule reads as a paycheck. Create one from a template, or add "
                "a schedule with gross pay into an income account and the net into a "
                "bank account."
            )
            return None
        try:
            when = date.fromisoformat(self.as_of_entry.get_text().strip())
        except ValueError:
            self._message("Enter the date as YYYY-MM-DD.", True)
            return None
        breakdown = paycheck_breakdown(self.db, schedule, max(when, schedule.recurrence.start))
        if breakdown is None:
            self.summary.set_text(
                service_error_message(ServiceError("payroll.schedule.not_paycheck")) + "."
            )
            return None
        self.summary.set_text(
            f"Gross {breakdown.gross.format()}; taxes and deductions "
            f"{breakdown.withheld.format()}; take-home {breakdown.net.format()} "
            f"({breakdown.take_home_percent}% of gross)."
        )
        _heading(self.breakdown_rows, ("Line", "Account", "Amount"))
        for row, leg in enumerate(breakdown.legs, start=1):
            self.breakdown_rows.attach(Gtk.Label(label=leg.kind.label, xalign=0), 0, row, 1, 1)
            self.breakdown_rows.attach(Gtk.Label(label=leg.name, xalign=0), 1, row, 1, 1)
            self.breakdown_rows.attach(_amount_label(leg.amount), 2, row, 1, 1)
        return breakdown

    # -------------------------------------------------------------- pay change

    def _build_change_rows(self) -> None:
        _clear(self.change_rows_grid)
        self.change_rows = {}
        self._after_labels = {}
        self.last_plan = None
        schedule = self._schedule()
        breakdown = (
            paycheck_breakdown(self.db, schedule, max(date.today(), schedule.recurrence.start))
            if schedule is not None
            else None
        )
        self.save_change_button.set_sensitive(breakdown is not None)
        if breakdown is None:
            return
        if not self.change_start_entry.get_text().strip():
            self.change_start_entry.set_text(date.today().isoformat())
        self.change_gross_entry.set_text(str(breakdown.gross.to_decimal()))
        _heading(self.change_rows_grid, ("Line", "Account", "Now", "Change", "Amount", "After"))
        for row, leg in enumerate(breakdown.legs, start=1):
            self.change_rows_grid.attach(Gtk.Label(label=leg.kind.label, xalign=0), 0, row, 1, 1)
            self.change_rows_grid.attach(Gtk.Label(label=leg.name, xalign=0), 1, row, 1, 1)
            self.change_rows_grid.attach(_amount_label(leg.amount), 2, row, 1, 1)
            after = Gtk.Label(xalign=1)
            after.add_css_class("numeric")
            self.change_rows_grid.attach(after, 5, row, 1, 1)
            self._after_labels[leg.account] = after
            if leg.kind in (PayLegKind.GROSS, PayLegKind.NET):
                continue
            how = bounded_dropdown(list(CHANGE_HOW))
            how.set_selected(1 if leg.kind is PayLegKind.TAX else 0)
            amount = Gtk.Entry(width_chars=10, placeholder_text="0.00")
            amount.set_sensitive(False)
            how.connect(
                "notify::selected",
                lambda picker, _param, entry=amount: entry.set_sensitive(
                    picker.get_selected() == 2
                ),
            )
            self.change_rows_grid.attach(how, 3, row, 1, 1)
            self.change_rows_grid.attach(amount, 4, row, 1, 1)
            self.change_rows[leg.account] = (how, amount, after)

    def set_line_change(self, account: str, how: str, amount: str = "") -> None:
        """Choose how one line changes: "keep", "scale", or "set" (with ``amount``)."""
        picker, entry, _after = self.change_rows[account]
        picker.set_selected({"keep": 0, "scale": 1, "set": 2}[how])
        entry.set_text(amount)

    def _change_request(self) -> PayChange | None:
        selected = self.selected_paycheck()
        if selected is None:
            return None
        try:
            start = date.fromisoformat(self.change_start_entry.get_text().strip())
        except ValueError:
            self._message("Enter the pay change date as YYYY-MM-DD.", True)
            return None
        try:
            gross = Money(parse_user_amount(self.change_gross_entry.get_text().strip()))
        except (ValueError, ArithmeticError):
            self._message("Enter the new gross pay as an amount.", True)
            return None
        scaled: set[str] = set()
        amounts: dict[str, Money] = {}
        for account, (picker, entry, _after) in self.change_rows.items():
            choice = picker.get_selected()
            if choice == 1:
                scaled.add(account)
            elif choice == 2:
                try:
                    amounts[account] = Money(parse_user_amount(entry.get_text().strip()))
                except (ValueError, ArithmeticError):
                    self._message(f"Enter the new amount for {self._name(account)}.", True)
                    return None
        return PayChange(selected.schedule, start, gross, frozenset(scaled), amounts)

    def _show_plan(self, plan: PayChangePlan) -> None:
        for line in plan.lines:
            label = self._after_labels.get(line.account)
            if label is not None:
                label.set_text(line.after.format())

    def preview_change(self) -> PayChangePlan | None:
        """Show each line after the change; writes nothing."""
        request = self._change_request()
        if request is None:
            return None
        result = preview_pay_change(self.db, request)
        if result.value is None:
            self._refused(result)
            return None
        self.last_plan = result.value
        self._show_plan(result.value)
        self._message(
            f"From {request.start.isoformat()}: take-home "
            f"{result.value.total(PayLegKind.NET).format()}. Nothing is saved until you "
            "choose Save pay change."
        )
        return result.value

    def save_change(self) -> PayChangePlan | None:
        """Save the pay change as one undo step."""
        request = self._change_request()
        if request is None:
            return None
        result = apply_pay_change(self.db, request)
        if result.value is None:
            self._refused(result)
            return None
        selected = self.selected_paycheck()
        self.refresh(select=selected.schedule if selected else None)
        self._message(
            f"Saved the pay change from {request.start.isoformat()}: take-home "
            f"{result.value.total(PayLegKind.NET).format()}. Edit → Undo reverses it."
        )
        return result.value

    # --------------------------------------------------------------- templates

    def _reload_templates(self, select: str | None = None) -> None:
        self.templates = list_payroll_templates(self.db)
        self.template_picker.set_model(
            Gtk.StringList.new(["New template", *(item.name for item in self.templates)])
        )
        index = next(
            (
                i
                for i, item in enumerate(self.templates, start=1)
                if select is not None and item.name.casefold() == select.casefold()
            ),
            0,
        )
        self.template_picker.set_selected(index)
        self._template_changed()

    def selected_template(self) -> PayrollTemplate | None:
        index = self.template_picker.get_selected() - 1
        return self.templates[index] if 0 <= index < len(self.templates) else None

    def select_template(self, name: str) -> None:
        index = next(i for i, item in enumerate(self.templates, start=1) if item.name == name)
        self.template_picker.set_selected(index)

    def _template_changed(self) -> None:
        template = self.selected_template()
        self._loaded_template = template.name if template is not None else None
        self.delete_template_button.set_sensitive(template is not None)
        self.create_button.set_sensitive(template is not None)
        self._fill_template_form(template)

    def _fill_template_form(self, template: PayrollTemplate | None) -> None:
        _clear(self.template_lines_grid)
        self.template_rows = []
        if template is None:
            self.template_name_entry.set_text("")
            self.template_gross_entry.set_text("")
            return
        self.template_name_entry.set_text(template.name)
        self.template_gross_entry.set_text(str(template.gross.to_decimal()))
        for picker, accounts, handle in (
            (self.income_picker, self.income_accounts, template.income_account),
            (self.deposit_picker, self.deposit_accounts, template.deposit_account),
        ):
            index = next((i for i, a in enumerate(accounts) if a.handle == handle), 0)
            picker.set_selected(index)
        for line in template.lines:
            text = (
                f"{line.percent}%"
                if line.percent is not None
                else str(line.amount.to_decimal())
                if line.amount is not None
                else ""
            )
            self.add_template_line(line.account, text)

    def add_template_line(self, account: str | None = None, text: str = "") -> None:
        row = len(self.template_rows)
        picker = bounded_dropdown(
            [self._name(a.handle) for a in self.line_accounts] or ["(no accounts)"]
        )
        picker.set_hexpand(True)
        if account is not None:
            index = next((i for i, a in enumerate(self.line_accounts) if a.handle == account), 0)
            picker.set_selected(index)
        entry = Gtk.Entry(text=text, width_chars=12, placeholder_text="85.50 or 6.2%")
        remove = Gtk.Button(label="Remove")
        pair = (picker, entry)
        remove.connect("clicked", lambda _b: self._remove_template_line(pair))
        self.template_lines_grid.attach(picker, 0, row, 1, 1)
        self.template_lines_grid.attach(entry, 1, row, 1, 1)
        self.template_lines_grid.attach(remove, 2, row, 1, 1)
        self.template_rows.append(pair)

    def _remove_template_line(self, pair) -> None:
        kept = [
            (self.line_accounts[picker.get_selected()].handle, entry.get_text())
            for picker, entry in self.template_rows
            if (picker, entry) != pair and self.line_accounts
        ]
        _clear(self.template_lines_grid)
        self.template_rows = []
        for account, text in kept:
            self.add_template_line(account, text)

    def _template_from_form(self) -> PayrollTemplate | None:
        if not self.income_accounts or not self.deposit_accounts:
            self._message("Add an income account and a bank account first.", True)
            return None
        try:
            gross = Money(parse_user_amount(self.template_gross_entry.get_text().strip()))
        except (ValueError, ArithmeticError):
            self._message("Enter the usual gross pay as an amount.", True)
            return None
        lines: list[PayrollLine] = []
        for picker, entry in self.template_rows:
            account = self.line_accounts[picker.get_selected()].handle
            text = entry.get_text().strip()
            try:
                if text.endswith("%"):
                    lines.append(PayrollLine(account, percent=parse_percent(text)))
                else:
                    lines.append(PayrollLine(account, amount=Money(parse_user_amount(text))))
            except (PayrollError, ValueError, ArithmeticError):
                self._message(f"Enter an amount or a percentage for {self._name(account)}.", True)
                return None
        return PayrollTemplate(
            name=self.template_name_entry.get_text(),
            income_account=self.income_accounts[self.income_picker.get_selected()].handle,
            deposit_account=self.deposit_accounts[self.deposit_picker.get_selected()].handle,
            gross=gross,
            lines=tuple(lines),
        )

    def fill_from_paycheck(self) -> PayrollTemplate | None:
        """Describe the selected paycheck in the template form; nothing is saved."""
        selected = self.selected_paycheck()
        if selected is None:
            self._message("Choose a paycheck first.", True)
            return None
        result = template_from_schedule(self.db, selected.schedule, selected.name)
        if result.value is None:
            self._refused(result)
            return None
        self.template_picker.set_selected(0)
        self._fill_template_form(result.value)
        self._message("Filled the template from the paycheck. Save template to keep it.")
        return result.value

    def save_template(self) -> PayrollTemplate | None:
        template = self._template_from_form()
        if template is None:
            return None
        result = save_payroll_template(
            self.db, SavePayrollTemplate(template, existing_name=self._loaded_template)
        )
        if result.value is None:
            self._refused(result)
            return None
        self._reload_templates(result.value.name)
        self._message(f"Saved payroll template {result.value.name}.")
        return result.value

    def delete_template(self) -> PayrollTemplate | None:
        template = self.selected_template()
        if template is None:
            return None
        result = delete_payroll_template(self.db, template.name)
        if result.value is None:
            self._refused(result)
            return None
        self._reload_templates()
        self._message(f"Deleted payroll template {template.name}. Edit → Undo restores it.")
        return result.value

    def create_paycheck(self) -> str | None:
        """Add a paycheck schedule from the selected template; returns its handle."""
        template = self.selected_template()
        if template is None:
            self._message("Save or choose a template first.", True)
            return None
        try:
            start = date.fromisoformat(self.new_start_entry.get_text().strip())
        except ValueError:
            self._message("Enter the first payday as YYYY-MM-DD.", True)
            return None
        gross_text = self.new_gross_entry.get_text().strip()
        try:
            gross = Money(parse_user_amount(gross_text)) if gross_text else None
        except (ValueError, ArithmeticError):
            self._message("Enter the gross pay as an amount, or leave it empty.", True)
            return None
        _label, period, interval = PAY_PERIODS[self.new_period_picker.get_selected()]
        name = self.new_name_entry.get_text().strip() or template.name
        result = create_paycheck_schedule(
            self.db,
            CreatePaycheck(template.name, name, _recurrence(period, interval, start), gross),
        )
        if result.value is None:
            self._refused(result)
            return None
        self.refresh(select=result.value.handle)
        self._message(f"Added paycheck {result.value.name}. Edit → Undo removes it.")
        return result.value.handle
