"""HTTP input and output for account registers and the simple entry form.

The register route consumes its query fields and serializes the shared ledger
register; the entry route parses the two-account form into the shared
transaction service's request. Neither calculates balances or writes the book
itself, so a rejected request never changes it.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import TYPE_CHECKING, Any

from ..gen.engine import ledger
from ..gen.lib.amount import Amount
from ..gen.lib.transaction import InvestmentActivityKind
from ..gen.services import (
    ClaimAttachment,
    SaveTransaction,
    TransactionInput,
    TransactionSplitInput,
    save_transaction,
    transaction_currency,
)
from .attachment_resource import document_json
from .controls import ResourceError, input_money, service_error

if TYPE_CHECKING:
    from .context import Api
    from .resources import QueryParams

__all__ = ["register", "transaction_add"]


def register(api: Api, query: QueryParams) -> dict[str, object]:
    """One account's register rows, newest ``limit`` rows, with editing detail."""
    handle = query.text("account", required=True) or ""
    limit = query.integer("limit", default=250, minimum=1, maximum=10000) or 250
    query.finish()
    account = api.db.get_account(handle)
    if account is None:
        raise KeyError(handle)
    rows = ledger.register(api.db, handle)[-limit:]
    debit_label, credit_label = ledger.register_headings(account.atype)
    return {
        "account": api.db.full_name(account),
        "type": account.atype.value,
        "debit_label": debit_label,
        "credit_label": credit_label,
        "rows": [
            {
                "handle": row.transaction.handle,
                "date": row.post_date,
                "num": row.transaction.num,
                "description": row.description,
                "notes": row.transaction.notes,
                "source_notes": row.transaction.source_notes,
                "tags": list(row.transaction.tags),
                "documents": document_json(api.db, row.transaction),
                "transfer": row.transfer_label(api.db),
                "amount": row.amount,
                "balance": row.running,
                # For editing the row in place (#158): this register's split
                # and every split of the transaction.
                "split": row.split.handle,
                # n, c, or y for this register's split (the R column).
                "reconcile": row.split.reconcile.value,
                "currency": row.transaction.currency,
                "splits": [
                    {
                        "handle": split.handle,
                        "account": split.account,
                        "account_name": api.db.full_name(split.account),
                        "value": split.value,
                        "memo": split.memo,
                    }
                    for split in row.transaction.splits
                ],
            }
            for row in rows
        ],
    }


def transaction_add(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    """Post a two-split transaction from the simple entry form."""

    debit = api.db.get_account_by_name(payload["to"])
    credit = api.db.get_account_by_name(payload["from"])
    if debit is None or credit is None:
        raise ResourceError(400, "transaction.account.not_found", ("from", "to"))
    when = date.fromisoformat(payload.get("date") or date.today().isoformat())
    amount = input_money(dict(payload), payload["amount"])
    if amount <= 0:
        raise ResourceError(400, "transaction.amount.non_positive", ("amount",))
    description = str(payload.get("description") or "").strip()
    notes = str(payload.get("notes") or "").strip()
    memo = payload.get("memo", "")
    investment_raw = str(payload.get("investment_activity") or "").strip()
    try:
        investment_activity = InvestmentActivityKind(investment_raw) if investment_raw else None
    except ValueError:
        raise ValueError("choose a valid investment activity") from None
    claim_handle = str(payload.get("fsa_claim") or "").strip()
    claim_role = str(payload.get("fsa_role") or "").strip()
    funding_year = str(payload.get("fsa_year") or "").strip()
    attachment = (
        ClaimAttachment(
            claim_handle,
            claim_role,
            date.fromisoformat(funding_year) if funding_year else None,
        )
        if claim_handle and claim_role
        else None
    )
    currency = transaction_currency(api.db)
    result = save_transaction(
        api.db,
        SaveTransaction(
            TransactionInput(
                post_date=when,
                description=description,
                notes=notes,
                currency=currency,
                splits=(
                    TransactionSplitInput(debit.handle, Amount(amount, currency), memo=memo),
                    TransactionSplitInput(credit.handle, Amount(-amount, currency), memo=memo),
                ),
                investment_activity=investment_activity,
            ),
            claim_attachment=attachment,
        ),
    )
    if result.value is None:
        raise service_error(result.errors[0])
    return {"handle": result.value.handle, "date": when, "amount": amount}
