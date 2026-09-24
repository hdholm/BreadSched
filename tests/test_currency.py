"""The reporting currency and its precision follow book-owned choices."""

from breadsched.gen.engine.currency import (
    book_currency,
    commodity_fraction,
    reporting_currency_handle,
    reporting_fraction,
)
from breadsched.gen.lib.commodity import DEFAULT_CURRENCY, Commodity


def test_usd_priority_and_empty_legacy_book_fallback():
    alternative = Commodity(mnemonic="ALT", fraction=1000)
    usd = Commodity(mnemonic="USD", fraction=100)

    class CurrencyBook:
        def __init__(self, currencies):
            self.currencies = currencies

        def get_metadata(self, _key):
            return None

        def get_commodity_by_mnemonic(self, mnemonic):
            return next((item for item in self.currencies if item.mnemonic == mnemonic), None)

        def iter_commodities(self):
            return iter(self.currencies)

    book = CurrencyBook([alternative, usd])
    assert book_currency(book).handle == usd.handle
    assert reporting_currency_handle(book) == usd.handle
    assert reporting_currency_handle(CurrencyBook([])) == DEFAULT_CURRENCY.handle


def test_configured_currency_by_handle_or_mnemonic(db):
    alternative = Commodity(mnemonic="ALT", fraction=1000)
    with db.transaction("currency") as txn:
        db.add_commodity(alternative, txn)
    for choice in (alternative.handle, "ALT"):
        db.set_metadata("default_currency", choice)
        assert book_currency(db).handle == alternative.handle
        assert reporting_currency_handle(db) == alternative.handle
        assert reporting_fraction(db) == 1000
        assert commodity_fraction(db, None) == 1000


def test_invalid_or_security_choice_falls_back_to_usd(db):
    security = Commodity(namespace="FUND", mnemonic="FUND", fraction=10)
    with db.transaction("currency") as txn:
        db.add_commodity(security, txn)
    for choice in (security.handle, "MISSING", 42):
        db.set_metadata("default_currency", choice)
        assert book_currency(db).mnemonic == "USD"
        assert reporting_fraction(db) == DEFAULT_CURRENCY.fraction
    assert commodity_fraction(db, security.handle) == 10
    whole = Commodity(namespace="FUND", mnemonic="WHOLE", fraction=1)
    with db.transaction("currency") as txn:
        db.add_commodity(whole, txn)
    assert commodity_fraction(db, whole.handle) == 1
    assert commodity_fraction(db, "MISSING") == DEFAULT_CURRENCY.fraction


def test_invalid_fraction_uses_default_without_changing_book(db):
    currency = Commodity(mnemonic="WHOLE", fraction=0)
    with db.transaction("currency") as txn:
        db.add_commodity(currency, txn)
    assert commodity_fraction(db, currency.handle) == DEFAULT_CURRENCY.fraction
