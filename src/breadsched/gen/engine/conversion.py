"""One as-of reporting-currency conversion shared by Plan and Projection.

Each foreign currency is converted with the single quote applicable on the
report's as-of date (direct, else an inverted reverse quote), exactly, before
any aggregation. Values with no applicable quote are reported as unconverted
and excluded; they are never treated as reporting-currency units.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from fractions import Fraction

from ..db.sqlite import DbSQLite
from ..lib.amount import Amount
from ..lib.money import Money
from . import valuation
from .completeness import Excluded
from .currency import reporting_currency_handle
from .planning import PlannedEvent, PlannedSplit

__all__ = [
    "CurrencyEvidence",
    "ReportingConverter",
    "UnconvertedActivity",
    "conversion_notes",
    "excluded_activity",
]


@dataclass(frozen=True, slots=True)
class CurrencyEvidence:
    """The one as-of quote used to convert a currency into reporting currency."""

    source_currency: str
    target_currency: str
    rate: Money
    quote_date: date | None
    quote_source: str | None
    path: str

    def as_dict(self) -> dict[str, object]:
        return {
            "source_currency": self.source_currency,
            "target_currency": self.target_currency,
            "rate": self.rate,
            "quote_date": self.quote_date,
            "quote_source": self.quote_source,
            "path": self.path,
        }


@dataclass(frozen=True, slots=True)
class UnconvertedActivity:
    """Foreign-currency activity left out of totals because no quote applies."""

    kind: str
    when: date
    description: str
    amount: Money
    currency: str
    accounts: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "date": self.when,
            "description": self.description,
            "amount": self.amount,
            "currency": self.currency,
            "accounts": list(self.accounts),
        }


def excluded_activity(db: DbSQLite, items: Iterable[UnconvertedActivity]) -> tuple[Excluded, ...]:
    """Completeness evidence for activity left out for lack of an exchange rate."""

    def code(handle: str) -> str:
        commodity = db.get_commodity(handle)
        return commodity.mnemonic if commodity is not None else handle

    return tuple(
        Excluded(
            item.kind,
            item.description or "Untitled",
            code(item.currency),
            item.amount,
            item.when,
            accounts=item.accounts,
        )
        for item in items
    )


class ReportingConverter:
    """Convert event and transaction values once, at one as-of quote per currency.

    Amounts stay exact; a currency with neither a direct nor an inverse quote is
    reported as missing rather than treated as reporting-currency units.
    """

    def __init__(self, db: DbSQLite, as_of: date) -> None:
        self.db = db
        self.as_of = as_of
        self.target = reporting_currency_handle(db)
        self._rates: dict[str, CurrencyEvidence | None] = {}
        self.used: dict[str, CurrencyEvidence] = {}

    def factor(self, currency: str | None) -> Fraction | None:
        """Reporting units per source unit, or None when no quote applies."""
        if currency is None or currency == self.target:
            return Fraction(1)
        if currency not in self._rates:
            try:
                converted = valuation.convert_currency(
                    self.db,
                    Amount(Money(1), currency),
                    as_of=self.as_of,
                    currency=self.target,
                )
            except ValueError:
                converted = None
            self._rates[currency] = (
                None
                if converted is None or converted.amount is None
                else CurrencyEvidence(
                    currency,
                    self.target,
                    converted.amount.value,
                    converted.quote_date,
                    converted.quote_source,
                    converted.path or "direct",
                )
            )
        evidence = self._rates[currency]
        if evidence is None:
            return None
        self.used[currency] = evidence
        return Fraction(evidence.rate.numerator, evidence.rate.denominator)

    @staticmethod
    def splits(splits: tuple[PlannedSplit, ...], factor: Fraction) -> tuple[PlannedSplit, ...]:
        if factor == 1:
            return splits
        return tuple(replace(split, amount=split.amount * factor) for split in splits)

    def event(self, event: PlannedEvent | None) -> PlannedEvent | None:
        """Return the event in reporting currency, or None when its plan lacks a quote.

        A resolved actual in a currency without a quote keeps its own currency, so
        callers comparing it with the converted expectation must check
        :meth:`reporting_actual` first.
        """
        if event is None:
            return None
        factor = self.factor(event.expected_currency)
        if factor is None:
            return None
        if factor != 1:
            event = replace(
                event,
                expected_splits=self.splits(event.expected_splits, factor),
                expected_amount=event.expected_amount * factor,
                expected_currency=self.target,
                converted_from=event.expected_currency,
            )
        if event.actual_transaction is not None:
            actual_factor = self.factor(event.actual_currency)
            if actual_factor is not None and actual_factor != 1:
                event = replace(
                    event,
                    actual_splits=self.splits(event.actual_splits, actual_factor),
                    actual_amount=(
                        None if event.actual_amount is None else event.actual_amount * actual_factor
                    ),
                    actual_currency=self.target,
                )
        return event

    def reporting_actual(self, event: PlannedEvent) -> bool:
        """True when the event's resolved actual is available in reporting currency."""
        return event.actual_transaction is not None and (
            event.actual_currency is None or event.actual_currency == self.target
        )


def _rate_text(rate: Money) -> str:
    """Show an exact rate without trailing zeros; long repeating rates are rounded."""
    text = f"{rate.rate().quantize(Decimal('1E-8')).normalize():f}"
    return text if rate.to_decimal(8).normalize() == rate.rate().normalize() else f"≈{text}"


def conversion_notes(
    db: DbSQLite,
    conversions: Iterable[CurrencyEvidence],
    unconverted: Iterable[UnconvertedActivity],
    as_of: date | None,
) -> tuple[str, ...]:
    """Disclose each quote used and every amount left out for lack of one.

    GTK, web, CLI, and print show these sentences verbatim, so the quote date,
    source, and inversion, or the explicit exclusion, read the same everywhere.
    """

    def code(handle: str) -> str:
        commodity = db.get_commodity(handle)
        return commodity.mnemonic if commodity is not None else handle

    when = as_of.isoformat() if as_of is not None else "today"
    notes: list[str] = []
    for evidence in conversions:
        source, target = code(evidence.source_currency), code(evidence.target_currency)
        quote = (
            f"the inverse of the {target}→{source} quote"
            if evidence.path == "inverse"
            else f"the {source}→{target} quote"
        )
        dated = f" dated {evidence.quote_date.isoformat()}" if evidence.quote_date else ""
        origin = evidence.quote_source or "unknown source"
        # Disclose age without imposing a staleness cutoff; the reader judges it.
        age = (
            f" (quote {valuation.quote_age_label((as_of - evidence.quote_date).days)})"
            if as_of is not None and evidence.quote_date is not None
            else ""
        )
        notes.append(
            f"{source} amounts are converted to {target} at {_rate_text(evidence.rate)} "
            f"{target} per {source}, using {quote}{dated} ({origin}) applicable on "
            f"{when}{age}. Converted amounts are not rounded to cents before they are added up."
        )
    missing: dict[str, list[UnconvertedActivity]] = {}
    for item in unconverted:
        missing.setdefault(item.currency, []).append(item)
    for currency, items in sorted(missing.items()):
        source = code(currency)
        target = code(reporting_currency_handle(db))
        listed = "; ".join(
            f"{item.description or 'Untitled'} {item.amount.format()} {source} "
            f"({item.kind} {item.when.isoformat()})"
            for item in items
        )
        notes.append(
            f"Not included in totals: no {source}→{target} or {target}→{source} quote "
            f"applies on {when}, so these {source} amounts are not converted: {listed}. "
            "Add an exchange rate to include them."
        )
    return tuple(notes)
