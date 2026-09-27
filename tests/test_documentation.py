"""Keep the product entry point and roadmap navigable in source distributions."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
LOCAL_LINK = re.compile(r"\[[^]]+]\(([^)]+)\)")


def test_readme_and_roadmap_local_document_links_resolve():
    for document in ("README.md", "ROADMAP.md"):
        source = ROOT / document
        for target in LOCAL_LINK.findall(source.read_text(encoding="utf-8")):
            path = unquote(target.split("#", 1)[0])
            if not path or "://" in path or path.startswith("mailto:"):
                continue
            assert (source.parent / path).is_file(), f"{document}: broken link {target}"
