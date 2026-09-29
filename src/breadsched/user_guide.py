"""The packaged user guide: an overview plus one part per interface (#181).

``USER_GUIDE.md`` is the interface-neutral overview; ``guide/desktop.md``,
``guide/web.md``, and ``guide/cli.md`` give each interface's steps. Every interface
shows all four parts through this module, so relative Markdown links between the
files (which also work when browsing the repository) resolve to a part and heading
the same way everywhere.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import resources
from posixpath import basename

__all__ = [
    "GUIDE_PARTS",
    "GuidePart",
    "GuideLink",
    "guide_part",
    "heading_slug",
    "read_guide",
    "resolve_link",
]


@dataclass(frozen=True, slots=True)
class GuidePart:
    #: Stable identifier used by the CLI, the web route, and the GTK switcher.
    id: str
    title: str
    #: Path of the Markdown file inside the ``breadsched`` package.
    resource: str


GUIDE_PARTS: tuple[GuidePart, ...] = (
    GuidePart("overview", "Overview", "USER_GUIDE.md"),
    GuidePart("desktop", "Desktop", "guide/desktop.md"),
    GuidePart("web", "Browser", "guide/web.md"),
    GuidePart("cli", "Command line", "guide/cli.md"),
)

_BY_ID = {part.id: part for part in GUIDE_PARTS}
_BY_FILE = {basename(part.resource): part for part in GUIDE_PARTS}


def guide_part(part_id: str) -> GuidePart:
    """The part with ``part_id``; raises ``KeyError`` for an unknown one."""
    return _BY_ID[part_id]


def read_guide(part_id: str = "overview") -> str:
    """The Markdown of one part, exactly as shipped in the installed package."""
    resource = resources.files("breadsched").joinpath(guide_part(part_id).resource)
    return resource.read_text(encoding="utf-8")


def heading_slug(heading: str) -> str:
    """The anchor a heading gets, as GitHub forms it: ``"Plan and projection"`` →
    ``"plan-and-projection"``."""
    text = re.sub(r"[`*_]", "", heading).strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


@dataclass(frozen=True, slots=True)
class GuideLink:
    """Where a link points: a guide part and heading, or an external address."""

    part: str | None
    anchor: str | None
    external: str | None = None


def resolve_link(href: str, current: str) -> GuideLink:
    """Resolve a Markdown link found in part ``current``.

    A link to one of the guide's files (by file name, whatever the relative path)
    or to ``#heading`` stays inside the guide; anything else is external.
    """
    path, _, anchor = href.partition("#")
    if not path:
        return GuideLink(current, anchor or None)
    if "://" not in path and not path.startswith("mailto:"):
        target = _BY_FILE.get(basename(path))
        if target is not None:
            return GuideLink(target.id, anchor or None)
    return GuideLink(None, None, href)
