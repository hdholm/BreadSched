"""The packaged guide: an overview plus desktop, browser, and command-line parts (#181).

Every part is reachable from every interface, and the parts refer to each other,
so a link that names a missing part or heading would strand a reader. These tests
keep every internal link resolvable.
"""

from __future__ import annotations

import re

import pytest

from breadsched.user_guide import (
    GUIDE_PARTS,
    GuideLink,
    guide_part,
    heading_slug,
    read_guide,
    resolve_link,
)

_LINK = re.compile(r"(?<!!)\[([^]]+)]\(([^)]+)\)")
_HEADING = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)


def _without_code(markdown: str) -> str:
    return re.sub(r"```.*?```", "", markdown, flags=re.DOTALL)


def _anchors(part_id: str) -> set[str]:
    return {heading_slug(h) for h in _HEADING.findall(_without_code(read_guide(part_id)))}


def test_every_part_is_packaged_and_titled():
    assert [part.id for part in GUIDE_PARTS] == ["overview", "desktop", "web", "cli"]
    for part in GUIDE_PARTS:
        text = read_guide(part.id)
        assert text.startswith("# "), part.id
        assert len(text) > 1500, part.id
    with pytest.raises(KeyError):
        guide_part("missing")


@pytest.mark.parametrize("part", [part.id for part in GUIDE_PARTS])
def test_every_internal_link_names_an_existing_part_and_heading(part):
    links = _LINK.findall(_without_code(read_guide(part)))
    assert links, part
    for _text, href in links:
        target = resolve_link(href, part)
        if target.external is not None:
            assert target.external.startswith("https://"), href
            continue
        assert target.part is not None
        if target.anchor is not None:
            assert target.anchor in _anchors(target.part), f"{part}: {href}"


def test_each_interface_part_links_to_the_overview_and_the_other_parts():
    for part in ("desktop", "web", "cli"):
        targets = {resolve_link(href, part).part for _t, href in _LINK.findall(read_guide(part))}
        assert {"overview", "desktop", "web", "cli"} - {part} <= targets, part


def test_links_resolve_the_same_way_from_any_file():
    assert resolve_link("guide/web.md#payees", "overview") == GuideLink("web", "payees")
    assert resolve_link("../USER_GUIDE.md", "cli") == GuideLink("overview", None)
    assert resolve_link("#plan", "overview") == GuideLink("overview", "plan")
    assert resolve_link("https://example.org/x", "web") == GuideLink(
        None, None, "https://example.org/x"
    )
    assert heading_slug("Plan and projection") == "plan-and-projection"
    assert heading_slug("Keep GnuCash and BreadSched side by side") == (
        "keep-gnucash-and-breadsched-side-by-side"
    )


def test_the_command_line_prints_and_lists_the_parts(capsys):
    import json

    from breadsched.cli.main import main

    assert main(["guide", "cli"]) == 0
    assert capsys.readouterr().out.startswith("# Command-line guide")
    assert main(["guide"]) == 0
    assert capsys.readouterr().out.startswith("# BreadSched User Guide")
    assert main(["guide", "--list", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [item["part"] for item in listed] == ["overview", "desktop", "web", "cli"]
    assert main(["guide", "web", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["title"] == "Browser"
