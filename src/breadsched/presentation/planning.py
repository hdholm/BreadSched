"""Wording for savings goals, the Plan, Projection notes, and cash runway."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..gen.lib.money import Money
from .benefits import reimbursement_outlook_text

if TYPE_CHECKING:
    from ..gen.engine.category_report import CategoryActivity
    from ..gen.engine.goal_projection import GoalMilestone
    from ..gen.engine.projection_result import CashRunway, Projection
    from ..gen.engine.savings_goals import GoalProgress
    from ..gen.lib.scenario import GoalOverride
    from ..gen.services.plan import PlanGoalMilestone


def goal_status_text(progress: GoalProgress) -> str:
    """One goal's status in the words every interface shows."""
    if progress.status == "closed":
        closed = progress.goal.closed_on
        return f"closed {closed.isoformat()}" if closed is not None else "closed"
    if progress.status == "not started":
        return f"starts {progress.goal.start_date.isoformat()}"
    if progress.status == "reached":
        return "fully set aside"
    if progress.basis == "time":
        return "saving (spread by day: no income scheduled)"
    return "saving"


def goal_override_text(scenario_name: str, override: GoalOverride) -> str:
    """How one scenario changes a pinned goal."""
    if override.excluded:
        return f"{scenario_name}: left out"
    parts = [
        f"target {override.target_amount.format()}" if override.target_amount else "",
        f"by {override.target_date.isoformat()}" if override.target_date else "",
        f"bought on {override.purchase_on.isoformat()}" if override.purchase_on else "",
    ]
    return f"{scenario_name}: " + ", ".join(part for part in parts if part)


def projection_goal_notes(result: Projection) -> list[str]:
    """What savings goals set aside in a projection, in the words every interface shows."""
    notes: list[str] = []
    shortfall = result.first_goal_shortfall()
    if shortfall is not None:
        notes.append(
            f"Cash no longer covers what savings goals set aside in {shortfall.label} "
            f"({shortfall.cash_after_goals.format(parens_negative=True)} after goals)."
        )
    notes.extend(f"Goal {goal_milestone_text(item)}." for item in result.goal_milestones)
    return notes


def runway_lines(runway: CashRunway) -> list[str]:
    """How long cash lasts in one projection, in the words every interface shows."""
    if runway.first_shortfall is None:
        lines = [f"Cash lasts the whole projection ({runway.months} months)."]
    else:
        lines = [
            f"Cash runs out in {runway.first_shortfall:%b %Y}, after "
            f"{runway.months_covered} month{'' if runway.months_covered == 1 else 's'}."
        ]
    if runway.lowest_month is not None:
        lines.append(
            f"Lowest cash: {runway.lowest_cash.format(parens_negative=True)} in "
            f"{runway.lowest_month:%b %Y}."
        )
    # The first month cash stops covering goal money is a savings-goal note.
    lines.extend(f"{name} runs out in {when:%b %Y}." for name, when in runway.depletions)
    return lines


def runway_comparison_text(
    primary: str, primary_runway: CashRunway, other: str, other_runway: CashRunway
) -> str:
    """Which of two scenarios' cash lasts longer, and by how much."""

    def lasts(name: str, runway: CashRunway) -> str:
        if runway.first_shortfall is None:
            return f"{name}: cash lasts the whole {runway.months} months"
        return (
            f"{name}: cash runs out in {runway.first_shortfall:%b %Y} "
            f"({runway.months_covered} months)"
        )

    gap = other_runway.months_covered - primary_runway.months_covered
    if gap == 0:
        verdict = "the same runway"
    else:
        longer = other if gap > 0 else primary
        months = abs(gap)
        verdict = f"{longer} lasts {months} month{'' if months == 1 else 's'} longer"
    return f"{lasts(primary, primary_runway)}; {lasts(other, other_runway)}; {verdict}."


def projection_notes(result: Projection) -> list[str]:
    """Runway, goal, and expected-reimbursement notes every interface shows."""
    return [
        *runway_lines(result.runway()),
        *projection_goal_notes(result),
        *(reimbursement_outlook_text(item) for item in result.reimbursements),
    ]


def plan_goal_text(milestone: PlanGoalMilestone) -> str:
    """A goal reaching its target date within the Plan range."""
    changed = " (changed in this scenario)" if milestone.overridden else ""
    return (
        f"{milestone.goal.name}: {milestone.target.format()} by "
        f"{milestone.target_date.isoformat()}{changed}; {milestone.set_aside.format()} set "
        f"aside so far, {milestone.remaining.format()} to go. Contributions are transfers, "
        "not expenses."
    )


#: Heading for the Plan's reimbursable-expense notes in every interface.
PLAN_REIMBURSABLE_HEADING = "Reimbursable expenses: gross and net cost"


def plan_reimbursable_text(row: CategoryActivity) -> str:
    """An expense category's gross cost beside its net household cost in the range.

    The category's actual figure is already the net household cost; the
    reimbursable amount is what was reimbursed or is still expected back, less
    write-offs. A negative amount is a write-off of a reimbursement expected in an
    earlier period, which raises this range's net cost without new spending.
    """
    gross = sum(row.gross, start=Money(0))
    net = gross - row.reimbursable_total
    back = row.reimbursable_total
    if back < 0:
        return (
            f"{row.full_name}: net household cost {net.format()} includes "
            f"{(-back).format()} written off from an earlier reimbursable expense; gross "
            f"cost {gross.format()}."
        )
    return (
        f"{row.full_name}: gross cost {gross.format()}, {back.format()} reimbursed or "
        f"expected back, net household cost {net.format()}."
    )


def plan_detail_cost_text(gross: Money, reimbursable: Money, net: Money) -> str:
    """A Plan cell's gross and net cost when reimbursements changed it."""
    return (
        f"Gross cost {gross.format(parens_negative=True)}; reimbursed or expected back "
        f"{reimbursable.format(parens_negative=True)}; the Actual figure is the net "
        f"household cost, {net.format(parens_negative=True)}."
    )


def goal_milestone_text(milestone: GoalMilestone) -> str:
    """A goal's target date in one projection, in the words every interface shows."""
    text = _goal_milestone_verdict(milestone)
    if milestone.purchase_on is not None:
        text += (
            f"; this scenario buys it on {milestone.purchase_on.isoformat()} into "
            f"{milestone.purchase_account_name}"
        )
    return text


def _goal_milestone_verdict(milestone: GoalMilestone) -> str:
    goal = milestone.goal
    changed = " (changed in this scenario)" if milestone.overridden else ""
    head = (
        f"{goal.name}: {milestone.target.format()} by {milestone.target_date.isoformat()}{changed}"
    )
    if milestone.month_index is None:
        return f"{head}; the target date is outside this projection"
    if milestone.covered is None:
        return (
            f"{head}; held in {milestone.account_name or 'a non-cash account'}, which this "
            "projection does not project, so it is not compared"
        )
    verdict = "covers" if milestone.covered else "does not cover"
    if not milestone.cash_account:
        assert milestone.account_close is not None and milestone.account_held is not None
        return (
            f"{head}; {milestone.account_name} is projected at "
            f"{milestone.account_close.format(parens_negative=True)}, which {verdict} the "
            f"{milestone.account_held.format()} set aside there for goals then"
        )
    assert milestone.cash_close is not None and milestone.goals_held is not None
    return (
        f"{head}; projected cash of {milestone.cash_close.format(parens_negative=True)} "
        f"{verdict} the {milestone.goals_held.format()} set aside for goals then"
    )
