"""Offline reader for BreadSched's packaged Markdown user guide."""

from __future__ import annotations

import re
from importlib import resources

from .gi_setup import Gtk

USER_GUIDE_RESOURCE = resources.files("breadsched").joinpath("USER_GUIDE.md")

_LINK = re.compile(r"\[([^]]+)]\([^)]+\)")
_MARKUP = re.compile(r"(\*\*|__|`)")


def read_user_guide() -> str:
    """Return the same versioned guide that is shipped in the installed package."""
    return USER_GUIDE_RESOURCE.read_text(encoding="utf-8")


def _plain_inline(text: str) -> str:
    """Remove the small amount of Markdown punctuation used by the guide."""
    return _MARKUP.sub("", _LINK.sub(r"\1", text))


def _guide_blocks(markdown: str) -> list[tuple[str, str]]:
    """Turn the guide's conservative Markdown into readable text-view blocks."""
    blocks: list[tuple[str, str]] = []
    paragraph: list[str] = []
    code: list[str] = []
    in_code = False
    list_index: int | None = None

    def flush_paragraph() -> None:
        if paragraph:
            blocks.append(("body", _plain_inline(" ".join(paragraph))))
            paragraph.clear()

    for raw in markdown.splitlines():
        line = raw.rstrip()
        if line.startswith("```"):
            flush_paragraph()
            list_index = None
            if in_code:
                blocks.append(("code", "\n".join(code)))
                code.clear()
            in_code = not in_code
            continue
        if in_code:
            code.append(line)
            continue
        if not line:
            flush_paragraph()
            list_index = None
            continue
        heading = re.match(r"^(#{1,3})\s+(.+)$", line)
        if heading:
            flush_paragraph()
            list_index = None
            blocks.append((f"heading{len(heading.group(1))}", _plain_inline(heading.group(2))))
            continue
        bullet = re.match(r"^\s*[-*]\s+(.+)$", line)
        if bullet:
            flush_paragraph()
            blocks.append(("bullet", f"• {_plain_inline(bullet.group(1))}"))
            list_index = len(blocks) - 1
            continue
        numbered = re.match(r"^\s*(\d+)\.\s+(.+)$", line)
        if numbered:
            flush_paragraph()
            blocks.append(("bullet", f"{numbered.group(1)}. {_plain_inline(numbered.group(2))}"))
            list_index = len(blocks) - 1
            continue
        if list_index is not None:
            style, text = blocks[list_index]
            blocks[list_index] = (style, f"{text} {_plain_inline(line.strip())}")
            continue
        paragraph.append(line.strip())

    flush_paragraph()
    if code:
        blocks.append(("code", "\n".join(code)))
    return blocks


class UserGuideWindow(Gtk.Window):
    """A scrollable, dependency-free presentation of the guide."""

    def __init__(self, application, parent) -> None:
        super().__init__(
            application=application,
            transient_for=parent,
            title="BreadSched User Guide",
        )
        self.set_default_size(860, 700)

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

        for style, text in _guide_blocks(read_user_guide()):
            buffer.insert_with_tags_by_name(buffer.get_end_iter(), f"{text}\n", style)

        scroller = Gtk.ScrolledWindow(child=self.text_view)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.set_child(scroller)
