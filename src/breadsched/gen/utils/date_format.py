"""How a date is written for people to read, and the order a typed date is read in.

The desktop shows every date in one style the user chooses in Edit → Preferences;
by default it follows the computer's locale. The style also decides how a typed
``3/4`` is read: month first (``mdy``) or day first (``dmy``, ``dmy-dot``), so a
date can always be typed back the way it is shown. ``YYYY-MM-DD`` is always read
as such, whatever the style. Stored dates, file contents, URLs, and the command
line are ISO and never pass through here.

Standard library only (``gen`` is), so every interface can share it.
"""

from __future__ import annotations

import locale
import re
import time
from datetime import date

__all__ = [
    "DEFAULT_STYLE",
    "STYLES",
    "current",
    "day_first",
    "entry_examples",
    "format_date",
    "locale_style",
    "pattern",
    "resolve",
    "rewrite_iso_dates",
    "set_current",
    "style_label",
]

#: The choices, in the order Preferences lists them; ``locale`` follows the computer.
STYLES = ("locale", "iso", "mdy", "dmy", "dmy-dot")
DEFAULT_STYLE = "locale"

_PATTERNS = {
    "iso": "YYYY-MM-DD",
    "mdy": "MM/DD/YYYY",
    "dmy": "DD/MM/YYYY",
    "dmy-dot": "DD.MM.YYYY",
}

#: An ISO date standing alone in text, as reports and notes write one.
_ISO = re.compile(r"(?<![\d-])(\d{4})-(\d{2})-(\d{2})(?![\d-])")

#: The probe whose ``%x`` reveals the locale's order: 3 February 2001.
_PROBE = (2001, 2, 3, 0, 0, 0, 5, 34, 0)

_locale_style: str | None = None

#: The style this process shows dates in: the desktop's choice in Preferences.
_current = DEFAULT_STYLE


def set_current(style: str | None) -> None:
    """Show dates in ``style`` from now on (anything unknown is the default)."""
    global _current
    _current = style if style in STYLES else DEFAULT_STYLE


def current() -> str:
    """The style chosen for this process, as stored (``locale`` by default)."""
    return _current


def locale_style() -> str:
    """The concrete style the computer's locale writes dates in (``iso`` when it
    cannot be told, and for the neutral ``C``/``POSIX`` locale)."""
    global _locale_style
    if _locale_style is None:
        _locale_style = _read_locale_style()
    return _locale_style


def _read_locale_style() -> str:
    try:
        previous = locale.setlocale(locale.LC_TIME)
    except locale.Error:
        return "iso"
    try:
        try:
            name = locale.setlocale(locale.LC_TIME, "")
        except locale.Error:
            return "iso"
        if name in ("C", "POSIX") or name.startswith(("C.", "POSIX.")):
            return "iso"
        written = time.strftime("%x", _PROBE)
    finally:
        try:
            locale.setlocale(locale.LC_TIME, previous)
        except locale.Error:
            pass
    return _style_of(written)


def _style_of(written: str) -> str:
    """The style of a date written for the probe (3 February 2001)."""
    numbers = re.findall(r"\d+", written)
    if len(numbers) != 3:
        return "iso"
    values = [int(number) for number in numbers]
    if values[0] in (2001, 1):  # year first, as 2001-02-03 or 01/02/03
        return "iso"
    if values[0] == 2 and values[1] == 3:
        return "mdy"
    if values[0] == 3 and values[1] == 2:
        return "dmy-dot" if "." in written else "dmy"
    return "iso"


def resolve(style: str | None) -> str:
    """The concrete style ``style`` names: ``locale`` (or anything unknown) is the
    locale's own."""
    if style in _PATTERNS:
        return style
    return locale_style()


def format_date(value: date, style: str | None = DEFAULT_STYLE) -> str:
    """``value`` written in ``style``."""
    concrete = resolve(style)
    if concrete == "mdy":
        return f"{value.month:02d}/{value.day:02d}/{value.year:04d}"
    if concrete == "dmy":
        return f"{value.day:02d}/{value.month:02d}/{value.year:04d}"
    if concrete == "dmy-dot":
        return f"{value.day:02d}.{value.month:02d}.{value.year:04d}"
    return value.isoformat()


def day_first(style: str | None = DEFAULT_STYLE) -> bool:
    """Whether a typed ``3/4`` means the 3rd of April in ``style``."""
    return resolve(style) in ("dmy", "dmy-dot")


def entry_examples(style: str | None = DEFAULT_STYLE) -> str:
    """Dates a field reads, written for an error message in ``style``."""
    short = "15/3" if day_first(style) else "3/15"
    return f"{format_date(date(2026, 3, 15), style)}, {short}, 15, or t"


def pattern(style: str | None = DEFAULT_STYLE) -> str:
    """How ``style`` writes a date, such as ``DD/MM/YYYY``, for a field's hint."""
    return _PATTERNS[resolve(style)]


def style_label(style: str) -> str:
    """What Preferences calls ``style``, with an example date."""
    example = date(2026, 3, 15)
    if style == "locale":
        return f"As the computer's locale writes it ({format_date(example, 'locale')})"
    return f"{format_date(example, style)} ({_PATTERNS[style]})"


def rewrite_iso_dates(text: str, style: str | None = DEFAULT_STYLE) -> str:
    """``text`` with each ISO date in it written in ``style`` instead.

    Text written for people by a shared report or note holds ISO dates; the desktop
    shows them in the chosen style. Something that only looks like a date (an
    impossible month, say) is left alone.
    """
    if resolve(style) == "iso" or "-" not in text:
        return text

    def written(match: re.Match[str]) -> str:
        try:
            value = date(int(match[1]), int(match[2]), int(match[3]))
        except ValueError:
            return match[0]
        return format_date(value, style)

    return _ISO.sub(written, text)
