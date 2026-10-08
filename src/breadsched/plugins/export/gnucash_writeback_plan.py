"""Plan a GnuCash write-back: what differs, what can be written, and why not.

``plan_writeback`` compares BreadSched with the ``SourceBook`` and returns each
supported change (``WritebackChange`` with its ``TargetTxn``) and each unsupported
one with its reason. Nothing here writes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from ...gen.db.sqlite import DbSQLite
from ...gen.lib.money import Money
from ...gen.lib.transaction import ReconcileState, Transaction
from .gnucash_source import SourceBook, SourceTxn, preflight, read_book, shown_value

__all__ = [
    "INVENTORY_KEY",
    "TargetSplit",
    "TargetTxn",
    "WritebackChange",
    "WritebackPlan",
    "WritebackUnsupported",
    "money_of",
    "plan_writeback",
    "split_state",
]


#: Book metadata the importer keeps: what the GnuCash book held, and what it skipped.
INVENTORY_KEY = "import.source_inventory"
_SKIPPED_HISTORY_KEY = "import.skipped_history"
_WRITABLE_STATES = {"n", "c", "y"}


@dataclass(frozen=True, slots=True)
class TargetSplit:
    guid: str
    account: str
    value: tuple[int, int]
    quantity: tuple[int, int]
    memo: str
    action: str
    state: str
    reconcile_date: date | None


@dataclass(frozen=True, slots=True)
class TargetTxn:
    guid: str
    currency: str
    num: str
    post_date: date
    description: str
    splits: tuple[TargetSplit, ...]


@dataclass(frozen=True, slots=True)
class _Operation:
    #: ``new``, ``edit``, or ``delete``.
    action: str
    guid: str
    target: TargetTxn | None


@dataclass(frozen=True, slots=True)
class WritebackChange:
    """Everything that would be written for one transaction."""

    transaction: str
    post_date: date
    description: str
    #: ``new``, ``edit``, ``reconcile``, and/or ``delete``.
    kinds: tuple[str, ...]
    #: Plain-language lines naming each change.
    details: tuple[str, ...]
    operation: _Operation | None = field(repr=False, default=None)


@dataclass(frozen=True, slots=True)
class WritebackUnsupported:
    transaction: str
    post_date: date
    description: str
    reason: str


@dataclass(frozen=True, slots=True)
class WritebackPlan:
    source: str
    changes: tuple[WritebackChange, ...]
    unsupported: tuple[WritebackUnsupported, ...]
    #: ``sqlite`` or ``xml``.
    format: str = "sqlite"


class _Refused(Exception):
    pass


def _pair(value: Money, fraction: int) -> tuple[int, int]:
    quantized = value.quantize(fraction)
    if quantized != value:
        raise _Refused("an amount has more precision than GnuCash allows for it")
    return quantized.as_gnc(fraction)


def split_state(split) -> str:
    raw = split.reconcile.value if isinstance(split.reconcile, ReconcileState) else "n"
    return raw if raw in _WRITABLE_STATES else "n"


def _inventory(db: DbSQLite, root_guid: str) -> set[str]:
    raw = db.get_metadata(INVENTORY_KEY, {})
    handles: set[str] = set()
    if not isinstance(raw, dict):
        return handles
    entries = [raw.get(f"gnucash:{root_guid}")] if root_guid else list(raw.values())
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("transactions"), list):
            handles.update(str(item) for item in entry["transactions"])
    return handles


def _skipped_identities(db: DbSQLite) -> set[str]:
    raw = db.get_metadata(_SKIPPED_HISTORY_KEY, {})
    found: set[str] = set()
    if isinstance(raw, dict):
        for entry in raw.values():
            if isinstance(entry, dict):
                found.update(
                    key.split(":", 1)[1] for key in entry if key.startswith("transaction:")
                )
    return found


def plan_writeback(db: DbSQLite) -> WritebackPlan:
    """Preview every supported local change; write nothing."""
    _recorded_fp, path = preflight(db)
    return _plan(db, read_book(path))


def _plan(db: DbSQLite, book: SourceBook) -> WritebackPlan:
    source_guid = {
        account.handle: account.source_guid
        for account in db.iter_accounts()
        if account.source_guid and account.source_guid in book.accounts
    }
    changes: list[WritebackChange] = []
    unsupported: list[WritebackUnsupported] = []
    from_gnucash = _inventory(db, book.root_guid)

    def refuse(guid: str, when: date, description: str, reason: str) -> None:
        unsupported.append(WritebackUnsupported(guid, when, description, reason))

    local_handles: set[str] = set()
    for transaction in sorted(db.iter_transactions(), key=lambda t: (t.post_date, t.handle)):
        local_handles.add(transaction.handle)
        existing = book.transactions.get(transaction.handle)
        if existing is None:
            if not any(split.account in source_guid for split in transaction.splits):
                continue  # entirely BreadSched-only accounts: nothing to do with GnuCash
            if transaction.handle in from_gnucash:
                refuse(
                    transaction.handle,
                    transaction.post_date,
                    transaction.description,
                    "deleted in GnuCash and kept here; not written back",
                )
                continue
        try:
            target = _target(db, transaction, book, source_guid, existing)
            change = _change(db, transaction, target, existing, book)
        except _Refused as exc:
            refuse(transaction.handle, transaction.post_date, transaction.description, str(exc))
            continue
        if change is not None:
            changes.append(change)

    skipped = _skipped_identities(db)
    for guid in sorted(from_gnucash - local_handles - skipped):
        source = book.transactions.get(guid)
        if source is None:
            continue
        when = source.post_date or date.min
        if source.reconciled:
            refuse(guid, when, source.description, "reconciled in GnuCash; not deleted there")
            continue
        if source.in_lot:
            refuse(guid, when, source.description, "part of a GnuCash lot; not deleted there")
            continue
        changes.append(
            WritebackChange(
                guid,
                when,
                source.description,
                ("delete",),
                (f"Delete {source.description!r} ({len(source.splits)} splits) from GnuCash",),
                _Operation("delete", guid, None),
            )
        )
    changes.sort(key=lambda item: (item.post_date, item.transaction))
    return WritebackPlan(str(book.path), tuple(changes), tuple(unsupported), book.format)


def _currency_of(db: DbSQLite, transaction: Transaction) -> tuple[str, int]:
    currency = db.get_commodity(transaction.currency) if transaction.currency else None
    if currency is None:
        from ...gen.engine.currency import reporting_currency_handle

        currency = db.get_commodity(reporting_currency_handle(db))
    if currency is None:
        raise _Refused("its currency is not known")
    return currency.mnemonic, int(currency.fraction or 100)


def _target(
    db: DbSQLite,
    transaction: Transaction,
    book: SourceBook,
    source_guid: dict[str, str],
    existing: SourceTxn | None,
) -> TargetTxn:
    mnemonic, fraction = _currency_of(db, transaction)
    if mnemonic not in book.currencies:
        raise _Refused("its currency is not in the GnuCash book")
    if book.currencies[mnemonic][1]:
        fraction = book.currencies[mnemonic][1]
    if existing is not None and existing.currency and existing.currency != mnemonic:
        raise _Refused("its currency changed; only GnuCash can change that")
    splits: list[TargetSplit] = []
    for split in transaction.splits:
        if split.importer_added:
            continue  # BreadSched's own bookkeeping, such as a share split's balancing leg
        account_guid = source_guid.get(split.account)
        if account_guid is None:
            raise _Refused("an account is not in the GnuCash book")
        account = book.accounts[account_guid]
        value = _pair(split.value, fraction)
        in_currency = account.commodity in ("", f"CURRENCY:{mnemonic}", f"ISO4217:{mnemonic}")
        if in_currency:
            # GnuCash keeps quantity equal to value in the transaction currency.
            quantity = value
        else:
            if split.quantity is None:
                raise _Refused("a split in a security or foreign-currency account has no quantity")
            quantity = _pair(split.quantity, account.scu)
        state = split_state(split)
        prior = existing.splits.get(split.handle) if existing is not None else None
        reconcile_date = split.reconcile_date if state == "y" else None
        if state == "y" and reconcile_date is None:
            reconcile_date = transaction.post_date
        splits.append(
            TargetSplit(
                split.handle,
                account_guid,
                value,
                quantity,
                split.memo or "",
                split.action or "",
                state,
                reconcile_date if (prior is None or prior.state != state) else None,
            )
        )
    return TargetTxn(
        transaction.handle,
        mnemonic,
        transaction.num or "",
        transaction.post_date,
        transaction.description,
        tuple(splits),
    )


def money_of(pair: tuple[int, int]) -> Money:
    return Money(pair[0], pair[1])


def _change(
    db: DbSQLite,
    transaction: Transaction,
    target: TargetTxn,
    existing: SourceTxn | None,
    book: SourceBook,
) -> WritebackChange | None:
    names = {
        account.source_guid: db.full_name(account)
        for account in db.iter_accounts()
        if account.source_guid
    }

    def name(account_guid: str) -> str:
        return names.get(account_guid, account_guid[:8])

    if existing is None:
        if len(target.splits) < 2:
            raise _Refused("a GnuCash transaction needs at least two splits")
        lines = [
            f"New transaction {target.guid[:8]}: {target.post_date.isoformat()} "
            f"{target.description!r} in {target.currency}"
        ]
        lines.extend(
            f"  split {split.guid[:8]}: {name(split.account)} "
            f"{money_of(split.value).format()} (reconcile {split.state})"
            for split in target.splits
        )
        return WritebackChange(
            target.guid,
            target.post_date,
            target.description,
            ("new",),
            tuple(lines),
            _Operation("new", target.guid, target),
        )

    details: list[str] = []
    ledger_changed = False
    for label, before, after in (
        ("date", existing.post_date, target.post_date),
        ("description", existing.description, target.description),
        ("number", existing.num, target.num),
    ):
        if before != after:
            details.append(f"{label}: {shown_value(before)} -> {shown_value(after)}")
            ledger_changed = True
    reconcile_lines: list[str] = []
    wanted = {split.guid: split for split in target.splits}
    for guid, removed in existing.splits.items():
        if guid not in wanted:
            details.append(
                f"remove split {name(removed.account)} {removed.value.format(parens_negative=True)}"
            )
            ledger_changed = True
    for split in target.splits:
        prior = existing.splits.get(split.guid)
        label = name(split.account)
        if prior is None:
            details.append(f"add split {label} {money_of(split.value).format()}")
            ledger_changed = True
            continue
        if prior.account != split.account:
            details.append(f"split account: {name(prior.account)} -> {label}")
            ledger_changed = True
        if prior.value != money_of(split.value):
            details.append(
                f"amount on {label}: {prior.value.format()} -> {money_of(split.value).format()}"
            )
            ledger_changed = True
        if prior.quantity != money_of(split.quantity):
            ledger_changed = True
        if prior.quantity != money_of(split.quantity) and split.quantity != split.value:
            details.append(f"quantity on {label}: {prior.quantity} -> {money_of(split.quantity)}")
            ledger_changed = True
        for field_label, before, after in (
            ("memo", prior.memo, split.memo),
            ("action", prior.action, split.action),
        ):
            if before != after:
                details.append(
                    f"{field_label} on {label}: {shown_value(before)} -> {shown_value(after)}"
                )
                ledger_changed = True
        if prior.state != split.state:
            reconcile_lines.append(f"reconcile on {label}: {prior.state} -> {split.state}")
    if ledger_changed and existing.reconciled:
        raise _Refused("reconciled in GnuCash; only its reconcile state is written back")
    if ledger_changed and existing.in_lot:
        raise _Refused("part of a GnuCash lot; only its reconcile state is written back")
    if not details and not reconcile_lines:
        return None
    kinds = (("edit",) if details else ()) + (("reconcile",) if reconcile_lines else ())
    return WritebackChange(
        target.guid,
        target.post_date,
        target.description,
        kinds,
        tuple(details + reconcile_lines),
        _Operation("edit", target.guid, target),
    )
