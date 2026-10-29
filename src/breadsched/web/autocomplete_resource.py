"""HTTP output for entry autocomplete proposals.

The adapter only translates query fields into the shared service's request and
its result into JSON. Matching and every eligibility rule stay in
``gen/services/autocomplete``, reached through the book server (``suggest_entry``);
a proposal never writes to the book.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..gen.services.autocomplete import SuggestEntry

if TYPE_CHECKING:
    from .context import Api
    from .resources import QueryParams


def entry_suggestion(api: Api, query: QueryParams) -> dict[str, object]:
    request = SuggestEntry(
        description=query.text("description") or "",
        account=query.text("account") or None,
    )
    query.finish()
    proposal = api.book.suggest_entry(request)
    suggestion = proposal.suggestion
    if suggestion is None:
        return {"suggestion": None}
    return {
        "suggestion": {
            "source": suggestion.source,
            "date": suggestion.when.isoformat(),
            "description": suggestion.description,
            "transfer": suggestion.transfer_account,
            "transfer_name": (
                proposal.name(suggestion.transfer_account) if suggestion.transfer_account else None
            ),
            "amount": str(suggestion.amount.to_decimal())
            if suggestion.amount is not None
            else None,
            "splits": [
                {
                    "account": split.account,
                    "account_name": proposal.name(split.account),
                    "value": str(split.value.to_decimal()),
                    "memo": split.memo,
                }
                for split in suggestion.splits
            ],
        }
    }
