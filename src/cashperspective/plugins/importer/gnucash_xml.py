"""Import a GnuCash XML book, compressed or not.

XML is still GnuCash's default save format, so an importer that only handled
SQLite would miss most books in the wild.  Parsing uses ``iterparse`` and clears
each element after use, so a decade-long book with a hundred thousand splits is
processed in constant memory rather than being materialised as a DOM.
"""

from __future__ import annotations

import gzip
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import IO, TypedDict, cast
from xml.etree import ElementTree as ET

from ...gen.db.sqlite import DbSQLite
from ...gen.lib.formula import FormulaError, evaluate
from ...gen.lib.money import Money
from ...gen.lib.recurrence import PeriodType, Recurrence, WeekendAdjust
from ...gen.lib.scheduled import ScheduledSplit, ScheduledTransaction
from ...gen.utils.logs import get_logger
from .gnucash_common import (
    ImportResult,
    ImportSink,
    balance_template_splits,
    money_from_fraction,
    parse_amount_text,
    parse_gnc_date,
)

__all__ = ["import_book", "NS"]

LOG = get_logger(__name__)

#: The namespaces GnuCash declares on its root element.
NS = {
    "gnc": "http://www.gnucash.org/XML/gnc",
    "act": "http://www.gnucash.org/XML/act",
    "trn": "http://www.gnucash.org/XML/trn",
    "split": "http://www.gnucash.org/XML/split",
    "cmdty": "http://www.gnucash.org/XML/cmdty",
    "ts": "http://www.gnucash.org/XML/ts",
    "slot": "http://www.gnucash.org/XML/slot",
    "sx": "http://www.gnucash.org/XML/sx",
    "recurrence": "http://www.gnucash.org/XML/recurrence",
}


def _open(path: str | Path) -> IO[bytes]:
    handle = open(path, "rb")
    magic = handle.read(2)
    handle.seek(0)
    if magic == b"\x1f\x8b":
        handle.close()
        return cast(IO[bytes], gzip.open(path, "rb"))
    return handle


def _text(node: ET.Element | None, path: str, default: str = "") -> str:
    if node is None:
        return default
    found = node.find(path, NS)
    return (found.text or default) if found is not None else default


#: Wrapper element holding the hidden accounts and transactions that back
#: scheduled transactions. Everything inside it describes what *would* happen.
TEMPLATE_SECTION = f"{{{NS['gnc']}}}template-transactions"


def _iter_top_level(stream: IO[bytes]) -> Iterator[tuple[ET.Element, bool]]:
    """Yield ``(element, is_template)`` for each interesting element, then free it.

    ``iterparse`` gives the element on its closing tag; clearing it there is what
    keeps memory flat. The root's children are also dropped, since ElementTree
    otherwise keeps every processed element attached to the root.

    The template flag matters enormously: GnuCash stores scheduled-transaction
    templates as ordinary ``gnc:account`` and ``gnc:transaction`` elements nested
    inside ``gnc:template-transactions``, and names those accounts after the
    schedule's GUID. Treating them as real is how a register fills up with
    thirty-two-character hexadecimal account names for transactions that have not
    happened.
    """
    wanted = {
        f"{{{NS['gnc']}}}account",
        f"{{{NS['gnc']}}}transaction",
        f"{{{NS['gnc']}}}commodity",
        f"{{{NS['gnc']}}}schedxaction",
    }
    context = ET.iterparse(stream, events=("start", "end"))
    _, root = next(context)
    depth = 0
    for event, element in context:
        if element.tag == TEMPLATE_SECTION:
            depth += 1 if event == "start" else -1
            continue
        if event != "end" or element.tag not in wanted:
            continue
        yield element, depth > 0
        if depth == 0:
            element.clear()
            root.clear()


def import_book(
    db: DbSQLite,
    path: str | Path,
    include_scheduled: bool = True,
    message: str | None = None,
    progress=None,
) -> ImportResult:
    """Copy a GnuCash XML book into an open CashPerspective database.

    The file is walked twice: once for commodities and accounts, once for
    transactions.  A single pass would risk meeting a split before its account,
    since XML books do not guarantee ordering between the two sections.
    """
    result = ImportResult(source=str(path), source_format="xml")
    LOG.info("importing GnuCash XML book %s", path)

    def report(stage: str, done: int, total: int = 0) -> None:
        """An XML book gives no count in advance, so the bar pulses instead."""
        if progress is None:
            return
        try:
            progress(stage, done, total)
        except Exception:  # noqa: BLE001 - a broken meter must not stop the import
            LOG.debug("progress callback failed", exc_info=True)

    with db.transaction(message or f"Import {Path(path).name}", batch=True) as txn:
        sink = ImportSink(db, txn, result)

        stream = _open(path)
        try:
            accounts: list[ET.Element] = []
            for element, is_template in _iter_top_level(stream):
                tag = element.tag.rsplit("}", 1)[-1]
                if tag == "commodity" and not is_template:
                    _read_commodity(element, sink)
                elif tag == "account" and not is_template:
                    # Copy the element: it is cleared as soon as we hand it back.
                    accounts.append(_snapshot_account(element))
        finally:
            stream.close()

        LOG.debug("parsed %d real account element(s)", len(accounts))
        report("Reading accounts", 0)
        _write_accounts(accounts, sink)

        stream = _open(path)
        try:
            template_splits: dict[str, list[dict]] = {}
            schedules: list[ET.Element] = []
            for element, is_template in _iter_top_level(stream):
                tag = element.tag.rsplit("}", 1)[-1]
                if tag == "transaction" and not is_template:
                    _read_transaction(element, sink)
                elif tag == "transaction" and is_template:
                    _collect_template_splits(element, template_splits)
                elif tag == "schedxaction":
                    schedules.append(_snapshot(element))
        finally:
            stream.close()

        if include_scheduled:
            LOG.debug("parsed %d scheduled transaction element(s)", len(schedules))
            for element in schedules:
                try:
                    _read_schedule(element, template_splits, sink, db, txn)
                except Exception as exc:  # noqa: BLE001 - one schedule, not the book
                    name = _text(element, "sx:name") or "(unnamed)"
                    LOG.exception("could not read scheduled transaction %r", name)
                    sink.result.skip(
                        f"could not be read ({type(exc).__name__}: {exc})",
                        f"scheduled transaction {name!r}",
                    )

    LOG.info("import finished: %s", result.describe())
    db.emit("database-changed", (db,))
    return result


def _read_commodity(element: ET.Element, sink: ImportSink) -> None:
    space = _text(element, "cmdty:space")
    mnemonic = _text(element, "cmdty:id")
    if not mnemonic:
        return
    fraction = _text(element, "cmdty:fraction", "100")
    sink.commodity(
        namespace=space or "CURRENCY",
        mnemonic=mnemonic,
        fullname=_text(element, "cmdty:name"),
        fraction=int(fraction) if fraction.isdigit() else 100,
    )


def _snapshot_account(element: ET.Element) -> ET.Element:
    """Deep-copy the parts of an account element we need before it is cleared."""
    copy = ET.Element(element.tag)
    for child in element:
        if child.tag.endswith(("}name", "}id", "}type", "}parent", "}code",
                               "}description", "}commodity")):
            new = ET.SubElement(copy, child.tag)
            new.text = child.text
            for grand in child:
                sub = ET.SubElement(new, grand.tag)
                sub.text = grand.text
        elif child.tag.endswith("}slots"):
            copy.append(_copy_slots(child))
    return copy


def _copy_slots(slots: ET.Element) -> ET.Element:
    copy = ET.Element(slots.tag)
    for slot in slots:
        new = ET.SubElement(copy, slot.tag)
        for child in slot:
            sub = ET.SubElement(new, child.tag)
            sub.text = child.text
    return copy


def _slot_value(element: ET.Element, key: str) -> str | None:
    for slot in element.findall("act:slots/slot", NS) + element.findall("slots/slot", NS):
        if _text(slot, "slot:key") == key:
            value = slot.find("slot:value", NS)
            if value is not None:
                return value.text
    return None


class _AccountRow(TypedDict):
    guid: str
    name: str
    type: str
    parent: str | None
    code: str
    description: str
    commodity: str
    namespace: str
    placeholder: bool
    hidden: bool


def _write_accounts(elements: list[ET.Element], sink: ImportSink) -> None:
    parsed: list[_AccountRow] = []
    for element in elements:
        guid = _text(element, "act:id")
        if not guid:
            continue
        parsed.append(
            {
                "guid": guid,
                "name": _text(element, "act:name"),
                "type": _text(element, "act:type", "ASSET"),
                "parent": _text(element, "act:parent") or None,
                "code": _text(element, "act:code"),
                "description": _text(element, "act:description"),
                "commodity": _text(element, "act:commodity/cmdty:id"),
                "namespace": _text(element, "act:commodity/cmdty:space", "CURRENCY"),
                "placeholder": (_slot_value(element, "placeholder") or "").lower() == "true",
                "hidden": (_slot_value(element, "hidden") or "").lower() == "true",
            }
        )

    known = {row["guid"] for row in parsed}
    ordered: list[_AccountRow] = []
    placed: set[str] = set()
    pending = parsed
    while pending:
        remaining: list[_AccountRow] = []
        progressed = False
        for row in pending:
            if row["parent"] is None or row["parent"] in placed or row["parent"] not in known:
                ordered.append(row)
                placed.add(row["guid"])
                progressed = True
            else:
                remaining.append(row)
        pending = remaining
        if not progressed:
            ordered.extend(pending)
            break

    for row in ordered:
        commodity = (
            sink.commodity(row["namespace"], row["commodity"]) if row["commodity"] else None
        )
        sink.account(
            guid=row["guid"],
            name=row["name"],
            atype=row["type"],
            parent=row["parent"],
            commodity=commodity,
            code=row["code"],
            description=row["description"],
            placeholder=row["placeholder"],
            hidden=row["hidden"],
        )


def _read_transaction(element: ET.Element, sink: ImportSink) -> None:
    guid = _text(element, "trn:id")
    if not guid:
        return
    splits = []
    for split in element.findall("trn:splits/trn:split", NS):
        account = _text(split, "split:account")
        if not account:
            continue
        value = _text(split, "split:value", "0/1")
        quantity = _text(split, "split:quantity", value)
        splits.append(
            {
                "handle": _text(split, "split:id") or None,
                "account": account,
                "value": money_from_fraction(value),
                "quantity": money_from_fraction(quantity),
                "memo": _text(split, "split:memo"),
                "action": _text(split, "split:action"),
                "reconcile": _text(split, "split:reconciled-state", "n"),
            }
        )
    if not splits:
        sink.result.skip(
            "no splits in the source record",
            f"{_text(element, 'trn:date-posted/ts:date')[:10]} "
            f"{_text(element, 'trn:description')!r} [{guid[:8]}]",
        )
        return

    sink.transaction(
        guid=guid,
        post_date=parse_gnc_date(_text(element, "trn:date-posted/ts:date")),
        description=_text(element, "trn:description"),
        currency=_text(element, "trn:currency/cmdty:id") or None,
        num=_text(element, "trn:num"),
        splits=splits,
    )


# ------------------------------------------------- scheduled transactions

#: GnuCash recurrence period name -> our period type.
_PERIODS = {
    "once": PeriodType.ONCE,
    "day": PeriodType.DAY,
    "week": PeriodType.WEEK,
    "semi_month": PeriodType.SEMI_MONTH,
    "month": PeriodType.MONTH,
    "end of month": PeriodType.MONTH,
    "year": PeriodType.YEAR,
}

_WEEKEND = {
    "none": WeekendAdjust.NONE,
    "back": WeekendAdjust.PREVIOUS,
    "forward": WeekendAdjust.NEXT,
}


def _snapshot(element: ET.Element) -> ET.Element:
    """Deep copy an element, since iterparse clears it once we hand it back."""
    copy = ET.Element(element.tag, dict(element.attrib))
    copy.text = element.text
    for child in element:
        copy.append(_snapshot(child))
    return copy


def _gdate(element: ET.Element | None) -> date | None:
    """Read a GnuCash ``<gdate>`` value, which carries no namespace prefix."""
    if element is None:
        return None
    for node in element.iter():
        if node.tag.rsplit("}", 1)[-1] == "gdate" and node.text:
            try:
                return date.fromisoformat(node.text.strip())
            except ValueError:
                return None
    return None


def _slot_frame(split: ET.Element) -> dict[str, str]:
    """Flatten a split's ``sched-xaction`` slot frame into plain key/value pairs.

    GnuCash nests the real account and the amount formulas one frame deep inside
    the template split's slots. Without following that indirection a schedule
    points at its own template placeholder rather than the account the money
    actually moves through.
    """
    found: dict[str, str] = {}
    for slot in split.iter():
        if slot.tag.rsplit("}", 1)[-1] != "slot":
            continue
        key = None
        for child in slot:
            name = child.tag.rsplit("}", 1)[-1]
            if name == "key":
                key = (child.text or "").strip()
            elif name == "value" and key and key != "sched-xaction":
                if child.text and child.text.strip():
                    found[key] = child.text.strip()
    return found


def _collect_template_splits(
    element: ET.Element, into: dict[str, list[dict]]
) -> None:
    """Index a template transaction's splits by the template account they sit under."""
    for split in element.findall("trn:splits/trn:split", NS):
        template_account = _text(split, "split:account")
        if not template_account:
            continue
        slots = _slot_frame(split)
        into.setdefault(template_account, []).append(
            {
                "account": slots.get("account"),
                "credit": slots.get("credit-formula", ""),
                "debit": slots.get("debit-formula", ""),
                "memo": _text(split, "split:memo"),
                "value": _text(split, "split:value", "0/1"),
            }
        )


def _read_schedule(
    element: ET.Element,
    template_splits: dict[str, list[dict]],
    sink: ImportSink,
    db: DbSQLite,
    txn,
) -> None:
    guid = _text(element, "sx:id")
    name = _text(element, "sx:name") or "Scheduled transaction"
    if not guid:
        return

    recurrence_node = element.find("sx:schedule/gnc:recurrence", NS)
    period = _PERIODS.get(
        _text(recurrence_node, "recurrence:period_type", "month").lower(),
        PeriodType.MONTH,
    )
    multiplier = _text(recurrence_node, "recurrence:mult", "1")
    start = (
        # An Element with no children is falsey, so identity is the only safe test.
        _gdate(
            recurrence_node.find("recurrence:start", NS)
            if recurrence_node is not None
            else None
        )
        or _gdate(element.find("sx:start", NS))
        or date.today()
    )
    weekend = _WEEKEND.get(
        _text(recurrence_node, "recurrence:weekend_adj", "none").lower(),
        WeekendAdjust.NONE,
    )
    end = _gdate(element.find("sx:end", NS))
    occurrences = _text(element, "sx:num-occur", "0")

    schedule = ScheduledTransaction(
        handle=guid,
        name=name,
        recurrence=Recurrence(
            period=period,
            interval=int(multiplier) if multiplier.isdigit() else 1,
            start=start,
            end=end,
            count=int(occurrences) if occurrences.isdigit() and int(occurrences) else None,
            day_of_month=-1
            if "end of month"
            in _text(recurrence_node, "recurrence:period_type", "").lower()
            else None,
            weekend_adjust=weekend,
        ),
        enabled=_text(element, "sx:enabled", "y").lower() in ("y", "true", "1"),
        auto_create=_text(element, "sx:autoCreate", "n").lower() in ("y", "true", "1"),
        advance_days=int(_text(element, "sx:advanceCreateDays", "0") or 0),
    )
    schedule.last_posted = _gdate(element.find("sx:last", NS))

    template_account = _text(element, "sx:templ-acct")
    for raw in template_splits.get(template_account, []):
        target = raw.get("account")
        if not target or not sink.has_account(target):
            continue
        amount, formula = _template_amount(raw)
        schedule.splits.append(
            ScheduledSplit(
                account=sink.resolve(target),
                amount=amount,
                formula=formula,
                memo=raw.get("memo", ""),
            )
        )

    if not schedule.splits:
        sink.result.warn(
            f"scheduled transaction {name!r} has no usable template splits; "
            "imported without amounts"
        )
    balance_template_splits(schedule, sink.result)
    db.add_scheduled(schedule, txn)
    sink.result.scheduled += 1
    LOG.debug("scheduled %r: %s, %d split(s)", name,
              schedule.recurrence.describe(), len(schedule.splits))


def _template_amount(raw: dict) -> tuple[Money | None, str]:
    """Resolve a template split's amount, preferring a literal over a formula.

    A credit formula moves money out, so it becomes a negative amount. Three cases
    have to be told apart, and getting them wrong is how an import either crashes
    or quietly invents a number:

    * a plain amount, in whatever locale the user typed it;
    * an arithmetic expression, which is evaluated safely;
    * an expression naming GnuCash variables or other splits, which is not
      portable and is kept as text for the user to resolve.

    Never raises.
    """
    for key, sign in (("debit", 1), ("credit", -1)):
        text = (raw.get(key) or "").strip()
        if not text:
            continue

        literal = parse_amount_text(text)
        if literal is not None:
            return literal * sign, ""

        # Not a bare number: try it as arithmetic, e.g. "1200 + 45.50".
        try:
            computed = evaluate(text.replace(",", ""))
        except FormulaError:
            computed = None
        if computed is not None:
            return Money(computed) * sign, ""

        # References something we cannot resolve; keep the text for the user.
        return None, f"-({text})" if sign < 0 else text

    value = raw.get("value") or "0/1"
    try:
        return money_from_fraction(value), ""
    except (ValueError, ZeroDivisionError, ArithmeticError):
        return None, ""
