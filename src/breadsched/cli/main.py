"""Command line interface.

Every subcommand is a thin shell over the engine layer, and every one of them
supports ``--json``, so the CLI doubles as the scripting interface: the same code
path that prints a table for a person emits structured output for a cron job.

The ``gnucash`` subcommands read a GnuCash book directly without importing it,
which is the behaviour gnucash-cli provides through the GnuCash Python bindings.
Here it is done by reading the file, so no GnuCash installation is required.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..gen.db.base import DbError
from ..gen.db.sqlite import DbSQLite
from ..gen.engine import (
    activity,
    estimates,
    inference,
    ledger,
    planning,
    projection,
    schedule,
    valuation,
)
from ..gen.engine import (
    dashboard as dashboard_engine,
)
from ..gen.lib import (
    Account,
    AccountType,
    Amount,
    Assumptions,
    Money,
    Payee,
    PeriodType,
    Rate,
    Receivable,
    Recurrence,
    Scenario,
    ScheduledSplit,
    ScheduledTransaction,
    Split,
    Transaction,
)
from ..gen.plug import EXPORTER, IMPORTER, PluginManager
from ..gen.sample_book import create_sample_book
from ..gen.services import (
    DeleteAccount,
    DeleteScenario,
    DeleteSchedule,
    DeleteTransaction,
    DueDecision,
    HeldImportDecision,
    ImportBook,
    ResolveDue,
    ResolveHeldImports,
    ReviewOccurrence,
    ReviewTransaction,
    SaveAccount,
    SaveScenario,
    SaveScenarioAssumptions,
    SaveSchedule,
    SaveTransaction,
    TransactionInput,
    TransactionSplitInput,
    delete_account,
    delete_scenario,
    delete_schedule,
    delete_transaction,
    import_book,
    mark_review_unexpected,
    match_review,
    pending_due_review,
    pending_import_changes,
    reject_review,
    resolve_due,
    resolve_import_changes,
    save_account,
    save_scenario,
    save_scenario_assumptions,
    save_schedule,
    save_transaction,
    transaction_currency,
)
from ..gen.services.categorization import (
    AddRule,
    add_rule,
    apply_category_proposals,
    delete_rule,
    list_rules,
    move_rule,
    preview_category_proposals,
)
from ..gen.services.csv_import import CsvImportRequest, CsvMapping, import_csv, preview_csv_import
from ..gen.services.gnucash_writeback import (
    ApplyWriteback,
    apply_writeback,
    preview_writeback,
    set_writeback_keep_backups,
    writeback_keep_backups,
)
from ..gen.services.payees import (
    SavePayee,
    apply_payee_proposals,
    delete_payee,
    preview_payee_proposals,
    save_payee,
)
from ..gen.services.receivables import (
    RecordWriteOff,
    SaveReceivable,
    accept_reimbursements,
    attach_expense_split,
    attach_reimbursement_split,
    clear_dispute,
    delete_receivable,
    detach_split,
    list_receivables,
    mark_disputed,
    record_write_off,
    reimbursement_proposals,
    save_receivable,
)
from ..gen.utils import logs
from ..presentation import reimbursement_notice, service_error_message

LOG = logs.get_logger(__name__)

__all__ = ["main", "build_parser"]


class CommandError(Exception):
    """A problem the user can fix, reported without a traceback."""


# --------------------------------------------------------------------- output


def _boolean(text: str) -> bool:
    return str(text).strip().lower() in ("1", "true", "yes", "y", "on")


def parse_date(text: str | None) -> date | None:
    if not text:
        return None
    if text == "today":
        return date.today()
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise CommandError(f"{text!r} is not a date; use YYYY-MM-DD") from exc


def emit(payload: Any, args: argparse.Namespace, text: str | None = None) -> None:
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, default=_encode))
    elif text is not None:
        print(text)


def _encode(value: Any) -> Any:
    if isinstance(value, Money):
        return str(value.to_decimal())
    if isinstance(value, Rate):
        return str(value.decimal)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"cannot serialise {type(value).__name__}")


def table(
    rows: Sequence[Sequence[Any]],
    headers: Sequence[str],
    right: frozenset[int] | set[int] | None = None,
) -> str:
    """Render a fixed-width table.  No dependencies, aligned numeric columns."""
    right = right or frozenset()
    body = [[str(cell) for cell in row] for row in rows]
    widths = [len(h) for h in headers]
    for row in body:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    def line(cells: Sequence[str]) -> str:
        return "  ".join(
            cell.rjust(widths[i]) if i in right else cell.ljust(widths[i])
            for i, cell in enumerate(cells)
        ).rstrip()

    out = [line(headers), "  ".join("-" * w for w in widths)]
    out.extend(line(row) for row in body)
    return "\n".join(out)


def open_book(path: str, mode: str = "w") -> DbSQLite:
    if mode == "r" and not Path(path).exists():
        raise CommandError(f"no book at {path}")
    db = DbSQLite()
    db.load(path, mode=mode)
    return db


def resolve_account(db: DbSQLite, reference: str) -> Account:
    account = db.get_account(reference) or db.get_account_by_name(reference)
    if account is None:
        raise CommandError(f"no account matches {reference!r}")
    return account


def resolve_currency(db: DbSQLite, reference: str) -> str:
    """Accept an exact handle or an unambiguous currency mnemonic."""
    direct = db.get_commodity(reference)
    if direct is not None:
        if direct.is_currency:
            return direct.handle
        raise CommandError(f"{reference!r} is not a currency")
    matches = [
        item
        for item in db.iter_commodities()
        if item.is_currency and item.mnemonic.upper() == reference.upper()
    ]
    if len(matches) != 1:
        reason = "ambiguous currency" if matches else "no currency matches"
        raise CommandError(f"{reason} {reference!r}; use an exact currency handle")
    return matches[0].handle


def cmd_rate(args: argparse.Namespace) -> int:
    """Write a dated directional FX quote through the shared valuation contract."""
    quote_date = parse_date(args.date)
    if quote_date is None:
        raise CommandError("rate date is required")
    try:
        value = Money(args.value)
    except (ValueError, TypeError, ZeroDivisionError) as exc:
        raise CommandError("rate must be a positive exact number") from exc
    db = open_book(args.book)
    try:
        source = resolve_currency(db, args.source)
        target = resolve_currency(db, args.target)
        try:
            quote = valuation.save_currency_quote(
                db,
                source_handle=source,
                target_handle=target,
                quote_date=quote_date,
                value=value,
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
    finally:
        db.close()
    exact_rate = f"{quote.value.numerator}/{quote.value.denominator}"
    emit(
        {
            "handle": quote.handle,
            "from": args.source,
            "to": args.target,
            "date": quote.quote_date,
            "rate": exact_rate,
            "source": quote.source,
        },
        args,
        f"Saved {args.source} → {args.target}: {exact_rate} on {quote_date} ({quote.source})",
    )
    return 0


# ------------------------------------------------------------------- commands


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


def cmd_import(args: argparse.Namespace) -> int:
    db = open_book(args.book)
    try:
        imported = import_book(
            db,
            ImportBook(
                source=args.source,
                format=args.format,
                include_scheduled=not args.no_scheduled,
            ),
        )
        if not imported.ok:
            raise CommandError(service_error_message(imported.errors[0]))
        assert imported.value is not None
        result = imported.value.result
        result.log_path = str(args.log_file) if args.log_file else None
        proposal_count = len(reimbursement_proposals(db).value or ())
    finally:
        db.close()
    suggestions = []
    if not args.no_infer:
        db = open_book(args.book)
        try:
            suggestions = inference.infer_all(db).suggestions
            if args.infer_apply:
                inference.apply_suggestions(
                    db,
                    [s for s in suggestions if s.confidence >= 0.6],
                    message="Apply relationships inferred on import",
                )
        finally:
            db.close()

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
            "warnings": result.warnings,
            "source": result.source,
            "source_format": result.source_format,
            "source_identity": result.source_identity,
            "log_file": result.log_path,
            "reimbursement_proposals": proposal_count,
            "suggestions": [
                {
                    "account": s.account_name,
                    "field": s.field,
                    "value": s.value_label,
                    "confidence": s.confidence,
                    "reason": s.reason,
                }
                for s in suggestions
            ],
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


def _due_cell(item: object, as_of: date) -> str:
    """A due date, or a grouped schedule's missed range and count."""
    if isinstance(item, dashboard_engine.MissedGroup):
        return item.missed_label(as_of)
    assert isinstance(item, dashboard_engine.BillRow)
    return item.next_due.isoformat()


def _missed_json(group: dashboard_engine.MissedGroup) -> dict[str, object]:
    return {
        "name": group.name,
        "schedule": group.schedule.handle if group.schedule is not None else None,
        "frequency": group.frequency,
        "missed": group.count,
        "first_due": group.next_due,
        "last_due": group.last_due,
        "total": group.amount,
        "occurrences": [{"date": when, "amount": amount} for when, amount in group.occurrences],
    }


def _due_references(reviews, reference: str) -> list[tuple[str, date]]:
    """Resolve ``SCHEDULE`` or ``SCHEDULE@YYYY-MM-DD`` against what is due."""
    name, _, raw_date = reference.partition("@")
    matches = [
        review
        for review in reviews
        if review.schedule == name or review.schedule.startswith(name) or review.name == name
    ]
    if not matches:
        raise CommandError(f"nothing due matches {name!r}")
    if len({review.schedule for review in matches}) > 1:
        raise CommandError(f"{name!r} matches more than one schedule; use more characters")
    review = matches[0]
    if not raw_date:
        return [(review.schedule, item.when) for item in review.items]
    try:
        when = date.fromisoformat(raw_date)
    except ValueError as exc:
        raise CommandError(f"invalid date in {reference!r}") from exc
    return [(review.schedule, when)]


def cmd_due_review(args: argparse.Namespace) -> int:
    """List or decide due and missed scheduled occurrences, grouped by schedule."""
    if args.post_all and args.skip_all:
        raise CommandError("choose either --post-all or --skip-all, not both")
    as_of = parse_date(args.as_of) or date.today()
    db = open_book(args.book)
    try:
        reviews = pending_due_review(db, as_of)
        decisions: dict[tuple[str, date], DueDecision] = {}
        if args.post_all or args.skip_all:
            chosen = DueDecision.POST if args.post_all else DueDecision.SKIP
            for review in reviews:
                decisions.update(((review.schedule, item.when), chosen) for item in review.items)
        for references, decision in (
            (args.post, DueDecision.POST),
            (args.skip, DueDecision.SKIP),
        ):
            for reference in references or ():
                for key in _due_references(reviews, reference):
                    decisions[key] = decision
        if decisions:
            resolved = resolve_due(
                db,
                ResolveDue(
                    tuple((handle, when, choice) for (handle, when), choice in decisions.items()),
                    as_of=as_of,
                ),
            )
            if not resolved.ok:
                raise CommandError(service_error_message(resolved.errors[0]))
            assert resolved.value is not None
            outcome = resolved.value
            emit(
                {"posted": outcome.posted, "skipped": outcome.skipped},
                args,
                f"Posted {outcome.posted}; marked {outcome.skipped} as done",
            )
            return 0
        rows = [
            [
                review.name[:32],
                review.schedule[:8],
                str(len(review.items)),
                f"{review.items[0].when.isoformat()} to {review.items[-1].when.isoformat()}",
                review.frequency,
                review.total.format(),
            ]
            for review in reviews
        ]
        emit(
            [
                {
                    "schedule": review.schedule,
                    "name": review.name,
                    "frequency": review.frequency,
                    "total": review.total,
                    "items": [
                        {"date": item.when, "amount": item.amount, "overdue": item.overdue}
                        for item in review.items
                    ],
                }
                for review in reviews
            ],
            args,
            table(rows, ["schedule", "id", "due", "dates", "frequency", "total"], right={2, 5})
            if rows
            else "Nothing is due.",
        )
        return 0
    finally:
        db.close()


def cmd_import_csv(args: argparse.Namespace) -> int:
    """Preview or import a CSV statement into one account through a column mapping."""
    db = open_book(args.book, "r" if args.preview else "w")
    try:
        request = CsvImportRequest(
            source=args.source,
            account=resolve_account(db, args.account).handle,
            mapping=CsvMapping(
                date=args.date,
                amount=args.amount,
                debit=args.debit,
                credit=args.credit,
                description=args.description,
                memo=args.memo,
                category=args.category,
                payee=args.payee,
                currency=args.currency,
                date_format=args.date_format,
                number_format=args.number_format,
                encoding=args.encoding,
                delimiter=args.delimiter,
                header=not args.no_header,
                invert=args.invert,
            ),
            include_duplicates=args.include_duplicates,
            link_transfers=args.link_transfers,
        )
        if args.preview:
            previewed = preview_csv_import(db, request)
            if previewed.value is None:
                raise CommandError(service_error_message(previewed.errors[0]))
            preview = previewed.value
            rows = [
                [
                    row.line,
                    row.when.isoformat() if row.when else "",
                    row.amount.format(parens_negative=True) if row.amount is not None else "",
                    row.description,
                    row.status.replace("_", " "),
                    row.reason or row.note,
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
                            "category": db.full_name(row.category) if row.category else None,
                            "payee": row.payee,
                            "note": row.note,
                        }
                        for row in preview.rows
                    ],
                },
                args,
                text,
            )
            return 0
        imported = import_csv(db, request)
        if imported.value is None:
            raise CommandError(service_error_message(imported.errors[0]))
        result = imported.value.result
        preview = imported.value.preview
        summary = {
            "transactions_new": result.transactions_new,
            "transactions_unchanged": result.transactions_unchanged,
            "transactions_linked": result.transactions_linked,
            "possible_duplicates": preview.count("possible_duplicate"),
            "duplicates_included": args.include_duplicates,
            "possible_transfers": preview.count("possible_transfer"),
            "skipped": result.skipped,
            "skipped_by_reason": result.reasons(),
            "reimbursement_proposals": len(reimbursement_proposals(db).value or ()),
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
    finally:
        db.close()


def _find_payee(db: DbSQLite, reference: str) -> Payee:
    payee = db.get_payee(reference)
    if payee is not None:
        return payee
    matches = [item for item in db.iter_payees() if item.name.casefold() == reference.casefold()]
    if not matches:
        raise CommandError(f"no payee matches {reference!r}")
    return matches[0]


def _find_receivable(db: DbSQLite, reference: str) -> Receivable:
    """Locate a receivable by handle, or by a unique prefix of one."""
    exact = db.get_receivable(reference)
    if exact is not None:
        return exact
    matches = [item for item in db.iter_receivables() if item.handle.startswith(reference)]
    if not matches:
        raise CommandError(f"no receivable matches {reference!r}")
    if len(matches) > 1:
        raise CommandError(f"{reference!r} matches {len(matches)} receivables; use more characters")
    return matches[0]


def _rule_at(db: DbSQLite, position: int) -> str:
    rules = list_rules(db)
    if not 1 <= position <= len(rules):
        raise CommandError(f"there is no rule {position}; there are {len(rules)}")
    return rules[position - 1].handle


def cmd_rules(args: argparse.Namespace) -> int:
    """List categorization rules, change them, or preview and accept their proposals."""
    writes = args.add_payee or args.add_description or args.delete or args.move
    read_only = not (writes or args.accept or args.accept_all)
    db = open_book(args.book, "r" if read_only else "w")

    def name(handle: str) -> str:
        return db.full_name(handle) or handle

    def check(result):
        if result.value is None:
            raise CommandError(service_error_message(result.errors[0]))
        return result.value

    try:
        if args.add_payee or args.add_description:
            if not args.category:
                raise CommandError("--category is required when adding a rule")
            payee = _find_payee(db, args.add_payee).handle if args.add_payee else None
            rule = check(
                add_rule(
                    db,
                    AddRule(
                        category=resolve_account(db, args.category).handle,
                        payee=payee,
                        description=args.add_description,
                        position=args.position,
                    ),
                )
            )
            emit(
                {"handle": rule.handle, "category": rule.category, "key": rule.key},
                args,
                f"Added rule: {rule.key or args.add_payee} -> {name(rule.category)}",
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
                else tuple(_find_transaction(db, reference).handle for reference in args.accept)
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
        payee_names = {payee.handle: payee.name for payee in db.iter_payees()}
        rules = list_rules(db)
        emit(
            [
                {
                    "position": position,
                    "handle": rule.handle,
                    "payee": rule.payee,
                    "payee_name": payee_names.get(rule.payee or ""),
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
                        f"payee {payee_names.get(rule.payee or '', rule.payee)}"
                        if rule.payee
                        else f"description {rule.key}",
                        name(rule.category),
                    ]
                    for position, rule in enumerate(rules, start=1)
                ],
                ["rule", "matches", "category"],
                right={0},
            )
            if rules
            else "No categorization rules yet. Add one with --add-description or --add-payee.",
        )
        return 0
    finally:
        db.close()


def cmd_payees(args: argparse.Namespace) -> int:
    """List payees, add or delete one, or preview and accept payee proposals."""
    read_only = not (args.add or args.delete or args.accept or args.accept_all)
    db = open_book(args.book, "r" if read_only else "w")
    try:
        if args.add:
            saved = save_payee(db, SavePayee(args.add, tuple(args.match or ())))
            if saved.value is None:
                raise CommandError(service_error_message(saved.errors[0]))
            payee = saved.value
            emit(
                {"handle": payee.handle, "name": payee.name, "match_keys": payee.match_keys},
                args,
                f"Saved payee {payee.name} matching {', '.join(payee.match_keys) or 'nothing'}",
            )
            return 0
        if args.delete:
            payee = _find_payee(db, args.delete)
            deleted = delete_payee(db, payee.handle)
            if deleted.value is None:
                raise CommandError(service_error_message(deleted.errors[0]))
            emit(
                {"deleted": payee.handle, "cleared": deleted.value},
                args,
                f"Deleted payee {payee.name}; cleared it from {deleted.value} transaction(s)",
            )
            return 0
        if args.accept or args.accept_all:
            chosen = (
                None
                if args.accept_all
                else tuple(_find_transaction(db, reference).handle for reference in args.accept)
            )
            applied = apply_payee_proposals(db, chosen)
            if applied.value is None:
                raise CommandError(service_error_message(applied.errors[0]))
            emit(
                {"assigned": applied.value.assigned, "unchanged": applied.value.unchanged},
                args,
                f"Assigned {applied.value.assigned} payee(s); "
                f"{applied.value.unchanged} left unchanged",
            )
            return 0
        if args.preview:
            proposals = preview_payee_proposals(db).value or ()
            emit(
                [
                    {
                        "transaction": item.transaction,
                        "date": item.when,
                        "description": item.description,
                        "payee": item.payee,
                        "payee_name": item.payee_name,
                        "key": item.key,
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
                            item.payee_name,
                            item.key,
                        ]
                        for item in proposals
                    ],
                    ["date", "transaction", "description", "proposed payee", "matched key"],
                )
                if proposals
                else "No transactions without a payee match a payee's description keys.",
            )
            return 0
        counts: dict[str, int] = {}
        for transaction in db.iter_transactions():
            if transaction.payee is not None:
                counts[transaction.payee] = counts.get(transaction.payee, 0) + 1
        payees = list(db.iter_payees())
        emit(
            [
                {
                    "handle": payee.handle,
                    "name": payee.name,
                    "match_keys": payee.match_keys,
                    "transactions": counts.get(payee.handle, 0),
                }
                for payee in payees
            ],
            args,
            table(
                [
                    [payee.name, ", ".join(payee.match_keys), counts.get(payee.handle, 0)]
                    for payee in payees
                ],
                ["payee", "matches", "transactions"],
                right={2},
            )
            if payees
            else "No payees yet. Add one with --add NAME --match DESCRIPTION.",
        )
        return 0
    finally:
        db.close()


def _receivable_split(transaction: Transaction, index: int) -> Split:
    if not 1 <= index <= len(transaction.splits):
        raise CommandError(f"transaction has {len(transaction.splits)} split(s); no split {index}")
    return transaction.splits[index - 1]


def cmd_receivables(args: argparse.Namespace) -> int:
    """List reimbursable expenses, add or resolve one, or link ledger splits."""
    read_only = not (
        args.add
        or args.attach_expense
        or args.attach_reimbursement
        or args.detach
        or args.dispute
        or args.clear_dispute
        or args.write_off
        or args.delete
        or args.accept_proposals
    )
    db = open_book(args.book, "r" if read_only else "w")
    try:
        if args.proposals or args.accept_proposals:
            proposals = reimbursement_proposals(db).value or ()
            if args.accept_proposals:
                accepted = accept_reimbursements(
                    db, tuple((p.receivable, p.transaction, p.split) for p in proposals)
                ).value
                assert accepted is not None
                emit(
                    {"linked": accepted.linked, "unchanged": accepted.unchanged},
                    args,
                    f"Linked {accepted.linked} reimbursement(s); "
                    f"{accepted.unchanged} left unchanged",
                )
                return 0
            emit(
                [
                    {
                        "receivable": item.receivable,
                        "payer": item.payer,
                        "transaction": item.transaction,
                        "split": item.split,
                        "date": item.when,
                        "description": item.description,
                        "amount": item.amount.format(),
                        "remaining_after": item.remaining_after.format(),
                        "reason": item.reason,
                    }
                    for item in proposals
                ],
                args,
                table(
                    [
                        [
                            item.when.isoformat(),
                            item.description,
                            item.payer,
                            item.amount.format(),
                            item.remaining_after.format(parens_negative=True),
                            item.reason,
                        ]
                        for item in proposals
                    ],
                    ["date", "description", "payer", "amount", "remaining after", "why"],
                    right={3, 4},
                )
                if proposals
                else "No unlinked credits clearly reimburse an open receivable.",
            )
            return 0
        if args.add:
            if not args.incurred:
                raise CommandError("--incurred DATE is required with --add")
            saved = save_receivable(
                db,
                SaveReceivable(
                    incurred_date=date.fromisoformat(args.incurred),
                    payer=args.add,
                    description=args.description or "",
                    expected_amount=Money(args.expected) if args.expected else None,
                    expected_cash_date=(
                        date.fromisoformat(args.expected_cash_date)
                        if args.expected_cash_date
                        else None
                    ),
                ),
            )
            if saved.value is None:
                raise CommandError(service_error_message(saved.errors[0]))
            receivable = saved.value
            emit(
                {"handle": receivable.handle, "payer": receivable.payer},
                args,
                f"Saved receivable for {receivable.payer}",
            )
            return 0
        if args.attach_expense or args.attach_reimbursement:
            receivable = _find_receivable(db, args.attach_expense or args.attach_reimbursement)
            if not args.transaction or not args.split_index:
                raise CommandError("--transaction and --split-index are required")
            transaction = _find_transaction(db, args.transaction)
            split = _receivable_split(transaction, args.split_index)
            linker = attach_expense_split if args.attach_expense else attach_reimbursement_split
            result = linker(db, receivable.handle, transaction.handle, split.handle)
            if result.value is None:
                raise CommandError(service_error_message(result.errors[0]))
            emit(
                {"handle": receivable.handle},
                args,
                f"Linked {'expense' if args.attach_expense else 'reimbursement'} split "
                f"to receivable for {receivable.payer}",
            )
            return 0
        if args.detach:
            receivable = _find_receivable(db, args.detach)
            if not args.transaction or not args.split_index:
                raise CommandError("--transaction and --split-index are required")
            transaction = _find_transaction(db, args.transaction)
            split = _receivable_split(transaction, args.split_index)
            result = detach_split(db, receivable.handle, transaction.handle, split.handle)
            if result.value is None:
                raise CommandError(service_error_message(result.errors[0]))
            emit({"handle": receivable.handle}, args, "Unlinked split from receivable")
            return 0
        if args.dispute:
            receivable = _find_receivable(db, args.dispute)
            if not args.on:
                raise CommandError("--on DATE is required with --dispute")
            result = mark_disputed(
                db, receivable.handle, date.fromisoformat(args.on), args.note or ""
            )
            if result.value is None:
                raise CommandError(service_error_message(result.errors[0]))
            emit({"handle": receivable.handle}, args, f"Disputed receivable for {receivable.payer}")
            return 0
        if args.clear_dispute:
            receivable = _find_receivable(db, args.clear_dispute)
            result = clear_dispute(db, receivable.handle)
            if result.value is None:
                raise CommandError(service_error_message(result.errors[0]))
            emit({"handle": receivable.handle}, args, "Cleared the dispute")
            return 0
        if args.write_off:
            receivable = _find_receivable(db, args.write_off)
            if not args.amount or not args.on:
                raise CommandError("--amount and --on DATE are required with --write-off")
            result = record_write_off(
                db,
                RecordWriteOff(
                    receivable=receivable.handle,
                    amount=Money(args.amount),
                    written_off_on=date.fromisoformat(args.on),
                    reason=args.reason or "",
                ),
            )
            if result.value is None:
                raise CommandError(service_error_message(result.errors[0]))
            emit(
                {"handle": receivable.handle},
                args,
                f"Wrote off {Money(args.amount).format()} for {receivable.payer}",
            )
            return 0
        if args.delete:
            receivable = _find_receivable(db, args.delete)
            deleted = delete_receivable(db, receivable.handle)
            if deleted.value is None:
                raise CommandError(service_error_message(deleted.errors[0]))
            emit({"deleted": deleted.value}, args, f"Deleted receivable for {receivable.payer}")
            return 0
        summaries = list_receivables(db).value or ()
        emit(
            [
                {
                    "handle": item.receivable.handle,
                    "payer": item.receivable.payer,
                    "description": item.receivable.description,
                    "incurred_date": item.receivable.incurred_date,
                    "expense_total": item.expense_total.format(),
                    "reimbursed": item.reimbursed.format(),
                    "written_off": item.written_off.format(),
                    "remaining": item.remaining.format(),
                    "age_days": item.age_days,
                    "status": item.status.value,
                }
                for item in summaries
            ],
            args,
            table(
                [
                    [
                        item.receivable.incurred_date.isoformat(),
                        item.receivable.payer,
                        item.receivable.description,
                        item.remaining.format(parens_negative=True),
                        item.status.label,
                        item.age_days,
                    ]
                    for item in summaries
                ],
                ["incurred", "payer", "description", "remaining", "status", "age (days)"],
                right={5},
            )
            if summaries
            else "No receivables yet. Add one with --add PAYER --incurred DATE.",
        )
        return 0
    finally:
        db.close()


def cmd_gnucash_writeback(args: argparse.Namespace) -> int:
    """Preview, or write chosen BreadSched changes back to the imported GnuCash book."""
    writing = bool(args.apply or args.all or args.keep_backups is not None)
    db = open_book(args.book, "w" if writing else "r")
    try:
        if args.keep_backups is not None:
            kept = set_writeback_keep_backups(db, args.keep_backups)
            if kept.value is None:
                raise CommandError(service_error_message(kept.errors[0]))
            if not (args.apply or args.all):
                emit({"keep_backups": kept.value}, args, f"Keeping {kept.value} backup(s)")
                return 0
        previewed = preview_writeback(db)
        if previewed.value is None:
            raise CommandError(service_error_message(previewed.errors[0]))
        plan = previewed.value
        if args.apply or args.all:
            chosen = (
                [change.transaction for change in plan.changes]
                if args.all
                else [_find_transaction(db, reference).handle for reference in args.apply]
            )
            applied = apply_writeback(db, ApplyWriteback(tuple(chosen)))
            if applied.value is None:
                raise CommandError(service_error_message(applied.errors[0]))
            outcome = applied.value
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
                "keep_backups": writeback_keep_backups(db),
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
    finally:
        db.close()


def cmd_import_review(args: argparse.Namespace) -> int:
    """List or decide GnuCash changes held back from reconciled transactions."""
    db = open_book(args.book)
    try:
        pending = pending_import_changes(db)
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
                decisions[_find_transaction(db, reference).handle] = decision
        if decisions:
            resolved = resolve_import_changes(db, ResolveHeldImports(tuple(decisions.items())))
            if not resolved.ok:
                raise CommandError(service_error_message(resolved.errors[0]))
            assert resolved.value is not None
            outcome = resolved.value
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
                }
                for item in pending
            ],
            args,
            table(rows, ["date", "transaction", "description", "GnuCash change", "reopen first"])
            if rows
            else "No GnuCash changes are held for reconciled transactions.",
        )
        return 0
    finally:
        db.close()


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


def cmd_restore(args: argparse.Namespace) -> int:
    """Restore a verified backup, preserving an overwritten book first."""
    destination = DbSQLite.restore_backup(args.source, args.destination, overwrite=args.overwrite)
    payload = {"backup": args.source, "book": destination}
    pre_restore = Path(str(destination) + ".pre-restore.bak")
    if pre_restore.exists():
        payload["pre_restore_backup"] = str(pre_restore)
    emit(payload, args, f"Restored {args.source} to {destination}")
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


def cmd_accounts(args: argparse.Namespace) -> int:
    db = open_book(args.book, "r")
    try:
        as_of = parse_date(args.as_of)
        payload = []
        rows = []

        def walk(handle: str | None, depth: int) -> None:
            for account in db.child_accounts(handle):
                if account.hidden and not args.all:
                    continue
                result = valuation.aggregate_value(
                    db, accounts=[account, *db.descendants(account.handle)], as_of=as_of
                )
                valued = valuation.account_value(db, account, as_of=as_of)
                total = result.amount.value if result.amount is not None else None
                try:
                    book_balance = ledger.balance_recursive(db, account.handle, as_of=as_of)
                except TypeError as exc:
                    if "cannot combine unlike commodities" not in str(exc):
                        raise
                    book_balance = None
                payload.append(
                    {
                        "handle": account.handle,
                        "name": db.full_name(account),
                        "type": account.atype.value,
                        "code": account.code,
                        "description": account.description,
                        "notes": account.notes,
                        "source_notes": account.source_notes,
                        "placeholder": account.placeholder,
                        "hidden": account.hidden,
                        "commodity": account.commodity,
                        "commodity_scu": account.commodity_scu,
                        "source_guid": account.source_guid,
                        "source_type": account.source_type or None,
                        "source_fields": [field.serialize() for field in account.source_fields],
                        "balance": total,
                        "missing_quotes": list(result.missing_quotes),
                        "quote_date": valued.price_date,
                        "quote_source": valued.price_source,
                        "quote_age_days": valued.quote_age_days,
                        "conversion_path": valued.conversion_path,
                        "quote_evidence": valuation.quote_evidence(db, valued),
                        "book_balance": book_balance,
                    }
                )
                label = ("  " * depth) + account.name
                quote_evidence = valuation.quote_evidence(db, valued)
                rows.append(
                    [
                        label,
                        account.atype.value,
                        total.format(parens_negative=True)
                        if total is not None
                        else "Missing reporting-currency quote",
                        quote_evidence,
                    ]
                )
                walk(account.handle, depth + 1)

        root = db.root_account()
        walk(root.handle if root else None, 0)
        emit(payload, args, table(rows, ["account", "type", "balance", "quote"], right={2}))
        return 0
    finally:
        db.close()


def cmd_register(args: argparse.Namespace) -> int:
    db = open_book(args.book, "r")
    try:
        account = resolve_account(db, args.account)
        rows = ledger.register(
            db, account.handle, start=parse_date(args.start), end=parse_date(args.end)
        )
        if args.limit:
            rows = rows[-args.limit :]
        payload = [
            {
                "handle": row.transaction.handle,
                "date": row.post_date,
                "description": row.description,
                "transfer": row.transfer_label(db),
                "amount": row.amount,
                "balance": row.running,
            }
            for row in rows
        ]
        text = table(
            [
                [
                    row.transaction.handle[:8],
                    row.post_date.isoformat(),
                    row.description[:40],
                    row.transfer_label(db)[:30],
                    row.amount.format(parens_negative=True),
                    row.running.format(parens_negative=True),
                ]
                for row in rows
            ],
            ["id", "date", "description", "transfer", "amount", "balance"],
            right={4, 5},
        )
        print(f"{db.full_name(account)}  ({account.atype.value})") if not args.json else None
        emit(payload, args, text)
        return 0
    finally:
        db.close()


def cmd_balance(args: argparse.Namespace) -> int:
    db = open_book(args.book, "r")
    try:
        as_of = parse_date(args.as_of)
        if args.account:
            account = resolve_account(db, args.account)
            amount = (
                ledger.balance_recursive(db, account.handle, as_of=as_of)
                if args.recursive
                else ledger.balance(db, account.handle, as_of=as_of)
            )
            emit(
                {"account": db.full_name(account), "balance": amount},
                args,
                f"{db.full_name(account)}: {amount.format('', parens_negative=True)}",
            )
        else:
            net_worth = valuation.aggregate_value(db, as_of=as_of, net_worth=True)
            cash = valuation.aggregate_value(
                db,
                accounts=[
                    a for a in db.iter_accounts() if a.atype.is_cash_like and not a.placeholder
                ],
                as_of=as_of,
            )
            summary = {
                "cash": cash.amount.value if cash.amount is not None else None,
                "net_worth": net_worth.amount.value if net_worth.amount is not None else None,
            }
            payload = {
                **summary,
                "cash_missing_quotes": list(cash.missing_quotes),
                "net_worth_missing_quotes": list(net_worth.missing_quotes),
                "net_worth_incompatible_accounts": list(net_worth.incompatible_accounts),
            }
            emit(
                payload,
                args,
                table(
                    [
                        [
                            k.replace("_", " "),
                            v.format(parens_negative=True)
                            if v is not None
                            else "Missing reporting-currency quote",
                        ]
                        for k, v in summary.items()
                    ],
                    ["measure", "amount"],
                    right={1},
                ),
            )
        return 0
    finally:
        db.close()


def cmd_add(args: argparse.Namespace) -> int:
    db = open_book(args.book)
    try:
        debit = resolve_account(db, args.to)
        credit = resolve_account(db, getattr(args, "from"))
        when = parse_date(args.date) or date.today()
        amount = Money(args.amount)
        currency = transaction_currency(db)
        result = save_transaction(
            db,
            SaveTransaction(
                TransactionInput(
                    post_date=when,
                    description=args.description,
                    num=args.num,
                    currency=currency,
                    splits=(
                        TransactionSplitInput(
                            debit.handle, Amount(amount, currency), memo=args.memo
                        ),
                        TransactionSplitInput(
                            credit.handle, Amount(-amount, currency), memo=args.memo
                        ),
                    ),
                )
            ),
        )
        if not result.ok:
            raise CommandError(service_error_message(result.errors[0]))
        saved = result.value
        assert saved is not None
        emit(
            {"handle": saved.handle, "date": when, "amount": amount},
            args,
            f"Posted {amount.format()} {db.full_name(credit)} -> {db.full_name(debit)} on {when}",
        )
        return 0
    finally:
        db.close()


def _find_transaction(db: DbSQLite, reference: str) -> Transaction:
    """Locate a transaction by handle, or by a unique prefix of one."""
    exact = db.get_transaction(reference)
    if exact is not None:
        return exact
    matches = [t for t in db.iter_transactions() if t.handle.startswith(reference)]
    if not matches:
        raise CommandError(f"no transaction matches {reference!r}")
    if len(matches) > 1:
        raise CommandError(
            f"{reference!r} matches {len(matches)} transactions; use more characters"
        )
    return matches[0]


def cmd_edit(args: argparse.Namespace) -> int:
    """Change a transaction that is already recorded.

    Only the fields given are touched. Amounts are only editable on a two-split
    transaction: with three or more legs there is no single 'the amount', and
    guessing which one to move would silently rewrite someone's books.
    """
    db = open_book(args.book)
    try:
        target = _find_transaction(db, args.transaction)
        before = target.describe()

        if args.date:
            parsed_date = parse_date(args.date)
            if parsed_date is not None:
                target.post_date = parsed_date
        if args.description is not None:
            target.description = args.description
        if args.num is not None:
            target.num = args.num
        if args.memo is not None:
            for split in target.splits:
                split.memo = args.memo

        if args.amount is not None:
            if len(target.splits) != 2:
                raise CommandError(
                    f"this transaction has {len(target.splits)} splits; amounts can "
                    "only be changed on a two-split transaction. Edit it in the "
                    "interface, or delete and re-enter it."
                )
            amount = Money(args.amount)
            positive = next((s for s in target.splits if s.value > 0), target.splits[0])
            for split in target.splits:
                split.value = amount if split is positive else -amount
                split.quantity = split.value

        result = save_transaction(
            db,
            SaveTransaction(
                _transaction_input(db, target),
                existing_handle=target.handle,
                source=target,
            ),
        )
        if not result.ok:
            raise CommandError(service_error_message(result.errors[0]))

        emit(
            {"handle": target.handle, "date": target.post_date, "description": target.description},
            args,
            f"Edited {before}\n     -> {target.describe()}",
        )
        return 0
    finally:
        db.close()


def cmd_delete(args: argparse.Namespace) -> int:
    db = open_book(args.book)
    try:
        target = _find_transaction(db, args.transaction)
        description = target.describe()
        result = delete_transaction(db, DeleteTransaction(target.handle))
        if not result.ok:
            raise CommandError(service_error_message(result.errors[0]))
        emit({"deleted": target.handle}, args, f"Deleted {description}")
        return 0
    finally:
        db.close()


def _transaction_input(db: DbSQLite, transaction: Transaction) -> TransactionInput:
    """Translate one editable CLI model to the shared transaction contract."""
    currency = transaction_currency(db, transaction.currency)

    def split_input(split: Split) -> TransactionSplitInput:
        account = db.get_account(split.account)
        quantity_commodity = account.commodity if account is not None else None
        return TransactionSplitInput(
            split.account,
            Amount(split.value, currency),
            quantity=Amount(split.quantity, quantity_commodity or currency),
            handle=split.handle,
            memo=split.memo,
            planning_flow=split.planning_flow,
            investment_activity=split.investment_activity,
        )

    return TransactionInput(
        post_date=transaction.post_date,
        description=transaction.description,
        num=transaction.num,
        notes=transaction.notes,
        currency=currency,
        splits=tuple(split_input(split) for split in transaction.splits),
    )


def cmd_scheduled(args: argparse.Namespace) -> int:
    db = open_book(args.book, "r" if not args.post else "w")
    try:
        as_of = parse_date(args.as_of) or date.today()
        if args.post:
            posted = schedule.post_due(db, as_of=as_of, only_auto=not args.all)
            emit(
                [{"date": t.post_date, "description": t.description} for t in posted],
                args,
                f"Posted {len(posted)} scheduled transaction(s)"
                + (
                    ""
                    if not posted
                    else ":\n" + "\n".join(f"  {t.post_date}  {t.description}" for t in posted)
                ),
            )
            return 0

        occurrences = schedule.due_occurrences(db, as_of=as_of, horizon_days=args.days)
        payload = [
            {"date": occ.when, "name": occ.name, "amount": occ.amount} for occ in occurrences
        ]
        emit(
            payload,
            args,
            table(
                [[o.when.isoformat(), o.name, o.amount.format()] for o in occurrences],
                ["due", "schedule", "amount"],
                right={2},
            ),
        )
        return 0
    finally:
        db.close()


def cmd_activity(args: argparse.Namespace) -> int:
    """Show event-driven plan versus actuals in display-only time buckets."""
    db = open_book(args.book, "r")
    try:
        start = parse_date(args.start)
        end = parse_date(args.end)
        if start is None or end is None:
            raise CommandError("activity requires --start and --end")
        report = activity.build_activity_report(
            db,
            start,
            end,
            period=activity.ReportingPeriod(args.period),
            as_of=parse_date(args.as_of),
        )
        rows = [
            [
                period.label,
                period.planned_amount.format(),
                period.actual_amount.format(),
                period.amount_variance.format(parens_negative=True),
                period.planned_cash_change.format(parens_negative=True),
                period.actual_cash_change.format(parens_negative=True),
                len(period.unresolved),
                len(period.unresolved_actuals),
                len(period.unexpected),
            ]
            for period in report.periods
        ]
        text = table(
            rows,
            [
                "period",
                "planned",
                "actual",
                "variance",
                "planned cash",
                "actual cash",
                "expected due",
                "actual unresolved",
                "unexpected",
            ],
            right={1, 2, 3, 4, 5, 6, 7, 8},
        )
        notes = activity.currency_notes(db, report)
        if notes:
            text = "\n".join((text, "", *notes))
        emit(report.as_dict(), args, text)
        return 0
    finally:
        db.close()


def cmd_plan_unresolved(args: argparse.Namespace) -> int:
    """List unresolved scheduled expectations over an exact date horizon."""
    db = open_book(args.book, "r")
    try:
        start = parse_date(args.start)
        end = parse_date(args.end)
        if start is None or end is None:
            raise CommandError("plan-unresolved requires --start and --end")
        events = planning.unresolved_events(db, start, end)
        rows = [
            [
                event.key,
                event.planned_date.isoformat(),
                event.description[:40],
                event.expected_amount.format(),
            ]
            for event in events
        ]
        emit(
            [event.as_dict() for event in events],
            args,
            table(rows, ["occurrence", "planned", "description", "expected"], right={3}),
        )
        return 0
    finally:
        db.close()


def cmd_plan_matches(args: argparse.Namespace) -> int:
    """Show unresolved scheduled occurrences that could explain one actual."""
    db = open_book(args.book, "r")
    try:
        transaction = _find_transaction(db, args.transaction)
        candidates = planning.match_candidates(db, transaction, window_days=args.window_days)
        payload = [
            {
                "occurrence": candidate.event.as_dict(),
                "date_distance_days": candidate.date_distance,
                "amount_difference": candidate.amount_difference,
                "common_accounts": candidate.common_accounts,
            }
            for candidate in candidates
        ]
        rows = [
            [
                candidate.event.key,
                candidate.event.planned_date.isoformat(),
                candidate.event.description[:36],
                candidate.event.expected_amount.format(),
                candidate.date_distance,
                candidate.amount_difference.format(),
                candidate.common_accounts,
            ]
            for candidate in candidates
        ]
        text = table(
            rows,
            ["occurrence", "planned", "description", "expected", "days", "amount diff", "accounts"],
            right={3, 4, 5, 6},
        )
        emit(payload, args, text)
        return 0
    finally:
        db.close()


def cmd_plan_resolve(args: argparse.Namespace) -> int:
    """Resolve one actual transaction against a selected scheduled occurrence."""
    db = open_book(args.book)
    try:
        transaction = _find_transaction(db, args.transaction)
        result = match_review(db, ReviewOccurrence(transaction.handle, args.occurrence))
        if not result.ok:
            raise CommandError(service_error_message(result.errors[0]))
        saved_transaction = db.get_transaction(transaction.handle)
        assert saved_transaction is not None
        event = planning.event_by_key(db, args.occurrence)
        assert event is not None
        emit(
            {
                "transaction": saved_transaction.handle,
                "resolution": saved_transaction.planning_resolution.value,
                "occurrence": event.key,
                "planned_for": event.planned_date,
                "planned_amount": event.expected_amount,
            },
            args,
            f"Matched {saved_transaction.handle[:8]} to {event.key}",
        )
        return 0
    finally:
        db.close()


def cmd_plan_reject(args: argparse.Namespace) -> int:
    """Persistently reject one suggested occurrence for an unresolved actual."""
    db = open_book(args.book)
    try:
        transaction = _find_transaction(db, args.transaction)
        result = reject_review(db, ReviewOccurrence(transaction.handle, args.occurrence))
        if not result.ok:
            raise CommandError(service_error_message(result.errors[0]))
        saved_transaction = db.get_transaction(transaction.handle)
        assert saved_transaction is not None
        emit(
            {
                "transaction": saved_transaction.handle,
                "rejected_occurrence": args.occurrence,
                "rejected": list(saved_transaction.rejected_plan_occurrences),
            },
            args,
            f"Rejected {args.occurrence} for {saved_transaction.handle[:8]}",
        )
        return 0
    finally:
        db.close()


def cmd_plan_unexpected(args: argparse.Namespace) -> int:
    """Explicitly declare an actual transaction to have no planned occurrence."""
    db = open_book(args.book)
    try:
        transaction = _find_transaction(db, args.transaction)
        result = mark_review_unexpected(db, ReviewTransaction(transaction.handle))
        if not result.ok:
            raise CommandError(service_error_message(result.errors[0]))
        saved_transaction = db.get_transaction(transaction.handle)
        assert saved_transaction is not None
        emit(
            {
                "transaction": saved_transaction.handle,
                "resolution": saved_transaction.planning_resolution.value,
            },
            args,
            f"Marked {saved_transaction.handle[:8]} as unexpected",
        )
        return 0
    finally:
        db.close()


def cmd_project(args: argparse.Namespace) -> int:
    db = open_book(args.book, "r")
    try:
        scenario = _scenario_for(db, args)
        result = projection.project(db, scenario)

        if args.csv:
            from ..plugins.export.csv_export import export_projection

            export_projection(result, args.csv)

        if args.monthly:
            rows = [
                [
                    row.label,
                    row.income.format(),
                    row.expense.format(),
                    row.cash_close.format(parens_negative=True),
                    row.holdings.format(),
                    row.net_worth.format(parens_negative=True),
                ]
                for row in result.rows
            ]
            text = table(
                rows,
                ["month", "income", "expense", "cash", "holdings", "net worth"],
                right={1, 2, 3, 4, 5},
            )
        else:
            years = result.year_end("net_worth")
            cash = result.year_end("cash_close")
            income = result.annual("income")
            expense = result.annual("expense")
            start_year = scenario.start.year
            text = table(
                [
                    [
                        f"Year {i + 1} ({start_year + i})",
                        income[i].format(),
                        expense[i].format(),
                        cash[i].format(parens_negative=True),
                        years[i].format(parens_negative=True),
                    ]
                    for i in range(len(years))
                ],
                ["period", "income", "expense", "cash", "net worth"],
                right={1, 2, 3, 4},
            )

        summary = result.summary()
        if not args.json:
            assumptions = scenario.effective_assumptions()
            print(f"Scenario: {scenario.name}  ({scenario.years} years from {scenario.start})")
            print(
                f"  income growth {assumptions.income_growth:.1%}   "
                f"expense inflation {assumptions.expense_inflation:.1%}   "
                f"investment return {assumptions.investment_return:.1%}"
            )
            print()
        emit({"summary": summary, "rows": [r.as_dict() for r in result.rows]}, args, text)

        if not args.json:
            shortfall = result.first_shortfall()
            print()
            if shortfall:
                print(
                    f"  Cash runs out in {shortfall.label} "
                    f"({shortfall.cash_close.format(parens_negative=True)})"
                )
            else:
                print(f"  Lowest cash balance: {result.minimum_cash.format()}")
            for warning in result.warnings:
                print(f"  warning: {warning}")
            if args.csv:
                print(f"  Wrote {args.csv}")
        return 0
    finally:
        db.close()


def _scenario_for(db: DbSQLite, args: argparse.Namespace) -> Scenario:
    """Load a saved scenario, or build a throwaway one from the flags."""
    if args.scenario:
        scenario = db.get_scenario_by_name(args.scenario)
        if scenario is None:
            raise CommandError(f"no scenario named {args.scenario!r}")
        if args.years:
            scenario.years = args.years
        return scenario

    return Scenario(
        name=args.name or "ad hoc",
        start=parse_date(args.start) or date.today().replace(day=1),
        years=args.years or 5,
        assumptions=Assumptions(
            income_growth=args.income_growth,
            expense_inflation=args.inflation,
            investment_return=args.investment_return,
            cash_interest=args.cash_interest,
        ),
    )


def cmd_scenario(args: argparse.Namespace) -> int:
    if args.action == "list":
        db = open_book(args.book, "r")
        try:
            scenarios = list(db.iter_scenarios())
            effective = {s.handle: s.effective_assumptions() for s in scenarios}
            names: dict[str | None, str] = {
                None: "Base",
                **{s.handle: s.name for s in scenarios},
            }
            emit(
                [
                    {
                        "name": s.name,
                        "parent": names.get(s.parent_handle, "Base"),
                        "years": s.years,
                        "income_growth": effective[s.handle].income_growth,
                        "expense_inflation": effective[s.handle].expense_inflation,
                        "investment_return": effective[s.handle].investment_return,
                        "assumption_sources": s.assumption_sources(),
                    }
                    for s in scenarios
                ],
                args,
                table(
                    [
                        [
                            s.name,
                            names.get(s.parent_handle, "Base"),
                            str(s.years),
                            f"{effective[s.handle].income_growth:.1%}",
                            f"{effective[s.handle].expense_inflation:.1%}",
                            f"{effective[s.handle].investment_return:.1%}",
                        ]
                        for s in scenarios
                    ],
                    ["name", "parent", "years", "income", "inflation", "return"],
                    right={2, 3, 4, 5},
                ),
            )
            return 0
        finally:
            db.close()

    db = open_book(args.book)
    try:
        if args.action == "save":
            existing = db.get_scenario_by_name(args.name)
            scenario = existing or Scenario(name=args.name)
            scenario.years = args.years or scenario.years
            scenario.start = parse_date(args.start) or scenario.start
            updated = Assumptions(
                income_growth=args.income_growth,
                expense_inflation=args.inflation,
                investment_return=args.investment_return,
                cash_interest=args.cash_interest,
            )
            scenario.assumptions = updated
            scenario.assumption_overrides.update(
                {
                    "income_growth",
                    "expense_inflation",
                    "investment_return",
                    "cash_interest",
                    "liability_interest",
                }
            )
            result = save_scenario_assumptions(
                db,
                SaveScenarioAssumptions(
                    scenario,
                    existing_handle=existing.handle if existing is not None else None,
                ),
            )
            if not result.ok:
                raise CommandError(service_error_message(result.errors[0]))
            emit(
                {"name": scenario.name, "handle": scenario.handle},
                args,
                f"Saved scenario {scenario.name!r}",
            )
        elif args.action == "reparent":
            reparented = db.get_scenario_by_name(args.name)
            if reparented is None:
                raise CommandError(f"no scenario named {args.name!r}")
            parent_name = str(args.parent or "").strip()
            if not parent_name or parent_name.casefold() == "base":
                reparented.parent_handle = None
                resolved_parent = "Base"
            else:
                parent = db.get_scenario_by_name(parent_name)
                if parent is None:
                    raise CommandError(f"no scenario named {parent_name!r}")
                reparented.parent_handle = parent.handle
                resolved_parent = parent.name
            reparented.inherits_base_assumptions = True
            result = save_scenario(db, SaveScenario(reparented, existing_handle=reparented.handle))
            if not result.ok:
                raise CommandError(service_error_message(result.errors[0]))
            emit(
                {"name": reparented.name, "parent": resolved_parent},
                args,
                f"Scenario {reparented.name!r} now inherits from {resolved_parent!r}",
            )
        elif args.action == "delete":
            to_delete = db.get_scenario_by_name(args.name)
            if to_delete is None:
                raise CommandError(f"no scenario named {args.name!r}")
            result = delete_scenario(db, DeleteScenario(to_delete.handle))
            if not result.ok:
                raise CommandError(service_error_message(result.errors[0]))
            emit({"deleted": args.name}, args, f"Deleted scenario {args.name!r}")
        return 0
    finally:
        db.close()


def cmd_compare(args: argparse.Namespace) -> int:
    db = open_book(args.book, "r")
    try:
        left_scenario = db.get_scenario_by_name(args.base)
        right_scenario = db.get_scenario_by_name(args.other)
        if left_scenario is None:
            raise CommandError(f"no scenario named {args.base!r}")
        if right_scenario is None:
            raise CommandError(f"no scenario named {args.other!r}")
        left = projection.project(db, left_scenario)
        right = projection.project(db, right_scenario)
        rows = projection.compare(left, right)
        yearly = [row for row in rows if (row["index"] + 1) % 12 == 0]
        emit(
            yearly,
            args,
            table(
                [
                    [
                        row["label"],
                        row["base_net_worth"].format(parens_negative=True),
                        row["other_net_worth"].format(parens_negative=True),
                        row["net_worth_delta"].format(parens_negative=True),
                    ]
                    for row in yearly
                ],
                ["month", args.base, args.other, "difference"],
                right={1, 2, 3},
            ),
        )
        return 0
    finally:
        db.close()


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


def _inference_note(suggestions: list, applied: bool) -> str:
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
        f"  - {s.account_name}: {s.field} = {s.value_label} ({s.confidence:.0%})"
        for s in suggestions[:8]
    ]
    if len(suggestions) > 8:
        lines.append(f"  ... and {len(suggestions) - 8} more")
    lines.append("Run 'breadsched infer BOOK --apply' to accept them.")
    return "\n".join(lines)


def cmd_account(args: argparse.Namespace) -> int:
    """Add, edit or remove an account."""
    db = open_book(args.book, "r" if args.action == "list" else "w")
    try:
        if args.action == "list":
            rows = []
            for account in sorted(db.iter_accounts(), key=db.full_name):
                if account.is_root:
                    continue
                linked = db.get_account(account.linked_asset or "")
                payment_account = db.get_account(account.card_payment_account or "")
                rows.append(
                    [
                        db.full_name(account),
                        account.atype.value,
                        account.group,
                        db.full_name(linked) if linked else "",
                        ""
                        if account.atype is not AccountType.CREDIT
                        else ("monthly" if account.pays_in_full else "carries"),
                        db.full_name(payment_account) if payment_account else "",
                    ]
                )
            emit(
                [
                    {
                        "name": r[0],
                        "type": r[1],
                        "group": r[2],
                        "linked_asset": r[3],
                        "card": r[4],
                        "card_payment_account": r[5],
                    }
                    for r in rows
                ],
                args,
                table(
                    rows,
                    ["account", "type", "group", "linked asset", "card", "paid from"],
                ),
            )
            return 0

        if args.action == "add":
            if not args.name:
                raise CommandError("--name is required")
            parent = resolve_account(db, args.parent) if args.parent else db.root_account()
            if parent is None:
                raise CommandError("this book has no root account")
            account = Account(
                name=args.name,
                atype=AccountType.parse(args.type),
                parent=parent.handle,
                code=args.code or "",
                description=args.description or "",
                placeholder=args.placeholder,
            )
            _apply_account_options(db, account, args)
            result = save_account(
                db,
                SaveAccount(
                    account,
                    opening_balance=Money(args.opening) if args.opening else None,
                    opening_date=parse_date(args.opening_date) or date.today(),
                ),
            )
            if not result.ok:
                raise CommandError(service_error_message(result.errors[0]))
            emit(
                {"handle": account.handle, "name": db.full_name(account)},
                args,
                f"Added {db.full_name(account)} ({account.atype.value})",
            )
            return 0

        account = resolve_account(db, args.name or "")
        if args.action == "remove":
            result = delete_account(db, DeleteAccount(account.handle))
            if not result.ok:
                raise CommandError(service_error_message(result.errors[0]))
            emit({"removed": account.handle}, args, f"Removed {db.full_name(account)}")
            return 0

        # edit
        source = Account.from_dict(account.serialize())
        if args.rename:
            account.name = args.rename
        if args.type:
            account.atype = AccountType.parse(args.type)
        if args.description is not None:
            account.description = args.description
        if args.code is not None:
            account.code = args.code
        if args.parent:
            account.parent = resolve_account(db, args.parent).handle
        _apply_account_options(db, account, args)
        result = save_account(
            db,
            SaveAccount(account, existing_handle=account.handle, source=source),
        )
        if not result.ok:
            raise CommandError(service_error_message(result.errors[0]))
        emit(
            {"handle": account.handle, "name": db.full_name(account)},
            args,
            f"Edited {db.full_name(account)}",
        )
        return 0
    finally:
        db.close()


def _apply_account_options(db: DbSQLite, account: Account, args) -> None:
    """Shared between add and edit: the relationship fields."""
    if args.group is not None:
        account.group = args.group
    if args.linked_asset:
        account.linked_asset = resolve_account(db, args.linked_asset).handle
    if args.carries_balance is not None:
        account.pays_in_full = not args.carries_balance
    if args.usual_payment:
        account.usual_payment = Money(args.usual_payment)
    if args.payment_day:
        account.payment_day = args.payment_day
    if args.payment_account is not None:
        if account.atype is not AccountType.CREDIT:
            raise CommandError("--payment-account applies only to a Credit card account")
        if args.payment_account.strip().lower() == "none":
            account.card_payment_account = None
            return
        payment = resolve_account(db, args.payment_account)
        if not payment.atype.is_cash_like:
            raise CommandError("--payment-account must name a Bank or Cash account")
        account.card_payment_account = payment.handle
    elif account.atype is not AccountType.CREDIT:
        account.card_payment_account = None


def cmd_infer(args: argparse.Namespace) -> int:
    """Suggest relationships an imported book does not state."""
    db = open_book(args.book, "r" if not args.apply else "w")
    try:
        result = inference.infer_all(db)
        rows = [
            [
                s.account_name,
                s.field,
                str(s.value_label or s.value),
                f"{s.confidence:.0%}",
                s.reason,
            ]
            for s in result.suggestions
        ]
        if args.apply:
            chosen = [s for s in result.suggestions if s.confidence >= args.min_confidence]
            applied = inference.apply_suggestions(db, chosen)
            emit(
                {"applied": applied, "considered": len(result.suggestions)},
                args,
                f"Applied {applied} of {len(result.suggestions)} suggestion(s)\n\n"
                + table(rows, ["account", "setting", "value", "confidence", "because"]),
            )
            return 0

        emit(
            [
                {
                    "account": s.account_name,
                    "field": s.field,
                    "value": s.value_label,
                    "confidence": s.confidence,
                    "reason": s.reason,
                }
                for s in result.suggestions
            ],
            args,
            table(rows, ["account", "setting", "value", "confidence", "because"])
            + ("\n\nNothing is changed until you pass --apply." if result.suggestions else ""),
        )
        return 0
    finally:
        db.close()


def cmd_dashboard(args: argparse.Namespace) -> int:
    """The overview: what is owned, what is owed, and what must stay liquid."""
    db = open_book(args.book, "r")
    try:
        config = dashboard_engine.DashboardConfig.load(db)
        if args.liquidity_days:
            config.liquidity_days = args.liquidity_days
        if args.emergency_months:
            config.emergency_months = args.emergency_months
        board = dashboard_engine.build(db, config, as_of=parse_date(args.as_of))
        summary = board.summary()
        report = board.report_summary()

        def shown(field: str, amount: Money) -> str:
            return (
                board.unavailable_reason(field)
                if report[field] is None
                else amount.format(parens_negative=True)
            )

        if args.json:
            emit(
                {
                    "summary": report,
                    "unavailable_reasons": {
                        key: board.unavailable_reason(key)
                        for key, value in report.items()
                        if value is None
                    },
                    "missing_quotes": list(board.missing_quotes),
                    "liquid_missing_quotes": list(board.liquid_missing_quotes),
                    "coverage_notes": list(board.coverage_notes),
                    "groups": [
                        {
                            "name": g.name,
                            "path": g.path,
                            "depth": g.depth,
                            "heading": g.heading,
                            "note": g.note,
                            "members": list(g.members),
                            "kind": g.kind,
                            "total": g.report_total,
                            "value": g.report_value,
                            "debt": g.report_debt,
                            "equity": g.report_equity,
                            "loan_to_value": g.report_loan_to_value,
                            "missing_quotes": list(g.missing_quotes),
                            "loan_end": g.loan_end,
                            "accounts": [
                                {
                                    "name": account.name,
                                    "balance": account.total,
                                    "source": account.source,
                                    "note": account.note,
                                }
                                for account in g.accounts
                            ],
                        }
                        for g in board.groups
                    ],
                    "bills": [
                        {
                            "name": item.name,
                            "next_due": item.next_due,
                            "frequency": item.frequency,
                            "cycle_months": item.cycle_months,
                            "amount": item.amount,
                            "monthly": item.monthly,
                            "annual": item.annual,
                            "hold": item.hold(board.as_of),
                            "reserve_for": item.reserve_for,
                            "estimate": item.estimate,
                            "generated": item.generated,
                        }
                        for item in board.bills
                    ],
                    "income": [
                        {
                            "name": item.name,
                            "next_due": item.next_due,
                            "frequency": item.frequency,
                            "cycle_months": item.cycle_months,
                            "amount": item.amount,
                            "monthly": item.monthly,
                            "annual": item.annual,
                        }
                        for item in board.incomes
                    ],
                    "missed_bills": [
                        _missed_json(item)
                        for item in board.display_bills
                        if isinstance(item, dashboard_engine.MissedGroup)
                    ],
                    "missed_income": [
                        _missed_json(item)
                        for item in board.display_incomes
                        if isinstance(item, dashboard_engine.MissedGroup)
                    ],
                },
                args,
            )
            return 0

        print(f"Dashboard as at {board.as_of}\n")
        rows = []
        for group in board.groups:
            group_label = f"{'  ' * group.depth}{group.name}"
            if group.note:
                group_label = f"{group_label} — {group.note}"
            loan_to_value = group.report_loan_to_value
            equity = group.report_equity
            if (
                not group.missing_quotes
                and loan_to_value is not None
                and group.report_value is not None
                and group.report_debt is not None
                and equity is not None
            ):
                rows.append(
                    [
                        group_label,
                        group.report_value.format(),
                        group.report_debt.format(),
                        equity.format(parens_negative=True),
                        f"{loan_to_value:.1%}",
                        str(group.loan_end or ""),
                    ]
                )
            else:
                rows.append(
                    [
                        group_label,
                        "",
                        "",
                        "Missing reporting-currency quote"
                        if group.missing_quotes
                        else group.total.format(parens_negative=True),
                        "",
                        str(group.loan_end or ""),
                    ]
                )
        print(
            table(
                rows,
                ["group", "value", "owed", "equity / total", "LTV", "loan end"],
                right={1, 2, 3, 4},
            )
        )

        print()
        for note in board.coverage_notes:
            print(note)
        if board.coverage_notes:
            print()
        headline = [
            ["Net worth", shown("net_worth", summary["net_worth"])],
            ["Liquid", shown("liquid", summary["liquid"])],
            [
                f"Needed within {config.liquidity_days} days",
                summary["required_liquid"].format(parens_negative=True),
            ],
            ["Available", shown("available", summary["available"])],
            [
                f"Emergency fund ({config.emergency_months} months)",
                shown("emergency_fund", summary["emergency_fund"]),
            ],
            [
                "Months covered",
                board.unavailable_reason("months_covered")
                if report["months_covered"] is None
                else f"{summary['months_covered']}",
            ],
            ["Committed outgoings, monthly", summary["monthly_outgoings"].format()],
            [
                "Outgoings including estimates, monthly",
                summary["monthly_outgoings_with_estimates"].format(),
            ],
            [
                "Committed emergency outgoings, monthly",
                summary["emergency_monthly_outgoings"].format(),
            ],
            [
                "Emergency outgoings including estimates, monthly",
                summary["emergency_monthly_outgoings_with_estimates"].format(),
            ],
            ["Committed income, monthly", summary["income_per_month"].format()],
            [
                "Income including estimates, monthly",
                summary["income_per_month_with_estimates"].format(),
            ],
        ]
        print(table(headline, ["measure", "amount"], right={1}))

        if board.bills:
            print()
            print("Pending bills")
            display_bills = board.display_bills
            bill_rows = [
                [
                    item.name[:32],
                    _due_cell(item, board.as_of),
                    item.frequency,
                    item.amount.format(),
                    "" if item.generated else item.monthly.format(),
                    item.held.format(),
                    "" if item.generated else item.annual.format(),
                    "account" if item.generated else "",
                ]
                for item in display_bills[: args.limit]
            ]
            print(
                table(
                    bill_rows,
                    [
                        "item",
                        "due",
                        "frequency",
                        "amount",
                        "monthly",
                        "hold",
                        "annual",
                        "",
                    ],
                    right={3, 4, 5, 6},
                )
            )
            if len(display_bills) > args.limit:
                print(f"... and {len(display_bills) - args.limit} more")
        if board.incomes:
            print()
            print("Expected income")
            display_incomes = board.display_incomes
            income_rows = [
                [
                    item.name[:32],
                    _due_cell(item, board.as_of),
                    item.frequency,
                    item.amount.format(),
                    item.monthly.format(),
                    item.annual.format(),
                ]
                for item in display_incomes[: args.limit]
            ]
            print(
                table(
                    income_rows,
                    ["item", "due", "frequency", "amount", "monthly", "annual"],
                    right={3, 4, 5},
                )
            )
            if len(display_incomes) > args.limit:
                print(f"... and {len(display_incomes) - args.limit} more")
        return 0
    finally:
        db.close()


def cmd_estimate(args: argparse.Namespace) -> int:
    """Manage placeholder flows: recurring estimates that are never posted."""
    db = open_book(
        args.book if args.action not in {"list", "suggest"} else args.book,
        "r" if args.action in {"list", "suggest"} else "w",
    )
    try:
        if args.action == "list":
            rows, payload = [], []
            for sched in db.iter_scheduled():
                if not sched.placeholder:
                    continue
                rows.append(
                    [
                        sched.name,
                        sched.recurrence.describe(),
                        sched.amount().format(),
                        "yes" if sched.enabled else "no",
                    ]
                )
                payload.append(
                    {
                        "name": sched.name,
                        "frequency": sched.recurrence.describe(),
                        "amount": sched.amount(),
                        "enabled": sched.enabled,
                    }
                )
            emit(payload, args, table(rows, ["name", "frequency", "amount", "enabled"], right={2}))
            return 0

        if args.action == "suggest":
            proposals = estimates.propose_historical_estimates(
                db,
                as_of=parse_date(args.as_of),
                months=args.months,
                min_active_months=args.min_active_months,
            )
            payload = [
                {
                    "key": proposal.key,
                    "purpose": proposal.purpose_name,
                    "account": proposal.category_name,
                    "funding": proposal.funding_name,
                    "amount": proposal.display_amount,
                    "frequency": proposal.recurrence.describe(),
                    "confidence": proposal.confidence,
                    "evidence": proposal.evidence.serialize(),
                }
                for proposal in proposals
            ]
            rows = [
                [
                    proposal.purpose_name,
                    proposal.display_amount.format(),
                    proposal.recurrence.describe(),
                    f"{proposal.confidence:.0%}",
                ]
                for proposal in proposals
            ]
            details = "\n\n".join(
                f"{proposal.purpose_name}\n" + "\n".join(proposal.evidence.summary_lines())
                for proposal in proposals
            )
            text = table(rows, ["purpose", "amount", "frequency", "confidence"], right={1})
            if details:
                text += "\n\n" + details
            emit(payload, args, text)
            return 0

        if args.action == "remove":
            match = next(
                (s for s in db.iter_scheduled() if s.placeholder and s.name == args.name), None
            )
            if match is None:
                raise CommandError(f"no estimate named {args.name!r}")
            result = delete_schedule(db, DeleteSchedule(match.handle))
            if not result.ok:
                raise CommandError(service_error_message(result.errors[0]))
            emit({"removed": args.name}, args, f"Removed estimate {args.name!r}")
            return 0

        account = resolve_account(db, args.account)
        funding = resolve_account(db, args.funded_from)
        recurrence = Recurrence(
            period=args.every,
            interval=args.interval,
            start=parse_date(args.start) or date.today().replace(day=1),
        )
        value = Money(args.amount)
        sched = ScheduledTransaction(
            name=args.name,
            recurrence=recurrence,
            splits=[
                ScheduledSplit(account.handle, value),
                ScheduledSplit(funding.handle, -value),
            ],
        )
        sched.placeholder = True
        result = save_schedule(db, SaveSchedule(sched))
        if not result.ok:
            raise CommandError(service_error_message(result.errors[0]))
        emit(
            {"name": sched.name, "handle": sched.handle},
            args,
            f"Added estimate {sched.name!r}: {Money(args.amount).format()} on "
            f"{db.full_name(account)} {recurrence.describe()}",
        )
        return 0
    finally:
        db.close()


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

    init = add("init", "Create a new book with a starter chart of accounts")
    init.set_defaults(func=cmd_init)

    sample = add("sample", "Create a synthetic household book for learning")
    sample.add_argument("--as-of", help="reference date for sample events (YYYY-MM-DD)")
    sample.set_defaults(func=cmd_sample)

    imp = add("import", "Import a GnuCash book")
    imp.add_argument("source", help="path to the GnuCash file")
    imp.add_argument("--format", help="force an importer instead of detecting one")
    imp.add_argument("--no-scheduled", action="store_true", help="skip scheduled transactions")
    imp.add_argument("--max-warnings", type=int, default=10)
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
    csv_cmd.add_argument("--description", help="description or payee column")
    csv_cmd.add_argument("--memo", help="memo column")
    csv_cmd.add_argument(
        "--category",
        help="category column: an existing account's full name or unique name",
    )
    csv_cmd.add_argument("--payee", help="payee column: a payee already in the book")
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
    csv_cmd.set_defaults(func=cmd_import_csv)

    due_review = add(
        "due-review",
        "List or decide due and missed scheduled transactions, grouped by schedule",
    )
    due_review.add_argument("--as-of", help="review date (YYYY-MM-DD, default today)")
    due_review.add_argument(
        "--post",
        action="append",
        metavar="SCHEDULE[@DATE]",
        help="post a schedule's due dates, or one date (repeatable; id prefix or exact name)",
    )
    due_review.add_argument(
        "--skip",
        action="append",
        metavar="SCHEDULE[@DATE]",
        help="mark a schedule's due dates, or one date, as done without posting (repeatable)",
    )
    due_review.add_argument("--post-all", action="store_true", help="post every due date")
    due_review.add_argument(
        "--skip-all", action="store_true", help="mark every due date as done without posting"
    )
    due_review.set_defaults(func=cmd_due_review)

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
    held.set_defaults(func=cmd_import_review)

    writeback = add(
        "gnucash-writeback",
        "Preview, or write chosen changes back to the imported GnuCash SQLite book",
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
    writeback.set_defaults(func=cmd_gnucash_writeback)

    rules_cmd = add(
        "rules",
        "List categorization rules, change them, or preview and accept their proposals",
    )
    rules_cmd.add_argument("--add-description", metavar="TEXT", help="match this description")
    rules_cmd.add_argument("--add-payee", metavar="PAYEE", help="match this payee (name or handle)")
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

    payees_cmd = add(
        "payees",
        "List payees, add or delete one, or preview and accept payee proposals",
    )
    payees_cmd.add_argument("--add", metavar="NAME", help="create a payee with this name")
    payees_cmd.add_argument(
        "--match",
        action="append",
        metavar="DESCRIPTION",
        help="a description that identifies the new payee (repeatable)",
    )
    payees_cmd.add_argument("--delete", metavar="PAYEE", help="delete a payee (name or handle)")
    payees_cmd.add_argument(
        "--preview", action="store_true", help="list proposals for transactions without a payee"
    )
    payees_cmd.add_argument(
        "--accept",
        action="append",
        metavar="TRANSACTION",
        help="accept the proposal for one transaction (repeatable; handle or unique prefix)",
    )
    payees_cmd.add_argument("--accept-all", action="store_true", help="accept every proposal")
    payees_cmd.set_defaults(func=cmd_payees)

    receivables_cmd = add(
        "receivables",
        "List reimbursable expenses, add or resolve one, or link ledger splits",
    )
    receivables_cmd.add_argument(
        "--add", metavar="PAYER", help="create a receivable for this payer"
    )
    receivables_cmd.add_argument("--incurred", metavar="DATE", help="date the expense was incurred")
    receivables_cmd.add_argument("--description", help="what the expense was for")
    receivables_cmd.add_argument("--expected", metavar="AMOUNT", help="amount expected back")
    receivables_cmd.add_argument(
        "--expected-cash-date", metavar="DATE", help="date the reimbursement is expected"
    )
    receivables_cmd.add_argument(
        "--attach-expense", metavar="RECEIVABLE", help="link the split recording the cost"
    )
    receivables_cmd.add_argument(
        "--attach-reimbursement",
        metavar="RECEIVABLE",
        help="link the split crediting money back",
    )
    receivables_cmd.add_argument(
        "--detach", metavar="RECEIVABLE", help="unlink a split (with --transaction --split-index)"
    )
    receivables_cmd.add_argument(
        "--transaction", metavar="TRANSACTION", help="transaction handle or unique prefix"
    )
    receivables_cmd.add_argument(
        "--split-index", type=int, metavar="N", help="1-based split position within --transaction"
    )
    receivables_cmd.add_argument(
        "--dispute", metavar="RECEIVABLE", help="mark a receivable as disputed (with --on)"
    )
    receivables_cmd.add_argument("--note", help="dispute note")
    receivables_cmd.add_argument(
        "--clear-dispute", metavar="RECEIVABLE", help="withdraw a receivable's dispute"
    )
    receivables_cmd.add_argument(
        "--write-off",
        metavar="RECEIVABLE",
        help="give up on collecting part or all of the balance (with --amount --on)",
    )
    receivables_cmd.add_argument("--amount", help="write-off amount")
    receivables_cmd.add_argument("--on", metavar="DATE", help="date of the dispute or write-off")
    receivables_cmd.add_argument("--reason", help="write-off reason")
    receivables_cmd.add_argument(
        "--proposals",
        action="store_true",
        help="list unlinked credits that clearly reimburse one open receivable",
    )
    receivables_cmd.add_argument(
        "--accept-proposals",
        action="store_true",
        help="link every current reimbursement proposal",
    )
    receivables_cmd.add_argument("--delete", metavar="RECEIVABLE", help="delete a receivable")
    receivables_cmd.set_defaults(func=cmd_receivables)

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

    verify = add("verify", "Verify SQLite integrity and financial object relationships")
    verify.set_defaults(func=cmd_verify)

    accounts = add("accounts", "Show the chart of accounts with balances")
    accounts.add_argument("--as-of", help="balances as at this date (YYYY-MM-DD)")
    accounts.add_argument("--all", action="store_true", help="include hidden accounts")
    accounts.set_defaults(func=cmd_accounts)

    register = add("register", "Show one account's register with a running balance")
    register.add_argument("account", help="account name, full path, or handle")
    register.add_argument("--start")
    register.add_argument("--end")
    register.add_argument("--limit", type=int, default=0, help="show only the last N rows")
    register.set_defaults(func=cmd_register)

    balance = add("balance", "Show a balance, or the book's headline totals")
    balance.add_argument("account", nargs="?")
    balance.add_argument("--as-of")
    balance.add_argument("--recursive", action="store_true", help="include child accounts")
    balance.set_defaults(func=cmd_balance)

    rate = add("rate", "Save a dated manual exchange rate (target units per source unit)")
    rate.add_argument("--from", dest="source", required=True, help="source currency code or handle")
    rate.add_argument("--to", dest="target", required=True, help="target currency code or handle")
    rate.add_argument("--date", required=True, help="quote date (YYYY-MM-DD)")
    rate.add_argument("--value", required=True, help="target units per one source unit")
    rate.set_defaults(func=cmd_rate)

    add_txn = add("add", "Post a two-split transaction")
    add_txn.add_argument("--date", default="today")
    add_txn.add_argument("--description", required=True)
    add_txn.add_argument("--from", required=True, dest="from", help="account the money leaves")
    add_txn.add_argument("--to", required=True, help="account the money arrives in")
    add_txn.add_argument("--amount", required=True)
    add_txn.add_argument("--memo", default="")
    add_txn.add_argument("--num", default="")
    add_txn.set_defaults(func=cmd_add)

    edit = add("edit", "Change a transaction that is already recorded")
    edit.add_argument("transaction", help="transaction handle, or a unique prefix")
    edit.add_argument("--date")
    edit.add_argument("--description")
    edit.add_argument("--num")
    edit.add_argument("--memo", help="set the memo on every split")
    edit.add_argument("--amount", help="two-split transactions only")
    edit.set_defaults(func=cmd_edit)

    delete = add("delete", "Remove a transaction")
    delete.add_argument("transaction", help="transaction handle, or a unique prefix")
    delete.set_defaults(func=cmd_delete)

    sched = add("scheduled", "List due scheduled transactions, or post them")
    sched.add_argument("--as-of")
    sched.add_argument("--days", type=int, default=30, help="look this far ahead")
    sched.add_argument("--post", action="store_true", help="write due occurrences")
    sched.add_argument("--all", action="store_true", help="with --post, include manual schedules")
    sched.set_defaults(func=cmd_scheduled)

    activity_cmd = add(
        "activity",
        "Event-driven plan versus actuals, grouped only for display",
    )
    activity_cmd.add_argument("--start", required=True, help="first date (YYYY-MM-DD)")
    activity_cmd.add_argument("--end", required=True, help="last date (YYYY-MM-DD)")
    activity_cmd.add_argument(
        "--period",
        default="month",
        choices=[period.value for period in activity.ReportingPeriod],
        help="display grouping; does not change event dates",
    )
    activity_cmd.add_argument(
        "--as-of",
        help="date whose exchange-rate quotes convert foreign amounts (default today)",
    )
    activity_cmd.set_defaults(func=cmd_activity)

    plan_unresolved = add(
        "plan-unresolved",
        "List scheduled expectations that have not been resolved to actuals",
    )
    plan_unresolved.add_argument("--start", required=True, help="first date (YYYY-MM-DD)")
    plan_unresolved.add_argument("--end", required=True, help="last date (YYYY-MM-DD)")
    plan_unresolved.set_defaults(func=cmd_plan_unresolved)

    plan_matches = add(
        "plan-matches",
        "Show planned occurrences that could match an actual transaction",
    )
    plan_matches.add_argument("transaction", help="transaction handle, or a unique prefix")
    plan_matches.add_argument("--window-days", type=int, default=7)
    plan_matches.set_defaults(func=cmd_plan_matches)

    plan_resolve = add("plan-resolve", "Match an actual transaction to a planned occurrence")
    plan_resolve.add_argument("transaction", help="transaction handle, or a unique prefix")
    plan_resolve.add_argument("occurrence", help="stable scheduled occurrence key")
    plan_resolve.set_defaults(func=cmd_plan_resolve)

    plan_reject = add("plan-reject", "Reject a planned occurrence as a match candidate")
    plan_reject.add_argument("transaction", help="transaction handle, or a unique prefix")
    plan_reject.add_argument("occurrence", help="stable scheduled occurrence key")
    plan_reject.set_defaults(func=cmd_plan_reject)

    plan_unexpected = add(
        "plan-unexpected",
        "Declare an actual transaction to be intentionally outside the plan",
    )
    plan_unexpected.add_argument("transaction", help="transaction handle, or a unique prefix")
    plan_unexpected.set_defaults(func=cmd_plan_unexpected)

    account = add("account", "Add, edit, list or remove accounts")
    account.add_argument("action", choices=["list", "add", "edit", "remove"])
    account.add_argument("--name", help="account name, or path when editing")
    account.add_argument("--rename")
    account.add_argument("--type", help="BANK, CREDIT CARD, LOAN, FSA, EXPENSE, ...")
    account.add_argument("--parent")
    account.add_argument("--description")
    account.add_argument("--code")
    account.add_argument("--placeholder", action="store_true")
    account.add_argument("--group", help="dashboard group this account belongs to")
    account.add_argument("--linked-asset", help="for a loan: the asset behind it")
    account.add_argument(
        "--carries-balance",
        type=_boolean,
        metavar="yes|no",
        help="for a credit card: whether a balance is carried",
    )
    account.add_argument("--usual-payment", help="typical payment on a card")
    account.add_argument("--payment-day", type=int)
    account.add_argument(
        "--payment-account",
        help="Bank or Cash account used to pay a card; 'none' clears it",
    )
    account.add_argument("--opening", help="opening balance to post")
    account.add_argument("--opening-date")
    account.set_defaults(func=cmd_account)

    infer = add("infer", "Suggest loan/asset links and credit-card settings")
    infer.add_argument("--apply", action="store_true", help="write the suggestions")
    infer.add_argument("--min-confidence", type=float, default=0.6)
    infer.set_defaults(func=cmd_infer)

    dash = add("dashboard", "Overview of balances, bills and liquidity")
    dash.add_argument("--as-of")
    dash.add_argument(
        "--liquidity-days", type=int, help="days of bills that must be covered by cash"
    )
    dash.add_argument(
        "--emergency-months", type=int, help="months of outgoings the emergency fund should cover"
    )
    dash.add_argument("--limit", type=int, default=40, help="bills and income rows to list")
    dash.set_defaults(func=cmd_dashboard)

    estimate = add("estimate", "Recurring Plan estimates that never post")
    estimate.add_argument("action", choices=["list", "suggest", "add", "remove"])
    estimate.add_argument("--name")
    estimate.add_argument("--account", help="the income or expense account")
    estimate.add_argument(
        "--funded-from",
        help="the account the money moves to or from",
    )
    estimate.add_argument("--amount")
    estimate.add_argument(
        "--every",
        default="month",
        choices=[p.value for p in PeriodType],
        help="how often the flow recurs",
    )
    estimate.add_argument("--interval", type=int, default=1)
    estimate.add_argument("--start")
    estimate.add_argument("--as-of", help="analysis date for suggest (YYYY-MM-DD)")
    estimate.add_argument("--months", type=int, default=12, help="completed history months")
    estimate.add_argument("--min-active-months", type=int, default=3)
    estimate.set_defaults(func=cmd_estimate)

    web = add("web", "Serve the browser interface on this machine")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--open", action="store_true", help="open a browser window")
    web.add_argument("--read-only", action="store_true")
    web.set_defaults(func=cmd_web)

    def assumption_flags(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--income-growth", default="0.03", help="annual, e.g. 0.03")
        sub.add_argument("--inflation", default="0.025", help="annual expense inflation")
        sub.add_argument("--investment-return", default="0.06", help="annual, nominal")
        sub.add_argument("--cash-interest", default="0.01")

    project_cmd = add("project", "Run a multi-year projection")
    project_cmd.add_argument("--scenario", help="use a saved scenario")
    project_cmd.add_argument("--name", help="label for an ad hoc scenario")
    project_cmd.add_argument("--years", type=int)
    project_cmd.add_argument("--start")
    project_cmd.add_argument(
        "--monthly", action="store_true", help="month by month instead of yearly"
    )
    project_cmd.add_argument("--csv", help="also write the monthly rows to this file")
    assumption_flags(project_cmd)
    project_cmd.set_defaults(func=cmd_project)

    scenario = add("scenario", "Save, list, reparent or delete projection scenarios")
    scenario.add_argument("action", choices=["list", "save", "reparent", "delete"])
    scenario.add_argument("--name")
    scenario.add_argument("--parent", help="parent scenario name, or Base")
    scenario.add_argument("--years", type=int)
    scenario.add_argument("--start")
    assumption_flags(scenario)
    scenario.set_defaults(func=cmd_scenario)

    compare = add("compare", "Compare two saved scenarios year by year")
    compare.add_argument("base")
    compare.add_argument("other")
    compare.set_defaults(func=cmd_compare)

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

    gui = add("gui", "Open the graphical interface", needs_book=False)
    gui.add_argument("book", nargs="?", help="book to open on start-up")
    gui.set_defaults(func=cmd_gui)

    plugins = add("plugins", "List registered importers and exporters", needs_book=False)
    plugins.set_defaults(func=cmd_plugins)

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
