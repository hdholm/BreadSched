"""One version of a schedule as split lines, typed as a register's are.

A version (``gen.lib.schedule_versions``) is the whole transaction for some
occurrences: every leg with its account, memo, and amount. The schedule editors show
it on the same grid as the schedule's own splits (``SplitSheet``), so a leg is added,
removed, or changed by typing, and its direction is the column it is in.

An amount may also be a **formula**, typed in either amount column: ``base * 1.2``
is the leg's amount below scaled by 1.2, ``base * 650 / base_total`` scales the
transaction to move 650, and ``0`` (or leaving the leg out) stops it. A formula in
the Decrease column takes money out: there ``base`` is the amount below as that
column shows it, so ``base * 1.2`` means 20% more in either column. A version with a
formula cannot be balanced by typing; the editor checks its first occurrence.

Nothing here imports GTK.
"""

from __future__ import annotations

import re

from ..gen.lib.money import Money
from ..gen.lib.scheduled import ScheduledSplit
from .split_entry import ACTIVITIES, PURPOSES, Line, SheetRow
from .split_sheet import SplitSheet

__all__ = ["VersionSheet", "is_formula"]

#: A word in an amount cell makes it a formula: ``base``, ``base_total``, a variable,
#: or a function such as ``ipmt``.
_WORD = re.compile(r"[A-Za-z_]")


def is_formula(text: str) -> bool:
    """Whether an amount cell holds a formula rather than a number or arithmetic."""
    return bool(_WORD.search(text))


class VersionSheet(SplitSheet):
    """The legs of one version, typed on the split grid; formulas allowed."""

    def load(self, splits: list[ScheduledSplit]) -> None:
        """Show ``splits`` as lines: a fixed amount in the column its sign gives, a
        formula in the column its direction gives."""
        lines = []
        for split in splits:
            if split.formula:
                increase = split.formula if split.direction > 0 else ""
                decrease = split.formula if split.direction < 0 else ""
            else:
                value = split.amount or Money(0)
                increase = value.format() if value > 0 else ""
                decrease = (-value).format() if value < 0 else ""
                if not value:
                    increase = "0"
            lines.append(
                Line(
                    account=self.full_names.get(split.account, split.account),
                    memo=split.memo,
                    increase=increase,
                    decrease=decrease,
                    purpose=split.planning_flow.label if split.planning_flow else "",
                    activity=(split.investment_activity.label if split.investment_activity else ""),
                )
            )
        self.draft.lines = [*lines, Line()]
        self.cursor = 0
        self._rebuild()
        self.snapshot = self.state()

    def _has_formula(self) -> bool:
        return any(is_formula(line.amount_text()[0]) for line in self.draft.lines)

    def residual(self) -> Money:
        # With a formula the balance depends on the occurrence; nothing is offered.
        return Money(0) if self._has_formula() else super().residual()

    def balancing_line(self) -> int | None:
        return None if self._has_formula() else super().balancing_line()

    def _settle_line(self, row: SheetRow) -> bool:
        if self.column in ("increase", "decrease") and is_formula(self.cell_text()):
            return True  # a formula is kept as typed
        return super()._settle_line(row)

    def legs(self) -> list[ScheduledSplit] | None:
        """The lines as the version's legs, or None with the reason in
        :attr:`message` and the cursor on the line to correct."""
        legs: list[ScheduledSplit] = []
        for index, line in enumerate(self.draft.lines):
            if not line.has_input():
                continue
            name = self._resolve_account(line.account.strip(), self.account_names)
            if not line.account.strip() or name is None:
                self._fail("Choose an account for this leg.", "transfer", index)
                return None
            text, sign = line.amount_text()
            handle = self.account_names[name]
            memo = line.memo.strip()
            purpose = PURPOSES.get(line.purpose.strip())
            activity = ACTIVITIES.get(line.activity.strip())
            if is_formula(text):
                legs.append(
                    ScheduledSplit(
                        handle,
                        formula=text,
                        memo=memo,
                        planning_flow=purpose,
                        investment_activity=activity,
                        direction=sign,
                    )
                )
                continue
            try:
                value = self.read_amount(text) if text else Money(0)
            except ValueError:
                self._fail(
                    "Enter an amount, arithmetic such as 12.50+3, or a formula such as base * 1.2.",
                    "increase",
                    index,
                )
                return None
            legs.append(
                ScheduledSplit(
                    handle,
                    value if sign > 0 else -value,
                    memo=memo,
                    planning_flow=purpose,
                    investment_activity=activity,
                )
            )
        if len(legs) < 2:
            self._fail("A version needs at least two legs.", "transfer", 0)
            return None
        if all(not leg.formula for leg in legs):
            total = sum((leg.amount or Money(0) for leg in legs), Money(0))
            if total:
                self._fail(
                    f"The legs are out of balance by {total.format(parens_negative=True)}.",
                    "increase",
                    0,
                )
                return None
        return legs
