"""Web tax year: the report, and which accounts and tags it totals.

The report is derived in ``gen/engine/tax_year`` and the marks are changed through
``gen/services/tax``; this adapter only parses queries and payloads and
serializes them.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import TYPE_CHECKING, Any

from ..gen.engine.cost_basis import shares_text
from ..gen.engine.tax_year import currency_labels, tax_year, tax_years
from ..gen.services import SetTaxMarks, TaxMarks, set_tax_marks, tax_marks

if TYPE_CHECKING:
    from .context import Api
    from .resources import QueryParams


def tax_year_report(api: Api, query: QueryParams) -> dict[str, object]:
    """One calendar year; without ``year``, the latest year with a transaction."""
    year = query.integer("year", minimum=1900, maximum=9999)
    query.finish()
    years = tax_years(api.db)
    if year is None:
        year = years[0] if years else date.today().year
    report = tax_year(api.db, year)
    labels = currency_labels(api.db, report)

    def label(handle: str | None) -> str:
        return labels.get(handle, handle or "")

    return {
        "year": report.year,
        "available_years": list(years),
        "gain_totals": [
            {
                "term": total.term.value,
                "label": total.term.label,
                "currency": label(total.currency),
                "lines": total.lines,
                "proceeds": total.proceeds,
                "cost": total.cost,
                "gain": total.gain,
            }
            for total in report.gain_totals
        ],
        "gains": [
            {
                **line.as_dict(label(line.currency)),
                "label": line.term.label,
                "quantity": shares_text(line.quantity),
            }
            for line in report.gains
        ],
        "accounts": [
            {
                "account": item.account,
                "name": item.full_name,
                "currency": label(item.currency),
                "amount": item.amount,
                "transactions": item.transactions,
                "marked_by": item.marked_by,
            }
            for item in report.accounts
        ],
        "tags": [
            {
                "tag": item.tag,
                "currency": label(item.currency),
                "spent": item.spent,
                "received": item.received,
                "transactions": item.transactions,
            }
            for item in report.tags
        ],
        "income": [
            {
                "account": item.account,
                "name": item.full_name,
                "currency": label(item.currency),
                "amount": item.amount,
                "transactions": item.transactions,
            }
            for item in report.income
        ],
        "income_totals": [
            {"currency": label(currency), "amount": total}
            for currency, total in report.income_totals
        ],
        "problems": list(report.problems),
    }


def _marks_data(marks: TaxMarks) -> dict[str, object]:
    return {
        "accounts": [
            {
                "account": mark.account.handle,
                "name": mark.full_name,
                "relevant": mark.relevant,
                "gnucash": mark.gnucash,
                "breadsched": mark.override,
            }
            for mark in marks.accounts
        ],
        "tags": [{"tag": tag, "relevant": marked} for tag, marked in marks.tags],
    }


def tax_marks_view(api: Api, query: QueryParams) -> dict[str, object]:
    """Every account and tag with whether the tax year totals it."""
    query.finish()
    return _marks_data(tax_marks(api.db))


def tax_marks_save(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    """Change marks: ``accounts`` maps a handle to true, false, or null (follow GnuCash);
    ``tags`` maps a tag to true or false."""
    from .controls import service_error

    accounts = payload.get("accounts", {})
    tags = payload.get("tags", {})
    if not isinstance(accounts, dict) or not all(
        isinstance(key, str) and (value is None or isinstance(value, bool))
        for key, value in accounts.items()
    ):
        raise ValueError("accounts must map an account to true, false, or null")
    if not isinstance(tags, dict) or not all(
        isinstance(key, str) and isinstance(value, bool) for key, value in tags.items()
    ):
        raise ValueError("tags must map a tag to true or false")
    result = set_tax_marks(api.db, SetTaxMarks(accounts, tags))
    if result.value is None:
        raise service_error(result.errors[0])
    return _marks_data(result.value)
