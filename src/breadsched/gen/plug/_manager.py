"""Plugin registry.

Gramps' approach, trimmed to what this application needs: plugins declare
themselves with metadata, the manager holds the registry, and callers ask for a
capability rather than importing a module directly.  The payoff is that adding a
new file format means dropping in a module and registering it -- the import dialog,
the CLI and the tests all pick it up with no edits.

Registration is by callable, not by scanning ``.gpr.py`` files off disk, because a
registry that executes arbitrary files found in a data directory is a liability for
an application whose whole job is reading other people's financial documents.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

__all__ = ["Plugin", "PluginManager", "IMPORTER", "EXPORTER", "REPORT", "register_builtins"]

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

    def __repr__(self) -> str:
        return f"<Plugin {self.category}:{self.id}>"


class PluginManager:
    """A registry of plugins, queried by category or by file."""

    _instance: PluginManager | None = None

    def __init__(self) -> None:
        self._plugins: dict[str, Plugin] = {}

    @classmethod
    def instance(cls) -> PluginManager:
        if cls._instance is None:
            cls._instance = cls()
            register_builtins(cls._instance)
        return cls._instance

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
    from ...plugins.importer import gnucash_common, gnucash_sqlite, gnucash_xml, qif

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
            description="Accounts and transactions from a GnuCash XML book, "
                        "compressed or plain",
            extensions=[".gnucash", ".xml", ".gnc"],
            sniff=lambda path: gnucash_common.detect_format(path) in ("xml", "xml-gz"),
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
