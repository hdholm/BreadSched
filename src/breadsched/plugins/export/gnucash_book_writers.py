"""Write planned changes into a GnuCash book, SQLite or XML.

``write_sqlite`` applies every change in one ``BEGIN IMMEDIATE`` transaction;
``write_xml`` replaces only the touched ``gnc:transaction`` blocks, keeping every
other byte, and swaps the file in atomically. Backup, verification, and restore
belong to ``gnucash_writeback.apply_plan``.
"""

from __future__ import annotations

import gzip
import os
import re
import shutil
import sqlite3
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape, quoteattr

from .gnucash_source import (
    NEUTRAL_TIME,
    SourceBook,
    SourceSplit,
    WritebackError,
    book_spans,
    load_xml,
    parse_block,
    qualified,
    table_names,
    xml_namespaces,
    xml_text,
)
from .gnucash_writeback_plan import TargetSplit, TargetTxn, WritebackChange

__all__ = [
    "write_sqlite",
    "write_xml",
]


def _sql_stamp(when: date, style: str) -> str:
    if style == "compact":
        return when.strftime("%Y%m%d") + NEUTRAL_TIME.replace(":", "")
    return f"{when.isoformat()} {NEUTRAL_TIME}"


def _slot_ids(conn: sqlite3.Connection, owners: Iterable[str]) -> list[int]:
    """Every slot row owned by ``owners``, following nested frames."""
    ids: list[int] = []
    frontier = set(owners)
    seen: set[str] = set()
    while frontier:
        seen |= frontier
        marks = ",".join("?" * len(frontier))
        rows = conn.execute(
            f"SELECT id, slot_type, guid_val FROM slots WHERE obj_guid IN ({marks})",
            tuple(frontier),
        ).fetchall()
        ids.extend(row[0] for row in rows)
        frontier = {row[2] for row in rows if row[1] == 9 and row[2]} - seen
    return ids


def _delete_slots(conn: sqlite3.Connection, owners: Iterable[str]) -> None:
    ids = _slot_ids(conn, owners)
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        conn.execute(f"DELETE FROM slots WHERE id IN ({','.join('?' * len(chunk))})", chunk)


def _expect(cursor: sqlite3.Cursor, description: str) -> None:
    if cursor.rowcount != 1:
        raise WritebackError(
            "writeback.source.conflict",
            f"GnuCash no longer matches the preview for {description!r}",
        )


def _sql_split_row(target: TargetTxn, split: TargetSplit, style: str) -> tuple[Any, ...]:
    reconciled = _sql_stamp(split.reconcile_date, style) if split.reconcile_date else None
    return (
        split.guid,
        target.guid,
        split.account,
        split.memo,
        split.action,
        split.state,
        reconciled,
        split.value[0],
        split.value[1],
        split.quantity[0],
        split.quantity[1],
        None,
    )


def _sqlite_apply(conn: sqlite3.Connection, book: SourceBook, change: WritebackChange) -> None:
    operation = change.operation
    assert operation is not None
    style = book.date_style
    has_slots = "slots" in table_names(conn)
    if operation.action == "delete":
        splits = [row[0] for row in conn.execute(
            "SELECT guid FROM splits WHERE tx_guid=?", (operation.guid,)
        )]  # fmt: skip
        if has_slots:
            _delete_slots(conn, [operation.guid, *splits])
        conn.execute("DELETE FROM splits WHERE tx_guid=?", (operation.guid,))
        _expect(conn.execute("DELETE FROM transactions WHERE guid=?", (operation.guid,)),
                change.description)  # fmt: skip
        return
    target = operation.target
    assert target is not None
    currency_guid = book.currencies[target.currency][0]
    posted = _sql_stamp(target.post_date, style)
    if operation.action == "new":
        now = datetime.now(UTC)
        entered = (
            now.strftime("%Y%m%d%H%M%S")
            if style == "compact"
            else now.strftime("%Y-%m-%d %H:%M:%S")
        )
        conn.execute(
            "INSERT INTO transactions (guid, currency_guid, num, post_date, enter_date,"
            " description) VALUES (?,?,?,?,?,?)",
            (target.guid, currency_guid, target.num, posted, entered, target.description),
        )
        if has_slots:
            conn.execute(
                "INSERT INTO slots (obj_guid, name, slot_type, gdate_val) VALUES (?,?,?,?)",
                (target.guid, "date-posted", 10, target.post_date.strftime("%Y%m%d")),
            )
        existing: dict[str, SourceSplit] = {}
    else:
        _expect(
            conn.execute(
                "UPDATE transactions SET num=?, post_date=?, description=? WHERE guid=?",
                (target.num, posted, target.description, target.guid),
            ),
            change.description,
        )
        if has_slots:
            conn.execute(
                "UPDATE slots SET gdate_val=? WHERE obj_guid=? AND name='date-posted'",
                (target.post_date.strftime("%Y%m%d"), target.guid),
            )
        existing = book.transactions[target.guid].splits
    wanted = {split.guid for split in target.splits}
    for guid in existing:
        if guid not in wanted:
            if has_slots:
                _delete_slots(conn, [guid])
            _expect(conn.execute("DELETE FROM splits WHERE guid=?", (guid,)), change.description)
    for split in target.splits:
        if split.guid not in existing:
            conn.execute(
                "INSERT INTO splits (guid, tx_guid, account_guid, memo, action,"
                " reconcile_state, reconcile_date, value_num, value_denom, quantity_num,"
                " quantity_denom, lot_guid) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                _sql_split_row(target, split, style),
            )
            continue
        assignments = (
            "account_guid=?, memo=?, action=?, reconcile_state=?, value_num=?,"
            " value_denom=?, quantity_num=?, quantity_denom=?"
        )
        params: list[Any] = [
            split.account,
            split.memo,
            split.action,
            split.state,
            split.value[0],
            split.value[1],
            split.quantity[0],
            split.quantity[1],
        ]
        if split.reconcile_date is not None:
            assignments += ", reconcile_date=?"
            params.append(_sql_stamp(split.reconcile_date, style))
        _expect(
            conn.execute(
                f"UPDATE splits SET {assignments} WHERE guid=? AND tx_guid=?",
                (*params, split.guid, target.guid),
            ),
            change.description,
        )


def write_sqlite(path: Path, book: SourceBook, selected: tuple[WritebackChange, ...]) -> None:
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        if (
            "gnclock" in table_names(conn)
            and conn.execute("SELECT COUNT(*) FROM gnclock").fetchone()[0]
        ):
            raise WritebackError(
                "writeback.source.locked", "GnuCash has the book open; close it in GnuCash first"
            )
        for change in selected:
            _sqlite_apply(conn, book, change)
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def _prefixed(tag: str, prefixes: dict[str, str]) -> str:
    if tag.startswith("{"):
        uri, local = tag[1:].split("}", 1)
        return f"{prefixes[uri]}:{local}"
    return tag


def _serialize(element: ET.Element, prefixes: dict[str, str], depth: int = 0) -> str:
    """Write an element the way GnuCash lays out its XML: two-space indents."""
    pad = "  " * depth
    tag = _prefixed(element.tag, prefixes)
    attrs = "".join(
        f" {_prefixed(key, prefixes)}={quoteattr(value)}" for key, value in element.attrib.items()
    )
    children = list(element)
    if children:
        inner = "".join(_serialize(child, prefixes, depth + 1) for child in children)
        return f"{pad}<{tag}{attrs}>\n{inner}{pad}</{tag}>\n"
    if element.text:
        return f"{pad}<{tag}{attrs}>{escape(element.text)}</{tag}>\n"
    return f"{pad}<{tag}{attrs}/>\n"


def _xml_stamp(when: date) -> str:
    return f"{when.isoformat()} {NEUTRAL_TIME} +0000"


def _set_child(
    parent: ET.Element, tag: str, text: str | None, order: list[str], *, keep_empty=False
) -> ET.Element | None:
    """Set, add (in GnuCash's element order), or remove a leaf child."""
    found = parent.find(tag)
    if not text and not keep_empty:
        if found is not None:
            parent.remove(found)
        return None
    if found is None:
        found = ET.Element(tag)
        position = order.index(tag)
        index = len(parent)
        for i, child in enumerate(parent):
            if child.tag in order and order.index(child.tag) > position:
                index = i
                break
        parent.insert(index, found)
    found.text = text
    return found


def _ts(parent: ET.Element, ns: dict[str, str], when: date) -> None:
    for child in list(parent):
        parent.remove(child)
    stamp = ET.SubElement(parent, qualified(ns, "ts:date"))
    stamp.text = _xml_stamp(when)


def _xml_split(element: ET.Element | None, split: TargetSplit, ns: dict[str, str]) -> ET.Element:
    q = lambda name: qualified(ns, name)  # noqa: E731
    order = [
        q("split:id"),
        q("split:memo"),
        q("split:action"),
        q("split:reconciled-state"),
        q("split:reconcile-date"),
        q("split:value"),
        q("split:quantity"),
        q("split:account"),
        q("split:lot"),
        q("split:slots"),
    ]
    if element is None:
        element = ET.Element(q("trn:split"))
        guid = ET.SubElement(element, q("split:id"), {"type": "guid"})
        guid.text = split.guid
    _set_child(element, q("split:memo"), split.memo, order)
    _set_child(element, q("split:action"), split.action, order)
    _set_child(element, q("split:reconciled-state"), split.state, order, keep_empty=True)
    if split.reconcile_date is not None:
        holder = element.find(q("split:reconcile-date"))
        if holder is None:
            holder = _set_child(element, q("split:reconcile-date"), "", order, keep_empty=True)
        assert holder is not None
        holder.text = None
        _ts(holder, ns, split.reconcile_date)
    _set_child(element, q("split:value"), f"{split.value[0]}/{split.value[1]}", order)
    _set_child(element, q("split:quantity"), f"{split.quantity[0]}/{split.quantity[1]}", order)
    account = _set_child(element, q("split:account"), split.account, order)
    assert account is not None
    account.attrib["type"] = "guid"
    return element


def _date_slot(element: ET.Element, ns: dict[str, str], when: date, create: bool) -> None:
    slots = element.find("trn:slots", ns)
    for slot in slots.findall("slot", ns) if slots is not None else []:
        key = slot.find("slot:key", ns)
        if key is not None and key.text == "date-posted":
            gdate = slot.find("slot:value/gdate", ns)
            if gdate is not None:
                gdate.text = when.isoformat()
            return
    if not create:
        return
    if slots is None:
        slots = ET.Element(qualified(ns, "trn:slots"))
        splits = element.find("trn:splits", ns)
        index = list(element).index(splits) if splits is not None else len(element)
        element.insert(index, slots)
    slot = ET.SubElement(slots, "slot")
    ET.SubElement(slot, qualified(ns, "slot:key")).text = "date-posted"
    value = ET.SubElement(slot, qualified(ns, "slot:value"), {"type": "gdate"})
    ET.SubElement(value, "gdate").text = when.isoformat()


def _xml_transaction_element(
    element: ET.Element | None, target: TargetTxn, ns: dict[str, str]
) -> ET.Element:
    q = lambda name: qualified(ns, name)  # noqa: E731
    order = [
        q("trn:id"),
        q("trn:currency"),
        q("trn:num"),
        q("trn:date-posted"),
        q("trn:date-entered"),
        q("trn:description"),
        q("trn:slots"),
        q("trn:splits"),
    ]
    creating = element is None
    if element is None:
        element = ET.Element(q("gnc:transaction"), {"version": "2.0.0"})
        ET.SubElement(element, q("trn:id"), {"type": "guid"}).text = target.guid
        currency = ET.SubElement(element, q("trn:currency"))
        ET.SubElement(currency, q("cmdty:space")).text = "CURRENCY"
        ET.SubElement(currency, q("cmdty:id")).text = target.currency
        posted = ET.SubElement(element, q("trn:date-posted"))
        _ts(posted, ns, target.post_date)
        entered = ET.SubElement(element, q("trn:date-entered"))
        stamp = ET.SubElement(entered, q("ts:date"))
        stamp.text = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S +0000")
        ET.SubElement(element, q("trn:description"))
        ET.SubElement(element, q("trn:splits"))
    _set_child(element, q("trn:num"), target.num, order)
    posted_holder = element.find(q("trn:date-posted"))
    assert posted_holder is not None
    _ts(posted_holder, ns, target.post_date)
    _set_child(element, q("trn:description"), target.description, order, keep_empty=True)
    _date_slot(element, ns, target.post_date, creating)
    splits = element.find(q("trn:splits"))
    assert splits is not None
    current = {xml_text(split, "split:id", ns): split for split in splits.findall(q("trn:split"))}
    wanted = {split.guid for split in target.splits}
    for guid, split_element in current.items():
        if guid not in wanted:
            splits.remove(split_element)
    for split in target.splits:
        existing = current.get(split.guid)
        updated = _xml_split(existing, split, ns)
        if existing is None:
            splits.append(updated)
    return element


def write_xml(path: Path, book: SourceBook, selected: tuple[WritebackChange, ...]) -> None:
    text, gzipped = load_xml(path)
    # Written blocks follow the book's own line endings, so a book saved with
    # CRLF does not end up with mixed ones.
    newline = "\r\n" if "\r\n" in text else "\n"

    def serialize(element: ET.Element) -> str:
        return _serialize(element, prefixes).replace("\n", newline)

    ns = xml_namespaces(text)
    prefixes = {uri: prefix for prefix, uri in ns.items()}
    spans = {}
    for start, end in book_spans(text):
        found = re.search(r'<trn:id type="guid">([0-9a-f]+)</trn:id>', text[start:end])
        if found:
            spans[found.group(1)] = (start, end)
    edits: list[tuple[int, int, str]] = []
    added: list[str] = []
    delta = 0
    for change in selected:
        operation = change.operation
        assert operation is not None
        if operation.action == "new":
            assert operation.target is not None
            element = _xml_transaction_element(None, operation.target, ns)
            added.append(serialize(element))
            delta += 1
            continue
        if operation.guid not in spans:
            raise WritebackError(
                "writeback.source.conflict",
                f"GnuCash no longer matches the preview for {change.description!r}",
            )
        start, end = spans[operation.guid]
        if operation.action == "delete":
            edits.append((start, end, ""))
            delta -= 1
            continue
        assert operation.target is not None
        element = parse_block(text[start:end], ns)
        _xml_transaction_element(element, operation.target, ns)
        edits.append((start, end, serialize(element)))
    if added:
        anchor = max((end for _start, end in spans.values()), default=None)
        if anchor is None:
            anchor = _insert_point(text)
        edits.append((anchor, anchor, "".join(added)))
    for start, end, replacement in sorted(edits, key=lambda item: item[0], reverse=True):
        text = text[:start] + replacement + text[end:]
    if delta:
        text = _recount(text, delta, newline)
    payload = text.encode("utf-8")
    if gzipped:
        payload = gzip.compress(payload, mtime=0)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(payload)
            out.flush()
            os.fsync(out.fileno())
        shutil.copymode(path, temporary)
        os.replace(temporary, path)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise


def _insert_point(text: str) -> int:
    """Where the first transaction goes when the book has none yet."""
    for marker in (
        "<gnc:template-transactions>",
        '<gnc:schedxaction version="2.0.0">',
        '<gnc:budget version="2.0.0">',
        "</gnc:book>",
    ):
        position = text.find(marker)
        if position != -1:
            return position
    raise WritebackError("writeback.source.unreadable", "the GnuCash XML book has no book element")


def _recount(text: str, delta: int, newline: str = "\n") -> str:
    pattern = re.compile(r'(<gnc:count-data cd:type="transaction">)(\d+)(</gnc:count-data>)')
    found = pattern.search(text)
    if found is None:
        if delta > 0:
            book = re.search(r'<book:id type="guid">[0-9a-f]+</book:id>\r?\n', text)
            if book is not None:
                line = f'<gnc:count-data cd:type="transaction">{delta}</gnc:count-data>{newline}'
                return text[: book.end()] + line + text[book.end() :]
        return text
    count = max(0, int(found.group(2)) + delta)
    return text[: found.start(2)] + str(count) + text[found.end(2) :]
