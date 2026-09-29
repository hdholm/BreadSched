"""Shared adapter-owned wording for stable service errors."""

from __future__ import annotations

import gettext
from pathlib import Path
from typing import TYPE_CHECKING

from .gen.services import ServiceError

if TYPE_CHECKING:
    from .gen.engine.fsa_claims import SharedCost

_LOCALE_DIR = Path(__file__).with_name("locale")
_translation: gettext.NullTranslations = gettext.translation(
    "breadsched", localedir=_LOCALE_DIR, fallback=True
)

_SERVICE_MESSAGES = {
    "account.not_found": "The account no longer exists",
    "account.name.required": "Give the account a name",
    "account.name.duplicate": "An account with that name already exists under this parent",
    "account.identity.changed": "The account identity changed while it was edited",
    "account.source.mismatch": "The account changed while it was being edited",
    "account.type.user_required": "Choose a user account type",
    "account.parent.required": "Choose a parent account",
    "account.parent.not_found": "The parent account no longer exists",
    "account.parent.cycle": "An account cannot be its own ancestor",
    "account.commodity.not_found": "The account commodity no longer exists",
    "account.commodity_scu.invalid": "Commodity SCU must be a positive integer",
    "account.linked_asset.not_found": "The linked asset no longer exists",
    "account.linked_asset.invalid": "A loan can only link to an asset account",
    "account.card.type_required": "Card payment settings require a credit-card account",
    "account.card.payment_day.invalid": "Payment day must be between 1 and 28",
    "account.card.usual_payment.required": "A card carrying a balance needs a usual payment",
    "account.card.usual_payment.non_positive": "The usual payment must be positive",
    "account.card.payment_account.not_found": "The card payment account no longer exists",
    "account.card.payment_account.invalid": "Choose a Bank or Cash payment account",
    "account.card.payment_account.hidden": "A hidden account cannot fund a new card payment",
    "account.fsa.years.overlap": "FSA funding years cannot overlap",
    "account.source_fields.read_only": (
        "Imported name, parent, code, and description are controlled by the source book"
    ),
    "account.opening.equity.not_found": "No equity account can receive the opening balance",
    "account.root.protected": "The root account cannot be deleted",
    "account.in_use": "Accounts with history or children cannot be deleted",
    "claim.not_found": "The FSA claim no longer exists",
    "claim.link.transaction.not_found": "A linked transaction no longer exists",
    "claim.link.split.not_found": "A linked transaction entry no longer exists",
    "claim.receivable.not_found": "The linked receivable no longer exists",
    "claim.receivable.taken": "Another FSA claim already covers the rest of this receivable",
    "claim.allocation.account.invalid": "A claim allocation must use an FSA account",
    "claim.allocation.year.not_found": "The selected FSA funding year no longer exists",
    "claim.allocation.target.negative": "An FSA allocation target cannot be negative",
    "claim.rejection.amount.negative": "A rejected reimbursement cannot be negative",
    "claim.rejection.after_runout": "A rejected reimbursement is after the run-out window",
    "claim.reimbursement.duplicate": "A reimbursement can only be allocated once",
    "claim.reimbursement.account.mismatch": (
        "A reimbursement does not belong to the selected FSA account"
    ),
    "claim.reimbursement.after_runout": "A reimbursement is after the run-out window",
    "reconciliation.not_found": "The reconciliation no longer exists",
    "reconciliation.account.not_found": "The account no longer exists",
    "reconciliation.account.ineligible": (
        "Reconciliation requires an asset or liability posting account"
    ),
    "reconciliation.open.multiple": "The account has more than one open reconciliation",
    "reconciliation.open.exists": "Finish or cancel the open reconciliation first",
    "reconciliation.statement_date.not_after_completed": (
        "The statement date must follow the latest completed statement"
    ),
    "reconciliation.split.ineligible": "A selected entry is not eligible for this statement",
    "reconciliation.unbalanced": "The reconciliation is out of balance",
    "reconciliation.status.not_completed": "Only a completed reconciliation can be reopened",
    "reconciliation.status.not_open": "The reconciliation is no longer open",
    "reconciliation.later_completed.exists": "Reopen later statements first",
    "reconciliation.split.changed": "A reconciled entry changed after completion",
    "reconciliation.split.missing": "A reconciled entry is missing",
    "transaction.not_found": "The transaction no longer exists",
    "transaction.source.mismatch": "The transaction changed while it was being edited",
    "transaction.description.required": "Give the transaction a description",
    "transaction.splits.too_few": "A transaction needs at least two splits",
    "transaction.accounts.same": "Use at least two different accounts",
    "transaction.account.not_found": "Choose an existing account for every split",
    "transaction.account.hidden": "Hidden accounts cannot be used for new transactions",
    "transaction.split.duplicate": "A transaction split was submitted more than once",
    "transaction.split.not_found": "A transaction split no longer exists",
    "transaction.currency.not_found": "Choose an existing transaction currency",
    "transaction.currency.invalid": "The transaction commodity must be a currency",
    "transaction.value.commodity": "Every split value must use the transaction currency",
    "transaction.quantity.commodity": "A split quantity must use its account commodity",
    "transaction.quantity.required": "Enter the account-commodity quantity for this split",
    "transaction.quantity.conversion": (
        "This split converts at its own rate; its amount cannot change without its quantity"
    ),
    "transaction.unbalanced": "The transaction is out of balance",
    "transaction.investment.invalid": "Check the investment activity classification",
    "transaction.claim.not_found": "The selected FSA claim no longer exists",
    "transaction.claim.invalid": "The transaction cannot be attached to that FSA claim",
    "transaction.receivable_posting": (
        "BreadSched keeps this transaction for a reimbursable expense; change the "
        "receivable instead"
    ),
    "loan.name.required": "Give the loan a name",
    "loan.principal.non_positive": "The amount borrowed must be positive",
    "loan.rate.negative": "The annual rate cannot be negative",
    "loan.years.out_of_range": "The term must be between 1 and 100 years",
    "loan.account.not_found": "A selected loan account no longer exists",
    "loan.account.unavailable": "Choose a visible posting account",
    "loan.account.liability_required": "Choose a loan or liability account",
    "loan.account.expense_required": "Choose an interest expense account",
    "loan.account.cash_required": "Choose a Bank or Cash payment account",
    "loan.escrow.incomplete": "Give both an escrow amount and an escrow account",
    "loan.escrow.non_positive": "The escrow amount must be positive",
    "loan.account.escrow_required": "Choose an escrow account",
    "import.source.required": "Choose a file to import",
    "import.source.not_found": "no file at the selected path",
    "import.source.not_file": "Choose a file, not a directory",
    "import.format.not_found": "The requested importer is not available",
    "import.format.unrecognized": "The file format is not recognised",
    "import.number_format.invalid": "Choose a valid number format",
    "import.date_format.invalid": "Choose a valid QIF date order",
    "writeback.source.unknown": (
        "Import the GnuCash SQLite book into BreadSched before writing changes back to it"
    ),
    "writeback.source.missing": "The GnuCash book is no longer at the path it was imported from",
    "writeback.source.other_book": "The file at that path is a different GnuCash book",
    "writeback.source.changed": (
        "The GnuCash book changed since it was last imported; import it again first"
    ),
    "writeback.source.locked": "GnuCash has the book open; close it in GnuCash first",
    "writeback.source.conflict": (
        "The GnuCash book no longer matches the preview; nothing was written"
    ),
    "writeback.verify.failed": (
        "Reading the written rows back did not match; the GnuCash book was restored"
    ),
    "writeback.selection.invalid": "Choose one or more changes from the preview",
    "writeback.roundtrip.mismatch": (
        "GnuCash would not read the change back exactly as written; the book was restored"
    ),
    "writeback.keep_backups.invalid": "Keep between 1 and 1000 backups",
    "rule.match.required": "Match a rule on either a payee or a description, not both",
    "rule.match.empty": (
        "A description match must contain at least one word without digits, such as the "
        "merchant name"
    ),
    "rule.match.duplicate": "Another rule already matches that payee or description",
    "rule.payee.not_found": "That payee no longer exists",
    "rule.category.invalid": "Choose an income or expense category that is not a placeholder",
    "rule.not_found": "That rule no longer exists",
    "rule.position.invalid": "Choose a position within the rule list",
    "rule.transaction.not_found": "That transaction no longer exists",
    "payee.name.required": "Enter a payee name",
    "payee.name.duplicate": "Another payee already has that name",
    "payee.match.empty": (
        "A match must contain at least one word without digits, such as the merchant name"
    ),
    "payee.match.conflict": "That description already identifies another payee",
    "payee.not_found": "That payee no longer exists",
    "payee.transaction.not_found": "That transaction no longer exists",
    "savings_goal.name.required": "Give the savings goal a name",
    "savings_goal.account.invalid": (
        "Choose an asset account in the reporting currency to hold the goal's money"
    ),
    "savings_goal.target.invalid": "The target amount must be more than zero",
    "savings_goal.target.below_allocated": (
        "The target cannot be less than the extra money already allocated"
    ),
    "savings_goal.dates.invalid": "The target date must be after the start date",
    "savings_goal.not_found": "That savings goal no longer exists",
    "savings_goal.closed": "That savings goal is closed",
    "savings_goal.open": "That savings goal is not closed",
    "savings_goal.allocation.invalid": "The amount to allocate must be more than zero",
    "savings_goal.allocation.exceeds_target": (
        "Allocations cannot add up to more than the goal's target"
    ),
    "savings_goal.close.before_start": "A goal cannot close before it starts",
    "receivable.payer.required": "Enter who owes this reimbursement",
    "receivable.not_found": "That receivable no longer exists",
    "receivable.expected_amount.negative": "The expected amount must not be negative",
    "receivable.transaction.not_found": "That transaction no longer exists",
    "receivable.split.not_found": "That split no longer exists",
    "receivable.split.not_expense": "Choose a split posted to an expense account",
    "receivable.split.not_a_cost": "Choose a split that records money spent, not received",
    "receivable.split.not_a_credit": "Choose a split that records money credited back",
    "receivable.split.duplicate": "That split is already linked to this receivable",
    "receivable.link.not_found": "That split is not linked to this receivable",
    "receivable.write_off.amount_not_positive": "The write-off amount must be positive",
    "receivable.split.owned_posting": (
        "That is BreadSched's own receivable reclassification; link the original "
        "transaction instead"
    ),
    "receivable.link.transaction.not_found": "A linked transaction no longer exists",
    "receivable.link.split.not_found": "A linked split no longer exists",
    "receivable.account.not_found": "That receivable account no longer exists",
    "receivable.account.not_receivable": "Choose a Receivable account to hold what is owed",
    "receivable.account.currency_mismatch": (
        "Choose a receivable account in the same currency as the linked expense"
    ),
    "receivable.currency.mixed": (
        "Linked expense and reimbursement splits must all be in one currency"
    ),
    "import.csv.account.invalid": "Choose a bank, cash, card, or other posting asset or liability",
    "import.csv.column.not_found": "A mapped column is not in the CSV file",
    "import.csv.amount.mapping": "Map either one amount column or debit and credit columns",
    "import.csv.date_format.ambiguous": (
        "Every date could be day-first or month-first; choose the date order"
    ),
    "import.csv.date_format.conflict": "The file mixes day-first and month-first dates",
    "import.csv.number_format.conflict": "The file mixes decimal-point and decimal-comma amounts",
    "import.csv.encoding.invalid": "The file cannot be read with the chosen encoding",
    "import.csv.delimiter.invalid": "Use a single-character delimiter",
    "schedule.due.duplicate": "Decide each due occurrence only once",
    "schedule.due.not_found": "The schedule no longer exists",
    "schedule.due.not_pending": (
        "That occurrence is no longer due; it may already have been posted or skipped"
    ),
    "schedule.due.unbalanced": "A scheduled transaction does not balance",
    "import.review.duplicate": "Decide each held GnuCash change only once",
    "import.review.not_pending": "That GnuCash change is no longer awaiting review",
    "import.review.missing": "The reconciled transaction no longer exists",
    "import.review.reconciliation_blocks": (
        "Reopen the completed statement before using the GnuCash version"
    ),
    "import.review.unbalanced": "The GnuCash version does not balance",
    "review.transaction.not_found": "The transaction no longer exists",
    "review.transaction.not_unresolved": "The transaction is no longer awaiting review",
    "review.occurrence.not_found": "The planned occurrence no longer exists",
    "review.occurrence.resolved": "The planned occurrence is already resolved",
    "review.occurrence.not_skippable": "This planned occurrence cannot be skipped",
    "review.claim.not_found": "The FSA claim no longer exists",
    "claim.attachment.split.ineligible": "Choose one eligible transaction split",
    "claim.attachment.funding_year.required": ("Choose an FSA funding year for this reimbursement"),
    "claim.attachment.role.invalid": "Choose a valid FSA claim role",
    "scenario.not_found": "The scenario no longer exists",
    "scenario.name.required": "Give the scenario a name",
    "scenario.name.duplicate": "A scenario with that name already exists",
    "scenario.identity.changed": "The scenario identity changed while it was edited",
    "scenario.parent.requires_inheritance": "A parent requires inherited assumptions",
    "scenario.parent.not_found": "The parent scenario no longer exists",
    "scenario.parent.cycle": "A scenario cannot inherit from itself or its descendants",
    "scenario.children.exist": "Reparent child scenarios before deleting this scenario",
    "scenario.schedule.not_found": "The baseline schedule no longer exists",
    "plan.start.before_book_data": "The plan starts before the book's available history",
    "plan.end.before_start": "The plan end must not precede its start",
    "plan.end.after_maximum": "The plan extends beyond the supported horizon",
    "plan.scenario.not_found": "The selected scenario no longer exists",
    "plan.comparison.not_found": "The comparison scenario no longer exists",
    "plan.comparison.same": "Choose two different scenarios to compare",
    "net_worth.range.invalid": "The net worth history must not end before it starts",
    "net_worth.range.future": "A net worth change cannot start after today",
    "net_worth.period.invalid": "Group net worth history by month, quarter, or year",
    "schedule.name.required": "Give the scheduled transaction a name",
    "schedule.not_found": "The scheduled transaction no longer exists",
    "schedule.identity.changed": "The schedule identity changed while it was edited",
    "schedule.source.not_found": "The baseline schedule no longer exists",
    "schedule.account.not_found": "A scheduled account no longer exists",
    "schedule.account.hidden": "Hidden accounts cannot be used for a new schedule",
    "schedule.accounts.same": "Choose two different accounts",
    "schedule.accounts.duplicate": "Each additional split needs a different account",
    "schedule.amount.non_positive": "The scheduled amount must be greater than zero",
    "schedule.category.classification_conflict": (
        "Choose a planning purpose or investment activity, not both"
    ),
    "schedule.category.role_required": (
        "Choose an income/expense account or an explicit planning role"
    ),
    "schedule.splits.too_few": "A schedule needs at least two splits",
    "schedule.recurrence.end_before_start": "The recurrence end precedes its start",
    "schedule.recurrence.count_invalid": "The recurrence count must be positive",
    "schedule.amount_changes.duplicate": "Future amount dates must be unique",
    "schedule.amount_changes.before_start": "Future amounts cannot precede the schedule",
    "schedule.seasonal_amounts.duplicate": "Seasonal months must be unique",
    "schedule.skipped.duplicate": "Skipped dates must be unique",
    "schedule.skipped.not_occurrence": "A skipped date is not a scheduled occurrence",
    "schedule.occurrence_adjustments.duplicate": "Adjusted occurrence dates must be unique",
    "schedule.occurrence_adjustments.not_occurrence": (
        "An adjusted date is not a scheduled occurrence"
    ),
    "schedule.occurrence_conflict": "An occurrence cannot be both skipped and adjusted",
    "schedule.split_amount_changes.duplicate": "Per-split future dates must be unique",
    "schedule.split_amount_changes.before_start": (
        "Per-split future amounts cannot precede the schedule"
    ),
    "schedule.split_amount_changes.unbalanced": "Future split amounts do not balance",
    "schedule.read_only": "This imported schedule is read-only",
    "schedule.formula.ownership": "Formula-owned schedule structure cannot be changed here",
    "schedule.formula.variables.invalid": "Check the formula variables",
    "schedule.formula.invalid": "Check the schedule formulas",
    "schedule.estimate.invalid": "The historical-estimate adjustment is invalid",
    "schedule.investment.invalid": "Check the scheduled investment classification",
    "schedule.scenario_reference.exists": (
        "Remove this schedule's scenario overrides before deleting it"
    ),
    "assumptions.rate.out_of_range": "Projection rates must be between -100% and 100%",
    "assumptions.account.not_found": "A projection-rate account no longer exists",
    "assumptions.account.unsupported": (
        "Account-specific projection rates require an investment or liability account"
    ),
    "assumptions.years.out_of_range": "Projection years must be between 1 and 100",
    "assumptions.scenario.not_found": "The scenario no longer exists",
    "assumptions.period.not_found": "The dated assumption period no longer exists",
}


def reimbursement_notice(count: int) -> str | None:
    """Where deposits arrive (import, reconciliation), point at waiting proposals."""
    if count <= 0:
        return None
    noun = "credit looks" if count == 1 else "credits look"
    return (
        f"{count} {noun} like money back on a reimbursable expense; "
        "review them under Reimbursable Expenses."
    )


def shared_cost_text(shared: SharedCost) -> str:
    """One line allocating an expense between a payer, the FSA, and you (issue #192)."""
    fsa = (
        f"{shared.fsa_share.format()} until the EOB is entered"
        if shared.waiting_eob
        else shared.fsa_share.format()
    )
    text = (
        f"Of {shared.expense.format()}: {shared.payer} pays {shared.payer_share.format()}, "
        f"the FSA {fsa}, you {shared.your_share.format(parens_negative=True)}"
    )
    if shared.needs_review:
        text += (
            f". Needs review: {shared.over_allocated.format()} more than was paid; "
            "adjust the expected amount, the EOB, or write off the difference"
        )
    return text


def service_error_message(error: ServiceError) -> str:
    """Translate a machine-readable service failure at the presentation boundary."""
    message = _SERVICE_MESSAGES.get(error.code, error.code)
    return _translation.gettext(message)


def service_error_codes() -> frozenset[str]:
    """Return the stable codes with presentation-owned English messages."""
    return frozenset(_SERVICE_MESSAGES)


def service_error_templates() -> frozenset[str]:
    """Return the English gettext message identifiers for catalog validation."""
    return frozenset(_SERVICE_MESSAGES.values())


def configure_language(languages: list[str] | None = None) -> None:
    """Load the gettext catalog for presentation-owned service wording."""
    global _translation
    _translation = gettext.translation(
        "breadsched",
        localedir=_LOCALE_DIR,
        languages=languages,
        fallback=True,
    )
