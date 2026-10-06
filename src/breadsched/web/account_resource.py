"""HTTP input and output for the chart of accounts, securities, and account settings.

Account reads value each account through ``engine.valuation`` with its quote
evidence; every account change goes through ``services.save_account``, so a
rejected request leaves the stored account unchanged.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from ..gen.engine import ledger, valuation
from ..gen.lib import Account, AccountType, FsaFundingYear
from ..gen.services import SaveAccount, save_account
from .controls import input_money, service_error

if TYPE_CHECKING:
    from .context import Api


def accounts(api: Api) -> list[dict]:
    """The chart of accounts as a flat list carrying its own depth."""
    rows: list[dict] = []

    def walk(parent: str | None, depth: int) -> None:
        for account in api.db.child_accounts(parent):
            valued = valuation.account_value(api.db, account)
            rollup = valuation.aggregate_value(
                api.db, accounts=[account, *api.db.descendants(account.handle)]
            )
            recursive = rollup.amount.value if rollup.amount is not None else None
            try:
                book_balance = ledger.balance_recursive(api.db, account.handle)
            except TypeError as exc:
                if "cannot combine unlike commodities" not in str(exc):
                    raise
                book_balance = None
            rows.append(
                {
                    "handle": account.handle,
                    "name": account.name,
                    "full_name": api.db.full_name(account),
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
                    "pays_in_full": account.pays_in_full,
                    "usual_payment": (
                        str(account.usual_payment.to_decimal())
                        if account.usual_payment is not None
                        else None
                    ),
                    "payment_day": account.payment_day,
                    "card_payment_account": account.card_payment_account,
                    "source_type": (
                        account.source_type
                        or (account.source_atype.value if account.source_atype else None)
                    ),
                    "source_guid": account.source_guid,
                    "source_fields": [field.serialize() for field in account.source_fields],
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
                        for year in account.fsa_years
                    ],
                    "depth": depth,
                    "balance": recursive,
                    "rollup_missing_quotes": list(rollup.missing_quotes),
                    "book_balance": book_balance,
                    "own_balance": ledger.balance(api.db, account.handle),
                    "valuation_source": valued.source,
                    "quantity": valued.quantity,
                    "price": valued.price,
                    "price_date": valued.price_date,
                    "price_source": valued.price_source,
                    "quote_age_days": valued.quote_age_days,
                    "conversion_path": valued.conversion_path,
                    "missing_quote": valued.missing_quote,
                    "quote_evidence": valuation.quote_evidence(api.db, valued),
                    "commodity": (
                        valued.commodity.mnemonic if valued.commodity is not None else None
                    ),
                    "currency": (valued.currency.mnemonic if valued.currency is not None else None),
                }
            )
            walk(account.handle, depth + 1)

    root = api.db.root_account()
    walk(root.handle if root else None, 0)
    return rows


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


def account_type_save(api: Api, payload: dict) -> dict:
    handle = str(payload.get("handle", ""))
    account = api.db.get_account(handle)
    if account is None:
        raise KeyError(handle)
    source = Account.from_dict(account.serialize())
    raw_type = str(payload.get("type", ""))
    try:
        account_type = AccountType(raw_type.strip().upper())
    except ValueError:
        raise ValueError("choose a valid account type") from None
    if account_type in {AccountType.ROOT, AccountType.TECHNICAL}:
        raise ValueError("choose a user account type")
    account.atype = account_type
    if account_type is not AccountType.CREDIT:
        account.card_payment_account = None
    result = save_account(
        api.db,
        SaveAccount(account, existing_handle=account.handle, source=source),
    )
    if not result.ok:
        raise service_error(result.errors[0])
    return {"handle": account.handle, "type": account.atype.value}


def account_emergency_fund_save(api: Api, payload: dict) -> dict:
    handle = str(payload.get("handle", ""))
    account = api.db.get_account(handle)
    if account is None:
        raise KeyError(handle)
    source = Account.from_dict(account.serialize())
    if not account.emergency_fund_eligible:
        raise ValueError("this account type is always excluded from the emergency fund")
    account.emergency_fund_override = bool(payload.get("included"))
    result = save_account(
        api.db,
        SaveAccount(account, existing_handle=account.handle, source=source),
    )
    if not result.ok:
        raise service_error(result.errors[0])
    return {
        "handle": account.handle,
        "emergency_fund_included": account.emergency_fund_included,
    }


def account_card_save(api: Api, payload: dict) -> dict:
    """Persist the account-owned definition of a credit-card payment."""
    handle = str(payload.get("handle", ""))
    account = api.db.get_account(handle)
    if account is None:
        raise KeyError(handle)
    source = Account.from_dict(account.serialize())
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
        payment = api.db.get_account(payment_handle)
        if payment is None or not payment.atype.is_cash_like or payment.placeholder:
            raise ValueError("paid from must be a Bank or Cash account")
        if payment.hidden and payment.handle != account.card_payment_account:
            raise ValueError("a hidden account cannot fund a new card payment")

    account.pays_in_full = raw_full
    account.usual_payment = usual_payment
    account.payment_day = payment_day
    account.card_payment_account = payment_handle
    result = save_account(
        api.db,
        SaveAccount(account, existing_handle=account.handle, source=source),
    )
    if not result.ok:
        raise service_error(result.errors[0])
    return {
        "handle": account.handle,
        "pays_in_full": account.pays_in_full,
        "usual_payment": (
            str(account.usual_payment.to_decimal()) if account.usual_payment is not None else None
        ),
        "payment_day": account.payment_day,
        "card_payment_account": account.card_payment_account,
    }


def account_fsa_years_save(api: Api, payload: dict) -> dict:
    handle = str(payload.get("handle", ""))
    account = api.db.get_account(handle)
    if account is None:
        raise KeyError(handle)
    source = Account.from_dict(account.serialize())
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
    account.fsa_years = years
    result = save_account(
        api.db,
        SaveAccount(account, existing_handle=account.handle, source=source),
    )
    if not result.ok:
        raise service_error(result.errors[0])
    return {"handle": account.handle, "years": [year.serialize() for year in years]}
