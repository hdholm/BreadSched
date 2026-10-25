"""One note per transaction or account, shared with an imported source (#385).

Before schema 13 an imported transaction or account kept two notes: BreadSched's
own, and a read-only copy of the source's (GnuCash) notes refreshed on every
re-import. They are one note now, owned by BreadSched. A note's first import
seeds it, and ``imported_notes_digest`` remembers the source text it was last
reconciled with, so a later import can tell who changed what:

- the source's text equals the note: nothing to do (also after a write-back);
- the source is unchanged since the last import: the note is kept;
- only the source changed (the note is still what was imported): it is taken;
- both changed: the note is kept, and the import reports it.
"""

from __future__ import annotations

import hashlib

__all__ = ["merge_legacy_notes", "notes_digest", "reconcile_imported_notes"]


def notes_digest(text: str) -> str:
    """A short fingerprint of a note; empty for an empty note."""
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def merge_legacy_notes(imported: str, local: str) -> tuple[str, str]:
    """The one note (and its digest) for a record that kept two before schema 13.

    The imported text comes first and BreadSched's after a blank line; the same
    text in both is kept once. The digest is the imported text's exactly as the
    source holds it, so a record with only an imported note stays equal to the
    source, and a later import that changes it keeps a joined note.
    """
    digest = notes_digest(imported)
    if not imported.strip() or imported.strip() == local.strip():
        return local, digest
    if not local.strip():
        return imported, digest
    return f"{imported.rstrip()}\n\n{local.lstrip()}", digest


def reconcile_imported_notes(local: str, digest: str, imported: str) -> tuple[str, str, bool]:
    """The note to keep after an import brings ``imported``: (note, digest, conflict).

    ``conflict`` is true when both BreadSched and the source changed the note
    since the last import; BreadSched's note is kept then.
    """
    incoming = notes_digest(imported)
    if imported == local or incoming == digest:
        return local, incoming, False
    if notes_digest(local) == digest:
        return imported, incoming, False
    return local, incoming, True
