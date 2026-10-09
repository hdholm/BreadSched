"""Realized gains by year, with every sale and the lots it took, printable."""

from __future__ import annotations

from ...gen.db.sqlite import DbSQLite
from ...gen.engine.cost_basis import shares_text
from ...gen.engine.realized_gains import RealizedGainsReport, currency_labels, realized_gains
from ...plugins.export.report_layout import realized_gains_layout
from ...presentation import lot_text
from .. import printing
from ..gi_setup import Gtk
from ..widgets.bounded import BoundedWindow
from ..widgets.choice import bounded_dropdown
from ..widgets.help import help_row

__all__ = ["RealizedGainsDialog"]


def _cell(text: str, *, numeric: bool = False) -> Gtk.Label:
    label = Gtk.Label(label=text, xalign=1 if numeric else 0)
    if numeric:
        label.add_css_class("numeric")
    return label


class RealizedGainsDialog(BoundedWindow):
    """Totals by year and currency, then each sale with its lots and how they were chosen."""

    def __init__(self, parent: Gtk.Window | None, db: DbSQLite) -> None:
        super().__init__(title="Realized Gains", transient_for=parent)
        self.db = db
        self.set_default_size(900, 600)
        self.years = sorted({sale.sold.year for sale in realized_gains(db).sales}, reverse=True)
        self.year: int | None = None
        self.report: RealizedGainsReport = realized_gains(db)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for side in ("top", "bottom", "start", "end"):
            getattr(outer, f"set_margin_{side}")(12)
        self.set_child(outer)
        outer.append(help_row("specific-lots"))
        toolbar = Gtk.Box(spacing=8)
        toolbar.append(Gtk.Label(label="Year"))
        self.year_choice = bounded_dropdown(["All years", *map(str, self.years)])
        self.year_choice.update_property([Gtk.AccessibleProperty.LABEL], ["Year"])
        self.year_choice.connect("notify::selected", lambda *_: self._choose_year())
        toolbar.append(self.year_choice)
        self.print_button = Gtk.Button(label="Print…")
        self.print_button.connect("clicked", lambda *_: self.print_report())
        toolbar.append(self.print_button)
        outer.append(toolbar)

        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        scroller = Gtk.ScrolledWindow(child=self.body, vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_min_content_height(200)
        outer.append(scroller)
        close = Gtk.Button(label="Close", halign=Gtk.Align.END)
        close.connect("clicked", lambda *_: self.close())
        outer.append(close)
        self.refresh()

    def _choose_year(self) -> None:
        index = self.year_choice.get_selected()
        self.year = None if index in (0, Gtk.INVALID_LIST_POSITION) else self.years[index - 1]
        self.refresh()

    def refresh(self) -> None:
        """Recompute the report for the chosen year and redraw it."""
        self.report = realized_gains(self.db, year=self.year)
        child = self.body.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self.body.remove(child)
            child = following
        labels = currency_labels(self.db, self.report)
        totals = Gtk.Grid(column_spacing=16, row_spacing=4)
        for column, heading in enumerate(("Year", "Currency", "Sales", "Proceeds", "Cost", "Gain")):
            label = _cell(heading, numeric=column > 1)
            label.add_css_class("dim")
            totals.attach(label, column, 0, 1, 1)
        for row, total in enumerate(self.report.years, start=1):
            for column, text in enumerate(
                (
                    str(total.year),
                    labels.get(total.currency, "mixed"),
                    str(total.sales),
                    total.proceeds.format(),
                    total.cost.format(),
                    total.gain.format(parens_negative=True),
                )
            ):
                totals.attach(_cell(text, numeric=column > 1), column, row, 1, 1)
        if not self.report.years:
            totals.attach(_cell("No sales."), 0, 1, 6, 1)
        self.body.append(totals)

        self.sale_rows: list[Gtk.Expander] = []
        for item in self.report.sales:
            chosen = "named lots" if item.sale.specific else "the account's method"
            expander = Gtk.Expander(
                label=(
                    f"{item.sold.isoformat()} {item.account_name}: "
                    f"{shares_text(item.sale.quantity)} shares, gain "
                    f"{item.sale.gain.format(parens_negative=True)} ({chosen})"
                )
            )
            expander.get_label_widget().set_wrap(True)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            box.set_margin_start(18)
            box.append(
                _cell(f"Proceeds {item.sale.proceeds.format()}, cost {item.sale.cost.format()}")
            )
            for lot in item.lots:
                box.append(_cell(lot_text(lot)))
            choose = Gtk.Button(label="Choose Lots…", halign=Gtk.Align.START)
            choose.connect(
                "clicked",
                lambda _b, sale=item.sale: self.choose_lots(sale.transaction, sale.split),
            )
            box.append(choose)
            expander.set_child(box)
            self.body.append(expander)
            self.sale_rows.append(expander)
        for problem in self.report.problems:
            warning = _cell(problem)
            warning.set_wrap(True)
            warning.add_css_class("negative")
            self.body.append(warning)

    def choose_lots(self, transaction: str, split: str):
        from .sale_lots_dialog import SaleLotsDialog

        dialog = SaleLotsDialog(self, self.db, transaction, split, lambda _saved: self.refresh())
        dialog.present()
        return dialog

    def layout(self):
        return realized_gains_layout(
            self.report, currency_labels(self.db, self.report), year=self.year
        )

    def print_report(self) -> None:
        printing.print_document(self, self.layout())
