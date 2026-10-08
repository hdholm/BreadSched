"""Commands that organize transactions: categorization rules, tags, attachments."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from ..gen.db.sqlite import DbSQLite
from ..gen.services.categorization import (
    AddRule,
    add_rule,
    apply_category_proposals,
    delete_rule,
    list_rules,
    move_rule,
    preview_category_proposals,
)
from ..presentation import (
    service_error_message,
)
from .common import (
    AddCommand,
    CommandError,
    emit,
    find_transaction,
    open_book,
    resolve_account,
    table,
)


def _rule_at(db: DbSQLite, position: int) -> str:
    rules = list_rules(db)
    if not 1 <= position <= len(rules):
        raise CommandError(f"there is no rule {position}; there are {len(rules)}")
    return rules[position - 1].handle


def cmd_rules(args: argparse.Namespace) -> int:
    """List categorization rules, change them, or preview and accept their proposals."""
    writes = args.add_description or args.delete or args.move
    read_only = not (writes or args.accept or args.accept_all)
    db = open_book(args.book, "r" if read_only else "w")

    def name(handle: str) -> str:
        return db.full_name(handle) or handle

    def check(result):
        if result.value is None:
            raise CommandError(service_error_message(result.errors[0]))
        return result.value

    try:
        if args.add_description:
            if not args.category:
                raise CommandError("--category is required when adding a rule")
            rule = check(
                add_rule(
                    db,
                    AddRule(
                        category=resolve_account(db, args.category).handle,
                        description=args.add_description,
                        position=args.position,
                    ),
                )
            )
            emit(
                {"handle": rule.handle, "category": rule.category, "key": rule.key},
                args,
                f"Added rule: {rule.key} -> {name(rule.category)}",
            )
            return 0
        if args.delete:
            rule = check(delete_rule(db, _rule_at(db, args.delete)))
            emit({"deleted": rule.handle}, args, f"Deleted rule {args.delete}")
            return 0
        if args.move:
            if args.to is None:
                raise CommandError("--to is required with --move")
            check(move_rule(db, _rule_at(db, args.move), args.to))
            emit({"moved": args.move, "to": args.to}, args, f"Moved rule {args.move} to {args.to}")
            return 0
        if args.accept or args.accept_all:
            chosen = (
                None
                if args.accept_all
                else tuple(find_transaction(db, reference).handle for reference in args.accept)
            )
            applied = check(apply_category_proposals(db, chosen))
            emit(
                {"assigned": applied.assigned, "unchanged": applied.unchanged},
                args,
                f"Categorized {applied.assigned} transaction(s); "
                f"{applied.unchanged} left unchanged",
            )
            return 0
        if args.preview:
            proposals = check(preview_category_proposals(db))
            emit(
                [
                    {
                        "transaction": item.transaction,
                        "date": item.when,
                        "description": item.description,
                        "amount": item.amount,
                        "category": item.category,
                        "category_name": name(item.category),
                        "rule_position": item.rule_position,
                        "conflicts": [
                            {"rule_position": c.rule_position, "category_name": name(c.category)}
                            for c in item.conflicts
                        ],
                    }
                    for item in proposals
                ],
                args,
                table(
                    [
                        [
                            item.when.isoformat(),
                            item.transaction[:8],
                            item.description,
                            item.amount.format(parens_negative=True),
                            name(item.category),
                            str(item.rule_position),
                            "; ".join(
                                f"rule {c.rule_position}: {name(c.category)}"
                                for c in item.conflicts
                            )
                            or "-",
                        ]
                        for item in proposals
                    ],
                    ["date", "transaction", "description", "amount", "category", "rule", "also"],
                    right={3, 5},
                )
                if proposals
                else "No uncategorized imported transactions match a rule.",
            )
            return 0
        rules = list_rules(db)
        emit(
            [
                {
                    "position": position,
                    "handle": rule.handle,
                    "key": rule.key,
                    "category": rule.category,
                    "category_name": name(rule.category),
                }
                for position, rule in enumerate(rules, start=1)
            ],
            args,
            table(
                [
                    [
                        str(position),
                        f"description {rule.key}",
                        name(rule.category),
                    ]
                    for position, rule in enumerate(rules, start=1)
                ],
                ["rule", "matches", "category"],
                right={0},
            )
            if rules
            else "No categorization rules yet. Add one with --add-description.",
        )
        return 0
    finally:
        db.close()


def cmd_tags(args: argparse.Namespace) -> int:
    """List the book's tags, the transactions carrying one, or set a transaction's tags."""
    from ..gen.services import set_tags, tag_counts, transactions_with_tag

    db = open_book(args.book, "w" if args.set is not None else "r")
    try:
        if args.set is not None:
            if not args.transaction:
                raise CommandError("--transaction is required with --set")
            target = find_transaction(db, args.transaction)
            result = set_tags(db, target.handle, args.set.split(","))
            if result.value is None:
                raise CommandError(service_error_message(result.errors[0]))
            tags = result.value.tags
            emit(
                {"handle": target.handle, "tags": tags},
                args,
                f"Tags: {', '.join(tags)}" if tags else "Removed every tag",
            )
            return 0
        if args.tag:
            found = sorted(
                transactions_with_tag(db, args.tag), key=lambda item: (item.post_date, item.handle)
            )
            emit(
                [
                    {
                        "handle": item.handle,
                        "date": item.post_date,
                        "description": item.description,
                        "tags": item.tags,
                    }
                    for item in found
                ],
                args,
                table(
                    [
                        [item.handle[:8], item.post_date.isoformat(), item.description[:40]]
                        for item in found
                    ],
                    ["id", "date", "description"],
                )
                if found
                else f"No transaction is tagged {args.tag!r}.",
            )
            return 0
        counts = tag_counts(db)
        emit(
            [{"tag": item.tag, "transactions": item.transactions} for item in counts],
            args,
            table(
                [[item.tag, str(item.transactions)] for item in counts],
                ["tag", "transactions"],
                right={1},
            )
            if counts
            else "No tags.",
        )
        return 0
    finally:
        db.close()


def cmd_attachments(args: argparse.Namespace) -> int:
    """List linked documents and which are missing, or link, unlink, and relink them."""
    from ..gen.engine import attachments as attachment_engine
    from ..gen.services import (
        attach_file,
        attach_location,
        attachment_report,
        detach,
        relink,
        set_attachment_folder,
        set_source_link_folder,
    )

    writes = (args.add, args.remove, args.relink, args.folder, args.gnucash_folder)
    db = open_book(args.book, "w" if any(item is not None for item in writes) else "r")
    try:

        def check(result: Any) -> Any:
            if result.value is None:
                raise CommandError(service_error_message(result.errors[0]))
            return result.value

        if args.folder is not None:
            folder = check(set_attachment_folder(db, args.folder))
            emit({"folder": str(folder)}, args, f"Attachment folder: {folder}")
            return 0
        if args.gnucash_folder is not None:
            folder = check(set_source_link_folder(db, args.gnucash_folder))
            emit({"folder": str(folder)}, args, f"GnuCash linked files resolve from: {folder}")
            return 0
        target = find_transaction(db, args.transaction) if args.transaction else None
        if any(item is not None for item in (args.add, args.remove, args.relink)):
            if target is None:
                raise CommandError("--transaction is required to change attachments")
            if args.add is not None:
                if attachment_engine.is_web_address(args.add):
                    changed = check(attach_location(db, target.handle, args.add))
                else:
                    changed = check(
                        attach_file(db, target.handle, Path(args.add), copy=not args.link)
                    )
                text = f"Linked {changed.attachments[-1]}"
            elif args.remove is not None:
                changed = check(detach(db, target.handle, args.remove))
                text = f"Unlinked {args.remove}; the file itself is kept"
            else:
                if not args.to:
                    raise CommandError("--to is required with --relink")
                changed = check(relink(db, target.handle, args.relink, args.to))
                text = f"Relinked {args.relink} to {args.to}"
            emit({"handle": changed.handle, "attachments": changed.attachments}, args, text)
            return 0
        if target is not None:
            found = attachment_engine.statuses(db, target)
            if args.missing:
                found = [item for item in found if item.missing]
            folder = attachment_engine.attachment_folder(db)
        else:
            report = attachment_report(db, missing_only=args.missing)
            found, folder = list(report.attachments), report.folder

        def state(item: attachment_engine.AttachmentStatus) -> str:
            if item.present is None:
                return "web address" if item.kind == "web" else "no folder"
            return "found" if item.present else "missing"

        text = (
            table(
                [
                    [
                        item.transaction[:8],
                        item.location,
                        "GnuCash" if item.owner == "source" else "BreadSched",
                        state(item),
                    ]
                    for item in found
                ],
                ["id", "document", "linked by", "state"],
            )
            if found
            else ("No missing documents." if args.missing else "No linked documents.")
        )
        if folder is not None:
            text += f"\nAttachment folder: {folder}"
        emit(
            {
                "folder": str(folder) if folder is not None else None,
                "attachments": [
                    {
                        "transaction": item.transaction,
                        "location": item.location,
                        "kind": item.kind,
                        "path": str(item.path) if item.path is not None else None,
                        "present": item.present,
                        "owner": item.owner,
                    }
                    for item in found
                ],
            },
            args,
            text,
        )
        return 0
    finally:
        db.close()


def register(add: AddCommand) -> None:
    """Add the rule, tag, and attachment subcommands."""
    rules_cmd = add(
        "rules",
        "List categorization rules, change them, or preview and accept their proposals",
    )
    rules_cmd.add_argument("--add-description", metavar="TEXT", help="match this description")
    rules_cmd.add_argument("--category", help="income or expense category for a new rule")
    rules_cmd.add_argument(
        "--position", type=int, help="1-based position for a new rule (default: last)"
    )
    rules_cmd.add_argument("--delete", type=int, metavar="N", help="delete rule N")
    rules_cmd.add_argument("--move", type=int, metavar="N", help="move rule N (with --to)")
    rules_cmd.add_argument("--to", type=int, metavar="M", help="new position for --move")
    rules_cmd.add_argument(
        "--preview", action="store_true", help="list proposals for uncategorized imports"
    )
    rules_cmd.add_argument(
        "--accept",
        action="append",
        metavar="TRANSACTION",
        help="accept one proposal (repeatable; handle or unique prefix)",
    )
    rules_cmd.add_argument("--accept-all", action="store_true", help="accept every proposal")
    rules_cmd.set_defaults(func=cmd_rules)

    tags_cmd = add("tags", "List tags, the transactions with one, or set a transaction's tags")
    tags_cmd.add_argument("tag", nargs="?", help="list the transactions with this tag")
    tags_cmd.add_argument(
        "--transaction", metavar="TRANSACTION", help="transaction handle or unique prefix"
    )
    tags_cmd.add_argument(
        "--set", metavar="TAGS", help="replace the transaction's tags (comma-separated; '' clears)"
    )
    tags_cmd.set_defaults(func=cmd_tags)

    attachments_cmd = add(
        "attachments", "List linked documents and which are missing, or link and unlink them"
    )
    attachments_cmd.add_argument(
        "--transaction", metavar="TRANSACTION", help="transaction handle or unique prefix"
    )
    attachments_cmd.add_argument(
        "--missing", action="store_true", help="only documents that cannot be found"
    )
    attachments_cmd.add_argument(
        "--add",
        metavar="FILE_OR_URL",
        help="copy a file into the attachment folder and link it, or link a web address",
    )
    attachments_cmd.add_argument(
        "--link", action="store_true", help="with --add: link the file where it is, no copy"
    )
    attachments_cmd.add_argument(
        "--remove", metavar="LOCATION", help="unlink a document (the file is kept)"
    )
    attachments_cmd.add_argument(
        "--relink", metavar="LOCATION", help="point a moved or missing document elsewhere (--to)"
    )
    attachments_cmd.add_argument("--to", metavar="LOCATION", help="new location for --relink")
    attachments_cmd.add_argument(
        "--folder",
        metavar="DIR",
        help="set the attachment folder (relative to the book; '' restores the default)",
    )
    attachments_cmd.add_argument(
        "--gnucash-folder",
        metavar="DIR",
        help="where relative GnuCash linked documents live (GnuCash's 'Path head for linked "
        "files'; '' restores the home folder)",
    )
    attachments_cmd.set_defaults(func=cmd_attachments)
