"""Build the BreadSched website: the pages in ``website/`` and the packaged user guide.

Usage, from the repository root::

    python scripts/build_site.py [OUTPUT]      # default: _site

The hand-written pages in ``website/`` (HTML, CSS, images) are copied as they are;
each ``website/*.md`` becomes a page of the same name; and the user guide's four
parts, the changelog and the roadmap become ``guide/*.html``, with their links
between one another rewritten to the built pages and links to any other repository
file sent to it on GitHub. Standard library only, so the Pages workflow needs no
packages; the Markdown it reads is the subset the guide uses.
"""

from __future__ import annotations

import html
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEBSITE = ROOT / "website"
GUIDE = ROOT / "src" / "breadsched"
REPOSITORY = "https://github.com/hdholm/BreadSched"
ICON = ROOT / "src/breadsched/gui/resources/icons/hicolor/64x64/apps/org.breadsched.BreadSched.png"


@dataclass(frozen=True)
class Page:
    """One built page from a Markdown source."""

    source: Path
    #: The output path, relative to the site root.
    target: str
    title: str
    #: Shown in the guide's side navigation.
    nav: bool = True


GUIDE_PAGES = (
    Page(GUIDE / "USER_GUIDE.md", "guide/overview.html", "Overview"),
    Page(GUIDE / "guide" / "desktop.md", "guide/desktop.html", "Desktop"),
    Page(GUIDE / "guide" / "web.md", "guide/web.html", "Browser"),
    Page(GUIDE / "guide" / "cli.md", "guide/cli.html", "Command line"),
    Page(ROOT / "CHANGELOG.md", "guide/changelog.html", "What's new"),
    Page(ROOT / "ROADMAP.md", "guide/roadmap.html", "Roadmap"),
)

#: Each Markdown source's file name, mapped to the page it became.
BUILT = {page.source.name: page.target for page in GUIDE_PAGES}


def slug(text: str) -> str:
    """The anchor GitHub gives a heading (``breadsched.user_guide.heading_slug``)."""
    sys.path.insert(0, str(ROOT / "src"))
    from breadsched.user_guide import heading_slug

    return heading_slug(text)


# ------------------------------------------------------------------ Markdown

_INLINE = re.compile(
    r"`(?P<code>[^`]+)`"
    r"|!\[(?P<alt>[^\]]*)\]\((?P<src>[^)\s]+)\)"
    r"|\[(?P<label>[^\]]+)\]\((?P<href>[^)\s]+)\)"
    r"|<(?P<auto>https://[^>\s]+)>"
    r"|\*\*(?P<strong>.+?)\*\*"
    r"|(?<![\w*])\*(?P<em>[^*\s][^*]*?)\*(?![\w*])"
    r"|(?<![\w_])_(?P<em2>[^_\s][^_]*?)_(?![\w_])"
)


class Renderer:
    """Markdown to HTML for one page, with its links resolved for ``target``."""

    def __init__(self, source: Path, target: str) -> None:
        self.source = source
        self.target = target
        self.depth = target.count("/")
        #: (level, text, id) of each heading, for the page's navigation.
        self.headings: list[tuple[int, str, str]] = []
        self._ids: dict[str, int] = {}

    # -- links

    def link(self, href: str) -> str:
        """``href`` from the Markdown, as it works on the built page."""
        if re.match(r"^[a-z]+:", href) or href.startswith("#"):
            return href
        path, _, anchor = href.partition("#")
        suffix = f"#{anchor}" if anchor else ""
        name = Path(path).name
        up = "../" * self.depth
        if name in BUILT:
            return f"{up}{BUILT[name]}{suffix}"
        if path.endswith(".html"):
            return href  # already a site page (website/*.md)
        resolved = (self.source.parent / path).resolve()
        try:
            relative = resolved.relative_to(ROOT).as_posix()
        except ValueError:
            return href
        kind = "tree" if resolved.is_dir() else "blob"
        return f"{REPOSITORY}/{kind}/main/{relative}{suffix}"

    # -- inline

    def inline(self, text: str) -> str:
        out: list[str] = []
        last = 0
        for match in _INLINE.finditer(text):
            out.append(html.escape(text[last : match.start()], quote=False))
            last = match.end()
            groups = match.groupdict()
            if groups["code"] is not None:
                out.append(f"<code>{html.escape(groups['code'], quote=False)}</code>")
            elif groups["src"] is not None:
                out.append(
                    f'<img src="{html.escape(self.link(groups["src"]))}" '
                    f'alt="{html.escape(groups["alt"])}">'
                )
            elif groups["href"] is not None:
                out.append(
                    f'<a href="{html.escape(self.link(groups["href"]))}">'
                    f"{self.inline(groups['label'])}</a>"
                )
            elif groups["auto"] is not None:
                url = html.escape(groups["auto"])
                out.append(f'<a href="{url}">{url}</a>')
            elif groups["strong"] is not None:
                out.append(f"<strong>{self.inline(groups['strong'])}</strong>")
            else:
                out.append(f"<em>{self.inline(groups['em'] or groups['em2'])}</em>")
        out.append(html.escape(text[last:], quote=False))
        return "".join(out)

    # -- blocks

    def heading(self, level: int, text: str) -> str:
        base = slug(text)
        count = self._ids.get(base, 0)
        self._ids[base] = count + 1
        anchor = base if count == 0 else f"{base}-{count}"
        self.headings.append((level, text, anchor))
        return f'<h{level} id="{anchor}">{self.inline(text)}</h{level}>'

    def render(self, markdown: str) -> str:
        lines = markdown.splitlines()
        out: list[str] = []
        paragraph: list[str] = []
        index = 0

        def flush() -> None:
            if paragraph:
                out.append(f"<p>{self.inline(' '.join(part.strip() for part in paragraph))}</p>")
                paragraph.clear()

        while index < len(lines):
            line = lines[index]
            stripped = line.strip()
            if not stripped:
                flush()
                index += 1
            elif stripped.startswith("```"):
                flush()
                language = stripped[3:].strip()
                index += 1
                code: list[str] = []
                while index < len(lines) and not lines[index].strip().startswith("```"):
                    code.append(lines[index])
                    index += 1
                index += 1
                cls = f' class="language-{html.escape(language)}"' if language else ""
                out.append(f"<pre><code{cls}>{html.escape(chr(10).join(code))}</code></pre>")
            elif match := re.match(r"^(#{1,6})\s+(.*?)\s*#*$", line):
                flush()
                out.append(self.heading(len(match.group(1)), match.group(2)))
                index += 1
            elif re.match(r"^\s*(---|\*\*\*)\s*$", line):
                flush()
                out.append("<hr>")
                index += 1
            elif (
                stripped.startswith("|")
                and index + 1 < len(lines)
                and re.match(r"^\s*\|?\s*:?-{3,}", lines[index + 1])
            ):
                flush()
                index = self._table(lines, index, out)
            elif stripped.startswith(">"):
                flush()
                quoted: list[str] = []
                while index < len(lines) and lines[index].strip().startswith(">"):
                    quoted.append(lines[index].strip()[1:].lstrip())
                    index += 1
                out.append(f"<blockquote>{self.render(chr(10).join(quoted))}</blockquote>")
            elif re.match(r"^\s*([-*+]|\d+[.)])\s+", line):
                flush()
                index = self._list(lines, index, out)
            elif re.match(r"^\s*<(p|div|img|br|details|summary|/)", line):
                flush()
                # The README-style logo block is the only raw HTML the docs use.
                out.append(line)
                index += 1
            else:
                paragraph.append(line)
                index += 1
        flush()
        return "\n".join(out)

    def _table(self, lines: list[str], index: int, out: list[str]) -> int:
        def cells(row: str) -> list[str]:
            row = row.strip().strip("|")
            return [cell.strip() for cell in re.split(r"(?<!\\)\|", row)]

        header = cells(lines[index])
        index += 2
        body: list[list[str]] = []
        while index < len(lines) and lines[index].strip().startswith("|"):
            body.append(cells(lines[index]))
            index += 1
        out.append("<table><thead><tr>")
        out.append("".join(f"<th>{self.inline(cell)}</th>" for cell in header))
        out.append("</tr></thead><tbody>")
        for row in body:
            out.append("<tr>" + "".join(f"<td>{self.inline(cell)}</td>" for cell in row) + "</tr>")
        out.append("</tbody></table>")
        return index

    def _list(self, lines: list[str], index: int, out: list[str]) -> int:
        """A list and everything nested in it (indented more than its markers)."""
        first = re.match(r"^(\s*)([-*+]|\d+[.)])\s+", lines[index])
        assert first is not None
        indent = len(first.group(1))
        ordered = first.group(2)[0].isdigit()
        tag = "ol" if ordered else "ul"
        items: list[list[str]] = []
        while index < len(lines):
            line = lines[index]
            marker = re.match(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$", line)
            if marker and len(marker.group(1)) == indent:
                items.append([marker.group(3)])
                content = len(marker.group(1)) + len(marker.group(2)) + 1
                index += 1
                continue
            if not line.strip():
                # A blank line ends the list unless the next line continues an item.
                following = lines[index + 1] if index + 1 < len(lines) else ""
                if following.strip() and len(following) - len(following.lstrip()) > indent:
                    items[-1].append("")
                    index += 1
                    continue
                break
            if len(line) - len(line.lstrip()) > indent:
                items[-1].append(line[min(content, len(line) - len(line.lstrip())) :])
                index += 1
                continue
            break
        out.append(f"<{tag}>")
        for item in items:
            text = "\n".join(item)
            inner = self.render(text)
            # A single paragraph item is drawn without its <p>.
            if inner.startswith("<p>") and inner.count("<p>") == 1 and inner.endswith("</p>"):
                inner = inner[3:-4]
            elif inner.startswith("<p>") and "\n" in inner:
                head, rest = inner.split("\n", 1)
                if head.endswith("</p>") and head.count("<p>") == 1:
                    inner = head[3:-4] + "\n" + rest
            out.append(f"<li>{inner}</li>")
        out.append(f"</{tag}>")
        return index


# ------------------------------------------------------------------ pages

HEADER = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — BreadSched</title>
<link rel="icon" href="{up}images/favicon.png">
<link rel="stylesheet" href="{up}style.css">
</head>
<body>
<header class="site"><div class="wrap">
  <a class="brand" href="{up}index.html"><img src="{up}images/favicon.png" alt="">BreadSched</a>
  <nav>
    <a href="{up}index.html">Home</a>
    <a href="{up}money-basics.html"{basics}>Money basics</a>
    <a href="{up}guide/overview.html"{guide}>User guide</a>
    <a href="{up}index.html#download">Download</a>
    <a href="https://github.com/hdholm/BreadSched">Source</a>
  </nav>
</div></header>
"""

FOOTER = """<footer class="site"><div class="wrap">
  BreadSched is free software. <a href="https://github.com/hdholm/BreadSched">Source code</a> ·
  <a href="https://github.com/hdholm/BreadSched/issues">Report a problem</a> ·
  <a href="{up}guide/changelog.html">What's new</a>
</div></footer>
</body>
</html>
"""


def _page(page: Page, *, guide: bool) -> str:
    renderer = Renderer(page.source, page.target)
    body = renderer.render(page.source.read_text(encoding="utf-8"))
    up = "../" * page.target.count("/")
    current = ' aria-current="page"'
    head = HEADER.format(
        title=html.escape(page.title),
        up=up,
        basics="" if guide else current,
        guide=current if guide else "",
    )
    side: list[str] = []
    if guide:
        for other in GUIDE_PAGES:
            mark = current if other.target == page.target else ""
            href = Path(other.target).name
            side.append(f'<a href="{href}"{mark}>{html.escape(other.title)}</a>')
        side.append("<hr>")
    for level, text, anchor in renderer.headings:
        if level == 2:
            side.append(f'<a href="#{anchor}">{renderer.inline(text)}</a>')
    nav = f'<aside><nav aria-label="On this page">{"".join(side)}</nav></aside>'
    return (
        f'{head}<main class="wrap doc">{nav}<article>{body}</article></main>\n'
        f"{FOOTER.format(up=up)}"
    )


def build(output: Path) -> list[Path]:
    """Write the site to ``output`` (replacing it) and return every file written."""
    if output.exists():
        shutil.rmtree(output)
    written: list[Path] = []
    for item in WEBSITE.rglob("*"):
        if item.is_dir() or item.suffix == ".md":
            continue
        destination = output / item.relative_to(WEBSITE)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(item, destination)
        written.append(destination)
    icon = output / "images" / "favicon.png"
    icon.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ICON, icon)
    written.append(icon)
    pages = [
        Page(source, f"{source.stem}.html", _title(source), nav=False)
        for source in sorted(WEBSITE.glob("*.md"))
    ]
    for page in pages:
        written.append(_write(output, page, _page(page, guide=False)))
    for page in GUIDE_PAGES:
        written.append(_write(output, page, _page(page, guide=True)))
    (output / ".nojekyll").write_text("", encoding="utf-8")
    return written


def _title(source: Path) -> str:
    for line in source.read_text(encoding="utf-8").splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return source.stem


def _write(output: Path, page: Page, text: str) -> Path:
    destination = output / page.target
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text, encoding="utf-8")
    return destination


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    output = Path(args[0]) if args else ROOT / "_site"
    written = build(output)
    print(f"Built {len(written)} files in {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
