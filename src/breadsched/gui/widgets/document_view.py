"""Any report document on screen: headings, notes, cards, charts, tables and actions.

A breadlet (or any report layout) describes its output as a
:class:`~breadsched.plugins.export.report_layout.ReportDocument`; this draws one with
GTK widgets, the way printing and the browser draw the same document. A chart is
drawn by :class:`ModelChartView`; its exact values are the table that follows it.

On screen a document can do more than print: a card or a table row with a ``link``
opens what it names (``on_link`` follows it), an ``Actions`` block is a row of
buttons, and a table with a ``key`` is a sortable ``Gtk.ColumnView`` whose columns
the person chooses, remembered under that key like every other table's.
"""

from __future__ import annotations

from collections.abc import Callable

from ...gen.engine.chart_model import PIE
from ...plugins.export.report_layout import (
    Actions,
    Cards,
    Cell,
    Heading,
    ModelChart,
    Paragraph,
    ReportDocument,
    Table,
    TableRow,
)
from ..gi_setup import Gio, GObject, Gtk, Pango
from .model_chart import ModelChartView

__all__ = ["DocumentView"]

#: Widest a card's text may ask to be before it wraps.
_CARD_TEXT_CHARS = 40


#: Widest wrapped text may ask to be, so a long note never widens its window (#140).
_TEXT_CHARS = 80


def _label(text: str, *, numeric: bool = False, css: tuple[str, ...] = ()) -> Gtk.Label:
    label = Gtk.Label(label=text, xalign=1 if numeric else 0, selectable=True)
    if not numeric:
        label.set_wrap(True)
        label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        label.set_max_width_chars(_TEXT_CHARS)
    for name in (*css, *(("numeric",) if numeric else ())):
        label.add_css_class(name)
    return label


def _card(caption: str, value: str, alarm: bool) -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    box.add_css_class("card")
    label = Gtk.Label(label=caption, xalign=0)
    label.add_css_class("summary-label")
    amount = Gtk.Label(label=value, xalign=0)
    # Long notes wrap instead of widening the window (#140).
    amount.set_wrap(True)
    amount.set_max_width_chars(_CARD_TEXT_CHARS)
    amount.add_css_class("summary-value")
    amount.add_css_class("numeric")
    if alarm or value.startswith(("(", "-")):
        amount.add_css_class("negative")
    box.append(label)
    box.append(amount)
    return box


class _RowItem(GObject.Object):
    """One table row in a ``Gio.ListStore``."""

    def __init__(self, row: TableRow) -> None:
        super().__init__()
        self.row = row


def _cell_text(cell: Cell) -> str:
    if cell.note and cell.below:
        return f"{cell.text}\n{cell.note}"
    return f"{cell.text} {cell.note}".strip() if cell.note else cell.text


def _sort_value(cell: Cell, numeric: bool):
    if cell.sort is not None:
        return cell.sort
    if numeric:
        text = cell.text.replace(",", "").replace("(", "-").replace(")", "")
        try:
            return float(text)
        except ValueError:
            return float("-inf")
    return cell.text.casefold()


def _compare(index: int, numeric: bool):
    def compare(left, right, _data=None) -> int:
        a = _sort_value(left.row.cells[index], numeric) if index < len(left.row.cells) else ""
        b = _sort_value(right.row.cells[index], numeric) if index < len(right.row.cells) else ""
        if type(a) is not type(b):
            a, b = str(a), str(b)
        return (a > b) - (a < b)

    return compare


class DocumentView(Gtk.Box):
    """A report document, block by block; ``show_document`` replaces what it shows."""

    def __init__(
        self,
        *,
        titled: bool = True,
        on_link: Callable[[str], None] | None = None,
        settings=None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.titled = titled
        self.on_link = on_link
        self.settings = settings
        self.document: ReportDocument | None = None
        #: The interactive tables shown, by their ``key``.
        self.tables: dict[str, Gtk.ColumnView] = {}
        #: Each interactive table's section (heading, column chooser, scroller), by key.
        self.table_sections: dict[str, Gtk.Box] = {}
        #: The card rows shown, in order.
        self.card_rows: list[Gtk.FlowBox] = []

    def show_document(self, document: ReportDocument | None) -> None:
        self.document = document
        self.tables = {}
        self.table_sections = {}
        self.card_rows = []
        while (child := self.get_first_child()) is not None:
            self.remove(child)
        if document is None:
            return
        if self.titled:
            self.append(_label(document.title, css=("title-2",)))
            if document.subtitle:
                self.append(_label(document.subtitle, css=("dim",)))
        for section in document.sections:
            blocks = list(section.blocks)
            index = 0
            while index < len(blocks):
                block = blocks[index]
                # A heading followed by an interactive table is that table's header,
                # beside its column chooser.
                following = blocks[index + 1] if index + 1 < len(blocks) else None
                if isinstance(block, Heading) and isinstance(following, Table) and following.key:
                    self.append(self._interactive_table(following, block.text))
                    index += 2
                    continue
                self.append(self._block(block))
                index += 1

    def follow(self, link: str) -> None:
        if link and self.on_link is not None:
            self.on_link(link)

    # ------------------------------------------------------------------ blocks

    def _block(self, block) -> Gtk.Widget:
        if isinstance(block, Heading):
            return _label(block.text, css=("total-row",))
        if isinstance(block, Paragraph):
            text = f"• {block.text}" if block.bullet else block.text
            return _label(text, css=("error",) if block.warning else ("dim",))
        if isinstance(block, Cards):
            return self._cards(block)
        if isinstance(block, ModelChart):
            return ModelChartView(block.model, height=260 if block.model.kind == PIE else 220)
        if isinstance(block, Actions):
            return self._actions(block)
        if block.key:
            return self._interactive_table(block, block.title)
        return self._table(block)

    def _cards(self, block: Cards) -> Gtk.Widget:
        # Cards wrap onto more rows on a narrow window instead of widening it.
        row = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE)
        row.set_homogeneous(False)
        row.set_column_spacing(14)
        row.set_row_spacing(10)
        row.set_max_children_per_line(12)
        row.set_valign(Gtk.Align.START)
        # A card that links somewhere opens it when chosen (a click, or Enter on it).
        row.set_activate_on_single_click(True)
        links: dict[Gtk.Widget, str] = {}
        for card in block.items:
            box = _card(card.label, card.value, card.alarm)
            row.append(box)
            if card.link and self.on_link is not None:
                box.add_css_class("card-link")
                box.set_tooltip_text(f"Open {card.label}")
                links[box] = card.link
        row.connect(
            "child-activated",
            lambda _row, child: self.follow(links.get(child.get_child(), "")),
        )
        self.card_rows.append(row)
        return row

    def _actions(self, block: Actions) -> Gtk.Widget:
        row = Gtk.Box(spacing=8)
        for item in block.items:
            button = Gtk.Button(label=item.label)
            button.connect("clicked", lambda _b, link=item.link: self.follow(link))
            row.append(button)
        return row

    def _table(self, block: Table) -> Gtk.Widget:
        if not block.rows:
            return _label(block.empty, css=("dim",))
        grid = Gtk.Grid(column_spacing=16, row_spacing=4)
        for column, heading in enumerate(block.columns):
            label = _label(heading.label, numeric=heading.numeric, css=("summary-label",))
            grid.attach(label, column, 0, 1, 1)
        for index, row in enumerate(block.rows, start=1):
            column = 0
            for cell in row.cells:
                numeric = cell.numeric or (
                    column < len(block.columns) and block.columns[column].numeric
                )
                css = (
                    ("total-row",) if row.style in ("heading", "section", "total", "grand") else ()
                )
                label = _label(
                    _cell_text(cell),
                    numeric=numeric,
                    css=(*css, *(("negative",) if cell.negative else ())),
                )
                if cell.indent:
                    label.set_margin_start(18 * cell.indent)
                if cell.hint:
                    label.set_tooltip_text(cell.hint)
                grid.attach(label, column, index, max(cell.span, 1), 1)
                column += max(cell.span, 1)
        return grid

    def _interactive_table(self, block: Table, title: str) -> Gtk.Widget:
        """A sortable table whose columns the person chooses; a row with a link opens
        it when activated."""
        from ..views._base import arrange_columns, table_section

        view = Gtk.ColumnView()
        view.add_css_class("data-table")
        view.set_show_row_separators(True)
        for index, heading in enumerate(block.columns):
            view.append_column(self._column(index, heading.label, heading.numeric, index == 0))
        store = Gio.ListStore.new(_RowItem)
        for row in block.rows:
            store.append(_RowItem(row))
        sorter = view.get_sorter()
        model = Gtk.SortListModel.new(store, sorter) if sorter is not None else store
        view.set_model(Gtk.SingleSelection(model=model))
        view.connect("activate", self._on_row_activated)
        section = table_section(view, block.key, self.settings, title=title)
        arrange_columns(view, block.key, self.settings)
        if not block.rows and block.empty:
            section.append(_label(block.empty, css=("dim",)))
        self.tables[block.key] = view
        self.table_sections[block.key] = section
        return section

    def _column(self, index: int, title: str, numeric: bool, expand: bool):
        factory = Gtk.SignalListItemFactory()

        def setup(_factory, item) -> None:
            label = Gtk.Label(xalign=1 if numeric else 0)
            if numeric:
                label.add_css_class("numeric")
            else:
                label.set_ellipsize(Pango.EllipsizeMode.END)
            item.set_child(label)

        def bind(_factory, item) -> None:
            row = item.get_item().row
            label = item.get_child()
            cell = row.cells[index] if index < len(row.cells) else Cell("")
            label.set_text(_cell_text(cell))
            label.set_tooltip_text(cell.hint or (cell.note if cell.note else None))
            if cell.negative:
                label.add_css_class("negative")
            else:
                label.remove_css_class("negative")

        factory.connect("setup", setup)
        factory.connect("bind", bind)
        column = Gtk.ColumnViewColumn(title=title, factory=factory)
        column.set_expand(expand)
        column.set_resizable(True)
        column.set_sorter(Gtk.CustomSorter.new(_compare(index, numeric)))
        return column

    def _on_row_activated(self, view: Gtk.ColumnView, position: int) -> None:
        item = view.get_model().get_item(position)
        if item is not None:
            self.follow(item.row.link)

    def text(self) -> str:
        """Every word shown, for tests."""
        return self.document.text() if self.document is not None else ""
