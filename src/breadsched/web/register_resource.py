"""HTTP input and output for account registers and the simple entry form.

The register route consumes its query fields and serializes the shared ledger
register; the entry route parses the two-account form into the shared
transaction service's request. Neither calculates balances or writes the book
itself, so a rejected request never changes it.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from fractions import Fraction
from typing import TYPE_CHECKING, Any

from ..gen.engine import ledger
from ..gen.engine.cost_basis import shares_text
from ..gen.engine.entry_input import trade_price
from ..gen.lib.amount import Amount
from ..gen.lib.transaction import InvestmentActivityKind
from ..gen.services import (
    ClaimAttachment,
    SaveTransaction,
    TransactionInput,
    TransactionSplitInput,
    transaction_currency,
)
from ..presentation.investments import register_share_summary, trade_price_text
from .attachment_resource import document_json
from .controls import ResourceError, input_money

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
    security = ledger.security_of(api.db, account)
    holds_shares: dict[str, bool] = {}

    def shares_of(split) -> str | None:
        # Shares as exact decimal text, for a split in an account holding a security.
        if split.account not in holds_shares:
            holds_shares[split.account] = ledger.security_of(api.db, split.account) is not None
        return shares_text(split.quantity) if holds_shares[split.account] else None

    def trade(row: ledger.RegisterRow) -> dict[str, object]:
        if security is None:
            return {}
        price = trade_price(
            Fraction(row.split.value.numerator, row.split.value.denominator),
            Fraction(row.shares.numerator, row.shares.denominator),
        )
        held = row.share_balance
        return {
            "shares": shares_text(row.shares) if row.shares else "",
            "price": trade_price_text(price),
            "share_balance": shares_text(held) if held is not None else "",
        }

    return {
        "account": api.db.full_name(account),
        "type": account.atype.value,
        "debit_label": debit_label,
        "credit_label": credit_label,
        # An account holding a security shows shares, price, and shares held (#323).
        "security": (
            None
            if security is None
            else {
                "mnemonic": security.mnemonic,
                "share_fraction": ledger.share_fraction(api.db, account),
            }
        ),
        "summary": register_share_summary(api.db, account),
        # Accounts whose splits take shares, for the split lines' Shares cells.
        "security_accounts": sorted(
            item.handle
            for item in api.db.iter_accounts()
            if ledger.security_of(api.db, item) is not None
        ),
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
                **trade(row),
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
                        # Exact shares for a split in a security account, else None.
                        "quantity": shares_of(split),
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
    saved = api.book.save_transaction(
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
    return {"handle": saved.handle, "date": when, "amount": amount}
