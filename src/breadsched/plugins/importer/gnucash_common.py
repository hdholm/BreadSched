"""Shared plumbing for reading GnuCash books.

GnuCash writes the same object model to two very different containers: a SQLite3
database and a gzip-compressed XML document (still the default for new books).
Both are supported, and both funnel into :class:`ImportSink` so the mapping rules
-- account types, GUID reuse, sign conventions -- live in exactly one place.

GUIDs are reused verbatim as handles.  Re-importing an updated copy of the same
book therefore updates the existing rows rather than duplicating the whole chart of
accounts, which is what makes "GnuCash is still my system of record, BreadSched does
the forecasting" a workable arrangement.
"""

from __future__ import annotations

import gzip
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ...gen.db.base import DbTxn
from ...gen.db.sqlite import DbSQLite
from ...gen.lib.account import Account, AccountType
from ...gen.lib.commodity import Commodity
from ...gen.lib.money import Money
from ...gen.lib.transaction import (
    PlanningResolution,
    ReconcileState,
    Split,
    Transaction,
    UnbalancedError,
)
from ...gen.utils.logs import get_logger

LOG = get_logger(__name__)

__all__ = ["ImportResult", "ImportSink", "detect_format", "parse_gnc_date"]


@dataclass
class ImportResult:
    """What an import did, and what it could not do."""

    accounts: int = 0
    transactions: int = 0
    splits: int = 0
    commodities: int = 0
    scheduled: int = 0
    skipped: int = 0
    warnings: list[str] = field(default_factory=list)
    #: One entry per rejected record: (reason, identification). Kept separate from
    #: warnings so a caller can report counts by reason without parsing prose.
    skipped_details: list[tuple[str, str]] = field(default_factory=list)
    source: str = ""
    source_format: str = ""
    log_path: str | None = None

    def skip(self, reason: str, subject: str) -> None:
        self.skipped += 1
        self.skipped_details.append((reason, subject))
        message = f"skipped {subject}: {reason}"
        self.warnings.append(message)
        LOG.warning(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)
        LOG.warning(message)

    def reasons(self) -> dict[str, int]:
        """How many records were skipped for each distinct reason."""
        counts: dict[str, int] = {}
        for reason, _subject in self.skipped_details:
            counts[reason] = counts.get(reason, 0) + 1
        return counts

    def describe(self) -> str:
        parts = [
            f"{self.accounts} accounts",
            f"{self.transactions} transactions ({self.splits} splits)",
            f"{self.commodities} commodities",
        ]
        if self.scheduled:
            parts.append(f"{self.scheduled} scheduled")
        if self.skipped:
            parts.append(f"{self.skipped} skipped")
        return ", ".join(parts)

    def detail(self, limit: int = 20) -> str:
        """A multi-line report: the summary, why things were skipped, then warnings."""
        lines = [self.describe()]
        reasons = self.reasons()
        if reasons:
            lines.append("")
            lines.append("Skipped records by reason:")
            lines.extend(f"  {count} x {reason}" for reason, count in reasons.items())
        if self.warnings:
            lines.append("")
            lines.append(f"{len(self.warnings)} warning(s):")
            lines.extend(f"  - {w}" for w in self.warnings[:limit])
            if len(self.warnings) > limit:
                lines.append(f"  ... and {len(self.warnings) - limit} more")
        else:
            lines.append("")
            lines.append("No problems found.")
        if self.log_path:
            lines.append("")
            lines.append(f"Full log: {self.log_path}")
        return "\n".join(lines)

    def __str__(self) -> str:
        return f"Imported {self.describe()} from {self.source or 'book'}"


def detect_format(path: str | Path) -> str:
    """Return ``sqlite``, ``xml``, ``xml-gz`` or ``unknown`` by sniffing the header.

    File extension is not trusted: GnuCash books are routinely named ``.gnucash``
    whichever container they use.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    with path.open("rb") as handle:
        magic = handle.read(16)
    if magic.startswith(b"SQLite format 3"):
        return "sqlite"
    if magic.startswith(b"\x1f\x8b"):
        try:
            with gzip.open(path, "rb") as gz:
                head = gz.read(512)
        except OSError:
            return "unknown"
        return "xml-gz" if b"gnc-v2" in head or b"<gnc" in head else "unknown"
    if magic.lstrip().startswith(b"<?xml"):
        return "xml"
    return "unknown"


def parse_gnc_date(raw: str | None) -> date:
    """Parse the several date shapes GnuCash emits into a plain date.

    SQLite books store ``YYYYMMDDHHMMSS`` in newer versions and
    ``YYYY-MM-DD HH:MM:SS`` in older ones; XML books store an ISO timestamp with a
    numeric zone offset.  All are truncated to the posting *date*, since a
    cash-flow model has no use for the time of day.
    """
    if not raw:
        return date.today()
    text = raw.strip()
    if len(text) == 14 and text.isdigit():
        return date(int(text[0:4]), int(text[4:6]), int(text[6:8]))
    text = text.replace("T", " ")
    head = text.split(" ")[0]
    try:
        return date.fromisoformat(head)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognised GnuCash date {raw!r}")


class ImportSink:
    """Accumulates parsed GnuCash records and writes them into a BreadSched book."""

    def __init__(self, db: DbSQLite, txn: DbTxn, result: ImportResult) -> None:
        self.db = db
        self.txn = txn
        self.result = result
        self._known_accounts: set[str] = {a.handle for a in db.iter_accounts()}
        self._commodities: dict[str, str] = {}  # "NAMESPACE:MNEMONIC" -> handle
        self._commodity_guids: dict[str, str] = {}  # source GUID -> destination handle
        #: Source GUID -> destination handle, for accounts that were merged into an
        #: account the book already had rather than created afresh.
        self._remap: dict[str, str] = {}

    # ------------------------------------------------------------- commodities

    def commodity(
        self, namespace: str, mnemonic: str, fullname: str = "", fraction: int = 100,
        source_guid: str | None = None,
    ) -> str:
        key = f"{namespace}:{mnemonic}"
        if key in self._commodities:
            handle = self._commodities[key]
            if source_guid:
                self._commodity_guids[source_guid] = handle
            return handle
        existing = self.db.get_commodity_by_mnemonic(mnemonic)
        if existing is not None:
            self._commodities[key] = existing.handle
            if source_guid:
                self._commodity_guids[source_guid] = existing.handle
            return existing.handle
        obj = Commodity(
            namespace=namespace or "CURRENCY",
            mnemonic=mnemonic,
            fullname=fullname or mnemonic,
            fraction=fraction or 100,
        )
        self.db.add_commodity(obj, self.txn)
        self._commodities[key] = obj.handle
        if source_guid:
            self._commodity_guids[source_guid] = obj.handle
        self.result.commodities += 1
        return obj.handle

    def resolve_commodity(self, identifier: str | None) -> str | None:
        """Resolve a source GUID or mnemonic to the destination commodity handle."""
        if identifier is None:
            return None
        mapped = self._commodity_guids.get(identifier)
        if mapped is not None:
            return mapped
        if self.db.get_commodity(identifier) is not None:
            return identifier
        existing = self.db.get_commodity_by_mnemonic(identifier)
        return existing.handle if existing is not None else None

    # ---------------------------------------------------------------- accounts

    def account(
        self,
        guid: str,
        name: str,
        atype: str,
        parent: str | None,
        commodity: str | None = None,
        code: str = "",
        description: str = "",
        placeholder: bool = False,
        hidden: bool = False,
    ) -> Account:
        """Add an account, or adopt one the book already has in that position.

        A book created with ``breadsched init`` already has a root and the usual five
        top-level placeholders. Importing blindly would give it a *second* root, and
        an account tree that walks from one root shows an entirely empty book while
        every balance sits under the other. So the source root is mapped onto the
        existing root, and an account matching an existing sibling by name and type
        is adopted rather than duplicated.
        """
        parsed = AccountType.parse(atype)
        mapped_parent = self._remap.get(parent, parent) if parent else None

        if parsed is AccountType.ROOT and mapped_parent is None:
            existing_root = self.db.root_account()
            if existing_root is not None:
                self._remap[guid] = existing_root.handle
                self._known_accounts.add(guid)
                LOG.debug("mapped source root %s onto the book's root", guid[:8])
                return existing_root

        twin = self._existing_sibling(mapped_parent, name, parsed)
        if twin is not None:
            self._remap[guid] = twin.handle
            self._known_accounts.add(guid)
            LOG.debug("merged imported %r into the existing account", name)
            return twin

        account = Account(
            handle=guid,
            name=name,
            atype=parsed,
            parent=mapped_parent,
            commodity=commodity,
            code=code,
            description=description,
            placeholder=placeholder,
            hidden=hidden,
        )
        self.db.add_account(account, self.txn)
        self._known_accounts.add(guid)
        self.result.accounts += 1
        return account

    def _existing_sibling(
        self, parent: str | None, name: str, atype: AccountType
    ) -> Account | None:
        """An account already under ``parent`` with the same name and type."""
        for candidate in self.db.child_accounts(parent):
            if candidate.name == name and candidate.atype is atype:
                return candidate
        return None

    def resolve(self, guid: str) -> str:
        """Destination handle for a source GUID, following any merge."""
        return self._remap.get(guid, guid)

    def has_account(self, guid: str) -> bool:
        return guid in self._known_accounts

    # ------------------------------------------------------------ transactions

    def transaction(
        self,
        guid: str,
        post_date: date,
        description: str,
        currency: str | None,
        num: str,
        splits: list[dict[str, Any]],
    ) -> Transaction | None:
        """Build and store one transaction.

        A single malformed record must never cost the user the rest of the book, so
        everything here either repairs the transaction or skips that one record.
        Nothing raises: an exception would abort the enclosing batch and roll back
        every transaction imported so far.
        """
        txn_obj = Transaction(
            handle=guid,
            post_date=post_date,
            description=description,
            currency=self.resolve_commodity(currency),
            num=num,
        )
        # Imported GnuCash history is already-established actual activity.  It
        # must not enter BreadSched's plan-resolution review queue en masse.
        txn_obj.planning_resolution = PlanningResolution.HISTORICAL
        subject = txn_obj.describe()

        if not splits:
            self.result.skip("no splits in the source record", subject)
            return None

        for raw in splits:
            # An account merged into one the book already had keeps its source
            # GUID in the split. Without this the split would name a handle that
            # does not exist: the transaction vanishes from that account's
            # register and its balance silently goes missing.
            raw["account"] = self._remap.get(raw["account"], raw["account"])
            if not self.has_account(raw["account"]):
                self.result.skip(
                    f"references account {raw['account'][:8]}, which is not in the book",
                    subject,
                )
                return None
            txn_obj.add_split(
                Split(
                    account=self.resolve(raw["account"]),
                    value=raw["value"],
                    quantity=raw.get("quantity"),
                    memo=raw.get("memo", ""),
                    action=raw.get("action", ""),
                    reconcile=_reconcile(raw.get("reconcile")),
                    handle=raw.get("handle"),
                )
            )

        LOG.debug(
            "%s: %d split(s) totalling %s",
            subject, len(txn_obj.splits), txn_obj.imbalance(),
        )

        residual = txn_obj.imbalance()
        if len(txn_obj.splits) == 1 and not residual:
            # A lone zero-value split carries no information and cannot be balanced
            # into anything meaningful.
            self.result.skip("only one split, with no value", subject)
            return None

        if residual or len(txn_obj.splits) < 2:
            # Either a genuine imbalance (commonly a multi-currency book whose
            # split values are denominated differently) or a lone split that
            # GnuCash allowed to be saved unbalanced. Both become readable by
            # booking the difference to Imbalance.
            imbalance = self._imbalance_account(txn_obj.currency)
            txn_obj.add_split(Split(imbalance, -residual, memo="Imported imbalance"))
            self.result.warn(
                f"{subject}: out of balance by {residual} across "
                f"{len(txn_obj.splits) - 1} split(s); posted the difference to "
                f"Imbalance"
            )

        try:
            self.db.add_transaction(txn_obj, self.txn)
        except UnbalancedError as exc:
            # Belt and braces: if a record is still not storable, drop that one
            # record rather than losing the import.
            self.result.skip(f"could not be repaired ({exc})", subject)
            return None

        self.result.transactions += 1
        self.result.splits += len(txn_obj.splits)
        return txn_obj

    def _imbalance_account(self, currency: str | None) -> str:
        existing = self.db.get_account_by_name("Imbalance")
        if existing is not None:
            return existing.handle
        root = self.db.root_account()
        account = Account(
            name="Imbalance",
            atype=AccountType.EQUITY,
            parent=root.handle if root else None,
            commodity=currency,
            description="Holds differences found while importing",
        )
        self.db.add_account(account, self.txn)
        self._known_accounts.add(account.handle)
        self.result.accounts += 1
        LOG.info("created an Imbalance account to hold differences found on import")
        return account.handle


def balance_template_splits(schedule, result: ImportResult | None = None) -> bool:
    """Repair a two-sided template whose legs do not agree.

    GnuCash calculates each leg of a scheduled transaction from its own formula.
    If one of them names something not portable -- another split, a GnuCash
    variable -- that leg resolves to nothing while the other resolves to a real
    amount, and the schedule arrives half-formed.

    For a two-split schedule the missing side is not a guess: a transaction with
    two legs balances by definition, so the absent one is the negation of the
    present one. Anything more complicated is left alone and reported, because
    inventing a figure across three or more legs would be exactly that.

    Returns True when a repair was made.
    """
    if len(schedule.splits) != 2:
        return False
    resolved = [split.resolve(schedule.variables) for split in schedule.splits]
    if not (resolved[0] + resolved[1]):
        return False  # already balances

    empty = [index for index, amount in enumerate(resolved) if not amount]
    if len(empty) != 1:
        if result is not None:
            result.warn(
                f"scheduled transaction {schedule.name!r} does not balance: its "
                f"calculated legs differ by {resolved[0] + resolved[1]}"
            )
        return False

    missing, present = empty[0], 1 - empty[0]
    schedule.splits[missing].amount = -resolved[present]
    schedule.splits[missing].formula = ""
    if result is not None:
        result.warn(
            f"scheduled transaction {schedule.name!r} had an unusable formula on one "
            f"leg; balanced it against the other at {-resolved[present]}"
        )
    return True


def _reconcile(raw: str | None) -> ReconcileState:
    try:
        return ReconcileState((raw or "n").lower())
    except ValueError:
        return ReconcileState.NOT_RECONCILED


def money_from_pair(numerator: Any, denominator: Any) -> Money:
    return Money(int(numerator), int(denominator or 1))


#: Separators a GnuCash formula may contain: thin and non-breaking spaces are
#: emitted by some locales' number formatting and are invisible in a debugger.
_SPACES = "\u00a0\u2007\u202f\u2009 \t"


def parse_amount_text(text: str) -> Money | None:
    """Read a human-written amount, or return ``None`` if it is not one.

    Template formulas in a GnuCash book are whatever the user typed, in whatever
    locale they were using. ``1,800.00`` and ``1.800,00`` are the same amount
    written two ways, and telling them apart matters more than it looks: reading
    the second with the first's rules gives 1.8 rather than 1800, which is a wrong
    number rather than an error, and would go unnoticed.

    Never raises. A caller handling a foreign file needs a decision, not an
    exception.
    """
    if text is None:
        return None
    cleaned = str(text)
    for space in _SPACES:
        cleaned = cleaned.replace(space, "")
    for symbol in "$\u00a3\u20ac\u00a5":
        cleaned = cleaned.replace(symbol, "")
    if not cleaned:
        return None

    comma, dot = cleaned.rfind(","), cleaned.rfind(".")
    if comma >= 0 and dot >= 0:
        # Whichever comes last is the decimal separator; the other groups digits.
        if comma > dot:
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif comma >= 0:
        # A lone comma is decimal when it separates the final one or two digits
        # (1.234,5 style); otherwise it is grouping (1,234).
        tail = cleaned[comma + 1:]
        cleaned = (
            cleaned.replace(",", ".") if len(tail) in (1, 2) else cleaned.replace(",", "")
        )

    try:
        return Money(cleaned)
    except (ValueError, ZeroDivisionError, ArithmeticError):
        return None


def money_from_fraction(text: str) -> Money:
    """Parse the ``num/denom`` string used throughout GnuCash XML."""
    numerator, _, denominator = str(text).strip().partition("/")
    return Money(int(numerator), int(denominator or 1))


def open_gnucash_sqlite(path: str | Path) -> sqlite3.Connection:
    """Open a GnuCash SQLite book read-only so the source can never be damaged."""
    uri = f"file:{Path(path).resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn
