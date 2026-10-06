"""Command line interface.

Every subcommand is a thin shell over the engine layer, and every one of them
supports ``--json``, so the CLI doubles as the scripting interface: the same code
path that prints a table for a person emits structured output for a cron job.

This module only builds the parser and dispatches. Each area's subcommands, with
their parsers and handlers, live in a ``cli/*_commands.py`` module listed in
``COMMAND_MODULES``; helpers they share live in :mod:`breadsched.cli.common`.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Sequence

from ..gen.db.base import DbError
from ..gen.utils import logs
from . import (
    benefit_commands,
    book_commands,
    categorization_commands,
    import_commands,
    investment_commands,
    ledger_commands,
    plan_commands,
    projection_commands,
)
from .common import CommandError

LOG = logs.get_logger(__name__)

__all__ = ["main", "build_parser"]

#: Command modules in help order; each registers its own subcommands.
COMMAND_MODULES = (
    book_commands,
    import_commands,
    ledger_commands,
    categorization_commands,
    plan_commands,
    benefit_commands,
    projection_commands,
    investment_commands,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="breadsched",
        description="Household ledger, event-driven Plan, and multi-year projection tool.",
    )
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    subparsers = parser.add_subparsers(dest="command")

    def add(name: str, help_text: str, needs_book: bool = True) -> argparse.ArgumentParser:
        sub = subparsers.add_parser(name, help=help_text, description=help_text)
        if needs_book:
            sub.add_argument("book", help="path to the BreadSched book")
        sub.add_argument("--json", action="store_true", help="emit JSON instead of a table")
        sub.add_argument(
            "-v",
            "--verbose",
            action="count",
            default=0,
            help="log progress and warnings to stderr; repeat (-vv) for full detail",
        )
        sub.add_argument(
            "--debug",
            action="store_true",
            help="full detail, equivalent to -vv",
        )
        sub.add_argument(
            "--log-file",
            metavar="PATH",
            help="also write a complete debug log to PATH",
        )
        return sub

    for module in COMMAND_MODULES:
        module.register(add)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if getattr(args, "version", False):
        from ..versioning import version_summary

        print(version_summary())
        return 0
    if not getattr(args, "func", None):
        parser.print_help()
        return 1

    verbosity = 2 if getattr(args, "debug", False) else getattr(args, "verbose", 0)
    logs.configure(verbosity=verbosity, path=getattr(args, "log_file", None))

    try:
        return args.func(args)
    except (CommandError, DbError, FileNotFoundError) as exc:
        print(f"breadsched: {exc}", file=sys.stderr)
        return 2
    except sqlite3.DatabaseError as exc:
        # A corrupt or non-SQLite file reaching a reader is a user-facing problem,
        # not a bug to report as a traceback.
        print(f"breadsched: could not read the database: {exc}", file=sys.stderr)
        LOG.debug("database error", exc_info=True)
        return 2
    except BrokenPipeError:  # pragma: no cover - e.g. piping into head
        return 0
    except KeyboardInterrupt:  # pragma: no cover
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
