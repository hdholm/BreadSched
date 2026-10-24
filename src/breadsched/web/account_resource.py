"""HTTP input and output for the chart of accounts, securities, and account settings.

The chart is the book server's (``Api.book.chart``): each account valued through
``engine.valuation`` with its quote evidence. Every account settings change goes
through the book server's ``save_account``, so a rejected request leaves the
stored account unchanged.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from ..api.contract import ApiError
from ..gen.engine import valuation
from ..gen.lib import Account, AccountType, FsaFundingYear
from ..gen.services import SaveAccount
from .controls import input_money

if TYPE_CHECKING:
    from .context import Api


def accounts(api: Api) -> list[dict]:
    """The chart of accounts as a flat list carrying its own depth."""
    rows: list[dict] = []
    for line in api.book.chart(include_hidden=True):
        account = line.record.account
        rows.append(
            {
                "handle": account.handle,
                "name": account.name,
                "full_name": line.record.full_name,
                "type": account.atype.value,
                "class": account.account_class.value,
                "placeholder": account.placeholder,
                "hidden": account.hidden,
                "code": account.code,
                "description": account.description,
                "notes": account.notes,
                "source_notes": account.source_notes,
                "commodity_scu": account.commodity_scu,
                "emergency_fund_eligible": account.emergency_fund_eligible,
                "emergency_fund_included": account.emergency_fund_included,
                "pays_in_full": account.card.pays_in_full,
                "usual_payment": (
                    str(account.card.usual_payment.to_decimal())
                    if account.card.usual_payment is not None
                    else None
                ),
                "payment_day": account.card.payment_day,
                "card_payment_account": account.card.payment_account,
                "source_type": (
                    account.source_type
                    or (account.source_atype.value if account.source_atype else None)
                ),
                "source_guid": account.source_guid,
                "source_fields": [field.serialize() for field in account.source_fields],
                "fsa_dependent_care": account.fsa.dependent_care,
                "cost_basis_method": account.investment.cost_basis_method,
                "fsa_years": [
                    {
                        "start": year.start.isoformat(),
                        "through": year.through.isoformat(),
                        "election": str(year.election.to_decimal()),
                        "runout_through": (
                            year.runout_through.isoformat() if year.runout_through else None
                        ),
                        "carryover_limit": (
                            str(year.carryover_limit.to_decimal())
                            if year.carryover_limit is not None
                            else None
                        ),
                        "grace_through": (
                            year.grace_through.isoformat() if year.grace_through else None
                        ),
                    }
                    for year in account.fsa.years
                ],
                "depth": line.depth,
                "balance": line.balance,
                "rollup_missing_quotes": list(line.missing_quotes),
                "book_balance": line.book_balance,
                "own_balance": line.own_balance,
                "valuation_source": line.valuation_source,
                "quantity": line.quantity,
                "price": line.price,
                "price_date": line.quote_date,
                "price_source": line.quote_source,
                "quote_age_days": line.quote_age_days,
                "conversion_path": line.conversion_path,
                "missing_quote": line.missing_quote,
                "quote_evidence": line.quote_evidence,
                "commodity": line.commodity,
                "currency": line.currency,
            }
        )
    return rows


def _stored(api: Api, handle: str) -> Account | None:
    """The account as stored, through the book server, or None."""
    try:
        return api.book.account(handle).account
    except ApiError as exc:
        if exc.status == 404:
            return None
        raise


def commodities(api: Api) -> dict:
    """Securities, currencies, and their latest exact dated prices."""
    currencies = [item for item in api.db.iter_commodities() if item.is_currency]
    securities = []
    for item in api.db.iter_commodities():
        if item.is_currency:
            continue
        latest = valuation.latest_price(api.db, item)
        securities.append(
            {
                "handle": item.handle,
                "namespace": item.namespace,
                "mnemonic": item.mnemonic,
                "fullname": item.fullname,
                "fraction": item.fraction,
                "price": latest.value if latest is not None else None,
                "price_date": latest.quote_date if latest is not None else None,
                "currency": latest.currency if latest is not None else None,
            }
        )
    return {
        "currencies": [
            {"handle": item.handle, "mnemonic": item.mnemonic, "fullname": item.fullname}
            for item in currencies
        ],
        "securities": securities,
    }


def commodity_price_save(api: Api, payload: dict) -> dict:
    """Create a security when needed and add or replace one dated quote."""
    security_handle = str(payload.get("commodity") or "").strip() or None
    currency_handle = str(payload.get("currency") or "").strip()
    try:
        quote_date = date.fromisoformat(str(payload.get("date") or ""))
    except ValueError as exc:
        raise ValueError("quote date is invalid") from exc
    try:
        value = input_money(payload, payload.get("price") or "0")
    except (ValueError, ArithmeticError) as exc:
        raise ValueError("price must be a valid number") from exc
    if value <= 0:
        raise ValueError("price must be greater than zero")
    try:
        fraction = int(payload.get("fraction") or 10000)
    except (TypeError, ValueError) as exc:
        raise ValueError("security fraction must be a whole number") from exc
    security, price = valuation.save_security_price(
        api.db,
        security_handle=security_handle,
        currency_handle=currency_handle,
        quote_date=quote_date,
        value=value,
        mnemonic=str(payload.get("mnemonic") or ""),
        fullname=str(payload.get("fullname") or ""),
        namespace=str(payload.get("namespace") or "FUND"),
        fraction=fraction,
    )
    currency = api.db.get_commodity(price.currency)
    assert currency is not None
    return {
        "commodity": security.handle,
        "mnemonic": security.mnemonic,
        "currency": currency.mnemonic,
        "date": quote_date,
        "price": value,
    }


def account_cost_basis_save(api: Api, payload: dict) -> dict:
    """Choose how a security account's sales take cost: fifo or average."""
    handle = str(payload.get("handle", ""))
    stored = _stored(api, handle)
    if stored is None:
        raise KeyError(handle)
    if stored.atype not in {AccountType.INVESTMENT, AccountType.RETIREMENT}:
        raise ValueError("cost basis applies only to an Investment or Retirement account")
    source = Account.from_dict(stored.serialize())
    account = Account.from_dict(stored.serialize())
    account.investment.cost_basis_method = str(payload.get("method", "")).strip()
    api.book.save_account(SaveAccount(account, existing_handle=account.handle, source=source))
    return {"handle": account.handle, "method": account.investment.cost_basis_method}


def account_type_save(api: Api, payload: dict) -> dict:
    handle = str(payload.get("handle", ""))
    stored = _stored(api, handle)
    if stored is None:
        raise KeyError(handle)
    # Edit a copy: a save the service refuses must leave the stored account as it was.
    source = Account.from_dict(stored.serialize())
    account = Account.from_dict(stored.serialize())
    raw_type = str(payload.get("type", ""))
    try:
        account_type = AccountType(raw_type.strip().upper())
    except ValueError:
        raise ValueError("choose a valid account type") from None
    if account_type in {AccountType.ROOT, AccountType.TECHNICAL}:
        raise ValueError("choose a user account type")
    account.atype = account_type
    if account_type is not AccountType.CREDIT:
        account.card.payment_account = None
    api.book.save_account(SaveAccount(account, existing_handle=account.handle, source=source))
    return {"handle": account.handle, "type": account.atype.value}


def account_emergency_fund_save(api: Api, payload: dict) -> dict:
    handle = str(payload.get("handle", ""))
    stored = _stored(api, handle)
    if stored is None:
        raise KeyError(handle)
    # Edit a copy: a save the service refuses must leave the stored account as it was.
    source = Account.from_dict(stored.serialize())
    account = Account.from_dict(stored.serialize())
    if not account.emergency_fund_eligible:
        raise ValueError("this account type is always excluded from the emergency fund")
    account.planning.emergency_fund = bool(payload.get("included"))
    api.book.save_account(SaveAccount(account, existing_handle=account.handle, source=source))
    return {
        "handle": account.handle,
        "emergency_fund_included": account.emergency_fund_included,
    }


def account_card_save(api: Api, payload: dict) -> dict:
    """Persist the account-owned definition of a credit-card payment."""
    handle = str(payload.get("handle", ""))
    stored = _stored(api, handle)
    if stored is None:
        raise KeyError(handle)
    # Edit a copy: a save the service refuses must leave the stored account as it was.
    source = Account.from_dict(stored.serialize())
    account = Account.from_dict(stored.serialize())
    if account.atype is not AccountType.CREDIT:
        raise ValueError("card payment settings require a Credit card account")
    raw_full = payload.get("pays_in_full", True)
    if not isinstance(raw_full, bool):
        raise ValueError("pays_in_full must be true or false")
    raw_day = payload.get("payment_day")
    payment_day = int(raw_day) if raw_day is not None and raw_day != "" else None
    if payment_day is not None and not 1 <= payment_day <= 28:
        raise ValueError("payment day must be between 1 and 28")
    raw_usual = str(payload.get("usual_payment") or "").strip()
    usual_payment = input_money(payload, raw_usual) if raw_usual else None
    if usual_payment is not None and usual_payment <= 0:
        raise ValueError("usual payment must be positive")
    if not raw_full and usual_payment is None:
        raise ValueError("a card carrying a balance needs a usual payment")
    payment_handle = str(payload.get("payment_account") or "") or None
    if payment_handle is not None:
        payment = _stored(api, payment_handle)
        if payment is None or not payment.atype.is_cash_like or payment.placeholder:
            raise ValueError("paid from must be a Bank or Cash account")
        if payment.hidden and payment.handle != account.card.payment_account:
            raise ValueError("a hidden account cannot fund a new card payment")

    account.card.pays_in_full = raw_full
    account.card.usual_payment = usual_payment
    account.card.payment_day = payment_day
    account.card.payment_account = payment_handle
    api.book.save_account(SaveAccount(account, existing_handle=account.handle, source=source))
    return {
        "handle": account.handle,
        "pays_in_full": account.card.pays_in_full,
        "usual_payment": (
            str(account.card.usual_payment.to_decimal())
            if account.card.usual_payment is not None
            else None
        ),
        "payment_day": account.card.payment_day,
        "card_payment_account": account.card.payment_account,
    }


def account_fsa_years_save(api: Api, payload: dict) -> dict:
    handle = str(payload.get("handle", ""))
    stored = _stored(api, handle)
    if stored is None:
        raise KeyError(handle)
    # Edit a copy: a save the service refuses must leave the stored account as it was.
    source = Account.from_dict(stored.serialize())
    account = Account.from_dict(stored.serialize())
    if account.atype is not AccountType.FSA:
        raise ValueError("FSA funding years require an FSA account")
    years: list[FsaFundingYear] = []
    for raw in payload.get("years", []):
        runout = str(raw.get("runout_through") or "").strip()
        carryover = str(raw.get("carryover_limit") or "").strip()
        grace = str(raw.get("grace_through") or "").strip()
        years.append(
            FsaFundingYear(
                start=date.fromisoformat(str(raw["start"])),
                through=date.fromisoformat(str(raw["through"])),
                election=input_money(payload, raw["election"]),
                runout_through=date.fromisoformat(runout) if runout else None,
                carryover_limit=input_money(payload, carryover) if carryover else None,
                grace_through=date.fromisoformat(grace) if grace else None,
            )
        )
    years.sort(key=lambda year: year.start)
    for earlier, later in zip(years, years[1:], strict=False):
        if later.start <= earlier.through:
            raise ValueError("FSA funding years cannot overlap")
    account.fsa.years = years
    dependent_care = payload.get("dependent_care", account.fsa.dependent_care)
    if not isinstance(dependent_care, bool):
        raise ValueError("dependent_care must be true or false")
    account.fsa.dependent_care = dependent_care
    api.book.save_account(SaveAccount(account, existing_handle=account.handle, source=source))
    return {"handle": account.handle, "years": [year.serialize() for year in years]}
