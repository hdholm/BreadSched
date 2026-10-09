"""One calendar tax year, printable, and the marks that say what it totals."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

from ...gen.db.sqlite import DbSQLite
from ...gen.engine.cost_basis import shares_text
from ...gen.engine.tax_year import TaxYearReport, currency_labels, tax_year, tax_years
from ...gen.lib.money import Money
from ...gen.services import SetTaxMarks, set_tax_marks, tax_marks
from ...plugins.export.report_layout import tax_year_layout
from ...presentation import service_error_message
from .. import printing
from ..gi_setup import Gtk, Pango
from ..widgets.bounded import BoundedWindow
from ..widgets.choice import bounded_dropdown
from ..widgets.help import help_row

__all__ = ["TaxMarksDialog", "TaxYearDialog"]


def _cell(text: str, *, numeric: bool = False) -> Gtk.Label:
    label = Gtk.Label(label=text, xalign=1 if numeric else 0)
    if numeric:
        label.add_css_class("numeric")
    return label


def _signed(value: Money) -> str:
    return value.format(parens_negative=True)


def _check_row(body: Gtk.Box, text: str, accessible: str, active: bool) -> Gtk.CheckButton:
    """A check box beside a wrapping label, so a long account name cannot widen the window."""
    row = Gtk.Box(spacing=6)
    check = Gtk.CheckButton(active=active, valign=Gtk.Align.START)
    check.update_property([Gtk.AccessibleProperty.LABEL], [accessible])
    label = Gtk.Label(label=text, xalign=0, wrap=True, hexpand=True)
    label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    click = Gtk.GestureClick()
    click.connect("released", lambda *_: check.set_active(not check.get_active()))
    label.add_controller(click)
    row.append(check)
    row.append(label)
    body.append(row)
    return check


class TaxYearDialog(BoundedWindow):
    """Gains by term, tax-relevant accounts and tags, and income by source for a year."""

    def __init__(self, parent: Gtk.Window | None, db: DbSQLite, year: int | None = None) -> None:
        super().__init__(title="Tax Year", transient_for=parent)
        self.db = db
        self.set_default_size(900, 640)
        self.years = list(tax_years(db)) or [date.today().year]
        self.year = year if year is not None else self.years[0]
        if self.year not in self.years:
            self.years = sorted({*self.years, self.year}, reverse=True)
        self.report: TaxYearReport = tax_year(db, self.year)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for side in ("top", "bottom", "start", "end"):
            getattr(outer, f"set_margin_{side}")(12)
        self.set_child(outer)
        outer.append(help_row("tax-year"))
        note = Gtk.Label(
            label=(
                "Calendar year, US rules: a lot is long-term when sold more than a year "
                "after its purchase. Amounts in different currencies are totalled "
                "separately. This summarizes the book; it is not tax advice or a tax form."
            ),
            xalign=0,
            wrap=True,
        )
        note.add_css_class("dim")
        outer.append(note)

        toolbar = Gtk.Box(spacing=8)
        toolbar.append(Gtk.Label(label="Year"))
        self.year_choice = bounded_dropdown([str(value) for value in self.years])
        self.year_choice.update_property([Gtk.AccessibleProperty.LABEL], ["Tax year"])
        self.year_choice.set_selected(self.years.index(self.year))
        self.year_choice.connect("notify::selected", lambda *_: self._choose_year())
        toolbar.append(self.year_choice)
        self.marks_button = Gtk.Button(label="Tax-Relevant Accounts and Tags…")
        self.marks_button.connect("clicked", lambda *_: self.open_marks())
        toolbar.append(self.marks_button)
        self.print_button = Gtk.Button(label="Print…")
        self.print_button.connect("clicked", lambda *_: self.print_report())
        toolbar.append(self.print_button)
        outer.append(toolbar)

        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        scroller = Gtk.ScrolledWindow(child=self.body, vexpand=True)
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_min_content_height(240)
        outer.append(scroller)
        close = Gtk.Button(label="Close", halign=Gtk.Align.END)
        close.connect("clicked", lambda *_: self.close())
        outer.append(close)
        self.refresh()

    def _choose_year(self) -> None:
        index = self.year_choice.get_selected()
        if index != Gtk.INVALID_LIST_POSITION:
            self.year = self.years[index]
            self.refresh()

    def _section(self, heading: str, headings: tuple[str, ...], rows: list[tuple], empty: str):
        title = Gtk.Label(label=heading, xalign=0)
        title.add_css_class("heading")
        self.body.append(title)
        if not rows:
            self.body.append(_cell(empty))
            return None
        grid = Gtk.Grid(column_spacing=16, row_spacing=4)
        for column, text in enumerate(headings):
            label = _cell(text.lstrip("#"), numeric=text.startswith("#"))
            label.add_css_class("dim")
            grid.attach(label, column, 0, 1, 1)
        for row, values in enumerate(rows, start=1):
            for column, value in enumerate(values):
                grid.attach(
                    _cell(value, numeric=headings[column].startswith("#")), column, row, 1, 1
                )
        self.body.append(grid)
        return grid

    def refresh(self) -> None:
        """Recompute the chosen year and redraw it."""
        self.report = report = tax_year(self.db, self.year)
        child = self.body.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self.body.remove(child)
            child = following
        labels = currency_labels(self.db, report)

        def label(handle: str | None) -> str:
            return labels.get(handle, handle or "")

        self.gain_totals = self._section(
            "Realized gains by term",
            ("Term", "Currency", "#Lines", "#Proceeds", "#Cost", "#Gain"),
            [
                (
                    total.term.label,
                    label(total.currency),
                    str(total.lines),
                    total.proceeds.format(),
                    total.cost.format(),
                    _signed(total.gain),
                )
                for total in report.gain_totals
            ],
            "No sales this year.",
        )
        if report.gains:
            self._section(
                "Sales by term",
                ("Security", "#Shares", "Acquired", "Sold", "Term", "#Proceeds", "#Cost", "#Gain"),
                [
                    (
                        line.account_name,
                        shares_text(line.quantity),
                        line.acquired.isoformat() if line.acquired else "Various",
                        line.sold.isoformat(),
                        line.term.label,
                        line.proceeds.format(),
                        line.cost.format(),
                        _signed(line.gain),
                    )
                    for line in report.gains
                ],
                "",
            )
        self.accounts = self._section(
            "Tax-relevant accounts",
            ("Account", "Currency", "#Transactions", "#Total", "Marked by"),
            [
                (
                    item.full_name,
                    label(item.currency),
                    str(item.transactions),
                    _signed(item.amount),
                    item.marked_by,
                )
                for item in report.accounts
            ],
            "No account is marked tax-relevant.",
        )
        self.tags = self._section(
            "Tax-relevant tags",
            ("Tag", "Currency", "#Transactions", "#Spent", "#Received"),
            [
                (
                    item.tag,
                    label(item.currency),
                    str(item.transactions),
                    item.spent.format(),
                    item.received.format(),
                )
                for item in report.tags
            ],
            "No tag is marked tax-relevant.",
        )
        income_rows = [
            (item.full_name, label(item.currency), str(item.transactions), _signed(item.amount))
            for item in report.income
        ]
        income_rows += [
            ("Total", label(currency), "", _signed(total))
            for currency, total in report.income_totals
        ]
        self.income = self._section(
            "Income by source",
            ("Source", "Currency", "#Transactions", "#Amount"),
            income_rows,
            "No income this year.",
        )
        for problem in report.problems:
            warning = _cell(problem)
            warning.set_wrap(True)
            warning.add_css_class("negative")
            self.body.append(warning)

    def open_marks(self) -> TaxMarksDialog:
        dialog = TaxMarksDialog(self, self.db, lambda: self.refresh())
        dialog.present()
        return dialog

    def layout(self):
        return tax_year_layout(self.report, currency_labels(self.db, self.report))

    def print_report(self) -> None:
        printing.print_document(self, self.layout())


class TaxMarksDialog(BoundedWindow):
    """Check the accounts (with those beneath them) and tags the tax year totals."""

    def __init__(
        self, parent: Gtk.Window | None, db: DbSQLite, on_saved: Callable[[], None] | None = None
    ) -> None:
        super().__init__(title="Tax-Relevant Accounts and Tags", transient_for=parent)
        self.db = db
        self.on_saved = on_saved
        self.set_default_size(560, 560)
        self.marks = tax_marks(db)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for side in ("top", "bottom", "start", "end"):
            getattr(outer, f"set_margin_{side}")(12)
        self.set_child(outer)
        outer.append(help_row("tax-year"))
        note = Gtk.Label(
            label=(
                "A marked account counts with the accounts beneath it. Until you change it "
                'here, an account follows GnuCash\'s "tax related" mark.'
            ),
            xalign=0,
            wrap=True,
        )
        note.add_css_class("dim")
        outer.append(note)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        scroller = Gtk.ScrolledWindow(child=body, vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_min_content_height(240)
        outer.append(scroller)

        tags_title = Gtk.Label(label="Tags", xalign=0)
        tags_title.add_css_class("heading")
        body.append(tags_title)
        self.tag_checks: dict[str, Gtk.CheckButton] = {}
        for tag, marked in self.marks.tags:
            check = _check_row(body, tag, f"Tag {tag} is tax-relevant", marked)
            self.tag_checks[tag] = check
        if not self.marks.tags:
            body.append(_cell("The book has no tags yet."))

        accounts_title = Gtk.Label(label="Accounts", xalign=0)
        accounts_title.add_css_class("heading")
        body.append(accounts_title)
        self.account_checks: dict[str, Gtk.CheckButton] = {}
        for mark in self.marks.accounts:
            text = mark.full_name + (
                " (GnuCash: tax related)" if mark.gnucash and mark.override is None else ""
            )
            check = _check_row(body, text, f"{mark.full_name} is tax-relevant", mark.relevant)
            self.account_checks[mark.account.handle] = check

        self.message = Gtk.Label(xalign=0, wrap=True)
        self.message.add_css_class("negative")
        outer.append(self.message)
        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        self.save_button = Gtk.Button(label="Save")
        self.save_button.add_css_class("suggested-action")
        self.save_button.connect("clicked", lambda *_: self.save())
        buttons.append(cancel)
        buttons.append(self.save_button)
        outer.append(buttons)

    def save(self) -> bool:
        accounts: dict[str, bool | None] = {
            mark.account.handle: self.account_checks[mark.account.handle].get_active()
            for mark in self.marks.accounts
            if self.account_checks[mark.account.handle].get_active() != mark.relevant
        }
        tags = {
            tag: self.tag_checks[tag].get_active()
            for tag, marked in self.marks.tags
            if self.tag_checks[tag].get_active() != marked
        }
        result = set_tax_marks(self.db, SetTaxMarks(accounts, tags))
        if result.value is None:
            self.message.set_label(service_error_message(result.errors[0]))
            return False
        if self.on_saved is not None:
            self.on_saved()
        self.close()
        return True
