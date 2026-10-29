"""Whether hidden accounts are shown (**View → Show Hidden Accounts**).

One setting for the whole application: while it is on, a hidden account is listed
and usable everywhere an account is chosen (registers, the transaction editor,
schedules, reconciliation, loans), not only in the Accounts tree; while it is off,
hidden accounts stay out of those lists unless a transaction already uses them.
"""

from __future__ import annotations

__all__ = ["set_shown", "shown"]

_shown = False


def shown() -> bool:
    return _shown


def set_shown(value: bool) -> None:
    global _shown
    _shown = bool(value)
