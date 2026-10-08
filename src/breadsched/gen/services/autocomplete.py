"""Propose an earlier transaction's accounts and amount while entering a new one.

Typing a description proposes the most recent matching transaction.
Descriptions match exactly on their normalized key
(``description_keys.match_key``), the same comparison categorization rules use,
so a proposal is always explainable. The proposal is plain data for a form: the
user edits or ignores it and saves through the ordinary transaction service. It
never carries reconcile state, source identifiers, notes, planning purposes or
links, investment activity, or FSA claims.

Candidates are limited to transactions with a split in the entry's account (when
given), in the entry's currency (the reporting currency when not given), and whose
accounts are all visible and postable, so a proposal can always be saved as is.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..db.sqlite import DbSQLite
from ..engine.currency import reporting_currency_handle
from ..engine.description_keys import match_key
from ..lib.money import Money
from .contracts import ServiceError, ServiceResult

__all__ = ["EntryProposal", "EntrySuggestion", "SuggestEntry", "SuggestedSplit", "suggest_entry"]


@dataclass(frozen=True, slots=True)
class SuggestEntry:
    description: str = ""
    #: The register account being entered into; narrows candidates and fixes
    #: ``transfer_account`` and ``amount``.
    account: str | None = None
    #: Currency of the entry; the reporting currency when not given.
    currency: str | None = None
    #: A transaction being edited, which must not be proposed to itself.
    exclude: str | None = None


@dataclass(frozen=True, slots=True)
class SuggestedSplit:
    account: str
    value: Money
    memo: str


@dataclass(frozen=True, slots=True)
class EntrySuggestion:
    #: The earlier transaction the proposal comes from (for display only).
    source: str
    when: date
    description: str
    currency: str
    splits: tuple[SuggestedSplit, ...]
    #: The other account of a two-split transaction in ``account``.
    transfer_account: str | None
    #: The signed value in ``account`` (positive increases a debit-normal account).
    amount: Money | None


@dataclass(frozen=True, slots=True)
class EntryProposal:
    #: ``None`` when no earlier transaction matches.
    suggestion: EntrySuggestion | None


def suggest_entry(db: DbSQLite, query: SuggestEntry) -> ServiceResult[EntryProposal]:
    """The most recent matching transaction, or ``None`` when nothing matches."""
    if query.account is not None and db.get_account(query.account) is None:
        return ServiceResult.failure(ServiceError("autocomplete.account.not_found", ("account",)))
    key = match_key(query.description)
    if not key:
        return ServiceResult.success(EntryProposal(None))
    reporting = reporting_currency_handle(db)
    currency = query.currency or reporting
    best = None
    for transaction in db.iter_transactions():
        if transaction.handle == query.exclude:
            continue
        if (transaction.currency or reporting) != currency:
            continue
        if match_key(transaction.description) != key:
            continue
        accounts = [db.get_account(split.account) for split in transaction.splits]
        if any(account is None or account.hidden or account.placeholder for account in accounts):
            continue
        if query.account is not None and not any(
            split.account == query.account for split in transaction.splits
        ):
            continue
        rank = (transaction.post_date, transaction.enter_date, transaction.handle)
        if best is None or rank > best[0]:
            best = (rank, transaction)
    if best is None:
        return ServiceResult.success(EntryProposal(None))
    transaction = best[1]
    splits = tuple(
        SuggestedSplit(split.account, split.value, split.memo) for split in transaction.splits
    )
    transfer = amount = None
    if query.account is not None:
        own = [split for split in splits if split.account == query.account]
        amount = own[0].value if len(own) == 1 else None
        others = [split for split in splits if split.account != query.account]
        if len(splits) == 2 and len(own) == 1 and len(others) == 1:
            transfer = others[0].account
    return ServiceResult.success(
        EntryProposal(
            EntrySuggestion(
                source=transaction.handle,
                when=transaction.post_date,
                description=transaction.description,
                currency=transaction.currency or reporting,
                splits=splits,
                transfer_account=transfer,
                amount=amount,
            )
        )
    )
