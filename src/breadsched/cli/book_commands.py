"""Commands that create, protect, inspect, open, or export a whole book.

The ``gnucash`` subcommand reads a GnuCash book directly without importing it,
which is the behaviour gnucash-cli provides through the GnuCash Python bindings.
Here it is done by reading the file, so no GnuCash installation is required.
"""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from ..gen.db.sqlite import DbSQLite
from ..gen.lib import (
    Account,
    AccountType,
)
from ..gen.plug import EXPORTER, IMPORTER, PluginManager
from ..gen.sample_book import create_sample_book
from .common import (
    AddCommand,
    CommandError,
    emit,
    open_book,
    parse_date,
    resolve_account,
    table,
)


def cmd_init(args: argparse.Namespace) -> int:
    if Path(args.book).exists():
        raise CommandError(f"{args.book} already exists")
    db = open_book(args.book)
    with db.transaction("Create book") as txn:
        root = Account(name="Root", atype=AccountType.ROOT)
        db.add_account(root, txn)
        for name, atype in (
            ("Assets", AccountType.ASSET),
            ("Liabilities", AccountType.LIABILITY),
            ("Income", AccountType.INCOME),
            ("Expenses", AccountType.EXPENSE),
            ("Equity", AccountType.EQUITY),
        ):
            db.add_account(
                Account(name=name, atype=atype, parent=root.handle, placeholder=True), txn
            )
    db.set_metadata("book_name", Path(args.book).stem)
    db.close()
    emit({"book": args.book}, args, f"Created {args.book} with a top-level chart of accounts")
    return 0


def cmd_sample(args: argparse.Namespace) -> int:
    """Create a clearly labeled, self-contained synthetic household book."""
    target = Path(args.book)
    if target.exists():
        raise CommandError(f"{target} already exists")
    reference = parse_date(args.as_of) or date.today()
    create_sample_book(target, as_of=reference)
    emit(
        {"book": str(target), "synthetic": True, "reference_date": reference},
        args,
        f"Created synthetic sample book {target}; no real financial data",
    )
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    """Create a consistent SQLite backup, including committed WAL contents."""
    db = open_book(args.book, "r")
    try:
        destination = db.backup_to(args.destination, overwrite=args.overwrite)
    finally:
        db.close()
    emit(
        {"book": args.book, "backup": destination},
        args,
        f"Backed up {args.book} to {destination}",
    )
    return 0


def cmd_guide(args: argparse.Namespace) -> int:
    """Print one part of the packaged user guide, or list the parts."""
    from ..user_guide import GUIDE_PARTS, guide_part, read_guide

    if args.list:
        emit(
            [{"part": part.id, "title": part.title} for part in GUIDE_PARTS],
            args,
            "\n".join(f"{part.id:<9} {part.title}" for part in GUIDE_PARTS),
        )
        return 0
    part = guide_part(args.part)
    text = read_guide(part.id)
    emit({"part": part.id, "title": part.title, "markdown": text}, args, text.rstrip("\n"))
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    """Restore a verified backup, preserving an overwritten book first."""
    destination = DbSQLite.restore_backup(args.source, args.destination, overwrite=args.overwrite)
    payload = {"backup": args.source, "book": destination}
    pre_restore = Path(str(destination) + ".pre-restore.bak")
    if pre_restore.exists():
        payload["pre_restore_backup"] = str(pre_restore)
    emit(payload, args, f"Restored {args.source} to {destination}")
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    """Bring a book from an earlier alpha to the current schema, keeping a backup."""
    from ..gen.db.sqlite import SCHEMA_VERSION, stored_schema_version

    path = Path(args.book)
    if not path.exists():
        raise CommandError(f"no book at {path}")
    before = stored_schema_version(path)
    db = open_book(str(path), "w")
    backup = db.migration_backup
    db.close()
    migrated = before is not None and before < SCHEMA_VERSION
    emit(
        {
            "schema_before": before,
            "schema": SCHEMA_VERSION,
            "migrated": migrated,
            "backup": backup if migrated else None,
        },
        args,
        f"Migrated {path} from schema {before} to {SCHEMA_VERSION}; backup at {backup}"
        if migrated
        else f"{path} already uses schema {SCHEMA_VERSION}; nothing to migrate",
    )
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Check SQLite integrity and logical book invariants without modifying the book."""
    report = DbSQLite.verify_path(args.book)
    payload = report.as_dict()
    from ..versioning import version_details, version_summary

    payload.update(version_details(native_schema_version=report.native_schema_version))
    version_line = version_summary()
    if report.ok:
        emit(
            payload,
            args,
            f"{version_line}\nBook verification passed: SQLite and logical checks are clean",
        )
        return 0
    lines = [version_line, "Book verification failed:"]
    lines.extend(f"  sqlite: {problem}" for problem in report.sqlite)
    lines.extend(f"  {issue.code}: {issue.message}" for issue in report.issues)
    emit(payload, args, "\n".join(lines))
    return 1


def cmd_export(args: argparse.Namespace) -> int:
    from ..plugins.export.csv_export import export_transactions

    db = open_book(args.book, "r")
    try:
        account = resolve_account(db, args.account).handle if args.account else None
        count = export_transactions(
            db,
            args.output,
            account=account,
            start=parse_date(args.start),
            end=parse_date(args.end),
        )
        emit({"rows": count, "output": args.output}, args, f"Wrote {count} rows to {args.output}")
        return 0
    finally:
        db.close()


def cmd_gnucash(args: argparse.Namespace) -> int:
    """Read a GnuCash book directly, without importing it."""
    from ..plugins.importer import gnucash_common, gnucash_sqlite

    fmt = gnucash_common.detect_format(args.source)
    if args.action == "info":
        emit({"path": args.source, "format": fmt}, args, f"{args.source}: {fmt}")
        return 0
    if fmt != "sqlite":
        raise CommandError(
            f"{args.source} is a {fmt} book; direct reading needs the SQLite format. "
            "Use 'breadsched import' to bring an XML book in."
        )
    if args.action == "accounts":
        rows = gnucash_sqlite.read_accounts(args.source)
        rows.sort(key=lambda r: r["full_name"])
        emit(
            rows,
            args,
            table(
                [[r["full_name"] or r["name"], r["type"], r["guid"][:8]] for r in rows],
                ["account", "type", "guid"],
            ),
        )
    elif args.action == "transactions":
        rows = gnucash_sqlite.read_transactions(
            args.source,
            account_guid=args.account,
            start=parse_date(args.start),
            end=parse_date(args.end),
        )
        emit(
            rows,
            args,
            table(
                [
                    [r["date"], r["description"][:40], str(len(r["splits"])), r["guid"][:8]]
                    for r in rows
                ],
                ["date", "description", "splits", "guid"],
                right={2},
            ),
        )
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    """Serve the browser interface on this machine."""
    from ..web.transport import serve

    db = open_book(args.book, "r" if args.read_only else "w")
    try:
        try:
            httpd = serve(db, host=args.host, port=args.port, open_browser=args.open)
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        except OSError as exc:
            raise CommandError(f"could not listen on {args.host}:{args.port}: {exc}") from exc
        address = f"http://{args.host}:{httpd.server_port}/#token={httpd.token}"
        print(f"BreadSched is serving {args.book} at {address}")
        print("Press Ctrl+C to stop.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")
        finally:
            httpd.shutdown()
            httpd.server_close()
        return 0
    finally:
        db.close()


def cmd_gui(args: argparse.Namespace) -> int:
    """Hand off to the GUI launcher.

    Imported here rather than at module scope so the CLI keeps working on a
    machine with no GTK installed, which the architecture tests enforce.
    """
    from ..gui.launcher import main as launch

    return launch([args.book] if args.book else [])


def cmd_plugins(args: argparse.Namespace) -> int:
    manager = PluginManager.instance()
    rows = []
    for category in (IMPORTER, EXPORTER):
        for plugin in manager.by_category(category):
            rows.append([plugin.category, plugin.id, plugin.name, plugin.description])
    emit(
        [{"category": r[0], "id": r[1], "name": r[2]} for r in rows],
        args,
        table(rows, ["category", "id", "name", "description"]),
    )
    return 0


def register(add: AddCommand) -> None:
    """Add the book subcommands."""
    init = add("init", "Create a new book with a starter chart of accounts")
    init.set_defaults(func=cmd_init)

    sample = add("sample", "Create a synthetic household book for learning")
    sample.add_argument("--as-of", help="reference date for sample events (YYYY-MM-DD)")
    sample.set_defaults(func=cmd_sample)

    backup = add("backup", "Create a consistent backup of a book")
    backup.add_argument("destination", help="path to write the backup")
    backup.add_argument("--overwrite", action="store_true", help="replace an existing backup")
    backup.set_defaults(func=cmd_backup)

    restore = add("restore", "Restore a verified backup", needs_book=False)
    restore.add_argument("source", help="path to the backup")
    restore.add_argument("destination", help="path to restore the book")
    restore.add_argument(
        "--overwrite",
        action="store_true",
        help="replace an existing book after preserving a pre-restore backup",
    )
    restore.set_defaults(func=cmd_restore)

    migrate = add(
        "migrate",
        "Bring a book from an earlier alpha to the current schema, keeping a verified backup",
    )
    migrate.set_defaults(func=cmd_migrate)

    verify = add("verify", "Verify SQLite integrity and financial object relationships")
    verify.set_defaults(func=cmd_verify)

    guide = add("guide", "Print the user guide packaged with this release", needs_book=False)
    guide.add_argument(
        "part",
        nargs="?",
        default="overview",
        choices=["overview", "desktop", "web", "cli"],
        help="which part to print (default: the overview)",
    )
    guide.add_argument("--list", action="store_true", help="list the parts")
    guide.set_defaults(func=cmd_guide)

    export = add("export", "Export transactions as CSV")
    export.add_argument("output")
    export.add_argument("--account")
    export.add_argument("--start")
    export.add_argument("--end")
    export.set_defaults(func=cmd_export)

    gnucash = add("gnucash", "Read a GnuCash book directly, without importing", needs_book=False)
    gnucash.add_argument("action", choices=["info", "accounts", "transactions"])
    gnucash.add_argument("source", help="path to the GnuCash file")
    gnucash.add_argument("--account", help="filter by GnuCash account GUID")
    gnucash.add_argument("--start")
    gnucash.add_argument("--end")
    gnucash.set_defaults(func=cmd_gnucash)

    web = add("web", "Serve the browser interface on this machine")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--open", action="store_true", help="open a browser window")
    web.add_argument("--read-only", action="store_true")
    web.set_defaults(func=cmd_web)

    gui = add("gui", "Open the graphical interface", needs_book=False)
    gui.add_argument("book", nargs="?", help="book to open on start-up")
    gui.set_defaults(func=cmd_gui)

    plugins = add("plugins", "List registered importers and exporters", needs_book=False)
    plugins.set_defaults(func=cmd_plugins)
