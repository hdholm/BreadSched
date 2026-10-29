"""Commands that bring outside data into a book, or write it back to GnuCash."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from decimal import Decimal

from ..gen.services import (
    HeldImportDecision,
    ImportBook,
    ResolveHeldImports,
)
from ..gen.services.account_inference import AccountSuggestion, ApplyAccountSuggestions
from ..gen.services.csv_import import CsvImportRequest, CsvMapping
from ..gen.services.gnucash_writeback import (
    ApplyWriteback,
)
from ..presentation import (
    reimbursement_notice,
)
from .common import (
    AddCommand,
    CommandError,
    add_server_options,
    book_api,
    emit,
    table,
)


def cmd_import(args: argparse.Namespace) -> int:
    """Through the book server (#358), so it works while another process holds the book."""
    with book_api(args, "w") as book:
        result = book.import_book(
            ImportBook(
                source=args.source,
                format=args.format,
                include_scheduled=not args.no_scheduled,
                include_duplicates=args.include_duplicates,
            )
        ).result
        result.log_path = str(args.log_file) if args.log_file else None
        proposal_count = len(book.reimbursement_proposals())
        suggestions: tuple[AccountSuggestion, ...] = ()
        if not args.no_infer:
            suggestions = book.account_suggestions()
            if args.infer_apply:
                book.apply_account_suggestions(
                    ApplyAccountSuggestions(message="Apply relationships inferred on import")
                )

    emit(
        {
            "accounts": result.accounts,
            "transactions": result.transactions,
            "transactions_new": result.transactions_new,
            "transactions_refreshed": result.transactions_refreshed,
            "transactions_unchanged": result.transactions_unchanged,
            "transactions_removed": result.transactions_removed,
            "transactions_retained": result.transactions_retained,
            "transactions_held": result.transactions_held,
            "transactions_kept": result.transactions_kept,
            "possible_duplicates": result.possible_duplicates,
            "share_splits": result.share_splits,
            "splits": result.splits,
            "splits_new": result.splits_new,
            "splits_refreshed": result.splits_refreshed,
            "splits_unchanged": result.splits_unchanged,
            "splits_removed": result.splits_removed,
            "commodities": result.commodities,
            "prices": result.prices,
            "scheduled": result.scheduled,
            "skipped": result.skipped,
            "skipped_new": result.skipped_new,
            "skipped_repeated": result.skipped_repeated,
            "skipped_resolved": result.skipped_resolved,
            "skipped_by_reason": result.reasons(),
            "problems": [problem.as_dict() for problem in result.problems()],
            "warnings": result.warnings,
            "notices": result.notices,
            "source": result.source,
            "source_format": result.source_format,
            "source_identity": result.source_identity,
            "log_file": result.log_path,
            "reimbursement_proposals": proposal_count,
            "suggestions": [_suggestion_json(s) for s in suggestions],
        },
        args,
        f"Imported into {args.book}\n"
        + result.detail(limit=args.max_warnings)
        + _inference_note(suggestions, args.infer_apply)
        + _reimbursement_note(proposal_count),
    )
    return 0


def _reimbursement_note(count: int) -> str:
    notice = reimbursement_notice(count)
    return f"\n{notice} (breadsched receivables BOOK --proposals)" if notice else ""


def _split_columns(values: list[str] | None) -> tuple[tuple[str, str], ...]:
    """``CATEGORY=AMOUNT`` column pairs from repeated ``--split`` options."""
    pairs = []
    for value in values or ():
        category, separator, amount = value.partition("=")
        if not separator or not category.strip() or not amount.strip():
            raise CommandError(
                f"--split {value!r}: give the category and amount columns as CATEGORY=AMOUNT"
            )
        pairs.append((category.strip(), amount.strip()))
    return tuple(pairs)


def _splits_text(names: dict[str, str], splits) -> str:
    return "; ".join(
        f"{names.get(account, account)} {value.format(parens_negative=True)}"
        for account, value in splits
    )


def cmd_import_csv(args: argparse.Namespace) -> int:
    """Preview or import a CSV statement into one account through a column mapping.

    Through the book server (#358), so it works while another process holds the book.
    """
    with book_api(args, "r" if args.preview else "w") as book:
        names = {record.account.handle: record.full_name for record in book.accounts()}
        request = CsvImportRequest(
            source=args.source,
            account=book.find_account(args.account).handle,
            mapping=CsvMapping(
                date=args.date,
                amount=args.amount,
                debit=args.debit,
                credit=args.credit,
                description=args.description,
                memo=args.memo,
                category=args.category,
                currency=args.currency,
                date_format=args.date_format,
                number_format=args.number_format,
                encoding=args.encoding,
                delimiter=args.delimiter,
                header=not args.no_header,
                invert=args.invert,
                splits=_split_columns(args.split),
            ),
            include_duplicates=args.include_duplicates,
            link_transfers=args.link_transfers,
        )
        if args.preview:
            preview = book.preview_csv(request)
            rows = [
                [
                    row.line,
                    row.when.isoformat() if row.when else "",
                    row.amount.format(parens_negative=True) if row.amount is not None else "",
                    row.description,
                    row.status.replace("_", " "),
                    row.reason or _splits_text(names, row.splits),
                ]
                for row in preview.rows
            ]
            text = "\n".join(
                (
                    f"{preview.encoding}, delimiter {preview.delimiter!r}, "
                    f"{preview.date_format} dates, {preview.number_format} decimals",
                    table(
                        rows,
                        ["line", "date", "amount", "description", "status", "reason"],
                        right={0, 2},
                    ),
                )
            )
            emit(
                {
                    "encoding": preview.encoding,
                    "delimiter": preview.delimiter,
                    "date_format": preview.date_format,
                    "number_format": preview.number_format,
                    "rows": [
                        {
                            "line": row.line,
                            "date": row.when,
                            "amount": row.amount,
                            "description": row.description,
                            "memo": row.memo,
                            "status": row.status,
                            "reason": row.reason,
                            "existing": row.existing,
                            "category": names.get(row.category) if row.category else None,
                            "splits": [
                                {"category": names.get(account, account), "amount": value}
                                for account, value in row.splits
                            ],
                        }
                        for row in preview.rows
                    ],
                },
                args,
                text,
            )
            return 0
        imported = book.import_csv(request)
        result = imported.result
        preview = imported.preview
        summary = {
            "transactions_new": result.transactions_new,
            "transactions_unchanged": result.transactions_unchanged,
            "transactions_linked": result.transactions_linked,
            "possible_duplicates": preview.count("possible_duplicate"),
            "duplicates_included": args.include_duplicates,
            "possible_transfers": preview.count("possible_transfer"),
            "skipped": result.skipped,
            "skipped_by_reason": result.reasons(),
            "reimbursement_proposals": len(book.reimbursement_proposals()),
        }
        emit(
            summary,
            args,
            f"Imported {result.transactions_new} new, {result.transactions_unchanged} already "
            f"imported, {preview.count('possible_duplicate')} possible duplicate(s) "
            f"{'included' if args.include_duplicates else 'held back'}, "
            f"{result.transactions_linked} transfer(s) linked, "
            f"{result.skipped} skipped"
            + _reimbursement_note(int(summary["reimbursement_proposals"])),
        )
        return 0


def cmd_gnucash_writeback(args: argparse.Namespace) -> int:
    """Preview, or write chosen BreadSched changes back to the imported GnuCash book."""
    writing = bool(args.apply or args.all or args.keep_backups is not None)
    with book_api(args, "w" if writing else "r") as book:
        if args.keep_backups is not None:
            kept = book.set_writeback_keep_backups(args.keep_backups)
            if not (args.apply or args.all):
                emit({"keep_backups": kept}, args, f"Keeping {kept} backup(s)")
                return 0
        plan = book.writeback_preview()
        if args.apply or args.all:
            chosen = (
                [change.transaction for change in plan.changes]
                if args.all
                else [book.find_transaction(reference).handle for reference in args.apply]
            )
            outcome = book.apply_writeback(ApplyWriteback(tuple(chosen)))
            emit(
                {
                    "written": [change.transaction for change in outcome.written],
                    "backup": outcome.backup,
                },
                args,
                f"Wrote {len(outcome.written)} transaction(s) to {plan.source}; "
                f"backup {outcome.backup}",
            )
            return 0
        emit(
            {
                "source": plan.source,
                "keep_backups": book.writeback_keep_backups(),
                "changes": [
                    {
                        "transaction": change.transaction,
                        "date": change.post_date,
                        "description": change.description,
                        "kinds": list(change.kinds),
                        "details": list(change.details),
                    }
                    for change in plan.changes
                ],
                "unsupported": [
                    {
                        "transaction": item.transaction,
                        "date": item.post_date,
                        "description": item.description,
                        "reason": item.reason,
                    }
                    for item in plan.unsupported
                ],
            },
            args,
            "\n".join(
                [f"GnuCash book: {plan.source}"]
                + (
                    [
                        f"{change.post_date} {change.transaction[:8]} {change.description} "
                        f"[{', '.join(change.kinds)}]\n    " + "\n    ".join(change.details)
                        for change in plan.changes
                    ]
                    or ["Nothing to write."]
                )
                + [
                    f"not written: {item.post_date} {item.transaction[:8]} "
                    f"{item.description}: {item.reason}"
                    for item in plan.unsupported
                ]
            ),
        )
        return 0


def cmd_import_review(args: argparse.Namespace) -> int:
    """List or decide GnuCash changes held back from reconciled transactions."""
    with book_api(args, "w") as book:
        pending = book.held_imports()
        decisions: dict[str, HeldImportDecision] = {}
        if args.keep_all:
            decisions.update((item.transaction, HeldImportDecision.KEEP_LOCAL) for item in pending)
        if args.use_gnucash_all:
            decisions.update((item.transaction, HeldImportDecision.USE_SOURCE) for item in pending)
        for references, decision in (
            (args.keep, HeldImportDecision.KEEP_LOCAL),
            (args.use_gnucash, HeldImportDecision.USE_SOURCE),
        ):
            for reference in references or ():
                decisions[book.find_transaction(reference).handle] = decision
        if decisions:
            outcome = book.resolve_held_imports(ResolveHeldImports(tuple(decisions.items())))
            emit(
                {"kept": outcome.kept, "applied": outcome.applied, "deferred": outcome.deferred},
                args,
                f"Kept {outcome.kept} BreadSched version(s); "
                f"applied {outcome.applied} GnuCash version(s)",
            )
            return 0
        rows = [
            [
                item.post_date.isoformat(),
                item.transaction[:8],
                item.description,
                "; ".join(item.changes),
                ", ".join(item.blocked_by) or "-",
            ]
            for item in pending
        ]
        emit(
            [
                {
                    "transaction": item.transaction,
                    "date": item.post_date,
                    "description": item.description,
                    "source": item.source,
                    "detected": item.detected,
                    "changes": list(item.changes),
                    "blocked_by": list(item.blocked_by),
                    "can_use_gnucash": item.can_use_source,
                    "deleted": item.deleted,
                }
                for item in pending
            ],
            args,
            table(rows, ["date", "transaction", "description", "GnuCash change", "reopen first"])
            if rows
            else "No GnuCash changes are held for reconciled transactions.",
        )
        return 0


def _inference_note(suggestions: Sequence[AccountSuggestion], applied: bool) -> str:
    """Tell the user what the importer noticed, without acting on it uninvited."""
    if not suggestions:
        return ""
    if applied:
        return (
            f"\n\nApplied {len(suggestions)} inferred relationship(s); "
            "'breadsched account list' shows them."
        )
    lines = [
        "",
        "",
        f"{len(suggestions)} relationship(s) could be inferred from this book:",
    ]
    lines += [
        f"  - {s.account_name}: {s.field} = {s.value} ({s.confidence:.0%})" for s in suggestions[:8]
    ]
    if len(suggestions) > 8:
        lines.append(f"  ... and {len(suggestions) - 8} more")
    lines.append("Run 'breadsched infer BOOK --apply' to accept them.")
    return "\n".join(lines)


def _suggestion_json(suggestion: AccountSuggestion) -> dict:
    return {
        "account": suggestion.account_name,
        "field": suggestion.field,
        "value": suggestion.value,
        "confidence": float(suggestion.confidence),
        "reason": suggestion.reason,
    }


def cmd_infer(args: argparse.Namespace) -> int:
    """Suggest relationships an imported book does not state."""
    with book_api(args, "w" if args.apply else "r") as book:
        suggestions = book.account_suggestions()
        rows = [
            [s.account_name, s.field, s.value, f"{s.confidence:.0%}", s.reason] for s in suggestions
        ]
        headings = ["account", "setting", "value", "confidence", "because"]
        if args.apply:
            applied = book.apply_account_suggestions(
                ApplyAccountSuggestions(min_confidence=Decimal(str(args.min_confidence)))
            )
            emit(
                {"applied": applied.applied, "considered": applied.considered},
                args,
                f"Applied {applied.applied} of {applied.considered} suggestion(s)\n\n"
                + table(rows, headings),
            )
            return 0
        emit(
            [_suggestion_json(s) for s in suggestions],
            args,
            table(rows, headings)
            + ("\n\nNothing is changed until you pass --apply." if suggestions else ""),
        )
        return 0


def register(add: AddCommand) -> None:
    """Add the import subcommands."""
    imp = add("import", "Import a GnuCash book")
    imp.add_argument("source", help="path to the GnuCash file")
    imp.add_argument("--format", help="force an importer instead of detecting one")
    imp.add_argument("--no-scheduled", action="store_true", help="skip scheduled transactions")
    imp.add_argument("--max-warnings", type=int, default=10)
    imp.add_argument(
        "--include-duplicates",
        action="store_true",
        help="QIF, OFX: also import rows matching a transaction already in the account on the "
        "same date for the same amount (held back by default)",
    )
    imp.add_argument(
        "--no-infer",
        action="store_true",
        help="skip looking for loan/asset links and card settings",
    )
    imp.add_argument(
        "--infer-apply",
        action="store_true",
        help="apply confident suggestions rather than only listing them",
    )
    add_server_options(imp)
    imp.set_defaults(func=cmd_import)

    csv_cmd = add(
        "import-csv",
        "Preview or import a CSV statement into one account using a column mapping",
    )
    csv_cmd.add_argument("source", help="path to the CSV file")
    csv_cmd.add_argument("--account", required=True, help="bank, cash, or card account")
    csv_cmd.add_argument("--date", required=True, help="date column name (or 1-based number)")
    csv_cmd.add_argument("--amount", help="signed amount column; negative is money out")
    csv_cmd.add_argument("--debit", help="money-out column, instead of --amount")
    csv_cmd.add_argument("--credit", help="money-in column, instead of --amount")
    csv_cmd.add_argument("--description", help="description column")
    csv_cmd.add_argument("--memo", help="memo column")
    csv_cmd.add_argument(
        "--category",
        help="category column: an existing account's full name or unique name",
    )
    csv_cmd.add_argument(
        "--split",
        action="append",
        metavar="CATEGORY=AMOUNT",
        help="a split's category and amount columns; repeat for each split. Filled splits "
        "must add up to the row's amount (use instead of --category)",
    )
    csv_cmd.add_argument("--currency", help="currency column; rows in another currency are refused")
    csv_cmd.add_argument(
        "--date-format", default="auto", choices=["auto", "iso", "month-first", "day-first"]
    )
    csv_cmd.add_argument("--number-format", default="auto", choices=["auto", "dot", "comma"])
    csv_cmd.add_argument("--encoding", default="auto", help="e.g. utf-8 or cp1252")
    csv_cmd.add_argument("--delimiter", default="auto", help="one character, e.g. ';'")
    csv_cmd.add_argument("--no-header", action="store_true", help="the first row is data")
    csv_cmd.add_argument("--invert", action="store_true", help="money out is shown positive")
    csv_cmd.add_argument(
        "--include-duplicates",
        action="store_true",
        help="also import rows matching an existing transaction on date and amount",
    )
    csv_cmd.add_argument(
        "--link-transfers",
        action="store_true",
        help="complete each offered transfer instead of importing that row as new",
    )
    csv_cmd.add_argument("--preview", action="store_true", help="show rows; write nothing")
    add_server_options(csv_cmd)
    csv_cmd.set_defaults(func=cmd_import_csv)

    held = add(
        "import-review",
        "List or decide GnuCash changes held back from reconciled transactions",
    )
    held.add_argument(
        "--keep",
        action="append",
        metavar="TRANSACTION",
        help="keep the BreadSched version (repeatable; handle or unique prefix)",
    )
    held.add_argument(
        "--use-gnucash",
        action="append",
        metavar="TRANSACTION",
        help="apply the held GnuCash version (repeatable; handle or unique prefix)",
    )
    held.add_argument("--keep-all", action="store_true", help="keep every BreadSched version")
    held.add_argument(
        "--use-gnucash-all", action="store_true", help="apply every held GnuCash version"
    )
    add_server_options(held)
    held.set_defaults(func=cmd_import_review)

    writeback = add(
        "gnucash-writeback",
        "Preview, or write chosen changes back to the imported GnuCash book",
    )
    writeback.add_argument(
        "--apply",
        action="append",
        metavar="TRANSACTION",
        help="write this previewed transaction's changes (repeatable; handle or prefix)",
    )
    writeback.add_argument("--all", action="store_true", help="write every previewed change")
    writeback.add_argument(
        "--keep-backups", type=int, metavar="N", help="how many write-back backups to keep"
    )
    add_server_options(writeback)
    writeback.set_defaults(func=cmd_gnucash_writeback)

    infer = add("infer", "Suggest loan/asset links and credit-card settings")
    infer.add_argument("--apply", action="store_true", help="write the suggestions")
    infer.add_argument("--min-confidence", type=float, default=0.6)
    add_server_options(infer)
    infer.set_defaults(func=cmd_infer)
