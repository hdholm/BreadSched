"""Normalized description keys: how two statement descriptions name the same thing.

Matching on a key is exact, never fuzzy: two descriptions match only when their
keys are identical, so every match can be explained by the key that produced it.
Categorization rules, expected-reimbursement proposals, and entry autocomplete all
compare descriptions through this one key.
"""

from __future__ import annotations

import re
import unicodedata

__all__ = ["match_key"]

_SEPARATORS = re.compile(r"[^\w]+")


def match_key(description: str) -> str:
    """The comparison key for one description.

    Case, accents' compatibility forms, punctuation, and spacing are ignored, and
    any word containing a digit is dropped, because statements append changing
    store numbers, card suffixes, and reference numbers to the same merchant.
    ``"CORNER GROCER #1234"`` and ``"Corner Grocer 0987"`` share ``"corner grocer"``.
    """
    text = unicodedata.normalize("NFKC", description).casefold().replace("_", " ")
    words = [
        word for word in _SEPARATORS.split(text) if word and not any(c.isdigit() for c in word)
    ]
    return " ".join(words)
