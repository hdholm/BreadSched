"""Presentation-owned wording for stable application-service errors."""

from __future__ import annotations

import ast
import gettext
import re
from pathlib import Path

from breadsched.gen.services import ServiceError
from breadsched.presentation import (
    configure_language,
    service_error_codes,
    service_error_message,
    service_error_templates,
)
from breadsched.web.server import Api

SERVICES = Path(__file__).resolve().parent.parent / "src" / "breadsched" / "gen" / "services"
SERVICE_CODE = re.compile(
    r"(?:account|assumptions|claim|import|loan|plan|reconciliation|review|scenario|schedule|transaction)"
    r"\.[a-z0-9_.]+"
)
CATALOG = (
    Path(__file__).resolve().parent.parent / "src/breadsched/locale/es/LC_MESSAGES/breadsched.mo"
)


def test_every_literal_service_error_code_has_presentation_wording():
    codes: set[str] = set()
    for path in SERVICES.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        codes.update(
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and SERVICE_CODE.fullmatch(node.value)
        )

    assert codes <= service_error_codes(), sorted(codes - service_error_codes())


def test_service_error_messages_are_presentation_owned_prose():
    error = ServiceError("schedule.account.not_found", ("category",))

    assert service_error_message(error) == "A scheduled account no longer exists"
    assert service_error_message(error) != error.code


def test_gettext_catalog_translates_the_shared_gtk_and_web_message_seam():
    error = ServiceError("schedule.account.not_found", ("category",))
    try:
        configure_language(["es"])
        assert service_error_message(error) == "Una cuenta programada ya no existe"
        assert Api._service_resource_error(error).message == ("Una cuenta programada ya no existe")
    finally:
        configure_language(["C"])


def test_compiled_catalog_contains_only_extractable_service_messages():
    with CATALOG.open("rb") as source:
        catalog = gettext.GNUTranslations(source)._catalog
    message_ids = {key for key in catalog if isinstance(key, str) and key}

    assert message_ids
    assert message_ids <= service_error_templates()
