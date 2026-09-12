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
    budgeting,
    cashflow,
    inference,
    ledger,
    planning,
    projection,
    schedule,
)
from ..gen.engine import (
    dashboard as dashboard_engine,
)
from ..gen.lib import (
    Account,
    AccountType,
    Assumptions,
    Budget,
    Money,
    PeriodKind,
    PeriodType,
    ProjectionBasis,
    Recurrence,
    Scenario,
    Transaction,
)
from ..gen.plug import EXPORTER, IMPORTER, PluginManager
from ..gen.utils import logs

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


def cmd_import(args: argparse.Namespace) -> int:
    source = Path(args.source)
    if not source.exists():
        raise CommandError(f"no file at {source}")
    if source.is_dir():
        raise CommandError(f"{source} is a directory, not a book file")
    manager = PluginManager.instance()
    plugin = (
        manager.get(IMPORTER, args.format)
        if args.format
        else manager.for_file(args.source, IMPORTER)
    )
    if plugin is None:
        raise CommandError(
            f"no importer handles {args.source}; try --format with one of: "
            + ", ".join(p.id for p in manager.by_category(IMPORTER))
        )
    db = open_book(args.book)
    try:
        result = plugin.run(db, args.source, include_scheduled=not args.no_scheduled)
        result.log_path = str(args.log_file) if args.log_file else None
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
            "splits": result.splits,
            "commodities": result.commodities,
            "scheduled": result.scheduled,
            "skipped": result.skipped,
            "skipped_by_reason": result.reasons(),
            "warnings": result.warnings,
            "source": result.source,
            "source_format": result.source_format,
            "log_file": result.log_path,
            "suggestions": [
                {"account": s.account_name, "field": s.field,
                 "value": s.value_label, "confidence": s.confidence,
                 "reason": s.reason}
                for s in suggestions
            ],
        },
        args,
        f"Imported into {args.book}\n"
        + result.detail(limit=args.max_warnings)
        + _inference_note(suggestions, args.infer_apply),
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


def cmd_restore(args: argparse.Namespace) -> int:
    """Restore a verified backup, preserving an overwritten book first."""
    destination = DbSQLite.restore_backup(
        args.source, args.destination, overwrite=args.overwrite
    )
    payload = {"backup": args.source, "book": destination}
    pre_restore = Path(str(destination) + ".pre-restore.bak")
    if pre_restore.exists():
        payload["pre_restore_backup"] = str(pre_restore)
    emit(payload, args, f"Restored {args.source} to {destination}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Check SQLite integrity and logical book invariants without modifying the book."""
    db = DbSQLite()
    db.load_for_verification(args.book)
    try:
        sqlite_issues = db.integrity_problems()
        logical = db.verify_book()
        payload = {
            "ok": not sqlite_issues and not logical,
            "sqlite": sqlite_issues,
            "issues": [issue.as_dict() for issue in logical],
        }
        if payload["ok"]:
            emit(payload, args, "Book verification passed: SQLite and logical checks are clean")
            return 0
        lines = ["Book verification failed:"]
        lines.extend(f"  sqlite: {problem}" for problem in sqlite_issues)
        lines.extend(f"  {issue.code}: {issue.message}" for issue in logical)
        emit(payload, args, "\n".join(lines))
        return 1
    finally:
        db.close()


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
                total = ledger.balance_recursive(db, account.handle, as_of=as_of)
                payload.append(
                    {
                        "handle": account.handle,
                        "name": db.full_name(account),
                        "type": account.atype.value,
                        "balance": total,
                    }
                )
                label = ("  " * depth) + account.name
                rows.append([label, account.atype.value, total.format(parens_negative=True)])
                walk(account.handle, depth + 1)

        root = db.root_account()
        walk(root.handle if root else None, 0)
        emit(payload, args, table(rows, ["account", "type", "balance"], right={2}))
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
            rows = rows[-args.limit:]
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
            summary = {
                "cash": ledger.cash_on_hand(db, as_of=as_of),
                "net_worth": ledger.net_worth(db, as_of=as_of),
            }
            emit(
                summary,
                args,
                table(
                    [[k.replace("_", " "), v.format(parens_negative=True)]
                     for k, v in summary.items()],
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
        posted = Transaction.simple(
            when, args.description, debit.handle, credit.handle,
            Money(args.amount), memo=args.memo,
        )
        posted.num = args.num
        with db.transaction(f"Add {args.description}") as txn:
            db.add_transaction(posted, txn)
        emit(
            {"handle": posted.handle, "date": when, "amount": posted.value_for(debit.handle)},
            args,
            f"Posted {Money(args.amount).format()} {db.full_name(credit)} "
            f"-> {db.full_name(debit)} on {when}",
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
            positive = next(
                (s for s in target.splits if s.value > 0), target.splits[0]
            )
            for split in target.splits:
                split.value = amount if split is positive else -amount
                split.quantity = split.value

        with db.transaction(f"Edit {target.description}") as txn:
            db.commit_transaction(target, txn)

        emit(
            {"handle": target.handle, "date": target.post_date,
             "description": target.description},
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
        with db.transaction(f"Delete {target.description}") as txn:
            db.remove_transaction(target.handle, txn)
        emit({"deleted": target.handle}, args, f"Deleted {description}")
        return 0
    finally:
        db.close()


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
                + ("" if not posted else ":\n" + "\n".join(
                    f"  {t.post_date}  {t.description}" for t in posted
                )),
            )
            return 0

        occurrences = schedule.due_occurrences(db, as_of=as_of, horizon_days=args.days)
        payload = [
            {"date": occ.when, "name": occ.name, "amount": occ.amount}
            for occ in occurrences
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


def cmd_budget(args: argparse.Namespace) -> int:
    db = open_book(args.book, "r")
    try:
        budgets = list(db.iter_budgets())
        if args.name:
            budgets = [b for b in budgets if b.name == args.name]
            if not budgets:
                raise CommandError(f"no budget named {args.name!r}")
        elif not budgets:
            raise CommandError("this book has no budgets yet")
        budget = budgets[0]
        report = cashflow.build_report(db, budget)

        rows = []
        payload = []
        for line in report.lines:
            rows.append([
                line.name,
                line.budgeted_total.format(),
                line.actual_total.format(),
                line.variance_total.format(parens_negative=True),
            ])
            payload.append({
                "account": line.name,
                "budgeted": line.budgeted_total,
                "actual": line.actual_total,
                "variance": line.variance_total,
            })
        summary = report.annual_summary()
        actual_summary = report.annual_summary(actual=True)
        rows.append(["", "", "", ""])
        rows.append([
            "Income for the period",
            summary["income"].format(), actual_summary["income"].format(), "",
        ])
        rows.append([
            "Expenses for the period",
            summary["expense"].format(), actual_summary["expense"].format(), "",
        ])
        rows.append([
            "NET CASH FLOW",
            summary["net"].format(parens_negative=True),
            actual_summary["net"].format(parens_negative=True),
            (actual_summary["net"] - summary["net"]).format(parens_negative=True),
        ])
        text = table(rows, ["account", "budgeted", "actual", "variance"], right={1, 2, 3})
        shortfalls = report.shortfall_periods()
        if shortfalls:
            text += "\n\nPlanned shortfall in: " + ", ".join(
                report.labels[p] for p in shortfalls
            )
        emit(
            {
                "budget": budget.name,
                "lines": payload,
                "totals": {
                    "budgeted": summary,
                    "actual": actual_summary,
                    "net_cash_flow": summary["net"],
                    "shortfall_periods": [report.labels[p] for p in shortfalls],
                },
            },
            args,
            text,
        )
        return 0
    finally:
        db.close()


def _budget_by_name(db: DbSQLite, name: str) -> Budget | None:
    """Return a legacy Budget by exact name without relying on SQLite-only helpers."""
    return next((budget for budget in db.iter_budgets() if budget.name == name), None)


def cmd_activity(args: argparse.Namespace) -> int:
    """Show event-driven plan versus actuals in display-only time buckets."""
    db = open_book(args.book, "r")
    try:
        start = parse_date(args.start)
        end = parse_date(args.end)
        if start is None or end is None:
            raise CommandError("activity requires --start and --end")
        budget_handle = None
        if args.budget:
            budget = _budget_by_name(db, args.budget)
            if budget is None:
                raise CommandError(f"no budget named {args.budget!r}")
            budget_handle = budget.handle
        report = activity.build_activity_report(
            db,
            start,
            end,
            period=activity.ReportingPeriod(args.period),
            budget_handle=budget_handle,
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
        emit(report.as_dict(), args, text)
        return 0
    finally:
        db.close()


def _budget_handle(db: DbSQLite, name: str | None) -> str | None:
    if not name:
        return None
    budget = _budget_by_name(db, name)
    if budget is None:
        raise CommandError(f"no budget named {name!r}")
    return budget.handle


def cmd_plan_unresolved(args: argparse.Namespace) -> int:
    """List unresolved scheduled expectations over an exact date horizon."""
    db = open_book(args.book, "r")
    try:
        start = parse_date(args.start)
        end = parse_date(args.end)
        if start is None or end is None:
            raise CommandError("plan-unresolved requires --start and --end")
        events = planning.unresolved_events(
            db, start, end, budget_handle=_budget_handle(db, args.budget)
        )
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
        candidates = planning.match_candidates(
            db,
            transaction,
            window_days=args.window_days,
            budget_handle=_budget_handle(db, args.budget),
        )
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


def _event_for_resolution(db: DbSQLite, key: str) -> planning.PlannedEvent:
    event = planning.event_by_key(db, key)
    if event is None:
        raise CommandError(f"no scheduled occurrence matches {key!r}")
    if event.status is planning.EventStatus.ACTUALIZED:
        raise CommandError(f"scheduled occurrence {key!r} is already resolved")
    return event


def cmd_plan_resolve(args: argparse.Namespace) -> int:
    """Resolve one actual transaction against a selected scheduled occurrence."""
    db = open_book(args.book)
    try:
        transaction = _find_transaction(db, args.transaction)
        event = _event_for_resolution(db, args.occurrence)
        planning.actualize_transaction(transaction, event)
        with db.transaction("Resolve transaction against planned occurrence") as txn:
            db.commit_transaction(transaction, txn)
        emit(
            {
                "transaction": transaction.handle,
                "resolution": transaction.planning_resolution.value,
                "occurrence": event.key,
                "planned_for": event.planned_date,
                "planned_amount": event.expected_amount,
            },
            args,
            f"Matched {transaction.handle[:8]} to {event.key}",
        )
        return 0
    finally:
        db.close()


def cmd_plan_reject(args: argparse.Namespace) -> int:
    """Persistently reject one suggested occurrence for an unresolved actual."""
    db = open_book(args.book)
    try:
        transaction = _find_transaction(db, args.transaction)
        event = _event_for_resolution(db, args.occurrence)
        planning.reject_candidate(transaction, event)
        with db.transaction("Reject planned occurrence candidate") as txn:
            db.commit_transaction(transaction, txn)
        emit(
            {
                "transaction": transaction.handle,
                "rejected_occurrence": event.key,
                "rejected": list(transaction.rejected_plan_occurrences),
            },
            args,
            f"Rejected {event.key} for {transaction.handle[:8]}",
        )
        return 0
    finally:
        db.close()


def cmd_plan_unexpected(args: argparse.Namespace) -> int:
    """Explicitly declare an actual transaction to have no planned occurrence."""
    db = open_book(args.book)
    try:
        transaction = _find_transaction(db, args.transaction)
        planning.mark_unexpected(transaction)
        with db.transaction("Mark transaction unexpected") as txn:
            db.commit_transaction(transaction, txn)
        emit(
            {
                "transaction": transaction.handle,
                "resolution": transaction.planning_resolution.value,
            },
            args,
            f"Marked {transaction.handle[:8]} as unexpected",
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
            print(f"Scenario: {scenario.name}  ({scenario.basis.value} basis, "
                  f"{scenario.years} years from {scenario.start})")
            print(f"  income growth {scenario.assumptions.income_growth:.1%}   "
                  f"expense inflation {scenario.assumptions.expense_inflation:.1%}   "
                  f"investment return {scenario.assumptions.investment_return:.1%}")
            print()
        emit({"summary": summary, "rows": [r.as_dict() for r in result.rows]}, args, text)

        if not args.json:
            shortfall = result.first_shortfall()
            print()
            if shortfall:
                print(f"  Cash runs out in {shortfall.label} "
                      f"({shortfall.cash_close.format(parens_negative=True)})")
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

    budgets = list(db.iter_budgets())
    if args.budget:
        budgets = [b for b in budgets if b.name == args.budget]
        if not budgets:
            raise CommandError(f"no budget named {args.budget!r}")
    return Scenario(
        name=args.name or "ad hoc",
        start=parse_date(args.start) or date.today().replace(day=1),
        years=args.years or 5,
        basis=ProjectionBasis(args.basis),
        budget=budgets[0].handle if budgets else None,
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
            emit(
                [
                    {
                        "name": s.name,
                        "years": s.years,
                        "basis": s.basis.value,
                        "income_growth": s.assumptions.income_growth,
                        "expense_inflation": s.assumptions.expense_inflation,
                        "investment_return": s.assumptions.investment_return,
                    }
                    for s in scenarios
                ],
                args,
                table(
                    [
                        [
                            s.name, str(s.years), s.basis.value,
                            f"{s.assumptions.income_growth:.1%}",
                            f"{s.assumptions.expense_inflation:.1%}",
                            f"{s.assumptions.investment_return:.1%}",
                        ]
                        for s in scenarios
                    ],
                    ["name", "years", "basis", "income", "inflation", "return"],
                    right={1, 3, 4, 5},
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
            scenario.basis = ProjectionBasis(args.basis)
            scenario.start = parse_date(args.start) or scenario.start
            scenario.assumptions = Assumptions(
                income_growth=args.income_growth,
                expense_inflation=args.inflation,
                investment_return=args.investment_return,
                cash_interest=args.cash_interest,
            )
            budgets = list(db.iter_budgets())
            if args.budget:
                match = [b for b in budgets if b.name == args.budget]
                if not match:
                    raise CommandError(f"no budget named {args.budget!r}")
                scenario.budget = match[0].handle
            elif budgets and scenario.budget is None:
                scenario.budget = budgets[0].handle
            with db.transaction(f"Save scenario {args.name}") as txn:
                if existing:
                    db.commit_scenario(scenario, txn)
                else:
                    db.add_scenario(scenario, txn)
            emit(
                {"name": scenario.name, "handle": scenario.handle},
                args,
                f"Saved scenario {scenario.name!r}",
            )
        elif args.action == "delete":
            to_delete = db.get_scenario_by_name(args.name)
            if to_delete is None:
                raise CommandError(f"no scenario named {args.name!r}")
            with db.transaction(f"Delete scenario {args.name}") as txn:
                db.remove_scenario(to_delete.handle, txn)
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
            db, args.output, account=account,
            start=parse_date(args.start), end=parse_date(args.end),
        )
        emit({"rows": count, "output": args.output}, args,
             f"Wrote {count} rows to {args.output}")
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
            args.source, account_guid=args.account,
            start=parse_date(args.start), end=parse_date(args.end),
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
                rows.append([
                    db.full_name(account), account.atype.value, account.group,
                    db.full_name(linked) if linked else "",
                    "" if account.atype is not AccountType.CREDIT
                    else ("monthly" if account.pays_in_full else "carries"),
                ])
            emit(
                [
                    {"name": r[0], "type": r[1], "group": r[2],
                     "linked_asset": r[3], "card": r[4]}
                    for r in rows
                ],
                args,
                table(rows, ["account", "type", "group", "linked asset", "card"]),
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
            with db.transaction(f"Add account {args.name}") as txn:
                db.add_account(account, txn)
                if args.opening:
                    equity = db.get_account_by_name("Equity:Opening Balances") or (
                        db.get_account_by_name("Equity")
                    )
                    if equity is None:
                        raise CommandError(
                            "no equity account to post the opening balance against"
                        )
                    db.add_transaction(
                        Transaction.simple(
                            parse_date(args.opening_date) or date.today(),
                            f"{args.name} opening balance",
                            account.handle, equity.handle, Money(args.opening),
                        ),
                        txn,
                    )
            emit(
                {"handle": account.handle, "name": db.full_name(account)},
                args,
                f"Added {db.full_name(account)} ({account.atype.value})",
            )
            return 0

        account = resolve_account(db, args.name or "")
        if args.action == "remove":
            with db.transaction(f"Remove account {account.name}") as txn:
                db.remove_account(account.handle, txn)
            emit({"removed": account.handle}, args,
                 f"Removed {db.full_name(account)}")
            return 0

        # edit
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
        with db.transaction(f"Edit account {account.name}") as txn:
            db.commit_account(account, txn)
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


def cmd_infer(args: argparse.Namespace) -> int:
    """Suggest relationships an imported book does not state."""
    db = open_book(args.book, "r" if not args.apply else "w")
    try:
        result = inference.infer_all(db)
        rows = [
            [
                s.account_name, s.field, str(s.value_label or s.value),
                f"{s.confidence:.0%}", s.reason,
            ]
            for s in result.suggestions
        ]
        if args.apply:
            chosen = [
                s for s in result.suggestions if s.confidence >= args.min_confidence
            ]
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
                {"account": s.account_name, "field": s.field, "value": s.value_label,
                 "confidence": s.confidence, "reason": s.reason}
                for s in result.suggestions
            ],
            args,
            table(rows, ["account", "setting", "value", "confidence", "because"])
            + ("\n\nNothing is changed until you pass --apply."
               if result.suggestions else ""),
        )
        return 0
    finally:
        db.close()


def cmd_budget_clone(args: argparse.Namespace) -> int:
    db = open_book(args.book)
    try:
        source = next((b for b in db.iter_budgets() if b.name == args.source), None)
        if source is None:
            raise CommandError(f"no budget named {args.source!r}")
        if any(b.name == args.name for b in db.iter_budgets()):
            raise CommandError(f"a budget named {args.name!r} already exists")
        copy = budgeting.clone_budget(db, source, args.name)
        emit(
            {"handle": copy.handle, "name": copy.name},
            args,
            f"Cloned {source.name!r} as {copy.name!r} with {len(copy.lines)} line(s)",
        )
        return 0
    finally:
        db.close()


def cmd_budget_use(args: argparse.Namespace) -> int:
    """Nominate the budget the dashboard and new projections follow."""
    db = open_book(args.book)
    try:
        budget = _budget_by_name(db, args.name)
        if budget is None:
            raise CommandError(f"no budget named {args.name!r}")
        budgeting.set_current_budget(db, budget)
        emit({"current": budget.name}, args, f"Now using budget {budget.name!r}")
        return 0
    finally:
        db.close()


def cmd_budget_member(args: argparse.Namespace) -> int:
    """Add or remove a scheduled flow from a budget."""
    db = open_book(args.book)
    try:
        budget = _budget_by_name(db, args.budget)
        if budget is None:
            raise CommandError(f"no budget named {args.budget!r}")
        sched = next(
            (s for s in db.iter_scheduled() if s.name == args.schedule), None
        )
        if sched is None:
            raise CommandError(f"no scheduled transaction named {args.schedule!r}")

        handles = [b.handle for b in db.iter_budgets()]
        if args.action == "add":
            sched.add_to_budget(budget.handle, handles)
        else:
            sched.remove_from_budget(budget.handle, handles)
        with db.transaction(f"{args.action} {sched.name} in {budget.name}") as txn:
            db.commit_scheduled(sched, txn)

        emit(
            {"schedule": sched.name, "budgets": sched.budgets},
            args,
            f"{sched.name!r} is now "
            f"{'in' if sched.in_budget(budget.handle) else 'out of'} {budget.name!r}",
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

        if args.json:
            emit(
                {
                    "summary": summary,
                    "groups": [
                        {
                            "name": g.name, "kind": g.kind, "total": g.total,
                            "value": g.value, "debt": g.debt, "equity": g.equity,
                            "loan_to_value": g.loan_to_value,
                            "accounts": [
                                {"name": n, "balance": b} for n, b in g.accounts
                            ],
                        }
                        for g in board.groups
                    ],
                    "bills": [
                        {
                            "name": b.name, "next_due": b.next_due,
                            "cycle_months": b.cycle_months, "amount": b.amount,
                            "monthly": b.monthly, "annual": b.annual,
                            "hold": b.hold(board.as_of), "estimate": b.estimate,
                        }
                        for b in board.bills
                    ],
                },
                args,
            )
            return 0

        print(f"Dashboard as at {board.as_of}\n")
        rows = []
        for group in board.groups:
            loan_to_value = group.loan_to_value
            equity = group.equity
            if (
                loan_to_value is not None
                and group.value is not None
                and group.debt is not None
                and equity is not None
            ):
                rows.append([
                    group.name,
                    group.value.format(), group.debt.format(),
                    equity.format(parens_negative=True),
                    f"{loan_to_value:.1%}",
                ])
            else:
                rows.append([
                    group.name, "", "",
                    group.total.format(parens_negative=True), "",
                ])
        print(table(rows, ["group", "value", "owed", "equity / total", "LTV"],
                    right={1, 2, 3, 4}))

        print()
        headline = [
            ["Net worth", summary["net_worth"].format(parens_negative=True)],
            ["Liquid", summary["liquid"].format()],
            [f"Needed within {config.liquidity_days} days",
             summary["required_liquid"].format(parens_negative=True)],
            ["Available", summary["available"].format(parens_negative=True)],
            [f"Emergency fund ({config.emergency_months} months)",
             summary["emergency_fund"].format()],
            ["Months covered", f"{summary['months_covered']}"],
            ["Outgoings, monthly", summary["monthly_outgoings"].format()],
            ["Income, monthly", summary["income_per_month"].format()],
        ]
        print(table(headline, ["measure", "amount"], right={1}))

        if board.bills:
            print()
            bill_rows = [
                [
                    bill.name[:32],
                    bill.next_due.isoformat(),
                    f"{bill.cycle_months:g}",
                    bill.amount.format(),
                    bill.monthly.format(),
                    bill.hold(board.as_of).format(),
                    bill.annual.format(),
                    "est" if bill.estimate else "",
                ]
                for bill in board.bills[: args.limit]
            ]
            print(table(
                bill_rows,
                ["bill", "next due", "cycle", "amount", "monthly", "hold", "annual", ""],
                right={3, 4, 5, 6},
            ))
            if len(board.bills) > args.limit:
                print(f"... and {len(board.bills) - args.limit} more")
        return 0
    finally:
        db.close()


def cmd_budget_new(args: argparse.Namespace) -> int:
    """Create a budget from the recurring flows the book already knows about."""
    db = open_book(args.book)
    try:
        if next((b for b in db.iter_budgets() if b.name == args.name), None):
            raise CommandError(
                f"a budget named {args.name!r} already exists; "
                "delete it or choose another name"
            )
        start = parse_date(args.start) or date(date.today().year, 1, 1)
        budget = budgeting.from_schedules(
            db, name=args.name, start=start, periods=args.periods,
            kind=args.kind, include_placeholders=not args.no_estimates,
            flows_only=not args.include_transfers,
        )
        if not budget.lines and not args.allow_empty:
            raise CommandError(
                "no scheduled transactions contribute to this budget. Import a book "
                "with schedules, or add estimates with 'breadsched estimate add'."
            )
        with db.transaction(f"Create budget {args.name}") as txn:
            db.add_budget(budget, txn)

        rows = []
        payload = []
        for handle, line in budget.lines.items():
            account = db.get_account(handle)
            name = db.full_name(account) if account else handle
            amounts = [line.amount(i) for i in range(budget.periods)]
            lumpy = len({a.to_decimal() for a in amounts}) > 1
            rows.append([
                name,
                budget.period_label(0),
                amounts[0].format(),
                line.total().format(),
                "varies" if lumpy else "level",
            ])
            payload.append({
                "account": name, "total": line.total(),
                "amounts": amounts, "varies": lumpy,
            })
        emit(
            {"budget": budget.name, "periods": budget.periods, "lines": payload},
            args,
            f"Created budget {budget.name!r} with {len(budget.lines)} line(s) "
            f"over {budget.periods} {budget.kind.value}(s)\n\n"
            + table(rows, ["account", "first period", "amount", "total", "shape"],
                    right={2, 3}),
        )
        return 0
    finally:
        db.close()


def cmd_estimate(args: argparse.Namespace) -> int:
    """Manage placeholder flows: recurring estimates that are never posted."""
    db = open_book(args.book if args.action != "list" else args.book,
                   "r" if args.action == "list" else "w")
    try:
        if args.action == "list":
            rows, payload = [], []
            for sched in db.iter_scheduled():
                if not sched.placeholder:
                    continue
                rows.append([
                    sched.name,
                    sched.recurrence.describe(),
                    sched.amount().format(),
                    "yes" if sched.enabled else "no",
                ])
                payload.append({
                    "name": sched.name, "frequency": sched.recurrence.describe(),
                    "amount": sched.amount(), "enabled": sched.enabled,
                })
            emit(payload, args,
                 table(rows, ["name", "frequency", "amount", "enabled"], right={2}))
            return 0

        if args.action == "remove":
            match = next(
                (s for s in db.iter_scheduled()
                 if s.placeholder and s.name == args.name), None
            )
            if match is None:
                raise CommandError(f"no estimate named {args.name!r}")
            with db.transaction(f"Remove estimate {args.name}") as txn:
                db.remove_scheduled(match.handle, txn)
            emit({"removed": args.name}, args, f"Removed estimate {args.name!r}")
            return 0

        account = resolve_account(db, args.account)
        funding = resolve_account(db, args.funded_from)
        recurrence = Recurrence(
            period=args.every, interval=args.interval,
            start=parse_date(args.start) or date.today().replace(day=1),
        )
        sched = budgeting.placeholder_schedule(
            name=args.name, account=account.handle, counter_account=funding.handle,
            amount=Money(args.amount), recurrence=recurrence,
        )
        with db.transaction(f"Add estimate {args.name}") as txn:
            db.add_scheduled(sched, txn)
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
    from ..web.server import serve

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


def cmd_budget_set(args: argparse.Namespace) -> int:
    db = open_book(args.book)
    try:
        budget = _budget_by_name(db, args.name)
        created = budget is None
        if budget is None:
            budget = Budget(
                name=args.name,
                start=parse_date(args.start) or date(date.today().year, 1, 1),
                periods=args.periods,
            )
        account = resolve_account(db, args.account)
        if args.period is None:
            budget.set_monthly(account.handle, args.amount)
        else:
            budget.set_amount(account.handle, args.period, args.amount)
        with db.transaction(f"Budget {account.name}") as txn:
            if created:
                db.add_budget(budget, txn)
            else:
                db.commit_budget(budget, txn)
        emit(
            {"budget": budget.name, "account": db.full_name(account), "amount": args.amount},
            args,
            f"{budget.name}: {db.full_name(account)} = {Money(args.amount).format()}"
            + (" every period" if args.period is None else f" in period {args.period}"),
        )
        return 0
    finally:
        db.close()


# --------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="breadsched",
        description="Cash-flow ledger, budget and multi-year projection tool.",
    )
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    subparsers = parser.add_subparsers(dest="command")

    def add(name: str, help_text: str, needs_book: bool = True) -> argparse.ArgumentParser:
        sub = subparsers.add_parser(name, help=help_text, description=help_text)
        if needs_book:
            sub.add_argument("book", help="path to the BreadSched book")
        sub.add_argument("--json", action="store_true", help="emit JSON instead of a table")
        sub.add_argument(
            "-v", "--verbose", action="count", default=0,
            help="log progress and warnings to stderr; repeat (-vv) for full detail",
        )
        sub.add_argument(
            "--debug", action="store_true",
            help="full detail, equivalent to -vv",
        )
        sub.add_argument(
            "--log-file", metavar="PATH",
            help="also write a complete debug log to PATH",
        )
        return sub

    init = add("init", "Create a new book with a starter chart of accounts")
    init.set_defaults(func=cmd_init)

    imp = add("import", "Import a GnuCash book")
    imp.add_argument("source", help="path to the GnuCash file")
    imp.add_argument("--format", help="force an importer instead of detecting one")
    imp.add_argument("--no-scheduled", action="store_true",
                     help="skip scheduled transactions")
    imp.add_argument("--max-warnings", type=int, default=10)
    imp.add_argument("--no-infer", action="store_true",
                     help="skip looking for loan/asset links and card settings")
    imp.add_argument("--infer-apply", action="store_true",
                     help="apply confident suggestions rather than only listing them")
    imp.set_defaults(func=cmd_import)

    backup = add("backup", "Create a consistent backup of a book")
    backup.add_argument("destination", help="path to write the backup")
    backup.add_argument("--overwrite", action="store_true", help="replace an existing backup")
    backup.set_defaults(func=cmd_backup)

    restore = add("restore", "Restore a verified backup", needs_book=False)
    restore.add_argument("source", help="path to the backup")
    restore.add_argument("destination", help="path to restore the book")
    restore.add_argument(
        "--overwrite", action="store_true",
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

    add_txn = add("add", "Post a two-split transaction")
    add_txn.add_argument("--date", default="today")
    add_txn.add_argument("--description", required=True)
    add_txn.add_argument("--from", required=True, dest="from",
                         help="account the money leaves")
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
    sched.add_argument("--all", action="store_true",
                       help="with --post, include manual schedules")
    sched.set_defaults(func=cmd_scheduled)

    budget = add("budget", "Budget versus actual")
    budget.add_argument("--name", help="which budget (defaults to the first)")
    budget.set_defaults(func=cmd_budget)

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
        "--budget",
        help="limit scheduled flows to members of this legacy budget",
    )
    activity_cmd.set_defaults(func=cmd_activity)

    plan_unresolved = add(
        "plan-unresolved",
        "List scheduled expectations that have not been resolved to actuals",
    )
    plan_unresolved.add_argument("--start", required=True, help="first date (YYYY-MM-DD)")
    plan_unresolved.add_argument("--end", required=True, help="last date (YYYY-MM-DD)")
    plan_unresolved.add_argument("--budget", help="limit occurrences to one legacy budget")
    plan_unresolved.set_defaults(func=cmd_plan_unresolved)

    plan_matches = add(
        "plan-matches",
        "Show planned occurrences that could match an actual transaction",
    )
    plan_matches.add_argument("transaction", help="transaction handle, or a unique prefix")
    plan_matches.add_argument("--window-days", type=int, default=7)
    plan_matches.add_argument("--budget", help="limit candidates to one legacy budget")
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

    budget_set = add("budget-set", "Set a budget amount for an account")
    budget_set.add_argument("--name", required=True, help="budget name")
    budget_set.add_argument("--account", required=True)
    budget_set.add_argument("--amount", required=True)
    budget_set.add_argument("--period", type=int, help="period index; omit to set all")
    budget_set.add_argument("--periods", type=int, default=12)
    budget_set.add_argument("--start")
    budget_set.set_defaults(func=cmd_budget_set)

    account = add("account", "Add, edit, list or remove accounts")
    account.add_argument("action", choices=["list", "add", "edit", "remove"])
    account.add_argument("--name", help="account name, or path when editing")
    account.add_argument("--rename")
    account.add_argument("--type", help="BANK, CREDIT, EXPENSE, ...")
    account.add_argument("--parent")
    account.add_argument("--description")
    account.add_argument("--code")
    account.add_argument("--placeholder", action="store_true")
    account.add_argument("--group", help="dashboard group this account belongs to")
    account.add_argument("--linked-asset", help="for a loan: the asset behind it")
    account.add_argument("--carries-balance", type=_boolean, metavar="yes|no",
                         help="for a credit card: whether a balance is carried")
    account.add_argument("--usual-payment", help="typical payment on a card")
    account.add_argument("--payment-day", type=int)
    account.add_argument("--opening", help="opening balance to post")
    account.add_argument("--opening-date")
    account.set_defaults(func=cmd_account)

    infer = add("infer", "Suggest loan/asset links and credit-card settings")
    infer.add_argument("--apply", action="store_true", help="write the suggestions")
    infer.add_argument("--min-confidence", type=float, default=0.6)
    infer.set_defaults(func=cmd_infer)

    clone = add("budget-clone", "Copy a budget, with its projection and members")
    clone.add_argument("source")
    clone.add_argument("name")
    clone.set_defaults(func=cmd_budget_clone)

    use = add("budget-use", "Choose the budget the dashboard follows")
    use.add_argument("name")
    use.set_defaults(func=cmd_budget_use)

    member = add("budget-member", "Add or remove a scheduled flow from a budget")
    member.add_argument("action", choices=["add", "remove"])
    member.add_argument("--budget", required=True)
    member.add_argument("--schedule", required=True)
    member.set_defaults(func=cmd_budget_member)

    dash = add("dashboard", "Overview of balances, bills and liquidity")
    dash.add_argument("--as-of")
    dash.add_argument("--liquidity-days", type=int,
                      help="days of bills that must be covered by cash")
    dash.add_argument("--emergency-months", type=int,
                      help="months of outgoings the emergency fund should cover")
    dash.add_argument("--limit", type=int, default=40, help="bills to list")
    dash.set_defaults(func=cmd_dashboard)

    budget_new = add("budget-new", "Create a budget from scheduled flows")
    budget_new.add_argument("--name", required=True)
    budget_new.add_argument("--start", help="first period (YYYY-MM-DD)")
    budget_new.add_argument("--periods", type=int, default=12)
    budget_new.add_argument(
        "--kind", default="month", choices=[k.value for k in PeriodKind],
        help="length of each budget period",
    )
    budget_new.add_argument(
        "--no-estimates", action="store_true",
        help="use only committed schedules, ignoring placeholder estimates",
    )
    budget_new.add_argument(
        "--include-transfers", action="store_true",
        help="also budget transfers and debt payments, not just income and expenses",
    )
    budget_new.add_argument("--allow-empty", action="store_true")
    budget_new.set_defaults(func=cmd_budget_new)

    estimate = add("estimate", "Recurring estimates that shape budgets but never post")
    estimate.add_argument("action", choices=["list", "add", "remove"])
    estimate.add_argument("--name")
    estimate.add_argument("--account", help="the income or expense account")
    estimate.add_argument(
        "--funded-from", help="the account the money moves to or from",
    )
    estimate.add_argument("--amount")
    estimate.add_argument(
        "--every", default="month", choices=[p.value for p in PeriodType],
        help="how often the flow recurs",
    )
    estimate.add_argument("--interval", type=int, default=1)
    estimate.add_argument("--start")
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
        sub.add_argument("--basis", default="scheduled",
                         choices=[b.value for b in ProjectionBasis])
        sub.add_argument("--budget", help="budget name to drive recurring amounts")

    project_cmd = add("project", "Run a multi-year projection")
    project_cmd.add_argument("--scenario", help="use a saved scenario")
    project_cmd.add_argument("--name", help="label for an ad hoc scenario")
    project_cmd.add_argument("--years", type=int)
    project_cmd.add_argument("--start")
    project_cmd.add_argument("--monthly", action="store_true",
                             help="month by month instead of yearly")
    project_cmd.add_argument("--csv", help="also write the monthly rows to this file")
    assumption_flags(project_cmd)
    project_cmd.set_defaults(func=cmd_project)

    scenario = add("scenario", "Save, list or delete projection scenarios")
    scenario.add_argument("action", choices=["list", "save", "delete"])
    scenario.add_argument("--name")
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

    gnucash = add("gnucash", "Read a GnuCash book directly, without importing",
                  needs_book=False)
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
        from .. import __version__

        print(f"breadsched {__version__}")
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
