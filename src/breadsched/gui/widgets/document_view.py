"""Any report document on screen: headings, notes, cards, charts and tables.

A breadlet (or any report layout) describes its output as a
:class:`~breadsched.plugins.export.report_layout.ReportDocument`; this draws one with
plain GTK widgets, the way printing and the browser draw the same document. A chart
is drawn by :class:`ModelChartView`; its exact values are the table that follows it.
"""

from __future__ import annotations

from ...gen.engine.chart_model import PIE
from ...plugins.export.report_layout import (
    Cards,
    Heading,
    ModelChart,
    Paragraph,
    ReportDocument,
    Table,
)
from ..gi_setup import Gtk, Pango
from .model_chart import ModelChartView

__all__ = ["DocumentView"]


def _label(text: str, *, numeric: bool = False, css: tuple[str, ...] = ()) -> Gtk.Label:
    label = Gtk.Label(label=text, xalign=1 if numeric else 0, selectable=True)
    if not numeric:
        label.set_wrap(True)
        label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    for name in (*css, *(("numeric",) if numeric else ())):
        label.add_css_class(name)
    return label


def _cards(block: Cards) -> Gtk.Widget:
    row = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, column_spacing=12, row_spacing=8)
    row.set_max_children_per_line(6)
    for card in block.items:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.add_css_class("card")
        box.append(_label(card.label, css=("dim",)))
        box.append(_label(card.value, css=("title-3", *(("error",) if card.alarm else ()))))
        row.append(box)
    return row


def _table(block: Table) -> Gtk.Widget:
    if not block.rows:
        return _label(block.empty, css=("dim",))
    grid = Gtk.Grid(column_spacing=16, row_spacing=4)
    for column, heading in enumerate(block.columns):
        grid.attach(_label(heading.label, numeric=heading.numeric, css=("dim",)), column, 0, 1, 1)
    for index, row in enumerate(block.rows, start=1):
        column = 0
        for cell in row.cells:
            numeric = cell.numeric or (
                column < len(block.columns) and block.columns[column].numeric
            )
            text = f"{cell.text}\n{cell.note}" if cell.note and cell.below else cell.text
            if cell.note and not cell.below:
                text = f"{text} {cell.note}"
            css = ("heading",) if row.style in ("heading", "section", "total", "grand") else ()
            label = _label(
                text, numeric=numeric, css=(*css, *(("error",) if cell.negative else ()))
            )
            if cell.indent:
                label.set_margin_start(12 * cell.indent)
            if cell.hint:
                label.set_tooltip_text(cell.hint)
            grid.attach(label, column, index, max(cell.span, 1), 1)
            column += max(cell.span, 1)
    return grid


class DocumentView(Gtk.Box):
    """A report document, block by block; ``show`` replaces what it shows."""

    def __init__(self, *, titled: bool = True) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.titled = titled
        self.document: ReportDocument | None = None

    def show_document(self, document: ReportDocument | None) -> None:
        self.document = document
        while (child := self.get_first_child()) is not None:
            self.remove(child)
        if document is None:
            return
        if self.titled:
            self.append(_label(document.title, css=("title-2",)))
            if document.subtitle:
                self.append(_label(document.subtitle, css=("dim",)))
        for section in document.sections:
            for block in section.blocks:
                self.append(self._block(block))

    def _block(self, block) -> Gtk.Widget:
        if isinstance(block, Heading):
            return _label(block.text, css=("heading",))
        if isinstance(block, Paragraph):
            text = f"• {block.text}" if block.bullet else block.text
            return _label(text, css=("error",) if block.warning else ("dim",))
        if isinstance(block, Cards):
            return _cards(block)
        if isinstance(block, ModelChart):
            return ModelChartView(block.model, height=260 if block.model.kind == PIE else 240)
        return _table(block)

    def text(self) -> str:
        """Every word shown, for tests."""
        return self.document.text() if self.document is not None else ""
