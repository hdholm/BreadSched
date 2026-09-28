"""HTTP output for entry autocomplete proposals.

The adapter only translates query fields into the shared service's request and
its result into JSON. Matching and every eligibility rule stay in
``gen/services/autocomplete``; a proposal never writes to the book.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..gen.services.autocomplete import SuggestEntry, suggest_entry

if TYPE_CHECKING:
    from .resources import QueryParams
    from .server import Api


def entry_suggestion(api: Api, query: QueryParams) -> dict[str, object]:
    request = SuggestEntry(
        description=query.text("description") or "",
        payee=query.text("payee") or None,
        account=query.text("account") or None,
    )
    query.finish()
    result = suggest_entry(api.db, request)
    if result.value is None:
        raise api._service_resource_error(result.errors[0])
    suggestion = result.value.suggestion
    if suggestion is None:
        return {"suggestion": None}
    db = api.db
    return {
        "suggestion": {
            "source": suggestion.source,
            "date": suggestion.when.isoformat(),
            "description": suggestion.description,
            "payee": suggestion.payee,
            "transfer": suggestion.transfer_account,
            "transfer_name": (
                db.full_name(suggestion.transfer_account) if suggestion.transfer_account else None
            ),
            "amount": str(suggestion.amount.to_decimal())
            if suggestion.amount is not None
            else None,
            "splits": [
                {
                    "account": split.account,
                    "account_name": db.full_name(split.account),
                    "value": str(split.value.to_decimal()),
                    "memo": split.memo,
                }
                for split in suggestion.splits
            ],
        }
    }
