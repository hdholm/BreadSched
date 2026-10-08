"""Keep the product entry point, contributor policy, and roadmap navigable."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
LOCAL_LINK = re.compile(r"\[[^]]+]\(([^)]+)\)")
HEADING = re.compile(r"^#{1,6} (.+)$", re.MULTILINE)
DOCUMENTS = (
    "README.md",
    "ROADMAP.md",
    "CONTRIBUTING.md",
    "AGENTS.md",
    "DESIGN.md",
    "CHANGELOG.md",
    *sorted(str(p.relative_to(ROOT)) for p in (ROOT / "docs" / "design").glob("*.md")),
)


def _slug(heading: str) -> str:
    """GitHub's anchor for a Markdown heading."""
    text = re.sub(r"[^\w\- ]", "", heading.strip().lower())
    return text.replace(" ", "-")


def test_local_document_links_and_anchors_resolve():
    for document in DOCUMENTS:
        source = ROOT / document
        text = source.read_text(encoding="utf-8")
        for target in LOCAL_LINK.findall(text):
            path, _, anchor = unquote(target).partition("#")
            if "://" in path or path.startswith("mailto:"):
                continue
            linked = source.parent / path if path else source
            assert linked.is_file(), f"{document}: broken link {target}"
            if anchor and linked.suffix == ".md":
                slugs = {_slug(h) for h in HEADING.findall(linked.read_text(encoding="utf-8"))}
                assert anchor in slugs, f"{document}: no heading for {target}"


def _backticked(text: str) -> set[str]:
    return set(re.findall(r"`([^`]+\.md)`", text))


def test_the_pull_request_template_asks_for_every_reviewed_document():
    """CONTRIBUTING's Document review list and the template's dispositions agree."""
    policy = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    section = policy.split("## Document review", 1)[1].split("\nA disposition", 1)[0]
    guide = ROOT / "src" / "breadsched" / "guide"
    reviewed = {
        f"src/breadsched/guide/{name}" if (guide / name).is_file() else name
        for name in _backticked(section)
    }
    template = (ROOT / ".github" / "pull_request_template.md").read_text(encoding="utf-8")
    listed = set(re.findall(r"^- ([\w./]+\.md)", template, re.MULTILINE))
    assert reviewed == listed
    for name in listed:
        assert (ROOT / name).is_file(), name
