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
import hashlib
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ...gen.db.base import DbTxn
from ...gen.db.sqlite import DbSQLite
from ...gen.lib.account import (
    Account,
    AccountType,
    GnuCashAccountField,
    GnuCashAccountType,
)
from ...gen.lib.commodity import Commodity, CommodityPrice
from ...gen.lib.formula import FormulaError, evaluate
from ...gen.lib.money import Money
from ...gen.lib.recurrence import PeriodType
from ...gen.lib.scheduled import ScheduledSplit, ScheduledTransaction
from ...gen.lib.transaction import (
    PlanningResolution,
    ReconcileState,
    Split,
    Transaction,
    UnbalancedError,
)
from ...gen.utils.logs import get_logger

LOG = get_logger(__name__)

_SKIPPED_HISTORY_KEY = "import.skipped_history"

__all__ = [
    "ImportResult",
    "ImportSink",
    "detect_format",
    "parse_gnc_date",
    "preserve_breadsched_schedule_state",
    "recurrence_interval",
]


@dataclass
class ImportResult:
    """What an import did, and what it could not do."""

    accounts: int = 0
    transactions: int = 0
    splits: int = 0
    commodities: int = 0
    prices: int = 0
    scheduled: int = 0
    skipped: int = 0
    transactions_new: int = 0
    transactions_refreshed: int = 0
    transactions_unchanged: int = 0
    splits_new: int = 0
    splits_refreshed: int = 0
    splits_unchanged: int = 0
    splits_removed: int = 0
    skipped_new: int = 0
    skipped_repeated: int = 0
    skipped_resolved: int = 0
    warnings: list[str] = field(default_factory=list)
    #: One entry per rejected record: (reason, identification). Kept separate from
    #: warnings so a caller can report counts by reason without parsing prose.
    skipped_details: list[tuple[str, str]] = field(default_factory=list)
    source: str = ""
    source_format: str = ""
    log_path: str | None = None
    resolved_skipped_details: list[tuple[str, str]] = field(default_factory=list)
    _skipped_records: dict[str, dict[str, str]] = field(default_factory=dict, repr=False)
    _scanned_kinds: set[str] = field(default_factory=set, repr=False)
    _had_skip_history: bool = field(default=False, repr=False)

    def scan(self, kind: str) -> None:
        """Declare that this import examined all source records of ``kind``."""
        self._scanned_kinds.add(kind)

    def skip(
        self,
        reason: str,
        subject: str,
        *,
        identity: str | None = None,
        kind: str = "record",
    ) -> None:
        self.skipped += 1
        self.skipped_details.append((reason, subject))
        self.scan(kind)
        stable_identity = identity or hashlib.sha256(subject.encode()).hexdigest()
        key = f"{kind}:{stable_identity}"
        self._skipped_records[key] = {"reason": reason, "subject": subject, "kind": kind}
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

    def finish(self, db: DbSQLite, txn: DbTxn) -> None:
        """Compare and transactionally retain skipped records for this source."""
        raw_history = db.get_metadata(_SKIPPED_HISTORY_KEY, {})
        history = raw_history if isinstance(raw_history, dict) else {}
        source_path = str(Path(self.source).expanduser().resolve(strict=False))
        source_key = f"{self.source_format}:{source_path}"
        raw_previous = history.get(source_key, {})
        previous = raw_previous if isinstance(raw_previous, dict) else {}
        self._had_skip_history = source_key in history

        retained = {
            key: value
            for key, value in previous.items()
            if isinstance(value, dict) and value.get("kind") not in self._scanned_kinds
        }
        previous_scanned = {
            key: value
            for key, value in previous.items()
            if isinstance(value, dict) and value.get("kind") in self._scanned_kinds
        }
        for key, value in self._skipped_records.items():
            prior = previous_scanned.get(key)
            if prior is not None and prior.get("reason") == value["reason"]:
                self.skipped_repeated += 1
            else:
                self.skipped_new += 1
        for key, value in previous_scanned.items():
            if key not in self._skipped_records:
                self.skipped_resolved += 1
                self.resolved_skipped_details.append(
                    (str(value.get("reason", "unknown reason")), str(value.get("subject", key)))
                )

        history[source_key] = retained | self._skipped_records
        db.set_metadata(_SKIPPED_HISTORY_KEY, history, txn)

    def describe(self) -> str:
        parts = [
            f"{self.accounts} accounts",
            f"{self.transactions} transactions ({self.splits} splits)",
            f"{self.commodities} commodities",
        ]
        if self.scheduled:
            parts.append(f"{self.scheduled} scheduled")
        if self.prices:
            parts.append(f"{self.prices} prices")
        if self.skipped:
            parts.append(f"{self.skipped} skipped")
        return ", ".join(parts)

    def detail(self, limit: int = 20) -> str:
        """A multi-line report: the summary, why things were skipped, then warnings."""
        lines = [self.describe()]
        if self.transactions:
            lines.append(
                "Transactions: "
                f"{self.transactions_new} new, {self.transactions_refreshed} refreshed, "
                f"{self.transactions_unchanged} unchanged"
            )
            split_line = (
                f"Splits: {self.splits_new} new, {self.splits_refreshed} refreshed, "
                f"{self.splits_unchanged} unchanged"
            )
            if self.splits_removed:
                split_line += f", {self.splits_removed} removed"
            lines.append(split_line)
        if self._had_skip_history or self.skipped or self.skipped_resolved:
            lines.append(
                "Skipped since previous import: "
                f"{self.skipped_new} new, {self.skipped_repeated} repeated, "
                f"{self.skipped_resolved} resolved"
            )
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
    if not raw or not raw.strip():
        raise ValueError("missing GnuCash date")
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


def recurrence_interval(period: PeriodType, raw: object) -> int:
    """Translate GnuCash's recurrence multiplier to BreadSched's invariant.

    GnuCash writes a multiplier of zero for a one-time recurrence. BreadSched's
    ``ONCE`` period already fires exactly once and therefore ignores the interval,
    but every :class:`Recurrence` still requires an interval of at least one.
    Normalize that source-specific representation at the import boundary. Other
    zero or negative multipliers remain invalid instead of silently changing a
    repeating schedule.
    """
    try:
        interval = int("1" if raw in (None, "") else str(raw))
    except (TypeError, ValueError):
        return 1
    if period is PeriodType.ONCE and interval == 0:
        return 1
    return interval


class ImportSink:
    """Accumulates parsed GnuCash records and writes them into a BreadSched book."""

    def __init__(self, db: DbSQLite, txn: DbTxn, result: ImportResult) -> None:
        self.db = db
        self.txn = txn
        self.result = result
        self._known_accounts: set[str] = {a.handle for a in db.iter_accounts()}
        self._commodities: dict[str, str] = {}  # "NAMESPACE:MNEMONIC" -> handle
        self._commodity_guids: dict[str, str] = {}  # source GUID -> destination handle
        self._source_accounts: dict[str, str] = {}
        for account in db.iter_accounts():
            if account.source_guid and account.source_guid not in self._source_accounts:
                self._source_accounts[account.source_guid] = account.handle
        #: Source GUID -> destination handle, for accounts that were merged into an
        #: account the book already had rather than created afresh.
        self._remap: dict[str, str] = {}

    # ------------------------------------------------------------- commodities

    def commodity(
        self,
        namespace: str,
        mnemonic: str,
        fullname: str = "",
        fraction: int = 100,
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

    def price(
        self,
        guid: str,
        commodity: str,
        currency: str,
        quote_date: date,
        value: Money,
        source: str = "gnucash",
        quote_type: str = "last",
    ) -> CommodityPrice | None:
        """Add or refresh a GnuCash quote after resolving its commodities."""
        security_handle = self.resolve_commodity(commodity)
        currency_handle = self.resolve_commodity(currency)
        if security_handle is None or currency_handle is None:
            self.result.warn(f"price {guid[:8]} refers to an unknown commodity")
            return None
        quote_currency = self.db.get_commodity(currency_handle)
        if quote_currency is None or not quote_currency.is_currency:
            self.result.warn(f"price {guid[:8]} does not use a currency quote")
            return None
        try:
            price = CommodityPrice(
                handle=guid,
                commodity=security_handle,
                currency=currency_handle,
                quote_date=quote_date,
                value=value,
                source=source or "gnucash",
                quote_type=quote_type or "last",
            )
        except ValueError as exc:
            self.result.warn(f"price {guid[:8]} was ignored: {exc}")
            return None
        if self.db.get_price(guid) is None:
            self.db.add_price(price, self.txn)
        else:
            self.db.commit_price(price, self.txn)
        self.result.prices += 1
        return price

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
        notes: str = "",
        placeholder: bool = False,
        hidden: bool = False,
        commodity_scu: int | None = None,
        source_fields: Sequence[GnuCashAccountField] = (),
    ) -> Account:
        """Add an account, or adopt one the book already has in that position.

        A book created with ``breadsched init`` already has a root and the usual five
        top-level placeholders. Importing blindly would give it a *second* root, and
        an account tree that walks from one root shows an entirely empty book while
        every balance sits under the other. So the source root is mapped onto the
        existing root, and an account matching an existing sibling by name and type
        is adopted rather than duplicated.
        """
        source_type = str(atype).strip()
        parsed = GnuCashAccountType.recognize(source_type)
        account_type = parsed.to_account_type() if parsed is not None else AccountType.TECHNICAL
        mapped_parent = self._remap.get(parent, parent) if parent else None

        if parsed is None:
            self.result.warn(
                f"account {name!r} has unsupported GnuCash type {source_type!r}; "
                "preserved as Technical for review"
            )

        if parsed is GnuCashAccountType.ROOT and mapped_parent is None:
            existing_root = self.db.root_account()
            if existing_root is not None:
                self._remap[guid] = existing_root.handle
                self._known_accounts.add(guid)
                existing_root.source_guid = guid
                existing_root.source_atype = parsed
                existing_root.source_type = source_type
                existing_root.source_fields = list(source_fields)
                self.db.commit_account(existing_root, self.txn)
                self._source_accounts[guid] = existing_root.handle
                LOG.debug("mapped source root %s onto the book's root", guid[:8])
                return existing_root

        existing_handle = self._source_accounts.get(guid, guid)
        existing = self.db.get_account(existing_handle)
        if existing is not None:
            previous_source_type = existing.source_type or (
                existing.source_atype.value if existing.source_atype is not None else ""
            )
            if previous_source_type and previous_source_type != source_type:
                self.result.warn(
                    f"account {name!r} changed GnuCash type from "
                    f"{previous_source_type!r} to {source_type!r}"
                )
            if parsed is not None and existing.account_class is not parsed.account_class:
                self.result.warn(
                    f"account {name!r} changed GnuCash accounting class; retained its "
                    "BreadSched type for review"
                )
            account = Account(
                handle=existing.handle,
                name=name,
                atype=existing.atype,
                parent=mapped_parent,
                commodity=commodity,
                code=code,
                description=description,
                placeholder=placeholder,
                hidden=hidden,
                commodity_scu=commodity_scu,
            )
            account.notes = notes
            account.source_atype = parsed
            account.source_guid = guid
            account.source_type = source_type
            account.source_fields = list(source_fields)
            self._preserve_breadsched_account_state(account, existing)
            self.db.commit_account(account, self.txn)
            if existing.handle != guid:
                self._remap[guid] = existing.handle
            self._source_accounts[guid] = existing.handle
            self._known_accounts.add(guid)
            return account

        twin = self._existing_sibling(mapped_parent, name, source_type, parsed)
        if twin is not None:
            self._remap[guid] = twin.handle
            self._known_accounts.add(guid)
            account = Account(
                handle=twin.handle,
                name=name,
                atype=twin.atype,
                parent=mapped_parent,
                commodity=commodity,
                code=code,
                description=description,
                placeholder=placeholder,
                hidden=hidden,
                commodity_scu=commodity_scu,
            )
            account.notes = notes
            account.source_atype = parsed
            account.source_guid = guid
            account.source_type = source_type
            account.source_fields = list(source_fields)
            self._preserve_breadsched_account_state(account, twin)
            self.db.commit_account(account, self.txn)
            self._source_accounts[guid] = twin.handle
            LOG.debug("merged imported %r into the existing account", name)
            return account

        account = Account(
            handle=guid,
            name=name,
            atype=account_type,
            parent=mapped_parent,
            commodity=commodity,
            code=code,
            description=description,
            placeholder=placeholder,
            hidden=hidden,
            commodity_scu=commodity_scu,
        )
        account.notes = notes
        account.source_atype = parsed
        account.source_guid = guid
        account.source_type = source_type
        account.source_fields = list(source_fields)
        self.db.add_account(account, self.txn)
        self._source_accounts[guid] = account.handle
        self._known_accounts.add(guid)
        self.result.accounts += 1
        return account

    @staticmethod
    def _preserve_breadsched_account_state(imported: Account, existing: Account) -> None:
        """Keep BreadSched-owned account configuration across source re-import."""
        imported.atype = existing.atype
        imported.fsa_years = list(existing.fsa_years)
        imported.annual_return = existing.annual_return
        imported.annual_interest = existing.annual_interest
        imported.exclude_from_projection = existing.exclude_from_projection
        imported.group = existing.group
        imported.linked_asset = existing.linked_asset
        imported.pays_in_full = existing.pays_in_full
        imported.usual_payment = existing.usual_payment
        imported.payment_day = existing.payment_day
        imported.card_payment_account = existing.card_payment_account
        imported.emergency_fund_override = existing.emergency_fund_override

    def _existing_sibling(
        self,
        parent: str | None,
        name: str,
        source_type: str,
        atype: GnuCashAccountType | None,
    ) -> Account | None:
        """An account already under ``parent`` with the same name and type."""
        for candidate in self.db.child_accounts(parent):
            same_source = bool(source_type) and candidate.source_type == source_type
            native_match = (
                atype is not None
                and candidate.source_atype is None
                and not candidate.source_type
                and candidate.atype is atype.to_account_type()
            )
            if candidate.name == name and (same_source or native_match):
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
        source_notes: str = "",
    ) -> Transaction | None:
        """Build and store one transaction.

        A single malformed record must never cost the user the rest of the book, so
        everything here either repairs the transaction or skips that one record.
        Nothing raises: an exception would abort the enclosing batch and roll back
        every transaction imported so far.
        """
        self.result.scan("transaction")
        existing = self.db.get_transaction(guid)
        txn_obj = Transaction(
            handle=guid,
            post_date=post_date,
            description=description,
            currency=self.resolve_commodity(currency),
            num=num,
        )
        # Imported GnuCash history is already-established actual activity. New
        # records must not enter BreadSched's plan-resolution review queue en
        # masse. On re-import, BreadSched-owned planning state is merged below.
        txn_obj.planning_resolution = PlanningResolution.HISTORICAL
        txn_obj.source_notes = source_notes
        subject = txn_obj.describe()

        if not splits:
            self.result.skip(
                "no splits in the source record",
                subject,
                identity=guid,
                kind="transaction",
            )
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
                    identity=guid,
                    kind="transaction",
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

        if existing is not None:
            self._preserve_breadsched_transaction_state(txn_obj, existing)

        LOG.debug(
            "%s: %d split(s) totalling %s",
            subject,
            len(txn_obj.splits),
            txn_obj.imbalance(),
        )

        residual = txn_obj.imbalance()
        if len(txn_obj.splits) == 1 and not residual:
            # A lone zero-value split carries no information and cannot be balanced
            # into anything meaningful.
            self.result.skip(
                "only one split, with no value",
                subject,
                identity=guid,
                kind="transaction",
            )
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
            self.result.skip(
                f"could not be repaired ({exc})",
                subject,
                identity=guid,
                kind="transaction",
            )
            return None

        self.result.transactions += 1
        self.result.splits += len(txn_obj.splits)
        self._count_transaction_change(existing, txn_obj)
        return txn_obj

    def _count_transaction_change(
        self, existing: Transaction | None, imported: Transaction
    ) -> None:
        """Classify source-owned ledger facts without counting local annotations."""
        current_splits = [_split_source_facts(split) for split in imported.splits]
        if existing is None:
            self.result.transactions_new += 1
            self.result.splits_new += len(current_splits)
            return

        prior_splits = [_split_source_facts(split) for split in existing.splits]
        if _transaction_source_facts(existing) == _transaction_source_facts(imported):
            self.result.transactions_unchanged += 1
        else:
            self.result.transactions_refreshed += 1

        prior_by_handle = {split.handle: split for split in existing.splits}
        matched_prior: set[int] = set()
        for index, split in enumerate(imported.splits):
            current = current_splits[index]
            matching_index: int | None = None
            if split.handle in prior_by_handle:
                matching_index = existing.splits.index(prior_by_handle[split.handle])
            elif index < len(prior_splits) and index not in matched_prior:
                # Formats without split IDs still have deterministic split order.
                matching_index = index
            if matching_index is None:
                self.result.splits_new += 1
            elif current == prior_splits[matching_index]:
                self.result.splits_unchanged += 1
                matched_prior.add(matching_index)
            else:
                self.result.splits_refreshed += 1
                matched_prior.add(matching_index)
        self.result.splits_removed += len(prior_splits) - len(matched_prior)

    @staticmethod
    def _preserve_breadsched_transaction_state(
        imported: Transaction, existing: Transaction
    ) -> None:
        """Merge BreadSched-owned annotations into a re-imported transaction.

        GnuCash remains authoritative for ledger facts such as dates, amounts,
        accounts, memos, actions, and reconcile state. BreadSched owns the
        planning/review annotations and FSA classifications added after import.
        Split annotations are preserved only when the source split GUID still
        exists, so a materially replaced source split cannot inherit stale state.
        """
        imported.notes = existing.notes
        imported.scheduled_from = existing.scheduled_from
        imported.planned_occurrence = existing.planned_occurrence
        imported.planned_for = existing.planned_for
        imported.planned_amount = existing.planned_amount
        imported.planning_resolution = existing.planning_resolution
        imported.rejected_plan_occurrences = list(existing.rejected_plan_occurrences)

        existing_splits = {split.handle: split for split in existing.splits}
        for split in imported.splits:
            prior = existing_splits.get(split.handle)
            if prior is None:
                continue
            split.planning_flow = prior.planning_flow
            split.investment_activity = prior.investment_activity
            split.fsa_year_start = prior.fsa_year_start

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
    context = schedule.context(schedule.recurrence.start)
    for split in schedule.splits:
        if not split.formula:
            continue
        try:
            evaluate(split.formula, context)
        except (FormulaError, ValueError, ArithmeticError) as exc:
            if result is not None:
                result.warn(
                    f"scheduled transaction {schedule.name!r} preserved unsupported "
                    f"formula {split.formula!r}: {exc}; it is inspectable but excluded "
                    "from planning and posting until translated"
                )
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


def preserve_breadsched_schedule_state(
    imported: ScheduledTransaction,
    existing: ScheduledTransaction | None,
) -> None:
    """Retain local split classifications when a source schedule is refreshed.

    Scheduled splits have no stable GnuCash GUID of their own. A classification is
    therefore retained only when its account occurs exactly once in both versions;
    ambiguous/restructured templates deliberately receive no stale annotation.
    """
    if existing is None:
        return
    prior_by_account: dict[str, list[ScheduledSplit]] = {}
    imported_by_account: dict[str, list[ScheduledSplit]] = {}
    for split in existing.splits:
        prior_by_account.setdefault(split.account, []).append(split)
    for split in imported.splits:
        imported_by_account.setdefault(split.account, []).append(split)
    for account, incoming in imported_by_account.items():
        prior = prior_by_account.get(account, [])
        if len(incoming) != 1 or len(prior) != 1:
            continue
        incoming[0].planning_flow = prior[0].planning_flow
        incoming[0].investment_activity = prior[0].investment_activity


def _split_source_facts(split: Split) -> tuple[object, ...]:
    """Fields for which an imported ledger source remains authoritative."""
    return (
        split.account,
        split.value,
        split.quantity,
        split.memo,
        split.action,
        split.reconcile,
    )


def _transaction_source_facts(transaction: Transaction) -> tuple[object, ...]:
    return (
        transaction.post_date,
        transaction.description,
        transaction.currency,
        transaction.num,
        transaction.source_notes,
        tuple(sorted((_split_source_facts(split) for split in transaction.splits), key=repr)),
    )


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
        tail = cleaned[comma + 1 :]
        cleaned = cleaned.replace(",", ".") if len(tail) in (1, 2) else cleaned.replace(",", "")

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
