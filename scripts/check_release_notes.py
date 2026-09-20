#!/usr/bin/env python3
"""Validate release-note identity and native-book compatibility disclosures."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from breadsched import __version__  # noqa: E402
from breadsched.gen.db.migrations import MIN_SUPPORTED_SCHEMA_VERSION  # noqa: E402
from breadsched.gen.db.sqlite import SCHEMA_VERSION  # noqa: E402


def validate_release_notes(tag: str, path: Path) -> list[str]:
    """Return human-readable validation failures for one proposed release."""
    expected_tag = f"v{__version__}"
    problems: list[str] = []
    if tag != expected_tag:
        problems.append(f"tag {tag!r} does not match application version {expected_tag!r}")
    if path.name != f"{tag}.md":
        problems.append(f"release notes must be named {tag}.md")
    try:
        notes = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [*problems, f"cannot read release notes: {exc}"]

    required = (
        f"# BreadSched {__version__}",
        f"Application version: `{__version__}`",
        f"Native schema version: `{SCHEMA_VERSION}`",
        (f"Supported native schemas: `{MIN_SUPPORTED_SCHEMA_VERSION}–{SCHEMA_VERSION}`"),
        "## Upgrade and rollback",
        "## Verified artifacts",
    )
    for text in required:
        if text not in notes:
            problems.append(f"release notes are missing {text!r}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("tag")
    parser.add_argument("notes", type=Path)
    args = parser.parse_args(argv)
    problems = validate_release_notes(args.tag, args.notes)
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1
    print(f"release notes valid for {args.tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
