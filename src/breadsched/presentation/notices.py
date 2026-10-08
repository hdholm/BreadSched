"""Notices and help that the interfaces show after an action: opening a book, the
Review actions, and proposals waiting after an import or reconciliation.
"""

from __future__ import annotations

#: What each Review action does, in the words every interface shows.
REVIEW_ACTION_HELP = {
    "match": (
        "Match: this transaction is the selected planned item. The item is marked "
        "done, and Plan and Projection compare what happened with what was planned."
    ),
    "reject": (
        "Reject: this transaction is not the selected item. The item stays planned "
        "and is not offered for this transaction again."
    ),
    "skip": (
        "Skip: the selected item will not happen this time. It is removed from the "
        "plan; the transaction still needs a decision."
    ),
    "unexpected": (
        "Unexpected: this transaction was not planned. It counts as unplanned "
        "activity and leaves Review."
    ),
    "fsa": (
        "Attach to FSA claim: link this transaction to a claim in the role shown, so "
        "the claim tracks what was paid, refunded, and reimbursed."
    ),
}


def book_open_notice(path: str, *, migration_backup: str | None = None) -> str | None:
    """What to tell the user after opening a book, or ``None`` when nothing needs saying.

    A book reached through the document portal (a sandboxed BreadSched opening a
    file outside the folders it may use directly) is only that one file, so
    backups and logs that belong beside it are kept in BreadSched's data folder.
    """
    from ..gen.utils.user_paths import portal_document_id

    lines = []
    if migration_backup:
        lines.append(
            "This book was upgraded to the current format. A verified copy of it "
            f"as it was is saved at {migration_backup}."
        )
    if path != ":memory:" and portal_document_id(path) is not None:
        lines.append(
            "This book is outside the folders BreadSched can use directly, so "
            "BreadSched can reach only the book file itself. Backups made before "
            "upgrades and restores are kept in BreadSched's data folder instead of "
            "beside the book, and attachments need a folder chosen in Attachments. "
            "Keep books in Documents to avoid this."
        )
    return "\n\n".join(lines) or None


def claim_link_notice(count: int) -> str | None:
    """Where FSA statements arrive (import, reconciliation), point at proposed links."""
    if count <= 0:
        return None
    if count == 1:
        found = "1 FSA transaction looks like it belongs on a claim"
    else:
        found = f"{count} FSA transactions look like they belong on claims"
    return f"{found}; review the proposed claim links on the FSA Dashboard."


def reimbursement_notice(count: int) -> str | None:
    """Where deposits arrive (import, reconciliation), point at waiting proposals."""
    if count <= 0:
        return None
    noun = "credit looks" if count == 1 else "credits look"
    return (
        f"{count} {noun} like money back on a reimbursable expense; "
        "review them under Reimbursable Expenses."
    )
