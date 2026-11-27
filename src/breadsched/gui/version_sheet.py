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

An account cell is left only with an account that exists, or none, as in the
register (and so are the planning purpose and activity). Nothing else is forced to
change before the schedule is saved: leaving an amount that does not read yet is
never refused, and reading the legs while the user types (:meth:`VersionSheet.legs`
with ``quiet``) never moves the cursor. Only an explicit check (Enter, or the
editor's save) puts the cursor on the line to correct.

Nothing here imports GTK.
"""

from __future__ import annotations

import re
from dataclasses import replace

from ..gen.lib.money import Money
from ..gen.lib.scheduled import ScheduledSplit
from .split_entry import ACTIVITIES, PURPOSES, Line, SheetRow
from .split_sheet import SplitSheet

__all__ = ["VersionSheet", "copy_line", "is_formula"]

#: A word in an amount cell makes it a formula: ``base``, ``base_total``, a variable,
#: or a function such as ``ipmt``.
_WORD = re.compile(r"[A-Za-z_]")


def copy_line(line: Line) -> Line:
    """An independent copy of a typed line."""
    return replace(line, settled=dict(line.settled))


def is_formula(text: str) -> bool:
    """Whether an amount cell holds a formula rather than a number or arithmetic."""
    return bool(_WORD.search(text))


class VersionSheet(SplitSheet):
    """The legs of one version, typed on the split grid; formulas allowed."""

    def load(self, splits: list[ScheduledSplit]) -> None:
        """Show ``splits`` as lines: a fixed amount in the column its sign gives, a
        formula in the column its direction gives."""
        self.load_lines(self.lines_of(splits))

    def lines_of(self, splits: list[ScheduledSplit]) -> list[Line]:
        """``splits`` as the lines :meth:`load` shows, without showing them."""
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
        return lines

    def load_lines(self, lines: list[Line]) -> None:
        """Show ``lines`` as typed (a version's lines, readable or not), with the
        empty line after them, and the cursor on the first."""
        self.draft.lines = [copy_line(line) for line in lines if line.has_input()]
        self.draft.lines.append(Line())
        self.cursor = 0
        self.message, self.error = "", False
        self._rebuild()
        self.snapshot = self.state()

    def typed_lines(self) -> list[Line]:
        """The lines as typed, without the empty last one, to keep and show again."""
        return [copy_line(line) for line in self.draft.lines if line.has_input()]

    def _has_formula(self) -> bool:
        return any(is_formula(line.amount_text()[0]) for line in self.draft.lines)

    def residual(self) -> Money:
        # With a formula the balance depends on the occurrence; nothing is offered.
        return Money(0) if self._has_formula() else super().residual()

    def balancing_line(self) -> int | None:
        return None if self._has_formula() else super().balancing_line()

    def _settle_line(self, row: SheetRow) -> bool:
        if self.column not in ("increase", "decrease"):
            # An account (or planning purpose or activity) is one that exists, or
            # none, when its cell is left, as in the register.
            return super()._settle_line(row)
        if is_formula(self.cell_text()):
            return True  # a formula is kept as typed
        held = (self.message, self.error)
        if not super()._settle_line(row):
            # An amount is kept as typed: one that does not read yet is reported
            # when the version is checked or the schedule saved, never by
            # stopping the cursor here.
            self.message, self.error = held
        return True

    def legs(self, *, quiet: bool = False) -> list[ScheduledSplit] | None:
        """The lines as the version's legs, or None with the reason in
        :attr:`message`. Unless ``quiet``, the cursor moves to the line to correct;
        quietly, the cursor, column, and message stay as they are and the reason is
        returned in :attr:`problem`."""
        if not quiet:
            found = self._legs()
            self.problem = "" if found is not None else self.message
            return found
        held = (self.cursor, self.column, self.message, self.error)
        self._quiet = True
        try:
            found = self._legs()
            self.problem = "" if found is not None else self.message
        finally:
            self._quiet = False
            self.cursor, self.column, self.message, self.error = held
        return found

    def legs_of(self, lines: list[Line]) -> tuple[list[ScheduledSplit] | None, str]:
        """``lines`` read as legs, quietly, without showing them: the legs or None,
        and the reason they cannot be read."""
        shown = self.draft.lines
        self.draft.lines = [*lines, Line()]
        try:
            return self.legs(quiet=True), self.problem
        finally:
            self.draft.lines = shown

    #: Why the lines last read (:meth:`legs`) cannot be legs, or "".
    problem = ""

    def _legs(self) -> list[ScheduledSplit] | None:
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
