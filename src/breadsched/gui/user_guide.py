"""Offline reader for BreadSched's packaged Markdown user guide.

The guide has an overview and desktop, browser, and command-line parts (#181),
loaded through the shared ``breadsched.user_guide`` module. A switcher chooses the
part, and a link to another part or heading opens it here; an external link opens
in the default browser.
"""

from __future__ import annotations

import re

from ..user_guide import GUIDE_PARTS, GuideLink, heading_slug, read_guide, resolve_link
from .gi_setup import Gtk, Pango
from .widgets.bounded import BoundedWindow

__all__ = ["UserGuideWindow", "read_user_guide"]

_INLINE = re.compile(r"`([^`]+)`|\*\*([^*]+)\*\*|__([^_]+)__|\[([^]]+)]\(([^)]+)\)")

#: A block's style, its text, and the (start, end, href) of each link in the text.
Block = tuple[str, str, list[tuple[int, int, str]]]


def read_user_guide() -> str:
    """Return the overview packaged with the installed release."""
    return read_guide("overview")


def _inline(text: str) -> tuple[str, list[tuple[int, int, str]]]:
    """Plain text for the guide's inline Markdown, with where each link sits."""
    out: list[str] = []
    links: list[tuple[int, int, str]] = []
    length = 0
    last = 0
    for match in _INLINE.finditer(text):
        before = text[last : match.start()]
        out.append(before)
        length += len(before)
        label = match.group(1) or match.group(2) or match.group(3) or match.group(4) or ""
        if match.group(5) is not None:
            links.append((length, length + len(label), match.group(5)))
        out.append(label)
        length += len(label)
        last = match.end()
    out.append(text[last:])
    return "".join(out), links


def _guide_blocks(markdown: str) -> list[Block]:
    """Turn the guide's conservative Markdown into readable text-view blocks."""
    blocks: list[Block] = []
    paragraph: list[str] = []
    code: list[str] = []
    in_code = False
    list_source: tuple[int, str, str] | None = None  # block index, prefix, source

    def flush_paragraph() -> None:
        if paragraph:
            text, links = _inline(" ".join(paragraph))
            blocks.append(("body", text, links))
            paragraph.clear()

    def add_item(prefix: str, source: str) -> None:
        nonlocal list_source
        text, links = _inline(source)
        shifted = [(start + len(prefix), end + len(prefix), href) for start, end, href in links]
        blocks.append(("bullet", prefix + text, shifted))
        list_source = (len(blocks) - 1, prefix, source)

    for raw in markdown.splitlines():
        line = raw.rstrip()
        if line.startswith("```"):
            flush_paragraph()
            list_source = None
            if in_code:
                blocks.append(("code", "\n".join(code), []))
                code.clear()
            in_code = not in_code
            continue
        if in_code:
            code.append(line)
            continue
        if not line:
            flush_paragraph()
            list_source = None
            continue
        heading = re.match(r"^(#{1,3})\s+(.+)$", line)
        if heading:
            flush_paragraph()
            list_source = None
            text, links = _inline(heading.group(2))
            blocks.append((f"heading{len(heading.group(1))}", text, links))
            continue
        bullet = re.match(r"^\s*[-*]\s+(.+)$", line)
        if bullet:
            flush_paragraph()
            add_item("• ", bullet.group(1))
            continue
        numbered = re.match(r"^\s*(\d+)\.\s+(.+)$", line)
        if numbered:
            flush_paragraph()
            add_item(f"{numbered.group(1)}. ", numbered.group(2))
            continue
        if list_source is not None:
            index, prefix, source = list_source
            blocks.pop(index)
            add_item(prefix, f"{source} {line.strip()}")
            continue
        paragraph.append(line.strip())

    flush_paragraph()
    if code:
        blocks.append(("code", "\n".join(code), []))
    return blocks


class UserGuideWindow(BoundedWindow):
    """A scrollable, dependency-free presentation of every part of the guide."""

    def __init__(self, application, parent) -> None:
        super().__init__(
            application=application,
            transient_for=parent,
            title="BreadSched User Guide",
        )
        self.set_default_size(860, 700)
        #: The part shown, and the target of each link tag in the buffer.
        self.part = GUIDE_PARTS[0].id
        self._links: dict[Gtk.TextTag, str] = {}
        self._marks: list[str] = []

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        switcher = Gtk.Box(spacing=0, halign=Gtk.Align.CENTER)
        switcher.add_css_class("linked")
        for side in ("top", "bottom"):
            getattr(switcher, f"set_margin_{side}")(8)
        #: One toggle per part, in guide order.
        self.part_buttons: dict[str, Gtk.ToggleButton] = {}
        group: Gtk.ToggleButton | None = None
        for part in GUIDE_PARTS:
            button = Gtk.ToggleButton(label=part.title)
            if group is not None:
                button.set_group(group)
            group = group or button
            button.connect("toggled", self._on_part_toggled, part.id)
            self.part_buttons[part.id] = button
            switcher.append(button)
        box.append(switcher)

        self.text_view = Gtk.TextView(
            editable=False,
            cursor_visible=False,
            wrap_mode=Gtk.WrapMode.WORD,
            left_margin=28,
            right_margin=28,
            top_margin=20,
            bottom_margin=28,
        )
        buffer = self.text_view.get_buffer()
        buffer.create_tag("heading1", weight=700, scale=1.65, pixels_above_lines=12)
        buffer.create_tag("heading2", weight=700, scale=1.35, pixels_above_lines=16)
        buffer.create_tag("heading3", weight=700, scale=1.12, pixels_above_lines=12)
        buffer.create_tag("body", pixels_above_lines=3, pixels_below_lines=6)
        buffer.create_tag("bullet", left_margin=22, indent=-16, pixels_below_lines=4)
        buffer.create_tag("code", family="monospace", left_margin=18, pixels_below_lines=8)
        buffer.create_tag("link", foreground="#1c63b7", underline=Pango.Underline.SINGLE)
        click = Gtk.GestureClick()
        click.connect("released", self._on_click)
        self.text_view.add_controller(click)

        self.scroller = Gtk.ScrolledWindow(child=self.text_view, vexpand=True)
        self.scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        box.append(self.scroller)
        self.set_child(box)
        self.part_buttons[self.part].set_active(True)
        self.show_part(self.part)

    # ------------------------------------------------------------------ parts

    def show_part(self, part_id: str, anchor: str | None = None) -> None:
        """Show one part, scrolled to ``anchor`` (a heading slug) when given."""
        buffer = self.text_view.get_buffer()
        if part_id != self.part or buffer.get_char_count() == 0:
            self.part = part_id
            self._render(read_guide(part_id))
            if not self.part_buttons[part_id].get_active():
                self.part_buttons[part_id].set_active(True)
        mark = buffer.get_mark(f"h:{anchor}") if anchor else None
        if mark is not None:
            self.text_view.scroll_to_mark(mark, 0.0, True, 0.0, 0.0)
        elif anchor is None:
            self.text_view.scroll_to_iter(buffer.get_start_iter(), 0.0, True, 0.0, 0.0)

    def follow(self, href: str) -> GuideLink:
        """Open a link as the reader would by clicking it."""
        target = resolve_link(href, self.part)
        if target.external is not None:
            launcher = getattr(Gtk, "UriLauncher", None)  # GTK 4.10 and later
            if launcher is not None:
                launcher.new(target.external).launch(self, None, None, None)
            else:
                Gtk.show_uri(self, target.external, 0)
        elif target.part is not None:
            self.show_part(target.part, target.anchor)
        return target

    def link_targets(self) -> list[str]:
        """Every link's href in the part shown, in order."""
        return list(self._links.values())

    def _on_part_toggled(self, button: Gtk.ToggleButton, part_id: str) -> None:
        if button.get_active() and part_id != self.part:
            self.show_part(part_id)

    def _render(self, markdown: str) -> None:
        buffer = self.text_view.get_buffer()
        table = buffer.get_tag_table()
        for tag in self._links:
            table.remove(tag)
        self._links = {}
        for name in self._marks:
            buffer.delete_mark_by_name(name)
        self._marks = []
        buffer.set_text("")
        for style, text, links in _guide_blocks(markdown):
            start = buffer.get_end_iter().get_offset()
            name = f"h:{heading_slug(text)}"
            if style.startswith("heading") and buffer.get_mark(name) is None:
                buffer.create_mark(name, buffer.get_end_iter(), True)
                self._marks.append(name)
            buffer.insert_with_tags_by_name(buffer.get_end_iter(), f"{text}\n", style)
            for begin, end, href in links:
                tag = buffer.create_tag(None)
                self._links[tag] = href
                first = buffer.get_iter_at_offset(start + begin)
                last = buffer.get_iter_at_offset(start + end)
                buffer.apply_tag_by_name("link", first, last)
                buffer.apply_tag(tag, first, last)

    def _on_click(self, gesture: Gtk.GestureClick, _presses: int, x: float, y: float) -> None:
        bx, by = self.text_view.window_to_buffer_coords(Gtk.TextWindowType.WIDGET, int(x), int(y))
        found, where = self.text_view.get_iter_at_location(bx, by)
        if not found:
            return
        for tag in where.get_tags():
            href = self._links.get(tag)
            if href is not None:
                self.follow(href)
                return
