"""Tag transactions and link documents to them or to schedules, for CLI, GTK, and web.

Tags and attachments belong to BreadSched: a GnuCash re-import keeps them, and
the document GnuCash itself links (``source_link``) is shown beside them but
never edited here. Each write is one undoable database transaction, and a
rejected request leaves the stored transaction unchanged. Removing an
attachment only unlinks it; the file is never deleted.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from ..db.sqlite import DbSQLite
from ..engine.attachments import (
    FOLDER_KEY,
    SOURCE_FOLDER_KEY,
    AttachmentStatus,
    attachment_folder,
    copy_into_folder,
    is_web_address,
    location_for,
    source_link_folder,
    statuses,
)
from ..lib.scheduled import ScheduledTransaction
from ..lib.transaction import Transaction
from .contracts import ServiceError, ServiceResult

__all__ = [
    "AttachmentReport",
    "TagCount",
    "attach_file",
    "attach_location",
    "attach_schedule_file",
    "attach_schedule_location",
    "attachment_report",
    "contained_location",
    "detach",
    "detach_schedule",
    "normalize_tags",
    "relink",
    "relink_schedule",
    "set_attachment_folder",
    "set_source_link_folder",
    "set_tags",
    "tag_counts",
    "transactions_with_tag",
]

MAX_TAG_LENGTH = 64


@dataclass(frozen=True, slots=True)
class AttachmentReport:
    folder: Path | None
    attachments: tuple[AttachmentStatus, ...]

    @property
    def missing(self) -> tuple[AttachmentStatus, ...]:
        return tuple(item for item in self.attachments if item.missing)


@dataclass(frozen=True, slots=True)
class TagCount:
    tag: str
    transactions: int


def _normalize_tags(tags: Sequence[str]) -> list[str] | ServiceError:
    kept: list[str] = []
    seen: set[str] = set()
    for raw in tags:
        tag = " ".join(raw.split())
        if not tag:
            continue
        if "," in tag or len(tag) > MAX_TAG_LENGTH:
            return ServiceError("tag.invalid", ("tags",))
        if tag.casefold() not in seen:
            seen.add(tag.casefold())
            kept.append(tag)
    return kept


def normalize_tags(db: DbSQLite, tags: Sequence[str]) -> list[str] | ServiceError:
    """Tidy, deduplicate, and respell tags as the book already spells them."""
    normalized = _normalize_tags(tags)
    if isinstance(normalized, ServiceError):
        return normalized
    known = {count.tag.casefold(): count.tag for count in tag_counts(db)}
    return [known.get(tag.casefold(), tag) for tag in normalized]


def contained_location(db: DbSQLite, location: str) -> str | ServiceError:
    """A web address, or a file location confined to the attachment folder.

    For callers that must not reach the rest of the file system (the browser):
    the location is resolved against the folder, symbolic links included, and
    refused unless it stays inside; the result is recorded relative to the folder.
    """
    location = location.strip()
    if not location:
        return ServiceError("attachment.location.required", ("location",))
    if is_web_address(location):
        return location
    folder = attachment_folder(db)
    if folder is None:
        return ServiceError("attachment.folder.unavailable", ("location",))
    root = os.path.realpath(folder)
    full = os.path.realpath(os.path.normpath(os.path.join(root, location)))
    if not full.startswith(root + os.sep):
        return ServiceError("attachment.outside_folder", ("location",))
    relative = Path(os.path.relpath(full, root)).as_posix()
    # A colon would be read back as a URI scheme or a drive letter.
    if ":" in relative:
        return ServiceError("attachment.outside_folder", ("location",))
    return relative


def _commit(db: DbSQLite, transaction: Transaction, label: str) -> ServiceResult[Transaction]:
    with db.transaction(label) as txn:
        db.commit_transaction(transaction, txn)
    return ServiceResult.success(transaction)


def set_tags(db: DbSQLite, handle: str, tags: Sequence[str]) -> ServiceResult[Transaction]:
    """Replace a transaction's tags; spelling of the first use of a tag is kept."""
    transaction = db.get_transaction(handle)
    if transaction is None:
        return ServiceResult.failure(ServiceError("transaction.not_found", ("transaction",)))
    normalized = normalize_tags(db, tags)
    if isinstance(normalized, ServiceError):
        return ServiceResult.failure(normalized)
    transaction.tags = normalized
    return _commit(db, transaction, f"Tag {transaction.description or 'transaction'}")


def tag_counts(db: DbSQLite) -> list[TagCount]:
    """Every tag in the book with how many transactions carry it, alphabetically."""
    counts: dict[str, list] = {}
    for transaction in db.iter_transactions():
        for tag in transaction.tags:
            entry = counts.setdefault(tag.casefold(), [tag, 0])
            entry[1] += 1
    return [
        TagCount(spelling, count)
        for _key, (spelling, count) in sorted(counts.items(), key=lambda item: item[0])
    ]


def transactions_with_tag(db: DbSQLite, tag: str) -> list[Transaction]:
    wanted = " ".join(tag.split()).casefold()
    return [
        transaction
        for transaction in db.iter_transactions()
        if any(item.casefold() == wanted for item in transaction.tags)
    ]


#: What may hold linked documents: a transaction, or a schedule whose every
#: posting carries them (schema 15).
Owner = Transaction | ScheduledTransaction
_OWNERS = {
    "transaction": ("transaction.not_found", "transaction"),
    "schedule": ("schedule.not_found", "schedule"),
}


def _owner(db: DbSQLite, kind: str, handle: str) -> Owner | ServiceError:
    found = db.get_transaction(handle) if kind == "transaction" else db.get_scheduled(handle)
    if found is None:
        code, field = _OWNERS[kind]
        return ServiceError(code, (field,))
    return found


def _store(db: DbSQLite, owner: Owner, label: str) -> ServiceResult:
    with db.transaction(label) as txn:
        if isinstance(owner, Transaction):
            db.commit_transaction(owner, txn)
        else:
            db.commit_scheduled(owner, txn)
    return ServiceResult.success(owner)


def _link(db: DbSQLite, kind: str, handle: str, location: str) -> ServiceResult:
    owner = _owner(db, kind, handle)
    if isinstance(owner, ServiceError):
        return ServiceResult.failure(owner)
    location = location.strip()
    if not location:
        return ServiceResult.failure(ServiceError("attachment.location.required", ("location",)))
    if location in owner.attachments:
        return ServiceResult.failure(ServiceError("attachment.duplicate", ("location",)))
    owner.attachments.append(location)
    return _store(db, owner, "Link document")


def _attach_file(db: DbSQLite, kind: str, handle: str, source: Path, copy: bool) -> ServiceResult:
    owner = _owner(db, kind, handle)
    if isinstance(owner, ServiceError):
        return ServiceResult.failure(owner)
    if not source.is_file():
        return ServiceResult.failure(ServiceError("attachment.file.not_found", ("file",)))
    if not copy:
        return _link(db, kind, handle, location_for(db, source))
    if attachment_folder(db) is None:
        return ServiceResult.failure(ServiceError("attachment.folder.unavailable", ("file",)))
    location = copy_into_folder(db, source)
    result = _link(db, kind, handle, location)
    if result.value is None:
        folder = attachment_folder(db)
        if folder is not None:
            (folder / location).unlink(missing_ok=True)
    return result


def _detach(db: DbSQLite, kind: str, handle: str, location: str) -> ServiceResult:
    owner = _owner(db, kind, handle)
    if isinstance(owner, ServiceError):
        return ServiceResult.failure(owner)
    if location not in owner.attachments:
        return ServiceResult.failure(ServiceError("attachment.not_found", ("location",)))
    owner.attachments.remove(location)
    return _store(db, owner, "Unlink document")


def _relink(db: DbSQLite, kind: str, handle: str, old: str, new: str) -> ServiceResult:
    owner = _owner(db, kind, handle)
    if isinstance(owner, ServiceError):
        return ServiceResult.failure(owner)
    if old not in owner.attachments:
        return ServiceResult.failure(ServiceError("attachment.not_found", ("location",)))
    new = new.strip()
    if not new:
        return ServiceResult.failure(ServiceError("attachment.location.required", ("location",)))
    if new != old and new in owner.attachments:
        return ServiceResult.failure(ServiceError("attachment.duplicate", ("location",)))
    owner.attachments[owner.attachments.index(old)] = new
    return _store(db, owner, "Relink document")


def attach_location(db: DbSQLite, handle: str, location: str) -> ServiceResult[Transaction]:
    """Link a web address, an absolute path, or a path inside the attachment folder."""
    return _link(db, "transaction", handle, location)


def attach_file(
    db: DbSQLite, handle: str, source: Path, *, copy: bool = True
) -> ServiceResult[Transaction]:
    """Link a file; by default copy it into the attachment folder first.

    Without ``copy`` the file stays where it is and is linked by absolute path.
    A copy made for a write that is then refused is removed again.
    """
    return _attach_file(db, "transaction", handle, source, copy)


def detach(db: DbSQLite, handle: str, location: str) -> ServiceResult[Transaction]:
    """Unlink a document; the file itself is kept."""
    return _detach(db, "transaction", handle, location)


def relink(db: DbSQLite, handle: str, old: str, new: str) -> ServiceResult[Transaction]:
    """Point a missing (or moved) attachment at its new location, keeping its place."""
    return _relink(db, "transaction", handle, old, new)


def attach_schedule_location(
    db: DbSQLite, handle: str, location: str
) -> ServiceResult[ScheduledTransaction]:
    """Link a document to a schedule; every transaction it posts links it too."""
    return _link(db, "schedule", handle, location)


def attach_schedule_file(
    db: DbSQLite, handle: str, source: Path, *, copy: bool = True
) -> ServiceResult[ScheduledTransaction]:
    """Link a file to a schedule, copying it into the attachment folder by default."""
    return _attach_file(db, "schedule", handle, source, copy)


def detach_schedule(
    db: DbSQLite, handle: str, location: str
) -> ServiceResult[ScheduledTransaction]:
    """Unlink a schedule's document; transactions already posted keep theirs."""
    return _detach(db, "schedule", handle, location)


def relink_schedule(
    db: DbSQLite, handle: str, old: str, new: str
) -> ServiceResult[ScheduledTransaction]:
    """Point a schedule's moved or missing document at its new location."""
    return _relink(db, "schedule", handle, old, new)


def set_attachment_folder(db: DbSQLite, folder: str) -> ServiceResult[Path]:
    """Choose where relative attachment locations resolve; empty restores the default."""
    folder = folder.strip()
    if folder and db.path in (None, ":memory:") and not Path(folder).is_absolute():
        return ServiceResult.failure(ServiceError("attachment.folder.relative", ("folder",)))
    db.set_metadata(FOLDER_KEY, folder or None)
    resolved = attachment_folder(db)
    if resolved is None:
        return ServiceResult.failure(ServiceError("attachment.folder.unavailable", ("folder",)))
    return ServiceResult.success(resolved)


def set_source_link_folder(db: DbSQLite, folder: str) -> ServiceResult[Path]:
    """Match GnuCash's "Path head for linked files"; empty restores the home folder."""
    folder = folder.strip()
    if folder and not Path(folder).expanduser().is_absolute():
        return ServiceResult.failure(ServiceError("attachment.folder.relative", ("folder",)))
    db.set_metadata(SOURCE_FOLDER_KEY, folder or None)
    return ServiceResult.success(source_link_folder(db))


def attachment_report(
    db: DbSQLite,
    *,
    missing_only: bool = False,
    transaction: str | None = None,
    schedule: str | None = None,
) -> AttachmentReport:
    """Every linked document in the book (or one transaction's, or one schedule's),
    checked against the file system. The whole book's lists transactions first,
    then schedules."""
    found: list[AttachmentStatus] = []
    owners: list[Owner]
    if transaction is not None:
        one = db.get_transaction(transaction)
        owners = [one] if one is not None else []
    elif schedule is not None:
        held = db.get_scheduled(schedule)
        owners = [held] if held is not None else []
    else:
        owners = [*db.iter_transactions(), *db.iter_scheduled()]
    for item in owners:
        if not item.attachments and not item.source_link:
            continue
        found.extend(statuses(db, item))
    if missing_only:
        found = [item for item in found if item.missing]
    return AttachmentReport(attachment_folder(db), tuple(found))
