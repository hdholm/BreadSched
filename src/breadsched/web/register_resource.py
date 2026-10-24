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

from ..api import ApiError
from ..gen.engine.cost_basis import shares_text
from ..gen.engine.entry_input import trade_price
from ..gen.lib.amount import Amount
from ..gen.lib.transaction import InvestmentActivityKind
from ..gen.services import (
    ClaimAttachment,
    SaveTransaction,
    TransactionInput,
    TransactionSplitInput,
)
from ..presentation.investments import share_summary_text, trade_price_text
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
    try:
        view = api.book.register_view(handle, limit)
    except ApiError as exc:
        if exc.status == 404:
            raise KeyError(handle) from None
        raise
    holding = view.holding
    shares_held = set(view.security_accounts)

    def shares_of(split) -> str | None:
        # Shares as exact decimal text, for a split in an account holding a security.
        return shares_text(split.quantity) if split.account in shares_held else None

    def trade(row) -> dict[str, object]:
        if holding is None:
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
        "account": view.full_name,
        "type": view.account.atype.value,
        "debit_label": view.debit_label,
        "credit_label": view.credit_label,
        # An account holding a security shows shares, price, and shares held (#323).
        "security": (
            None
            if holding is None
            else {
                "mnemonic": holding.security.mnemonic,
                "share_fraction": holding.share_fraction,
            }
        ),
        "summary": (
            None
            if holding is None
            else share_summary_text(
                holding.security.mnemonic, holding.held, holding.valued, holding.evidence
            )
        ),
        # Accounts whose splits take shares, for the split lines' Shares cells.
        "security_accounts": list(view.security_accounts),
        "rows": [
            {
                "handle": row.transaction.handle,
                "date": row.post_date,
                "num": row.transaction.num,
                "description": row.description,
                "notes": row.transaction.notes,
                "source_notes": row.transaction.source_notes,
                "tags": list(row.transaction.tags),
                "documents": document_json(view.documents_of(row.transaction.handle)),
                "transfer": view.transfer(row),
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
                        "account_name": view.name(split.account),
                        "value": split.value,
                        "memo": split.memo,
                        # Exact shares for a split in a security account, else None.
                        "quantity": shares_of(split),
                    }
                    for split in row.transaction.splits
                ],
            }
            for row in view.rows
        ],
    }


def transaction_add(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    """Post a two-split transaction from the simple entry form."""

    try:
        debit = api.book.find_account(str(payload["to"]))
        credit = api.book.find_account(str(payload["from"]))
    except ApiError:
        raise ResourceError(400, "transaction.account.not_found", ("from", "to")) from None
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
    currency = api.book.transaction_currency()
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
