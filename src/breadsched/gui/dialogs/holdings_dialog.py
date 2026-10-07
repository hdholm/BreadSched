"""Read-only GTK view of each security's cost basis, lots, and gains."""

from __future__ import annotations

from datetime import date

from ...gen.db.sqlite import DbSQLite
from ...gen.engine.cost_basis import HoldingCostBasis, holdings_cost_basis, shares_text
from ...gen.lib.money import Money
from ...presentation import holding_cost_text, lot_move_text
from ..gi_setup import Gtk
from ..widgets.bounded import BoundedWindow

__all__ = ["HoldingsDialog"]


def _cell(text: str, *, numeric: bool = False) -> Gtk.Label:
    label = Gtk.Label(label=text, xalign=1 if numeric else 0)
    if numeric:
        label.add_css_class("numeric")
    return label


def _money(value: Money | None) -> str:
    return "—" if value is None else value.format(parens_negative=True)


class HoldingsDialog(BoundedWindow):
    """Shares, cost basis, market value, and gains per holding."""

    def __init__(self, parent: Gtk.Window | None, db: DbSQLite, as_of: date | None = None) -> None:
        super().__init__(title="Holdings and Cost Basis", transient_for=parent)
        self.set_default_size(820, 560)
        self.holdings: list[HoldingCostBasis] = holdings_cost_basis(db, as_of=as_of)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for side in ("top", "bottom", "start", "end"):
            getattr(outer, f"set_margin_{side}")(12)
        self.set_child(outer)
        note = Gtk.Label(
            label=(
                "Lots come from each security account's transactions: a sale takes the "
                "oldest shares first, or the average cost where the account says so. "
                "Gains compare with the latest quote; nothing is "
                "stored or rewritten."
            ),
            xalign=0,
            wrap=True,
        )
        note.add_css_class("dim")
        outer.append(note)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        scroller = Gtk.ScrolledWindow(child=body, vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_min_content_height(160)
        outer.append(scroller)

        self.summary = Gtk.Grid(column_spacing=16, row_spacing=4)
        body.append(self.summary)
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
        for item in self.holdings:
            expander = Gtk.Expander(
                label=f"{db.full_name(item.account)}: {holding_cost_text(item)}"
            )
            expander.get_label_widget().set_wrap(True)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            box.set_margin_start(18)
            expander.set_child(box)
            lines = [
                f"Bought {lot.acquired.isoformat()}: {shares_text(lot.quantity)} shares, "
                f"cost {lot.cost.format()}"
                for lot in item.lots
            ] + [
                f"Sold {sale.sold.isoformat()}: {shares_text(sale.quantity)} shares for "
                f"{sale.proceeds.format()}, cost {sale.cost.format()}, gain "
                f"{sale.gain.format(parens_negative=True)}"
                for sale in item.sales
            ]
            for move in item.moves:
                other = db.get_account(move.other_account) if move.other_account else None
                text = lot_move_text(move, db.full_name(other) if other is not None else None)
                lines.append(text[:1].upper() + text[1:])
            for line in lines:
                box.append(_cell(line))
            for problem in item.problems:
                warning = _cell(problem)
                warning.set_wrap(True)
                warning.add_css_class("negative")
                box.append(warning)
            body.append(expander)
            self.details.append(expander)

        close = Gtk.Button(label="Close", halign=Gtk.Align.END)
        close.connect("clicked", lambda *_: self.close())
        outer.append(close)
