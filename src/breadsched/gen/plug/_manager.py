"""The breadlet registry.

Gramps' approach, trimmed to what this application needs: breadlets (plug-ins)
declare themselves with metadata, the manager holds the registry, and callers ask
for a capability rather than importing a module directly. Adding a file format, a
report or a Dashboard item means registering one more breadlet: the interfaces, the
command line and the tests all pick it up with no edits. ``_breadlets`` describes
the report and dashboard kinds and their options.

Registration is by callable, not by scanning ``.gpr.py`` files off disk, because a
registry that executes arbitrary files found in a data directory is a liability for
an application whose whole job is reading other people's financial documents. An
add-on is an installed Python package naming an entry point instead.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ._breadlets import DASHBOARD, BreadletOption, LoadProblem, load_installed, resolve_options

__all__ = [
    "DASHBOARD",
    "EXPORTER",
    "IMPORTER",
    "REPORT",
    "Plugin",
    "PluginManager",
    "register_builtins",
]

IMPORTER = "importer"
EXPORTER = "exporter"
REPORT = "report"


@dataclass
class Plugin:
    """One registered capability."""

    id: str
    name: str
    category: str
    run: Callable[..., Any]
    description: str = ""
    #: File extensions this plugin handles, e.g. ``[".gnucash", ".sqlite"]``.
    extensions: list[str] = field(default_factory=list)
    #: Called with a path; returns True if this plugin can read it.
    sniff: Callable[[str], bool] | None = None
    version: str = "1.0"
    #: A report or dashboard breadlet's settings; ``run(book, options)`` gets them typed.
    options: tuple[BreadletOption, ...] = ()
    #: Where it came from: "built in", or the installed package's entry point.
    source: str = "built in"
    #: A dashboard breadlet shown until the household hides it.
    shown_by_default: bool = False

    def resolve(self, given: Mapping[str, str], today: date) -> dict[str, object]:
        """This breadlet's options from text; see ``resolve_options``."""
        return resolve_options(self.options, given, today)

    def produce(self, book: Any, given: Mapping[str, str], today: date) -> Any:
        """Run a report or dashboard breadlet on ``book`` with the option text given."""
        return self.run(book, self.resolve(given, today), today)

    def describe(self, today: date) -> dict[str, object]:
        """What an interface needs to list this breadlet and build its options form."""
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.category,
            "description": self.description,
            "source": self.source,
            "shown_by_default": self.shown_by_default,
            "options": [option.as_dict(today) for option in self.options],
        }

    def __repr__(self) -> str:
        return f"<Plugin {self.category}:{self.id}>"


class PluginManager:
    """A registry of plugins, queried by category or by file."""

    _instance: PluginManager | None = None

    def __init__(self) -> None:
        self._plugins: dict[str, Plugin] = {}
        #: Installed breadlet packages that failed to load.
        self.problems: list[LoadProblem] = []

    @classmethod
    def instance(cls) -> PluginManager:
        if cls._instance is None:
            manager = cls()
            register_builtins(manager)
            manager.problems = load_installed(manager)
            cls._instance = manager
        return cls._instance

    def all(self) -> list[Plugin]:
        return list(self._plugins.values())

    def register(self, plugin: Plugin) -> Plugin:
        self._plugins[f"{plugin.category}:{plugin.id}"] = plugin
        return plugin

    def get(self, category: str, plugin_id: str) -> Plugin | None:
        return self._plugins.get(f"{category}:{plugin_id}")

    def by_category(self, category: str) -> list[Plugin]:
        return sorted(
            (p for p in self._plugins.values() if p.category == category),
            key=lambda p: p.name,
        )

    def for_file(self, path: str, category: str = IMPORTER) -> Plugin | None:
        """Best plugin for a file: content sniffing first, extension as fallback."""
        candidates = self.by_category(category)
        for plugin in candidates:
            if plugin.sniff is not None:
                try:
                    if plugin.sniff(path):
                        return plugin
                except Exception:  # noqa: BLE001 - a bad sniffer must not block others
                    continue
        lowered = path.lower()
        for plugin in candidates:
            if any(lowered.endswith(ext) for ext in plugin.extensions):
                return plugin
        return None

    def __len__(self) -> int:
        return len(self._plugins)


def register_builtins(manager: PluginManager) -> None:
    """Register the plugins shipped with the application."""
    from ...plugins.export import csv_export
    from ...plugins.importer import gnucash_common, gnucash_sqlite, gnucash_xml, ofx, qif

    manager.register(
        Plugin(
            id="gnucash-sqlite",
            name="GnuCash book (SQLite)",
            category=IMPORTER,
            run=gnucash_sqlite.import_book,
            description="Accounts, transactions and scheduled transactions "
            "from a GnuCash SQLite3 book",
            extensions=[".gnucash", ".sqlite", ".sqlite3", ".db"],
            sniff=lambda path: gnucash_common.detect_format(path) == "sqlite",
        )
    )
    manager.register(
        Plugin(
            id="gnucash-xml",
            name="GnuCash book (XML)",
            category=IMPORTER,
            run=gnucash_xml.import_book,
            description="Accounts and transactions from a GnuCash XML book, compressed or plain",
            extensions=[".gnucash", ".xml", ".gnc"],
            sniff=lambda path: gnucash_common.detect_format(path) in ("xml", "xml-gz"),
        )
    )
    manager.register(
        Plugin(
            id="ofx",
            name="Open Financial Exchange (OFX/QFX)",
            category=IMPORTER,
            run=ofx.import_book,
            description="Bank and credit-card statement transactions from OFX/QFX",
            extensions=[".ofx", ".qfx"],
            sniff=ofx.sniff,
        )
    )
    manager.register(
        Plugin(
            id="qif",
            name="Quicken Interchange Format (QIF)",
            category=IMPORTER,
            run=qif.import_book,
            description="Bank, cash and credit-card accounts and transactions from QIF",
            extensions=[".qif"],
            sniff=qif.sniff,
        )
    )
    manager.register(
        Plugin(
            id="csv-transactions",
            name="Transactions as CSV",
            category=EXPORTER,
            run=csv_export.export_transactions,
            description="One row per split, for a spreadsheet",
            extensions=[".csv"],
        )
    )
    manager.register(
        Plugin(
            id="csv-projection",
            name="Projection as CSV",
            category=EXPORTER,
            run=csv_export.export_projection,
            description="One row per projected month",
            extensions=[".csv"],
        )
    )
    _register_report_breadlets(manager)


def _register_report_breadlets(manager: PluginManager) -> None:
    """The report and dashboard breadlets shipped with the application."""
    from ...plugins.breadlets import register

    register(manager)
