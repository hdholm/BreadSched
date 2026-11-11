"""Breadlets on the command line: list them, and run a report breadlet.

``breadsched breadlets`` lists every breadlet (importers, exporters, reports and
Dashboard items) with its options; ``breadsched report BOOK ID`` runs one report or
dashboard breadlet on the book through its book server and prints the document as
text, as JSON (``--json``, the same form the browser receives), or as a printable
HTML page (``--html FILE``).
"""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from ..api.views import breadlets_json, report_document_json
from ..gen.plug import BREADLET_KINDS, DASHBOARD, REPORT, BreadletOptionError, PluginManager
from .common import AddCommand, CommandError, book_api, emit, parse_date, table


def cmd_breadlets(args: argparse.Namespace) -> int:
    manager = PluginManager.instance()
    kinds = (args.kind,) if args.kind else BREADLET_KINDS
    listed = [plugin for kind in kinds for plugin in manager.by_category(kind)]
    rows = [
        [
            plugin.category,
            plugin.id,
            plugin.name,
            " ".join(f"{option.key}=" for option in plugin.options),
            plugin.source,
        ]
        for plugin in listed
    ]
    text = table(rows, ["kind", "id", "name", "options", "source"])
    for problem in manager.problems:
        text += f"\nAdd-on {problem.name} did not load: {problem.reason}"
    emit(breadlets_json(listed, date.today(), manager.problems), args, text)
    return 0


def _given(pairs: list[str]) -> dict[str, str]:
    given: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise CommandError(f"--option takes KEY=VALUE, not {pair!r}")
        given[key.strip()] = value
    return given


def cmd_report(args: argparse.Namespace) -> int:
    manager = PluginManager.instance()
    plugin = manager.get(args.kind, args.breadlet)
    if plugin is None:
        raise CommandError(
            f"no {args.kind} breadlet named {args.breadlet!r}; `breadsched breadlets` lists them"
        )
    today = parse_date(args.as_of) or date.today()
    with book_api(args, "r") as book:
        try:
            document = plugin.produce(book, _given(args.option or []), today)
        except BreadletOptionError as error:
            raise CommandError(f"{error.key}: {error}") from None
    if args.html:
        from ..plugins.export.html_report import render_html

        Path(args.html).write_text(render_html(document), encoding="utf-8")
    emit(report_document_json(document), args, document.text(include_optional=True))
    return 0


def register(add: AddCommand) -> None:
    """Add the breadlet subcommands."""
    listing = add(
        "breadlets",
        "List breadlets: importers, exporters, reports and Dashboard items",
        needs_book=False,
    )
    listing.add_argument("--kind", choices=BREADLET_KINDS, help="list only one kind")
    listing.set_defaults(func=cmd_breadlets)

    report = add("report", "Run a report breadlet on the book and print its document")
    report.add_argument("breadlet", help="the breadlet's id, as `breadsched breadlets` lists it")
    report.add_argument(
        "--option",
        action="append",
        metavar="KEY=VALUE",
        help="one of the breadlet's options (repeat for each); the rest take their defaults",
    )
    report.add_argument(
        "--kind", choices=(REPORT, DASHBOARD), default=REPORT, help="a report or Dashboard item"
    )
    report.add_argument("--as-of", help="the day the defaults count from (YYYY-MM-DD)")
    report.add_argument("--html", metavar="FILE", help="also write a printable HTML page")
    report.set_defaults(func=cmd_report)
