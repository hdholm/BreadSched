"""GTK view of each security's cost basis, lots, and gains, with lot choice per sale."""

from __future__ import annotations

from datetime import date

from ...gen.db.sqlite import DbSQLite
from ...gen.engine.cost_basis import (
    HoldingCostBasis,
    holdings_charts,
    holdings_cost_basis,
    shares_text,
)
from ...gen.lib.money import Money
from ...presentation import holding_cost_text, lot_move_text, lot_text, sale_text
from ..gi_setup import Gtk
from ..widgets.bounded import BoundedWindow
from ..widgets.model_chart import ModelChartView

__all__ = ["HoldingsDialog"]


def _cell(text: str, *, numeric: bool = False) -> Gtk.Label:
    label = Gtk.Label(label=text, xalign=1 if numeric else 0)
    if numeric:
        label.add_css_class("numeric")
    return label


def _money(value: Money | None) -> str:
    return "—" if value is None else value.format(parens_negative=True)


def _capital(text: str) -> str:
    return text[:1].upper() + text[1:]


class HoldingsDialog(BoundedWindow):
    """Shares, cost basis, market value, and gains per holding."""

    def __init__(self, parent: Gtk.Window | None, db: DbSQLite, as_of: date | None = None) -> None:
        super().__init__(title="Holdings and Cost Basis", transient_for=parent)
        self.db = db
        self.as_of = as_of
        self.set_default_size(820, 560)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for side in ("top", "bottom", "start", "end"):
            getattr(outer, f"set_margin_{side}")(12)
        self.set_child(outer)
        note = Gtk.Label(
            label=(
                "Lots come from each security account's transactions: a sale takes the "
                "lots it names, otherwise the oldest shares first, or the average cost "
                "where the account says so. Gains compare with the latest quote; the "
                "lots themselves are never stored or rewritten."
            ),
            xalign=0,
            wrap=True,
        )
        note.add_css_class("dim")
        outer.append(note)

        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        scroller = Gtk.ScrolledWindow(child=self.body, vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_min_content_height(160)
        outer.append(scroller)

        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        self.gains_button = Gtk.Button(label="Realized Gains…")
        self.gains_button.connect("clicked", lambda *_: self.open_realized_gains())
        close = Gtk.Button(label="Close")
        close.connect("clicked", lambda *_: self.close())
        buttons.append(self.gains_button)
        buttons.append(close)
        outer.append(buttons)
        self.refresh()

    def refresh(self) -> None:
        """Recompute every holding and redraw (after a sale's lots change)."""
        db = self.db
        # Holdings the user had open stay open after the redraw.
        expanded = {
            holding.account.handle
            for holding, detail in zip(
                getattr(self, "holdings", ()), getattr(self, "details", ()), strict=False
            )
            if detail.get_expanded()
        }
        self.holdings: list[HoldingCostBasis] = holdings_cost_basis(db, as_of=self.as_of)
        child = self.body.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self.body.remove(child)
            child = following

        # Cost, market value, and unrealized gain per holding, one chart per currency.
        self.charts: list[ModelChartView] = []
        for model in holdings_charts(db, self.holdings):
            if model.empty:
                continue
            chart = ModelChartView(model, height=220)
            self.body.append(chart)
            self.charts.append(chart)
        self.summary = Gtk.Grid(column_spacing=16, row_spacing=4)
        self.body.append(self.summary)
        headings = ("Holding", "Shares", "Cost", "Market value", "Unrealized")
        for column, heading in enumerate(headings):
            label = _cell(heading, numeric=column > 0)
            label.add_css_class("dim")
            self.summary.attach(label, column, 0, 1, 1)
        if not self.holdings:
            self.summary.attach(_cell("No security holdings."), 0, 1, 5, 1)
        for row, item in enumerate(self.holdings, start=1):
            for column, text in enumerate(
                (
                    db.full_name(item.account),
                    shares_text(item.quantity),
                    item.cost.format(),
                    _money(item.market_value),
                    _money(item.unrealized_gain),
                )
            ):
                self.summary.attach(_cell(text, numeric=column > 0), column, row, 1, 1)

        self.details: list[Gtk.Expander] = []
        self.choose_buttons: list[Gtk.Button] = []
        for item in self.holdings:
            expander = Gtk.Expander(
                label=f"{db.full_name(item.account)}: {holding_cost_text(item)}"
            )
            expander.get_label_widget().set_wrap(True)
            expander.set_expanded(item.account.handle in expanded)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            box.set_margin_start(18)
            expander.set_child(box)
            for lot in item.lots:
                box.append(_cell(_capital(lot_text(lot))))
            for sale in item.sales:
                line = Gtk.Box(spacing=8)
                line.append(_cell(_capital(sale_text(sale))))
                choose = Gtk.Button(label="Choose Lots…")
                choose.update_property(
                    [Gtk.AccessibleProperty.LABEL],
                    [f"Choose the lots sold on {sale.sold.isoformat()}"],
                )
                choose.connect(
                    "clicked",
                    lambda _b, sale=sale: self.choose_lots(sale.transaction, sale.split),
                )
                line.append(choose)
                box.append(line)
                self.choose_buttons.append(choose)
            for move in item.moves:
                other = db.get_account(move.other_account) if move.other_account else None
                text = lot_move_text(move, db.full_name(other) if other is not None else None)
                box.append(_cell(_capital(text)))
            for problem in item.problems:
                warning = _cell(problem)
                warning.set_wrap(True)
                warning.add_css_class("negative")
                box.append(warning)
            self.body.append(expander)
            self.details.append(expander)

    def choose_lots(self, transaction: str, split: str):
        from .sale_lots_dialog import SaleLotsDialog

        dialog = SaleLotsDialog(self, self.db, transaction, split, lambda _saved: self.refresh())
        dialog.present()
        return dialog

    def open_realized_gains(self):
        from .realized_gains_dialog import RealizedGainsDialog

        dialog = RealizedGainsDialog(self, self.db)
        dialog.present()
        return dialog
