"""Web breadlets: list them, and run one report or dashboard breadlet.

The breadlet reads the open book only through its book server (``Api.book``), so
it can do no more than the signed-in role allows; its options are validated by the
shared ``resolve_options`` and the document is serialized by ``api.views``.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from ..api.contract import ApiError
from ..api.views import breadlets_json, report_document_json
from ..gen.plug import BREADLET_KINDS, DASHBOARD, REPORT, BreadletOptionError, PluginManager

if TYPE_CHECKING:
    from .context import Api
    from .resources import QueryParams


def breadlets(api: Api, query: QueryParams) -> dict[str, object]:
    """Every breadlet, by kind; ``kind`` narrows the list to one kind."""
    kind = query.text("kind")
    query.finish()
    if kind is not None and kind not in BREADLET_KINDS:
        raise ApiError(400, "query.invalid", ("kind",))
    manager = PluginManager.instance()
    kinds = (kind,) if kind else BREADLET_KINDS
    listed = [plugin for each in kinds for plugin in manager.by_category(each)]
    return breadlets_json(listed, date.today(), manager.problems)


def run_breadlet(api: Api, query: QueryParams) -> dict[str, object]:
    """One report or dashboard breadlet's document; each other field is an option."""
    breadlet_id = query.text("id", required=True)
    kind = query.choice("kind", (REPORT, DASHBOARD), REPORT)
    manager = PluginManager.instance()
    plugin = manager.get(kind, breadlet_id or "")
    if plugin is None:
        raise ApiError(404, "breadlet.not_found", ("id",), "There is no such breadlet.")
    given = {option.key: query.text(option.key) for option in plugin.options}
    query.finish()
    try:
        document = plugin.produce(
            api.book,
            {key: value for key, value in given.items() if value is not None},
            date.today(),
        )
    except BreadletOptionError as error:
        raise ApiError(400, "breadlet.option.invalid", (error.key,), str(error)) from None
    return {"breadlet": plugin.describe(date.today()), "document": report_document_json(document)}
