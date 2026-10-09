"""A GnuCash-style register grid: ruled plain-text rows, one editor at the cursor.

The grid draws the rows of a :class:`~breadsched.gui.register_sheet.RegisterSheet`
itself (Cairo and Pango), so a register reads like a checkbook: ruled lines,
banded rows, and no entry boxes. A single frameless ``Gtk.Entry`` sits over the
cell under the cursor; the transaction under the cursor is shaded as GnuCash
shades it. Only the visible rows are drawn, so a register of many thousands of
transactions scrolls as quickly as a short one.

Keys follow GnuCash's register: Tab and Shift+Tab move between cells (past the
last one, the transaction is saved), Enter saves and moves on, Up and Down move
between transactions (saving the one left), Escape puts it back, and Page Up and
Page Down move a page. The date and number cells take their shortcut keys; the
description quick-fills from earlier entries, and the transfer cell completes an
account one ``:`` segment at a time, listing the accounts it may mean beneath the
cell. Clicking a cell moves the cursor there; clicking R marks a split cleared.
"""

from __future__ import annotations

from collections.abc import Callable

from ..gi_setup import Gdk, Gtk, Pango, PangoCairo
from ..register_sheet import BLANK, IMBALANCE, SPLIT, RegisterSheet

__all__ = ["RegisterGrid", "grid_colours"]

#: Column widths in characters for the fixed columns; the rest share what is left.
_FIXED = {"date": 11, "num": 6, "reconcile": 3, "increase": 12, "decrease": 12, "balance": 13}
_SHARED = {"description": 0.55, "transfer": 0.45}
_NUMERIC = {"increase", "decrease", "balance", "reconcile"}
_PAD = 6
#: Accounts listed beneath the transfer cell.
_MATCH_ROWS = 8

_LIGHT = {
    "surface": "#ffffff",
    "band": "#f4f6f8",
    "header": "#e7eaee",
    "current": "#fff7c4",
    "current_split": "#fdfbe8",
    "grid": "#d6dbe1",
    "dim": "#5f6670",
    "negative": "#c01c28",
    "future": "#1c71d8",
}
_DARK = {
    "surface": "#1d1e20",
    "band": "#25272a",
    "header": "#303337",
    "current": "#4a4424",
    "current_split": "#3a3727",
    "grid": "#3b3f45",
    "dim": "#a3a9b1",
    "negative": "#ff7b72",
    "future": "#62a0ea",
}


def grid_colours(dark: bool) -> dict[str, str]:
    """The grid's colours for a light or dark theme."""
    return _DARK if dark else _LIGHT


def _rgb(colour: str) -> tuple[float, float, float]:
    colour = colour.lstrip("#")
    return tuple(int(colour[i : i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


def _typed_end(typed: str, shown: str) -> int:
    """Where the typed part of a quick-filled cell ends, so the rest can be selected.

    Earlier account segments may change spelling (``ex:`` shows ``Expenses:``), so
    the typed end is found segment by segment rather than by length.
    """
    if shown.casefold().startswith(typed.casefold()):
        return len(typed)
    typed_parts, shown_parts = typed.split(":"), shown.split(":")
    if len(shown_parts) < len(typed_parts):
        return len(shown)
    head = ":".join(shown_parts[: len(typed_parts) - 1])
    end = len(head) + (1 if len(typed_parts) > 1 else 0) + len(typed_parts[-1])
    return min(end, len(shown))


class RegisterGrid(Gtk.Box):
    """The drawn register, its cursor editor, and its account list."""

    def __init__(self, on_change: Callable[[], None] | None = None) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
        self.sheet: RegisterSheet | None = None
        #: Called after anything the user did, so the view can show the message.
        self.on_change = on_change
        self.row_height = 26
        self.char_width = 8.0
        self._syncing = False
        self._widths: dict[str, float] = {}
        self._width = 0
        self._height = 0
        self._dark = False

        self.adjustment = Gtk.Adjustment(lower=0, upper=1, step_increment=1, page_increment=10)
        self.adjustment.connect("value-changed", lambda *_: self._after_scroll())

        self.area = Gtk.DrawingArea(hexpand=True, vexpand=True, focusable=True)
        self.area.set_draw_func(self._draw)
        self.area.connect("resize", lambda _a, width, height: self._on_resize(width, height))
        self.area.update_property([Gtk.AccessibleProperty.LABEL], ["Register"])
        self.area.add_css_class("register-grid")

        self.editor = Gtk.Entry(has_frame=False, halign=Gtk.Align.START, valign=Gtk.Align.START)
        self.editor.add_css_class("register-editor")
        self.editor.set_visible(False)
        self.editor.connect("changed", self._on_editor_changed)
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", lambda _c, keyval, _code, state: self.key(keyval, state))
        self.editor.add_controller(keys)
        # Keys reach the grid itself while no cell takes typing (a hidden account).
        area_keys = Gtk.EventControllerKey()
        area_keys.connect("key-pressed", lambda _c, keyval, _code, state: self.key(keyval, state))
        self.area.add_controller(area_keys)

        self.matches = Gtk.ListBox()
        self.matches.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.matches.set_can_focus(False)
        self.matches.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Accounts matching what you typed"]
        )
        self.matches.connect("row-activated", lambda _box, row: self.accept_match(row.name))
        self.match_frame = Gtk.ScrolledWindow(
            child=self.matches,
            halign=Gtk.Align.START,
            valign=Gtk.Align.START,
            propagate_natural_height=True,
            max_content_height=_MATCH_ROWS * 26,
        )
        self.match_frame.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.match_frame.add_css_class("register-matches")
        self.match_frame.set_visible(False)

        self.overlay = Gtk.Overlay(child=self.area, hexpand=True, vexpand=True)
        self.overlay.add_overlay(self.editor)
        self.overlay.add_overlay(self.match_frame)
        self.append(self.overlay)
        self.scrollbar = Gtk.Scrollbar(
            orientation=Gtk.Orientation.VERTICAL, adjustment=self.adjustment
        )
        self.append(self.scrollbar)

        click = Gtk.GestureClick()
        click.connect("pressed", lambda _g, _n, x, y: self.click(x, y))
        self.area.add_controller(click)
        scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", lambda _c, _dx, dy: self._on_scroll(dy))
        self.overlay.add_controller(scroll)

    # ------------------------------------------------------------------ model

    def set_sheet(self, sheet: RegisterSheet | None) -> None:
        self.sheet = sheet
        self.sync(focus=False)

    @property
    def first_row(self) -> int:
        return int(self.adjustment.get_value())

    @property
    def visible_rows(self) -> int:
        return max(1, (self._height - self.row_height) // self.row_height)

    def _on_resize(self, width: int, height: int) -> None:
        self._width, self._height = width, height
        self._measure()
        self.sync(focus=False)

    def _measure(self) -> None:
        layout = self.area.create_pango_layout("0000000000")
        width, height = layout.get_pixel_size()
        self.char_width = width / 10
        self.row_height = max(22, height + 8)
        self.match_frame.set_max_content_height(_MATCH_ROWS * self.row_height)
        available = max(self._width, 1)
        fixed = {key: chars * self.char_width + 2 * _PAD for key, chars in _FIXED.items()}
        rest = max(available - sum(fixed.values()), 160)
        widths = dict(fixed)
        for key, share in _SHARED.items():
            widths[key] = rest * share
        self._widths = {key: widths[key] for key in self.columns}

    @property
    def columns(self) -> tuple[str, ...]:
        from ..register_sheet import COLUMNS

        return COLUMNS

    def column_x(self, column: str) -> tuple[float, float]:
        """The left edge and width of ``column``."""
        x = 0.0
        for key in self.columns:
            width = self._widths.get(key, 0.0)
            if key == column:
                return x, width
            x += width
        return x, 0.0

    def cell_at(self, x: float, y: float) -> tuple[int, str] | None:
        """The (row index, column) under a point, or None for the header or below."""
        if self.sheet is None or y < self.row_height:
            return None
        index = self.first_row + int((y - self.row_height) // self.row_height)
        if index >= len(self.sheet.rows):
            return None
        left = 0.0
        for key in self.columns:
            width = self._widths.get(key, 0.0)
            if left <= x < left + width:
                return index, key
            left += width
        return index, self.columns[-1]

    # ---------------------------------------------------------------- syncing

    def sync(self, *, focus: bool = True) -> None:
        """Show the sheet as it is now: scroll to the cursor, place the editor."""
        sheet = self.sheet
        rows = len(sheet.rows) if sheet is not None else 1
        visible = self.visible_rows
        self.adjustment.configure(
            min(self.adjustment.get_value(), max(0, rows - visible)),
            0,
            rows,
            1,
            max(1, visible - 1),
            min(visible, rows),
        )
        if sheet is not None:
            self.ensure_visible(sheet.cursor)
            account = sheet.account_object
            name = sheet.db.full_name(account) if account is not None else ""
            self.area.update_property([Gtk.AccessibleProperty.LABEL], [f"Register for {name}"])
        self._place_editor(focus)
        self.area.queue_draw()
        if self.on_change is not None:
            self.on_change()

    def ensure_visible(self, index: int) -> None:
        first, visible = self.first_row, self.visible_rows
        if index < first:
            self.adjustment.set_value(index)
        elif index >= first + visible:
            self.adjustment.set_value(index - visible + 1)

    def _after_scroll(self) -> None:
        self._place_editor(False)
        self.area.queue_draw()

    def _on_scroll(self, dy: float) -> bool:
        self.adjustment.set_value(self.adjustment.get_value() + dy * 3)
        return True

    def _place_editor(self, focus: bool) -> None:
        sheet = self.sheet
        editable = (
            sheet is not None
            and 0 <= sheet.cursor < len(sheet.rows)
            and sheet.column in sheet.editable_columns(sheet.cursor)
        )
        position = sheet.cursor - self.first_row if sheet is not None else -1
        if not editable or not 0 <= position < self.visible_rows or not self._widths:
            self.editor.set_visible(False)
            self.hide_matches()
            if focus and sheet is not None:
                self.area.grab_focus()
            return
        assert sheet is not None
        x, width = self.column_x(sheet.column)
        y = self.row_height * (position + 1)
        self.editor.set_margin_start(int(x))
        self.editor.set_margin_top(int(y))
        self.editor.set_size_request(max(int(width) - 1, 20), self.row_height - 1)
        self.editor.set_alignment(1.0 if sheet.column in _NUMERIC else 0.0)
        text = sheet.cell_text()
        if self.editor.get_text() != text:
            self._syncing = True
            try:
                self.editor.set_text(text)
            finally:
                self._syncing = False
        heading = sheet.headings()[sheet.column]
        self.editor.update_property(
            [Gtk.AccessibleProperty.LABEL], [f"{heading}: {sheet.summary(sheet.cursor)}"]
        )
        self.editor.set_visible(True)
        if focus:
            self.editor.grab_focus()
            self.editor.set_position(-1)

    def _on_editor_changed(self, entry: Gtk.Entry) -> None:
        if self._syncing or self.sheet is None:
            return
        self.sheet.set_text(entry.get_text())
        if self.sheet.column == "transfer":
            # Deleting keeps the list current but completes nothing back in.
            _shown, names = self.sheet.quickfill(entry.get_text())
            self.show_matches(names)

    # ---------------------------------------------------------------- matches

    def show_matches(self, names: list[str]) -> None:
        while (child := self.matches.get_first_child()) is not None:
            self.matches.remove(child)
        if not names or self.sheet is None:
            self.match_frame.set_visible(False)
            return
        for name in names:
            row = Gtk.ListBoxRow()
            row.name = name
            label = Gtk.Label(label=name, xalign=0)
            label.set_margin_start(_PAD)
            label.set_margin_end(_PAD)
            row.set_child(label)
            self.matches.append(row)
        self.matches.select_row(self.matches.get_row_at_index(0))
        x, width = self.column_x("transfer")
        position = self.sheet.cursor - self.first_row
        self.match_frame.set_margin_start(int(x))
        self.match_frame.set_margin_top(int(self.row_height * (position + 2)))
        self.match_frame.set_size_request(int(max(width, 240)), -1)
        self.match_frame.set_visible(True)

    def hide_matches(self) -> None:
        self.match_frame.set_visible(False)

    def match_names(self) -> list[str]:
        if not self.match_frame.get_visible():
            return []
        names = []
        row = self.matches.get_first_child()
        while row is not None:
            names.append(row.name)
            row = row.get_next_sibling()
        return names

    def selected_match(self) -> str | None:
        if not self.match_frame.get_visible():
            return None
        row = self.matches.get_selected_row()
        return row.name if row is not None else None

    def _step_match(self, step: int) -> None:
        row = self.matches.get_selected_row()
        index = row.get_index() + step if row is not None else 0
        target = self.matches.get_row_at_index(max(0, index))
        if target is not None:
            self.matches.select_row(target)

    def accept_match(self, name: str) -> None:
        """Take an account from the list into the transfer cell."""
        if self.sheet is None:
            return
        self.sheet.set_text(name)
        self.hide_matches()
        self._syncing = True
        try:
            self.editor.set_text(name)
        finally:
            self._syncing = False
        self.editor.grab_focus()
        self.editor.set_position(-1)

    # ------------------------------------------------------------------ input

    def click(self, x: float, y: float) -> bool:
        """Move the cursor to the clicked cell; clicking R marks the split cleared."""
        cell = self.cell_at(x, y)
        if cell is None or self.sheet is None:
            return False
        index, column = cell
        row = self.sheet.rows[index]
        if (
            column == "reconcile"
            and row.register_row is not None
            and row.kind
            not in (
                SPLIT,
                IMBALANCE,
            )
        ):
            self.sheet.toggle_reconcile(index)
            self.sync()
            return True
        self.hide_matches()
        self.sheet.move_to(index, column)
        self.sync()
        return True

    def key(self, keyval: int, state: Gdk.ModifierType) -> bool:
        """Handle one key at the cursor; True when the grid used it."""
        sheet = self.sheet
        if sheet is None:
            return False
        control = bool(state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK))
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        character = chr(Gdk.keyval_to_unicode(keyval) or 0)
        listing = self.match_frame.get_visible()
        if keyval in (Gdk.KEY_Tab, Gdk.KEY_ISO_Left_Tab, Gdk.KEY_KP_Tab):
            self._take_match()
            sheet.tab(backwards=shift or keyval == Gdk.KEY_ISO_Left_Tab)
            return self._done()
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_ISO_Enter):
            self._take_match()
            sheet.enter()
            return self._done()
        if keyval == Gdk.KEY_Escape:
            if listing:
                self.hide_matches()
                return True
            sheet.escape()
            return self._done()
        if keyval in (Gdk.KEY_Up, Gdk.KEY_KP_Up, Gdk.KEY_Down, Gdk.KEY_KP_Down):
            step = -1 if keyval in (Gdk.KEY_Up, Gdk.KEY_KP_Up) else 1
            if listing:
                self._step_match(step)
                return True
            sheet.move(step)
            return self._done()
        if keyval in (Gdk.KEY_Page_Up, Gdk.KEY_Page_Down, Gdk.KEY_KP_Page_Up, Gdk.KEY_KP_Page_Down):
            step = self.visible_rows - 1
            if keyval in (Gdk.KEY_Page_Up, Gdk.KEY_KP_Page_Up):
                step = -step
            if sheet.settle():
                sheet.move_to(max(0, min(sheet.cursor + step, len(sheet.rows) - 1)), sheet.column)
            return self._done()
        if control and keyval in (Gdk.KEY_End, Gdk.KEY_KP_End):
            sheet.to_blank()
            return self._done()
        if control and keyval in (Gdk.KEY_Home, Gdk.KEY_KP_Home):
            if sheet.settle():
                sheet.move_to(0, sheet.column)
            return self._done()
        if control or not self.editor.get_visible() or not character.isprintable() or not character:
            return False
        text = self.editor.get_text()
        if sheet.column == "date" and sheet.rows[sheet.cursor].kind != SPLIT:
            shifted = sheet.date_key(text, character)
            if shifted is not None:
                self._replace(shifted, len(shifted))
                return True
            return False
        if sheet.column == "num" and character in "+=-_":
            stepped = sheet.num_key(text, 1 if character in "+=" else -1)
            if stepped is not None:
                self._replace(stepped, len(stepped))
                return True
            return False
        if sheet.column in ("transfer", "description"):
            bounds = self.editor.get_selection_bounds()
            start = bounds[0] if bounds else self.editor.get_position()
            typed = text[:start] + character
            if not bounds and start < len(text):
                return False  # typing inside the text: no quick-fill
            shown, names = sheet.quickfill(typed)
            self._replace(shown, _typed_end(typed, shown))
            if sheet.column == "transfer":
                self.show_matches(names)
            return True
        return False

    def _take_match(self) -> None:
        """Leaving the transfer cell takes the account chosen in the list."""
        name = self.selected_match()
        if name is not None and self.sheet is not None:
            self.sheet.set_text(name)
        self.hide_matches()

    def _replace(self, text: str, keep: int) -> None:
        """Show ``text`` in the editor with everything after ``keep`` selected."""
        assert self.sheet is not None
        self._syncing = True
        try:
            self.editor.set_text(text)
        finally:
            self._syncing = False
        self.sheet.set_text(text)
        if keep < len(text):
            self.editor.select_region(keep, len(text))
        else:
            self.editor.set_position(-1)

    def _done(self) -> bool:
        self.sync()
        return True

    # --------------------------------------------------------------- drawing

    def _draw(self, area, cr, width: int, height: int) -> None:
        colour = area.get_color()
        self._dark = (colour.red + colour.green + colour.blue) / 3 > 0.5
        palette = grid_colours(self._dark)
        ink = (colour.red, colour.green, colour.blue)
        if not self._widths or width != self._width:
            self._width, self._height = width, height
            self._measure()
        cr.set_source_rgb(*_rgb(palette["surface"]))
        cr.paint()
        sheet = self.sheet
        self._draw_header(area, cr, width, palette, ink)
        if sheet is None:
            self._draw_rules(cr, self.row_height, palette)
            return
        first = self.first_row
        future_ruled = False
        for position in range(self.visible_rows + 1):
            index = first + position
            if index >= len(sheet.rows):
                break
            y = self.row_height * (position + 1)
            row = sheet.rows[index]
            if row.current:
                fill = (
                    palette["current_split"]
                    if row.kind in (SPLIT, IMBALANCE)
                    else palette["current"]
                )
            elif index % 2:
                fill = palette["band"]
            else:
                fill = palette["surface"]
            cr.set_source_rgb(*_rgb(fill))
            cr.rectangle(0, y, width, self.row_height)
            cr.fill()
            for column in self.columns:
                text = sheet.text(index, column)
                if not text or self.under_editor(index, column):
                    continue
                if sheet.negative(index, column):
                    cr.set_source_rgb(*_rgb(palette["negative"]))
                elif row.kind in (SPLIT, IMBALANCE) or (row.kind == BLANK and not row.current):
                    cr.set_source_rgb(*_rgb(palette["dim"]))
                else:
                    cr.set_source_rgb(*ink)
                self._text(area, cr, column, y, text)
            # GnuCash rules off the first transaction dated after today.
            if not future_ruled and sheet.future(index) and row.kind not in (SPLIT, IMBALANCE):
                future_ruled = True
                if index == 0 or not sheet.future(index - 1):
                    cr.set_source_rgb(*_rgb(palette["future"]))
                    cr.set_line_width(2)
                    cr.move_to(0, y + 1)
                    cr.line_to(width, y + 1)
                    cr.stroke()
            cr.set_source_rgb(*_rgb(palette["grid"]))
            cr.set_line_width(1)
            cr.move_to(0, y + self.row_height - 0.5)
            cr.line_to(width, y + self.row_height - 0.5)
            cr.stroke()
        drawn = min(len(sheet.rows) - first, self.visible_rows + 1)
        self._draw_rules(cr, self.row_height * (drawn + 1), palette)

    def under_editor(self, index: int, column: str) -> bool:
        """Whether the editor covers this cell (so its text is not drawn twice)."""
        sheet = self.sheet
        return (
            sheet is not None
            and index == sheet.cursor
            and column == sheet.column
            and self.editor.get_visible()
        )

    def _draw_header(self, area, cr, width: int, palette, ink) -> None:
        cr.set_source_rgb(*_rgb(palette["header"]))
        cr.rectangle(0, 0, width, self.row_height)
        cr.fill()
        headings = self.sheet.headings() if self.sheet is not None else {}
        cr.set_source_rgb(*ink)
        for column in self.columns:
            self._text(area, cr, column, 0, headings.get(column, ""), bold=True)

    def _draw_rules(self, cr, height: int, palette) -> None:
        cr.set_source_rgb(*_rgb(palette["grid"]))
        cr.set_line_width(1)
        x = 0.0
        for column in self.columns[:-1]:
            x += self._widths.get(column, 0.0)
            cr.move_to(round(x) - 0.5, 0)
            cr.line_to(round(x) - 0.5, height)
            cr.stroke()

    def _text(self, area, cr, column: str, y: float, text: str, *, bold: bool = False) -> None:
        x, width = self.column_x(column)
        layout = area.create_pango_layout(text)
        layout.set_width(int(max(width - 2 * _PAD, 1) * Pango.SCALE))
        layout.set_ellipsize(Pango.EllipsizeMode.END)
        if column in _NUMERIC:
            layout.set_alignment(Pango.Alignment.RIGHT)
        if bold:
            description = area.get_pango_context().get_font_description().copy()
            description.set_weight(Pango.Weight.BOLD)
            layout.set_font_description(description)
        _w, text_height = layout.get_pixel_size()
        cr.move_to(x + _PAD, y + (self.row_height - text_height) / 2)
        PangoCairo.show_layout(cr, layout)

    # ---------------------------------------------------------------- testing

    def paint(self, width: int, height: int):
        """Draw onto an image surface (no window needed); returns the surface."""
        import cairo

        self._width, self._height = width, height
        self._measure()
        self.sync(focus=False)
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
        self._draw(self.area, cairo.Context(surface), width, height)
        return surface
