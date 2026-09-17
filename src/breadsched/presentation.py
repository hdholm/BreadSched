"""Shared adapter-owned wording for stable service errors."""

from __future__ import annotations

from .gen.services import ServiceError

_SERVICE_MESSAGES = {
    "claim.not_found": "The FSA claim no longer exists",
    "claim.link.transaction.not_found": "A linked transaction no longer exists",
    "claim.link.split.not_found": "A linked transaction entry no longer exists",
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
    "transaction.unbalanced": "The transaction is out of balance",
    "transaction.investment.invalid": "Check the investment activity classification",
    "transaction.claim.not_found": "The selected FSA claim no longer exists",
    "transaction.claim.invalid": "The transaction cannot be attached to that FSA claim",
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
}


def service_error_message(error: ServiceError) -> str:
    """Translate a machine-readable service failure at the presentation boundary."""
    return _SERVICE_MESSAGES.get(error.code, error.code)
