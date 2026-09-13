"""Import a GnuCash SQLite3 book.

The source is opened read-only through a ``file:...?mode=ro`` URI: this reads a
book GnuCash itself may have open, and can never write to it. The tables consumed
include commodities, dated prices, accounts, transactions, splits, slots, and
scheduled-transaction definitions.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

from ...gen.db.sqlite import DbSQLite
from ...gen.lib.money import Money
from ...gen.lib.recurrence import PeriodType, Recurrence, WeekendAdjust
from ...gen.lib.scheduled import ScheduledSplit, ScheduledTransaction
from ...gen.utils.logs import get_logger
from .gnucash_common import (
    ImportResult,
    ImportSink,
    balance_template_splits,
    money_from_pair,
    open_gnucash_sqlite,
    parse_gnc_date,
)

__all__ = ["import_book", "read_accounts", "read_transactions", "PERIOD_MAP"]

LOG = get_logger(__name__)

#: GnuCash ``recurrences.recurrence_period_type`` -> our period type.
PERIOD_MAP = {
    "once": PeriodType.ONCE,
    "day": PeriodType.DAY,
    "week": PeriodType.WEEK,
    "semi_month": PeriodType.SEMI_MONTH,
    "end of month": PeriodType.MONTH,
    "month": PeriodType.MONTH,
    "year": PeriodType.YEAR,
}

_WEEKEND_MAP = {
    "none": WeekendAdjust.NONE,
    "back": WeekendAdjust.PREVIOUS,
    "forward": WeekendAdjust.NEXT,
}


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


# ------------------------------------------------------------------- readers
# These are usable on their own, in the spirit of gnucash-cli: point them at a
# book and get plain dictionaries back, with no BreadSched database involved.


def read_accounts(path: str | Path, include_templates: bool = False) -> list[dict]:
    """Every account in a GnuCash book, as dictionaries with full paths."""
    conn = open_gnucash_sqlite(path)
    try:
        excluded = set() if include_templates else _template_account_guids(conn)
        rows = {
            row["guid"]: dict(row)
            for row in conn.execute("SELECT * FROM accounts")
            if row["guid"] not in excluded
        }
    finally:
        conn.close()

    def full_name(guid: str) -> str:
        parts: list[str] = []
        seen: set[str] = set()
        node = rows.get(guid)
        while node and node.get("parent_guid") and node["guid"] not in seen:
            seen.add(node["guid"])
            parts.append(node["name"])
            node = rows.get(node["parent_guid"])
        return ":".join(reversed(parts))

    return [
        {
            "guid": row["guid"],
            "name": row["name"],
            "full_name": full_name(guid),
            "type": row["account_type"],
            "parent": row.get("parent_guid"),
            "code": row.get("code") or "",
            "description": row.get("description") or "",
            "placeholder": bool(row.get("placeholder")),
            "hidden": bool(row.get("hidden")),
        }
        for guid, row in rows.items()
    ]


def read_transactions(
    path: str | Path,
    account_guid: str | None = None,
    start: date | None = None,
    end: date | None = None,
    include_templates: bool = False,
) -> list[dict]:
    """Transactions with their splits, optionally filtered by account and date.

    Scheduled-transaction templates are hidden by default: they live in the same
    table as real postings but describe things that have not happened.
    """
    conn = open_gnucash_sqlite(path)
    try:
        excluded = set() if include_templates else _template_account_guids(conn)
        sql = "SELECT DISTINCT t.* FROM transactions t"
        params: list = []
        if account_guid:
            sql += " JOIN splits s ON s.tx_guid = t.guid WHERE s.account_guid = ?"
            params.append(account_guid)
        else:
            sql += " WHERE 1=1"
        if start:
            sql += " AND t.post_date >= ?"
            params.append(start.strftime("%Y%m%d000000"))
        if end:
            sql += " AND t.post_date <= ?"
            params.append(end.strftime("%Y%m%d235959"))
        sql += " ORDER BY t.post_date"

        out: list[dict] = []
        for row in conn.execute(sql, params):
            raw_splits = list(
                conn.execute("SELECT * FROM splits WHERE tx_guid = ?", (row["guid"],))
            )
            if raw_splits and all(s["account_guid"] in excluded for s in raw_splits):
                continue
            splits = [
                {
                    "guid": s["guid"],
                    "account": s["account_guid"],
                    "memo": s["memo"] or "",
                    "value": str(money_from_pair(s["value_num"], s["value_denom"]).to_decimal()),
                    "reconcile": s["reconcile_state"],
                }
                for s in raw_splits
            ]
            out.append(
                {
                    "guid": row["guid"],
                    "date": parse_gnc_date(row["post_date"]).isoformat(),
                    "description": row["description"] or "",
                    "num": row["num"] or "",
                    "splits": splits,
                }
            )
        return out
    finally:
        conn.close()


# -------------------------------------------------------------------- import


def import_book(
    db: DbSQLite,
    path: str | Path,
    include_scheduled: bool = True,
    message: str | None = None,
    progress=None,
) -> ImportResult:
    """Copy a GnuCash SQLite book into an open BreadSched database."""
    result = ImportResult(source=str(path), source_format="sqlite")
    LOG.info("importing GnuCash SQLite book %s", path)
    conn = open_gnucash_sqlite(path)
    try:
        counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("accounts", "transactions", "splits")
            if _table_exists(conn, table)
        }
        LOG.debug("source book contains %s", counts)

        report = _reporter(progress, counts)
        with db.transaction(message or f"Import {Path(path).name}", batch=True) as txn:
            sink = ImportSink(db, txn, result)
            report("Reading commodities", 0)
            _import_commodities(conn, sink)
            LOG.debug("imported %d commodities", result.commodities)
            _import_prices(conn, sink)
            LOG.debug("imported %d prices", result.prices)
            report("Reading accounts", 0)
            _import_accounts(conn, sink)
            LOG.debug("imported %d accounts", result.accounts)
            _import_transactions(conn, sink, report)
            LOG.debug(
                "imported %d transactions, skipped %d",
                result.transactions,
                result.skipped,
            )
            if include_scheduled:
                report("Reading scheduled transactions", 0)
                _import_scheduled(conn, sink, db, txn)
            report("Finishing", counts.get("transactions", 0))
    finally:
        conn.close()
    LOG.info("import finished: %s", result.describe())
    db.emit("database-changed", (db,))
    return result


def _import_commodities(conn: sqlite3.Connection, sink: ImportSink) -> None:
    if not _table_exists(conn, "commodities"):
        return
    for row in conn.execute("SELECT * FROM commodities"):
        sink.commodity(
            namespace=row["namespace"],
            mnemonic=row["mnemonic"],
            fullname=row["fullname"] or "",
            fraction=row["fraction"] or 100,
            source_guid=row["guid"],
        )


def _import_prices(conn: sqlite3.Connection, sink: ImportSink) -> None:
    if not _table_exists(conn, "prices"):
        return
    for row in conn.execute("SELECT * FROM prices"):
        try:
            sink.price(
                guid=row["guid"],
                commodity=row["commodity_guid"],
                currency=row["currency_guid"],
                quote_date=parse_gnc_date(row["date"]),
                value=money_from_pair(row["value_num"], row["value_denom"]),
                source=row["source"] or "gnucash",
                quote_type=row["type"] or "last",
            )
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            sink.result.warn(f"price {row['guid'][:8]} was ignored: {exc}")


def book_roots(conn: sqlite3.Connection) -> tuple[str | None, str | None]:
    """The chart-of-accounts root and the template root, from the ``books`` table.

    This is the authoritative answer and the only reliable one. Inferring the
    template tree from ``schedxactions.template_act_guid`` misses any template
    account not currently referenced by a schedule, and GnuCash names those
    accounts after the schedule's GUID -- which is how a book ends up with
    thirty-two-character hexadecimal account names in its register.
    """
    if not _table_exists(conn, "books"):
        return None, None
    row = conn.execute("SELECT root_account_guid, root_template_guid FROM books LIMIT 1").fetchone()
    if row is None:
        return None, None
    return row["root_account_guid"], row["root_template_guid"]


def _template_account_guids(conn: sqlite3.Connection) -> set[str]:
    """Accounts belonging to GnuCash's hidden scheduled-transaction template tree.

    GnuCash keeps template transactions in the same ``transactions`` and ``splits``
    tables as real ones, distinguished only by hanging off a separate ROOT account.
    Importing them blind adds a phantom branch of GUID-named accounts to the chart
    of accounts and shows every scheduled transaction as though it had happened.
    """
    parent_of: dict[str, str | None] = {}
    children: dict[str | None, list[str]] = {}
    for row in conn.execute("SELECT guid, parent_guid FROM accounts"):
        parent_of[row["guid"]] = row["parent_guid"]
        children.setdefault(row["parent_guid"], []).append(row["guid"])

    _, template_root = book_roots(conn)
    roots: set[str] = {template_root} if template_root in parent_of else set()

    # Fall back to the schedule references for books with no usable books table.
    if not roots and _table_exists(conn, "schedxactions"):
        roots = {
            row["template_act_guid"]
            for row in conn.execute("SELECT template_act_guid FROM schedxactions")
            if row["template_act_guid"] and row["template_act_guid"] in parent_of
        }
    if not roots:
        return set()

    found: set[str] = set()
    for guid in roots:  # the template root itself and everything above it
        node: str | None = guid
        while node and node in parent_of and node not in found:
            found.add(node)
            node = parent_of[node]
    stack = list(roots)  # and everything below it
    while stack:
        current = stack.pop()
        if current in found and current not in roots:
            continue
        found.add(current)
        stack.extend(children.get(current, []))
    return found


def _import_accounts(conn: sqlite3.Connection, sink: ImportSink) -> None:
    excluded = _template_account_guids(conn)
    rows = {
        row["guid"]: dict(row)
        for row in conn.execute("SELECT * FROM accounts")
        if row["guid"] not in excluded
    }
    columns = _columns(conn, "accounts")

    # Parents before children, so the tree is never momentarily orphaned.
    ordered: list[dict] = []
    placed: set[str] = set()
    pending = list(rows.values())
    while pending:
        progressed = False
        remaining: list[dict] = []
        for row in pending:
            parent = row.get("parent_guid")
            if parent is None or parent in placed or parent not in rows:
                ordered.append(row)
                placed.add(row["guid"])
                progressed = True
            else:
                remaining.append(row)
        pending = remaining
        if not progressed:  # a cycle in the source book
            ordered.extend(pending)
            break

    for row in ordered:
        commodity = None
        if row.get("commodity_guid"):
            source = conn.execute(
                "SELECT namespace, mnemonic, fullname, fraction FROM commodities WHERE guid=?",
                (row["commodity_guid"],),
            ).fetchone()
            if source is not None:
                commodity = sink.commodity(
                    source["namespace"],
                    source["mnemonic"],
                    source["fullname"] or "",
                    source["fraction"] or 100,
                )
        notes = ""
        if _table_exists(conn, "slots"):
            note_row = conn.execute(
                "SELECT string_val FROM slots "
                "WHERE obj_guid = ? AND name = 'notes' ORDER BY id LIMIT 1",
                (row["guid"],),
            ).fetchone()
            if note_row is not None and note_row["string_val"]:
                notes = note_row["string_val"]
        LOG.debug(
            "account %s (%s) parent=%s",
            row["name"],
            row["account_type"],
            (row.get("parent_guid") or "-")[:8],
        )
        sink.account(
            guid=row["guid"],
            name=row["name"],
            atype=row["account_type"],
            parent=row.get("parent_guid"),
            commodity=commodity,
            code=(row.get("code") or "") if "code" in columns else "",
            description=(row.get("description") or "") if "description" in columns else "",
            notes=notes,
            placeholder=bool(row.get("placeholder")),
            hidden=bool(row.get("hidden")),
            commodity_scu=(
                int(row["commodity_scu"])
                if "commodity_scu" in columns and row.get("commodity_scu") is not None
                else None
            ),
        )


def _reporter(progress, counts: dict):
    """Wrap a caller's progress callback so importers need not care if it is None.

    The total is the transaction count, which dominates the time on any real book;
    counting accounts as well would make the bar jump and then stall.
    """
    total = max(1, counts.get("transactions", 0))

    def report(stage: str, done: int) -> None:
        if progress is None:
            return
        try:
            progress(stage, min(done, total), total)
        except Exception:  # noqa: BLE001 - a broken meter must not stop the import
            LOG.debug("progress callback failed", exc_info=True)

    return report


def _import_transactions(conn: sqlite3.Connection, sink: ImportSink, report=None) -> None:
    excluded = _template_account_guids(conn)
    if excluded:
        LOG.debug("excluding %d template account(s) from the ledger", len(excluded))
    splits_by_txn: dict[str, list[dict]] = {}
    template_txns: set[str] = set()
    for row in conn.execute("SELECT * FROM splits"):
        if row["account_guid"] in excluded:
            template_txns.add(row["tx_guid"])
            continue
        splits_by_txn.setdefault(row["tx_guid"], []).append(
            {
                "handle": row["guid"],
                "account": row["account_guid"],
                "value": money_from_pair(row["value_num"], row["value_denom"]),
                "quantity": money_from_pair(row["quantity_num"], row["quantity_denom"]),
                "memo": row["memo"] or "",
                "action": row["action"] or "",
                "reconcile": row["reconcile_state"],
            }
        )

    done = 0
    for row in conn.execute("SELECT * FROM transactions ORDER BY post_date"):
        done += 1
        if report is not None and done % 200 == 0:
            report("Reading transactions", done)
        if row["guid"] in template_txns:
            LOG.debug("skipping template transaction %s", row["guid"][:8])
            continue
        if row["guid"] not in splits_by_txn:
            LOG.debug(
                "transaction %s (%s) has no usable splits in the source",
                row["guid"][:8],
                row["description"] or "",
            )
        try:
            post_date = parse_gnc_date(row["post_date"])
        except ValueError as exc:
            sink.result.skip(
                str(exc),
                f"transaction {row['description'] or row['guid'][:8]!r}",
            )
            continue
        sink.transaction(
            guid=row["guid"],
            post_date=post_date,
            description=row["description"] or "",
            currency=row["currency_guid"],
            num=row["num"] or "",
            splits=splits_by_txn.get(row["guid"], []),
        )


def _import_scheduled(conn: sqlite3.Connection, sink: ImportSink, db: DbSQLite, txn) -> None:
    """Read ``schedxactions`` plus the template transactions they point at."""
    if not (_table_exists(conn, "schedxactions") and _table_exists(conn, "recurrences")):
        return

    for row in conn.execute("SELECT * FROM schedxactions"):
        recurrence = conn.execute(
            "SELECT * FROM recurrences WHERE obj_guid=? LIMIT 1", (row["guid"],)
        ).fetchone()
        if recurrence is None:
            continue
        period = PERIOD_MAP.get(
            (recurrence["recurrence_period_type"] or "month").lower(), PeriodType.MONTH
        )
        try:
            start = parse_gnc_date(recurrence["recurrence_period_start"])
            end = parse_gnc_date(row["end_date"]) if row["end_date"] else None
        except ValueError as exc:
            sink.result.skip(
                str(exc),
                f"scheduled transaction {row['name'] or row['guid'][:8]!r}",
            )
            continue
        weekend = _WEEKEND_MAP.get(
            (
                recurrence["recurrence_weekend_adjust"]
                if "recurrence_weekend_adjust" in recurrence.keys()
                else "none"
            )
            or "none",
            WeekendAdjust.NONE,
        )
        day_of_month = (
            -1 if "end of month" in (recurrence["recurrence_period_type"] or "").lower() else None
        )

        sched = ScheduledTransaction(
            handle=row["guid"],
            name=row["name"] or "Scheduled transaction",
            recurrence=Recurrence(
                period=period,
                interval=recurrence["recurrence_mult"] or 1,
                start=start,
                end=end,
                count=row["num_occur"] if (row["num_occur"] or 0) > 0 else None,
                day_of_month=day_of_month,
                weekend_adjust=weekend,
            ),
            enabled=_source_flag(row["enabled"]),
            auto_create=_source_flag(row["auto_create"]),
            advance_days=row["adv_creation"] or 0,
        )
        sched.splits = _template_splits(conn, sink, row["template_act_guid"])
        if not sched.splits:
            # Without a usable template the schedule is a name and a rule; keep it
            # so the user can see it, but it contributes nothing to a forecast.
            sink.result.warn(
                f"scheduled transaction {sched.name!r} has no readable template splits"
            )
        balance_template_splits(sched, sink.result)
        db.add_scheduled(sched, txn)
        sink.result.scheduled += 1


def _source_flag(value: object) -> bool:
    """Read a source boolean even when SQLite retained a textual flag."""
    if isinstance(value, str):
        return value.strip().lower() in {"1", "y", "yes", "true"}
    return bool(value)


def _template_splits(
    conn: sqlite3.Connection, sink: ImportSink, template_account: str | None
) -> list[ScheduledSplit]:
    """Resolve a template account's splits into real account references.

    GnuCash keeps scheduled transaction templates under a hidden account tree, and
    stores the *real* target account as a slot on each template split.  Without
    following that indirection every scheduled transaction would point at the
    template placeholder instead of the account the money moves through.
    """
    if not template_account or not _table_exists(conn, "slots"):
        return []
    rows = conn.execute(
        "SELECT s.* FROM splits s JOIN transactions t ON t.guid = s.tx_guid"
        " WHERE s.account_guid IN ("
        "   SELECT guid FROM accounts WHERE parent_guid = ? OR guid = ?)",
        (template_account, template_account),
    ).fetchall()

    splits: list[ScheduledSplit] = []
    for row in rows:
        target = None
        formula = ""
        amount: Money | None = money_from_pair(row["value_num"], row["value_denom"])
        for slot in conn.execute(
            "SELECT name, string_val, guid_val FROM slots WHERE obj_guid = ?", (row["guid"],)
        ):
            name = (slot["name"] or "").lower()
            if name.endswith("account"):
                target = slot["guid_val"]
            elif name.endswith(("credit-formula", "debit-formula")) and slot["string_val"]:
                formula = slot["string_val"]
                if name.endswith("credit-formula"):
                    formula = f"-({formula})"
                amount = None
        if target and sink.has_account(target):
            splits.append(
                ScheduledSplit(
                    account=sink.resolve(target),
                    amount=amount,
                    formula=formula if _is_simple_formula(formula) else "",
                    memo=row["memo"] or "",
                )
            )
    return splits


def _is_simple_formula(text: str) -> bool:
    """GnuCash formulas may reference other splits; only arithmetic is portable."""
    if not text:
        return False
    return all(ch.isdigit() or ch in "+-*/(). " for ch in text)
