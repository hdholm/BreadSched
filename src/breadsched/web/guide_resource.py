"""HTTP output for the packaged user guide (#181).

The browser's **Guide** page reads any of the four parts through the shared
``breadsched.user_guide`` module, the same Markdown the desktop and command line
show.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..user_guide import GUIDE_PARTS, guide_part, help_target, read_guide

if TYPE_CHECKING:
    from .context import Api
    from .resources import QueryParams


def guide(api: Api, query: QueryParams) -> dict[str, object]:
    """One guide part's Markdown, with the list of parts to switch between.

    ``topic`` names a workflow's contextual help instead of a part: the browser
    part, with ``anchor`` set to that workflow's heading.
    """
    part_id = query.text("part")
    topic = query.text("topic")
    query.finish()
    from .resources import QueryError  # resources imports this module

    anchor = None
    if topic:
        if part_id:
            raise QueryError("query.invalid", ("topic",))
        try:
            part_id, anchor = help_target(topic, "web")
        except KeyError:
            raise QueryError("query.invalid", ("topic",)) from None
    try:
        part = guide_part(part_id or "overview")
    except KeyError:
        raise QueryError("query.invalid", ("part",)) from None
    return {
        "anchor": anchor,
        "part": part.id,
        "title": part.title,
        "markdown": read_guide(part.id),
        "parts": [{"part": item.id, "title": item.title} for item in GUIDE_PARTS],
        "files": {item.resource.rsplit("/", 1)[-1]: item.id for item in GUIDE_PARTS},
    }
