"""Breadlets: the plug-ins that show a book.

A breadlet is a registered :class:`~breadsched.gen.plug.Plugin` of one of four kinds:
an importer or exporter reads or writes a file, a report breadlet builds a printable
report, and a dashboard breadlet builds a small report the Dashboard shows below its
own sections. Report and dashboard breadlets take the open book (any book server:
in process or over HTTP) and their typed options, and return a
:class:`~breadsched.plugins.export.report_layout.ReportDocument`, which the desktop,
the browser, the command line and printing all draw the same way. A breadlet reads
the book only through the book server, so it can do nothing its user's role does
not allow.

Options are declared, not drawn: each interface builds its own form from
:class:`BreadletOption` and hands the text it collected to :func:`resolve_options`,
which validates it once for all of them.

Breadlets shipped with BreadSched register in ``register_builtins``. A separately
installed Python package can add more by naming a ``register(manager)`` function
under the ``breadsched.breadlets`` entry-point group; nothing is ever loaded from a
data folder.
"""

from __future__ import annotations

import calendar
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date

__all__ = [
    "BREADLET_KINDS",
    "DASHBOARD",
    "ENTRY_POINT_GROUP",
    "BreadletOption",
    "BreadletOptionError",
    "LoadProblem",
    "load_installed",
    "month_end",
    "month_start",
    "resolve_options",
]

log = logging.getLogger(__name__)

#: A small report the Dashboard shows below its own sections.
DASHBOARD = "dashboard"
#: The entry-point group an installed package names its ``register(manager)`` under.
ENTRY_POINT_GROUP = "breadsched.breadlets"

#: Option kinds: a date, a whole number, one of ``choices``, or on/off.
DATE, INTEGER, CHOICE, FLAG = "date", "integer", "choice", "flag"


def month_start(day: date, months_back: int = 0) -> date:
    """The first of ``day``'s month, ``months_back`` months earlier."""
    index = day.year * 12 + day.month - 1 - months_back
    return date(index // 12, index % 12 + 1, 1)


def month_end(day: date) -> date:
    return date(day.year, day.month, calendar.monthrange(day.year, day.month)[1])


@dataclass(frozen=True, slots=True)
class BreadletOption:
    """One setting a breadlet takes, with its default for a given day."""

    key: str
    label: str
    kind: str
    #: The default as text, or a function of today giving it ("" is "not set").
    default: str | Callable[[date], str] = ""
    #: For a choice, (value, label) pairs.
    choices: tuple[tuple[str, str], ...] = ()
    minimum: int | None = None
    maximum: int | None = None
    help: str = ""

    def default_text(self, today: date) -> str:
        return self.default(today) if callable(self.default) else self.default

    def as_dict(self, today: date) -> dict[str, object]:
        return {
            "key": self.key,
            "label": self.label,
            "kind": self.kind,
            "default": self.default_text(today),
            "choices": [list(choice) for choice in self.choices],
            "minimum": self.minimum,
            "maximum": self.maximum,
            "help": self.help,
        }


class BreadletOptionError(ValueError):
    """An option's text that cannot be used; ``key`` names the option."""

    def __init__(self, key: str, message: str) -> None:
        super().__init__(message)
        self.key = key


def _value(option: BreadletOption, text: str) -> object:
    if option.kind == DATE:
        try:
            return date.fromisoformat(text)
        except ValueError:
            raise BreadletOptionError(
                option.key, f"{option.label} must be a date (YYYY-MM-DD)."
            ) from None
    if option.kind == INTEGER:
        try:
            number = int(text)
        except ValueError:
            raise BreadletOptionError(
                option.key, f"{option.label} must be a whole number."
            ) from None
        low, high = option.minimum, option.maximum
        if (low is not None and number < low) or (high is not None and number > high):
            raise BreadletOptionError(
                option.key, f"{option.label} must be from {low} to {high}."
            ) from None
        return number
    if option.kind == CHOICE:
        if text not in {value for value, _label in option.choices}:
            names = ", ".join(value for value, _label in option.choices)
            raise BreadletOptionError(option.key, f"{option.label} must be one of {names}.")
        return text
    if option.kind == FLAG:
        lowered = text.lower()
        if lowered not in {"true", "false", "yes", "no", "1", "0", "on", "off"}:
            raise BreadletOptionError(option.key, f"{option.label} must be yes or no.")
        return lowered in {"true", "yes", "1", "on"}
    return text


def resolve_options(
    options: tuple[BreadletOption, ...], given: Mapping[str, str], today: date
) -> dict[str, object]:
    """Each option's typed value: the text given, else its default; blank is None.

    Raises :class:`BreadletOptionError` for text that does not fit the option, or for
    a key the breadlet does not take.
    """
    known = {option.key for option in options}
    for key in given:
        if key not in known:
            raise BreadletOptionError(key, f"This breadlet has no option named {key!r}.")
    values: dict[str, object] = {}
    for option in options:
        text = given.get(option.key)
        text = (option.default_text(today) if text is None else text).strip()
        values[option.key] = _value(option, text) if text else None
    dates = [option for option in options if option.kind == DATE]
    if len(dates) == 2:
        start, end = (values[option.key] for option in dates)
        if isinstance(start, date) and isinstance(end, date) and end < start:
            raise BreadletOptionError(dates[1].key, "The end date is before the start date.")
    return values


@dataclass
class LoadProblem:
    """An installed breadlet package that could not be loaded, and why."""

    name: str
    reason: str


def load_installed(manager, entry_points: Callable[[], object] | None = None) -> list[LoadProblem]:
    """Let each installed package's ``breadsched.breadlets`` entry point register its
    breadlets. A package that fails is reported, never fatal; nothing it registered
    before failing is kept."""
    from importlib import metadata

    found = entry_points() if entry_points else metadata.entry_points(group=ENTRY_POINT_GROUP)
    problems: list[LoadProblem] = []
    for point in found:  # type: ignore[attr-defined]
        source = f"installed: {getattr(point, 'name', point)}"
        trial = type(manager)()
        try:
            point.load()(trial)
        except Exception as error:  # noqa: BLE001 - a broken add-on must not stop the app
            log.warning("breadlet package %s failed to load: %s", source, error)
            problems.append(LoadProblem(str(getattr(point, "name", point)), str(error)))
            continue
        for plugin in trial.all():
            plugin.source = source
            manager.register(plugin)
    return problems


#: Every kind of breadlet, in the order interfaces list them.
BREADLET_KINDS = ("importer", "exporter", "report", DASHBOARD)
