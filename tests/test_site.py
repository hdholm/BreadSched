"""The website builds, and every link and anchor on it leads somewhere."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _builder():
    spec = importlib.util.spec_from_file_location("build_site", ROOT / "scripts/build_site.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # A dataclass looks its module up by name while it is being defined.
    sys.modules.setdefault("build_site", module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    output = tmp_path_factory.mktemp("site") / "_site"
    _builder().build(output)
    return output


def test_every_page_is_built(site):
    pages = {path.relative_to(site).as_posix() for path in site.rglob("*.html")}
    assert pages == {
        "index.html",
        "money-basics.html",
        "guide/overview.html",
        "guide/desktop.html",
        "guide/web.html",
        "guide/cli.html",
        "guide/changelog.html",
        "guide/roadmap.html",
    }
    assert (site / "style.css").is_file() and (site / "images/favicon.png").is_file()
    assert (site / ".nojekyll").is_file()


def test_every_internal_link_and_anchor_resolves(site):
    ids = {
        page: set(re.findall(r'id="([^"]+)"', page.read_text())) for page in site.rglob("*.html")
    }
    broken = []
    for page in ids:
        for ref in re.findall(r'(?:href|src)="([^"]+)"', page.read_text()):
            if re.match(r"^[a-z]+:", ref):
                continue
            path, _, anchor = ref.partition("#")
            target = (page.parent / path).resolve() if path else page
            if not target.exists():
                broken.append((page.name, ref))
            elif anchor and target.suffix == ".html" and anchor not in ids[target]:
                broken.append((page.name, ref))
    assert broken == []


def test_the_pages_load_nothing_from_elsewhere(site):
    for page in site.rglob("*.html"):
        text = page.read_text()
        assert "<script" not in text, page
        assert not re.search(r'<link[^>]+href="https?:', text), page


def test_the_guide_links_its_parts_to_one_another_and_other_files_to_github(site):
    overview = (site / "guide/overview.html").read_text()
    assert 'href="../guide/desktop.html#reports-and-breadlets"' in overview
    desktop = (site / "guide/desktop.html").read_text()
    assert 'href="../guide/overview.html#' in desktop and "USER_GUIDE.md" not in desktop
    changelog = (site / "guide/changelog.html").read_text()
    assert 'href="https://github.com/hdholm/BreadSched/blob/main/DESIGN.md"' in changelog


def test_the_five_stages_each_have_a_picture(site):
    index = (site / "index.html").read_text()
    assert index.count('<li><div class="stage">') == 5
    for name in re.findall(r'src="images/([a-z]+\.png)"', index):
        assert (site / "images" / name).is_file(), name


class TestMarkdown:
    def _render(self, text: str, target: str = "guide/overview.html") -> str:
        module = _builder()
        return module.Renderer(ROOT / "src/breadsched/USER_GUIDE.md", target).render(text)

    def test_headings_take_githubs_anchors_and_repeat_with_a_number(self):
        out = self._render("## Plan and projection\n\n## Notes\n\n## Notes\n")
        assert '<h2 id="plan-and-projection">' in out
        assert '<h2 id="notes">' in out and '<h2 id="notes-1">' in out

    def test_lists_nest_and_items_continue(self):
        out = self._render("- one\n  still one\n  - inner\n- two\n\n1. first\n2. second\n")
        assert out.count("<ul>") == 2 and "<li>one still one" in out
        assert "<ol>" in out and "<li>second</li>" in out

    def test_tables_code_and_inline_marks(self):
        out = self._render(
            "| A | B |\n| --- | --- |\n| `x` | **y** |\n\n```bash\na < b\n```\n\n*e* and _f_\n"
        )
        assert "<th>A</th>" in out and "<td><code>x</code></td>" in out
        assert "<td><strong>y</strong></td>" in out
        assert '<pre><code class="language-bash">a &lt; b</code></pre>' in out
        assert "<em>e</em> and <em>f</em>" in out

    def test_links_to_other_repository_files_go_to_github(self):
        out = self._render("[d](../../DESIGN.md#product-boundary) [w](guide/web.md#print)")
        assert (
            'href="https://github.com/hdholm/BreadSched/blob/main/DESIGN.md#product-boundary"'
            in out
        )
        assert 'href="../guide/web.html#print"' in out
