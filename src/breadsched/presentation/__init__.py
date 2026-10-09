"""Shared adapter-owned wording every interface (GTK, web, CLI, print) shows.

The wording is grouped by area; this package re-exports every public name, so
``from breadsched.presentation import ...`` works whatever module holds it.
"""

from __future__ import annotations

from .benefits import (
    CLAIM_ROLE_LABELS,
    claim_role_label,
    fsa_account_text,
    fsa_usage_text,
    reimbursement_outlook_text,
    reimbursement_override_text,
    shared_cost_text,
)
from .charts import (
    CHART_CHROME_DARK,
    CHART_CHROME_LIGHT,
    CHART_SERIES_DARK,
    CHART_SERIES_LIGHT,
    ChartBar,
    ChartBarLayout,
    ChartLineLayout,
    chart_bar_layout,
    chart_chrome,
    chart_label_indices,
    chart_line_layout,
    chart_nice_ticks,
    chart_series_colour,
)
from .investments import (
    holding_cost_text,
    lot_move_text,
    lot_text,
    price_text,
    sale_text,
)
from .messages import (
    configure_language,
    service_error_codes,
    service_error_message,
    service_error_templates,
)
from .notices import (
    REVIEW_ACTION_HELP,
    book_open_notice,
    book_upgrade_progress,
    claim_link_notice,
    reimbursement_notice,
)
from .planning import (
    PLAN_REIMBURSABLE_HEADING,
    goal_milestone_text,
    goal_override_text,
    goal_status_text,
    plan_detail_cost_text,
    plan_goal_text,
    plan_reimbursable_text,
    projection_goal_notes,
    projection_notes,
    runway_comparison_text,
    runway_lines,
)

__all__ = [
    "CHART_CHROME_DARK",
    "CHART_CHROME_LIGHT",
    "CHART_SERIES_DARK",
    "CHART_SERIES_LIGHT",
    "ChartBar",
    "ChartBarLayout",
    "ChartLineLayout",
    "chart_bar_layout",
    "chart_label_indices",
    "chart_line_layout",
    "chart_chrome",
    "chart_nice_ticks",
    "chart_series_colour",
    "CLAIM_ROLE_LABELS",
    "PLAN_REIMBURSABLE_HEADING",
    "REVIEW_ACTION_HELP",
    "book_open_notice",
    "book_upgrade_progress",
    "claim_link_notice",
    "claim_role_label",
    "configure_language",
    "fsa_account_text",
    "fsa_usage_text",
    "goal_milestone_text",
    "goal_override_text",
    "goal_status_text",
    "holding_cost_text",
    "price_text",
    "sale_text",
    "lot_move_text",
    "lot_text",
    "plan_detail_cost_text",
    "plan_goal_text",
    "plan_reimbursable_text",
    "projection_goal_notes",
    "projection_notes",
    "reimbursement_notice",
    "reimbursement_outlook_text",
    "reimbursement_override_text",
    "runway_comparison_text",
    "runway_lines",
    "service_error_codes",
    "service_error_message",
    "service_error_templates",
    "shared_cost_text",
]
