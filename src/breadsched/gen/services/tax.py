"""Mark the accounts and tags whose totals a tax year reports.

An account follows GnuCash's *tax related* mark until BreadSched marks it
(``Account.tax_relevant_override``); the BreadSched mark is kept across re-import
and never written back. Tags are marked in book metadata
(``engine.tax_year.TAX_TAGS_KEY``). One request changes any number of marks as
one undo step, and a rejected request changes nothing.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from ..db.sqlite import DbSQLite
from ..engine.tax_year import TAX_TAGS_KEY, gnucash_tax_related, tax_relevant_tags
from ..lib.account import Account
from .attachments import normalize_tags, tag_counts
from .contracts import ServiceError, ServiceResult

__all__ = ["SetTaxMarks", "TaxAccountMark", "TaxMarks", "set_tax_marks", "tax_marks"]


@dataclass(frozen=True, slots=True)
class TaxAccountMark:
    account: Account
    full_name: str
    #: GnuCash's own "tax related" mark, as last imported.
    gnucash: bool
    #: BreadSched's mark, or None to follow GnuCash.
    override: bool | None

    @property
    def relevant(self) -> bool:
        return self.override if self.override is not None else self.gnucash


@dataclass(frozen=True, slots=True)
class TaxMarks:
    accounts: tuple[TaxAccountMark, ...]
    #: Every tag in the book and every marked tag, with whether it is marked.
    tags: tuple[tuple[str, bool], ...]


@dataclass(frozen=True, slots=True)
class SetTaxMarks:
    """Account handle -> True, False, or None (follow GnuCash); tag -> marked."""

    accounts: Mapping[str, bool | None] = field(default_factory=dict)
    tags: Mapping[str, bool] = field(default_factory=dict)


def tax_marks(db: DbSQLite) -> TaxMarks:
    """Every account and tag with its tax mark, by full name and tag."""
    accounts = sorted(
        (account for account in db.iter_accounts() if not account.is_root),
        key=lambda account: db.full_name(account).casefold(),
    )
    marked = {tag.casefold(): tag for tag in tax_relevant_tags(db)}
    tags = {count.tag.casefold(): count.tag for count in tag_counts(db)}
    for key, tag in marked.items():
        tags.setdefault(key, tag)
    return TaxMarks(
        tuple(
            TaxAccountMark(
                account,
                db.full_name(account),
                gnucash_tax_related(account),
                account.tax_relevant_override,
            )
            for account in accounts
        ),
        tuple((tag, key in marked) for key, tag in sorted(tags.items())),
    )


def set_tax_marks(db: DbSQLite, request: SetTaxMarks) -> ServiceResult[TaxMarks]:
    """Change the marks named in ``request`` together, as one undo step."""
    errors: list[ServiceError] = []
    changed: list[Account] = []
    for handle, mark in request.accounts.items():
        account = db.get_account(handle)
        if account is None or account.is_root:
            errors.append(ServiceError("tax.account.not_found", (f"accounts.{handle}",)))
            continue
        if mark is not None and not isinstance(mark, bool):
            errors.append(ServiceError("tax.mark.invalid", (f"accounts.{handle}",)))
            continue
        if account.tax_relevant_override != mark:
            # Edit a copy: the cached account must not change if the request fails.
            edited = Account.from_dict(account.serialize())
            edited.tax_relevant_override = mark
            changed.append(edited)

    current = list(tax_relevant_tags(db))
    tags = current
    if request.tags:
        normalized = normalize_tags(db, list(request.tags))
        if isinstance(normalized, ServiceError):
            errors.append(ServiceError(normalized.code, ("tags",)))
        else:
            spelled = dict(zip(request.tags, normalized, strict=False))
            if len(normalized) != len(request.tags):
                errors.append(ServiceError("tag.invalid", ("tags",)))
            else:
                tags = list(current)
                for raw, mark in request.tags.items():
                    tag = spelled[raw]
                    tags = [item for item in tags if item.casefold() != tag.casefold()]
                    if mark:
                        tags.append(tag)
                tags.sort(key=str.casefold)
    if errors:
        return ServiceResult.failure(*errors)
    if changed or tags != current:
        with db.transaction("Change tax marks") as txn:
            for account in changed:
                db.commit_account(account, txn)
            if tags != current:
                db.set_metadata(TAX_TAGS_KEY, tags, txn)
    return ServiceResult.success(tax_marks(db))
