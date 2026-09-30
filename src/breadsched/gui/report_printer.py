"""Draw a report layout on printed pages with Pango and cairo.

``ReportPrinter`` lays a :class:`~breadsched.plugins.export.report_layout.ReportDocument`
out on pages of a given size and draws each page on a cairo context, so the same
code serves GTK's print dialog (printer, preview, or PDF) and a direct PDF export.
Every page carries the report title and page number at its foot. Tables are
split between rows, never inside one; a continued table repeats its column
headings, and a heading is never left alone at the foot of a page. Wide tables
shrink their type to fit the page width instead of being cut off.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..plugins.export.report_layout import (
    Cards,
    Cell,
    Chart,
    Heading,
    Paragraph,
    ReportDocument,
    Table,
)
from .gi_setup import Pango, PangoCairo

__all__ = ["ReportPrinter"]

_INK = (0.110, 0.122, 0.141)
_DIM = (0.365, 0.392, 0.439)
_ALARM = (0.616, 0.125, 0.106)
_RULE = (0.875, 0.886, 0.902)
_STRONG_RULE = (0.306, 0.333, 0.376)
_SHADE = (0.933, 0.941, 0.953)

_FONT = "Sans"
_BODY = 9.0
_SMALL = 7.5
_MIN_TABLE_SIZE = 5.5
_PAD = 3.0
_GAP = 8.0
_FOOTER = 16.0
_BOLD_ROWS = frozenset({"heading", "section", "total", "grand"})


def _rgb(colour: str) -> tuple[float, float, float]:
    colour = colour.lstrip("#")
    red, green, blue = (int(colour[index : index + 2], 16) / 255 for index in (0, 2, 4))
    return red, green, blue


@dataclass
class _Item:
    """Something placed in the page flow: a height and a way to draw it."""

    height: float
    draw: Any
    #: Printed again at the top of a page this item starts, e.g. table headings.
    repeat: _Item | None = None
    #: Keep on the same page as the item that follows (headings, table heads).
    keep_with_next: bool = False
    page_break: bool = False
    #: Plain words, so tests and callers can see what each page holds.
    text: str = ""


@dataclass
class _Page:
    placed: list[tuple[float, _Item]] = field(default_factory=list)

    def text(self) -> str:
        return "\n".join(item.text for _y, item in self.placed if item.text)


def _cell_at(row, column: int) -> Cell | None:
    """The cell that alone occupies ``column`` in ``row``; None under a span."""
    start = 0
    for cell in row.cells:
        span = max(1, cell.span)
        if start == column:
            return cell if span == 1 else None
        if start < column < start + span:
            return None
        start += span
    return None


class ReportPrinter:
    """Paginate and draw one report on pages of ``width`` × ``height`` points."""

    def __init__(self, document: ReportDocument, *, include_optional: bool = False) -> None:
        self.document = document
        self.include_optional = include_optional
        self.pages: list[_Page] = []
        self._context: Any = None
        self.width = 0.0
        self.height = 0.0

    # ---------------------------------------------------------------- layout

    def paginate(self, cr, width: float, height: float) -> int:
        """Lay the report out for pages of this size; return the page count."""
        self.width, self.height = width, height
        self._context = PangoCairo.create_context(cr)
        PangoCairo.context_set_resolution(self._context, 72.0)
        items = self._items()
        usable = height - _FOOTER
        pages: list[_Page] = [_Page()]
        y = 0.0
        index = 0
        while index < len(items):
            item = items[index]
            page = pages[-1]
            if item.page_break:
                if page.placed:
                    pages.append(_Page())
                    y = 0.0
                index += 1
                continue
            # Keep a heading (or table head) with what follows it.
            group = [item]
            follow = index
            while group[-1].keep_with_next and follow + 1 < len(items):
                follow += 1
                if items[follow].page_break:
                    break
                group.append(items[follow])
            needed = sum(member.height for member in group)
            if page.placed and y + needed > usable:
                pages.append(_Page())
                page = pages[-1]
                y = 0.0
                if item.repeat is not None:
                    page.placed.append((y, item.repeat))
                    y += item.repeat.height
            page.placed.append((y, item))
            y += item.height
            index += 1
        self.pages = [page for page in pages if page.placed] or [_Page()]
        return len(self.pages)

    def page_text(self, number: int) -> str:
        return self.pages[number].text()

    # ------------------------------------------------------------------ draw

    def draw_page(self, cr, number: int) -> None:
        """Draw one page, laid out by :meth:`paginate`, on ``cr``."""
        PangoCairo.update_context(cr, self._context)
        for y, item in self.pages[number].placed:
            cr.save()
            cr.translate(0, y)
            item.draw(cr)
            cr.restore()
        footer = self._layout(
            f"{self.document.title} · page {number + 1} of {len(self.pages)}", _SMALL
        )
        _, logical = footer.get_pixel_extents()
        cr.set_source_rgb(*_DIM)
        cr.move_to(self.width - logical.width, self.height - logical.height)
        PangoCairo.show_layout(cr, footer)

    # --------------------------------------------------------------- helpers

    def _layout(
        self,
        text: str,
        size: float,
        *,
        bold: bool = False,
        width: float | None = None,
        align: Any = None,
    ):
        layout = Pango.Layout.new(self._context)
        font = Pango.FontDescription.from_string(f"{_FONT} {'Bold ' if bold else ''}{size}")
        layout.set_font_description(font)
        layout.set_text(text, -1)
        if width is not None:
            layout.set_width(int(max(width, 1) * Pango.SCALE))
            layout.set_wrap(Pango.WrapMode.WORD_CHAR)
        if align is not None:
            layout.set_alignment(align)
        return layout

    def _text_item(self, text: str, size: float, colour, *, bold=False, gap=0.0) -> _Item:
        layout = self._layout(text, size, bold=bold, width=self.width)
        _, logical = layout.get_pixel_extents()

        def draw(cr) -> None:
            cr.set_source_rgb(*colour)
            cr.move_to(0, gap)
            PangoCairo.show_layout(cr, layout)

        return _Item(logical.height + gap + 2, draw, text=text)

    def _items(self) -> list[_Item]:
        items = [
            self._text_item(self.document.title, 18, _INK, bold=True),
            self._text_item(self.document.subtitle, _BODY, _DIM),
            _Item(_GAP, lambda _cr: None),
        ]
        for section in self.document.sections:
            if section.optional and not self.include_optional:
                continue
            if section.optional:
                items.append(_Item(0, lambda _cr: None, page_break=True))
            for block in section.blocks:
                items.extend(self._block(block))
        return items

    def _block(self, block) -> list[_Item]:
        if isinstance(block, Heading):
            item = self._text_item(block.text, 12, _INK, bold=True, gap=_GAP)
            item.keep_with_next = True
            return [item]
        if isinstance(block, Paragraph):
            text = f"• {block.text}" if block.bullet else block.text
            return [self._text_item(text, _BODY, _ALARM if block.warning else _DIM, gap=2)]
        if isinstance(block, Cards):
            return self._cards(block)
        if isinstance(block, Chart):
            return [self._chart(block)]
        return self._table(block)

    def _cards(self, block: Cards) -> list[_Item]:
        spacing = 6.0
        per_row = max(1, int((self.width + spacing) // (130 + spacing)))
        card_width = (self.width - spacing * (per_row - 1)) / per_row
        rows = [block.items[i : i + per_row] for i in range(0, len(block.items), per_row)]
        items = []
        for row in rows:
            laid = [
                (
                    self._layout(card.label, _SMALL, width=card_width - 2 * _PAD),
                    self._layout(card.value, 12, bold=True, width=card_width - 2 * _PAD),
                    card.alarm,
                )
                for card in row
            ]
            height = (
                max(
                    label.get_pixel_extents()[1].height + value.get_pixel_extents()[1].height
                    for label, value, _alarm in laid
                )
                + 2 * _PAD
            )

            def draw(cr, laid=laid, height=height) -> None:
                for position, (label, value, alarm) in enumerate(laid):
                    x = position * (card_width + spacing)
                    cr.set_source_rgb(*_RULE)
                    cr.set_line_width(0.75)
                    cr.rectangle(x + 0.5, 0.5, card_width - 1, height - 1)
                    cr.stroke()
                    cr.set_source_rgb(*_DIM)
                    cr.move_to(x + _PAD, _PAD)
                    PangoCairo.show_layout(cr, label)
                    cr.set_source_rgb(*(_ALARM if alarm else _INK))
                    cr.move_to(x + _PAD, _PAD + label.get_pixel_extents()[1].height)
                    PangoCairo.show_layout(cr, value)

            text = "  ".join(f"{card.label} {card.value}" for card in row)
            items.append(_Item(height + spacing, draw, text=text))
        return items

    # ----------------------------------------------------------------- table

    def _table(self, table: Table) -> list[_Item]:
        if not table.rows:
            if not table.empty:
                return []
            return [self._text_item(table.empty, _BODY, _DIM, gap=2)]
        size, widths = self._fit_columns(table)
        head = self._table_head(table, size, widths)
        items = [head]
        for number, row in enumerate(table.rows):
            item = self._table_row(table, row, size, widths)
            item.repeat = head
            if number == 0:
                head.keep_with_next = True
            items.append(item)
        items.append(_Item(_GAP, lambda _cr: None))
        return items

    def _natural(self, text: str, size: float, bold: bool = False) -> float:
        if not text:
            return 0.0
        layout = self._layout(text, size, bold=bold)
        return layout.get_pixel_extents()[1].width

    def _cell_text(self, cell: Cell) -> str:
        return ("    " * cell.indent) + cell.text

    def _fit_columns(self, table: Table) -> tuple[float, list[float]]:
        """A type size and column widths that fit the page width."""
        size = _BODY
        while True:
            widths = []
            for index, column in enumerate(table.columns):
                natural = self._natural(column.label, size * _SMALL / _BODY, bold=True)
                for row in table.rows:
                    cell = _cell_at(row, index)
                    if row.style == "section" or cell is None:
                        continue
                    text = self._cell_text(cell)
                    if cell.note and not cell.below:
                        text = f"{text} {cell.note}"
                    bold = row.style in _BOLD_ROWS
                    natural = max(natural, self._natural(text, size, bold))
                    if cell.note and cell.below:
                        natural = max(natural, self._natural(cell.note, size * _SMALL / _BODY))
                widths.append(natural + 2 * _PAD)
            text_columns = [i for i, column in enumerate(table.columns) if not column.numeric]
            total = sum(widths)
            if total <= self.width:
                # Give spare width to the text columns so notes wrap less.
                spare = self.width - total
                for index in text_columns or range(len(widths)):
                    widths[index] += spare / max(1, len(text_columns) or len(widths))
                return size, widths
            # Text columns may wrap down to a floor; numbers never wrap.
            numeric = sum(w for i, w in enumerate(widths) if i not in text_columns)
            floor = 60.0 * size / _BODY
            room = self.width - numeric
            if text_columns and room >= floor * len(text_columns):
                wanted = sum(widths[i] for i in text_columns)
                for index in text_columns:
                    widths[index] = max(floor, room * widths[index] / wanted)
                scale = self.width / sum(widths)
                if scale >= 1:
                    return size, widths
            if size <= _MIN_TABLE_SIZE:
                scale = self.width / sum(widths)
                return size, [width * scale for width in widths]
            size = max(_MIN_TABLE_SIZE, size - 0.5)

    def _table_head(self, table: Table, size: float, widths: list[float]) -> _Item:
        small = size * _SMALL / _BODY
        layouts = [
            self._layout(
                column.label,
                small,
                bold=True,
                width=widths[index] - 2 * _PAD,
                align=Pango.Alignment.RIGHT if column.numeric else None,
            )
            for index, column in enumerate(table.columns)
        ]
        height = max(layout.get_pixel_extents()[1].height for layout in layouts) + 2 * _PAD

        def draw(cr) -> None:
            x = 0.0
            cr.set_source_rgb(*_DIM)
            for layout, width in zip(layouts, widths, strict=True):
                cr.move_to(x + _PAD, _PAD)
                PangoCairo.show_layout(cr, layout)
                x += width
            cr.set_source_rgb(*_STRONG_RULE)
            cr.set_line_width(0.75)
            cr.move_to(0, height - 0.5)
            cr.line_to(sum(widths), height - 0.5)
            cr.stroke()

        text = " | ".join(column.label for column in table.columns)
        return _Item(height, draw, text=text)

    def _table_row(self, table: Table, row, size: float, widths: list[float]) -> _Item:
        bold = row.style in _BOLD_ROWS
        if row.style == "section":
            spans = [(row.cells[0], sum(widths), False)]
        else:
            spans = []
            start = 0
            for cell in row.cells:
                if start >= len(widths):
                    break
                end = min(len(widths), start + max(1, cell.span))
                numeric = end - start == 1 and table.columns[start].numeric
                spans.append((cell, sum(widths[start:end]), numeric))
                start = end
        laid = []
        for cell, width, numeric in spans:
            text = self._cell_text(cell)
            if cell.note and not cell.below:
                text = f"{text} {cell.note}"
            layout = self._layout(
                text,
                size,
                bold=bold,
                width=width - 2 * _PAD,
                align=Pango.Alignment.RIGHT if numeric or cell.numeric else None,
            )
            note = (
                self._layout(cell.note, size * _SMALL / _BODY, width=width - 2 * _PAD)
                if cell.note and cell.below
                else None
            )
            laid.append((cell, width, layout, note))
        height = (
            max(
                layout.get_pixel_extents()[1].height
                + (note.get_pixel_extents()[1].height if note is not None else 0)
                for _cell, _width, layout, note in laid
            )
            + 2 * _PAD
        )

        def draw(cr) -> None:
            if row.style == "section":
                cr.set_source_rgb(*_SHADE)
                cr.rectangle(0, 0, sum(widths), height)
                cr.fill()
            x = 0.0
            for cell, width, layout, note in laid:
                cr.set_source_rgb(*(_ALARM if cell.negative else _INK))
                cr.move_to(x + _PAD, _PAD)
                PangoCairo.show_layout(cr, layout)
                if note is not None:
                    cr.set_source_rgb(*_DIM)
                    cr.move_to(x + _PAD, _PAD + layout.get_pixel_extents()[1].height)
                    PangoCairo.show_layout(cr, note)
                x += width
            if row.style in {"total", "grand"}:
                cr.set_source_rgb(*_STRONG_RULE)
                cr.set_line_width(1.5 if row.style == "grand" else 0.75)
                cr.move_to(0, 0.5)
                cr.line_to(sum(widths), 0.5)
                cr.stroke()
            cr.set_source_rgb(*_RULE)
            cr.set_line_width(0.5)
            cr.move_to(0, height - 0.25)
            cr.line_to(sum(widths), height - 0.25)
            cr.stroke()

        text = " | ".join(
            f"{self._cell_text(cell).strip()} {cell.note}".strip()
            for cell, *_rest in laid
            if cell.text or cell.note
        )
        return _Item(height, draw, text=text)

    # ----------------------------------------------------------------- chart

    def _chart(self, chart: Chart) -> _Item:
        height = min(240.0, self.width * 0.34)
        values = [value for series in chart.series for value in series.values]
        legend = [self._layout(series.name, _SMALL) for series in chart.series]
        ticks = [(index, self._layout(label, _SMALL)) for index, label in chart.ticks]
        if not values:
            return self._text_item("No projected values.", _BODY, _DIM)
        low, high = min(values), max(values)
        if low == high:
            low, high = low - 1, high + 1
        count = max(len(series.values) for series in chart.series)
        left, top, bottom = 8.0, 16.0, 16.0
        plot_width = self.width - left
        plot_height = height - top - bottom

        def at(index: int, value: float) -> tuple[float, float]:
            x = left + plot_width * index / max(1, count - 1)
            return x, top + plot_height * (high - value) / (high - low)

        def draw(cr) -> None:
            cr.set_source_rgb(*_RULE)
            cr.set_line_width(0.75)
            cr.move_to(left, top)
            cr.line_to(left, top + plot_height)
            cr.line_to(self.width, top + plot_height)
            cr.stroke()
            if low <= 0 <= high:
                zero = at(0, 0.0)[1]
                cr.set_dash([3.0, 3.0])
                cr.move_to(left, zero)
                cr.line_to(self.width, zero)
                cr.stroke()
                cr.set_dash([])
            x = left
            for series, label in zip(chart.series, legend, strict=True):
                cr.set_source_rgb(*_rgb(series.colour))
                cr.set_line_width(1.5)
                for index, value in enumerate(series.values):
                    point = at(index, value)
                    if index:
                        cr.line_to(*point)
                    else:
                        cr.move_to(*point)
                cr.stroke()
                cr.move_to(x, 6)
                cr.line_to(x + 14, 6)
                cr.stroke()
                cr.set_source_rgb(*_INK)
                cr.move_to(x + 18, 0)
                PangoCairo.show_layout(cr, label)
                x += 18 + label.get_pixel_extents()[1].width + 16
            cr.set_source_rgb(*_DIM)
            for index, label in ticks:
                width = label.get_pixel_extents()[1].width
                tick_x = at(index, low)[0]
                tick_x = min(max(tick_x - width / 2, left), self.width - width)
                cr.move_to(tick_x, top + plot_height + 3)
                PangoCairo.show_layout(cr, label)

        return _Item(height + _GAP, draw, text=chart.label)
