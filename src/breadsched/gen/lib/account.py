"""Accounts and their household-finance types.

An account has one BreadSched-owned type.  Its accounting class and planning
behaviour are derived from that type, so users never have to choose a potentially
contradictory ledger-type/account-kind pair.  Imported accounts separately retain
their exact GnuCash source type for re-import and diagnostics.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any

from .base import PrimaryObject
from .money import Money

__all__ = [
    "Account",
    "AccountClass",
    "AccountType",
    "FsaFundingYear",
    "GnuCashAccountField",
    "GnuCashAccountType",
]


class AccountClass(str, Enum):
    ASSET = "asset"
    LIABILITY = "liability"
    INCOME = "income"
    EXPENSE = "expense"
    EQUITY = "equity"
    ROOT = "root"


@dataclass(frozen=True)
class FsaFundingYear:
    """One FSA election period, independent of the custodial ledger balance."""

    start: date
    through: date
    election: Money
    runout_through: date | None = None

    def __post_init__(self) -> None:
        if self.through < self.start:
            raise ValueError("FSA funding year through date cannot precede start")
        if self.election < 0:
            raise ValueError("FSA election cannot be negative")
        if self.runout_through is not None and self.runout_through < self.through:
            raise ValueError("FSA run-out date cannot precede funding-year end")

    def serialize(self) -> dict[str, object]:
        return {
            "start": self.start.isoformat(),
            "through": self.through.isoformat(),
            "election": [self.election.numerator, self.election.denominator],
            "runout_through": (self.runout_through.isoformat() if self.runout_through else None),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FsaFundingYear:
        raw_runout = data.get("runout_through")
        return cls(
            start=date.fromisoformat(str(data["start"])),
            through=date.fromisoformat(str(data["through"])),
            election=Money(*data["election"]),
            runout_through=date.fromisoformat(str(raw_runout)) if raw_runout else None,
        )


class AccountType(str, Enum):
    """The single user-visible classification of a BreadSched account."""

    ROOT = "ROOT"
    BANK = "BANK"
    CASH = "CASH"
    ASSET = "ASSET"
    INVESTMENT = "INVESTMENT"
    RETIREMENT = "RETIREMENT"
    FSA = "FSA"
    ESCROW = "ESCROW"
    CREDIT = "CREDIT CARD"
    LOAN = "LOAN"
    LIABILITY = "LIABILITY"
    INCOME = "INCOME"
    EXPENSE = "EXPENSE"
    EQUITY = "EQUITY"
    TECHNICAL = "TECHNICAL"

    @classmethod
    def parse(cls, value: str) -> AccountType:
        normalized = str(value).strip().upper().replace("_", " ")
        return cls(normalized)

    @property
    def account_class(self) -> AccountClass:
        return _CLASS_OF[self]

    @property
    def is_debit_balance(self) -> bool:
        """True when a positive split value increases the account's balance.

        Assets and expenses carry debit balances; liabilities, income and equity
        carry credit balances and are displayed sign-flipped.
        """
        return self.account_class in (AccountClass.ASSET, AccountClass.EXPENSE)

    @property
    def is_flow(self) -> bool:
        """Income and expense accounts measure a rate, not a stock of value."""
        return self.account_class in (AccountClass.INCOME, AccountClass.EXPENSE)

    @property
    def is_cash_like(self) -> bool:
        """Money that can be spent this month without selling anything."""
        return self in (AccountType.BANK, AccountType.CASH)

    @property
    def is_investment(self) -> bool:
        """Balances that compound at an assumed rate of return."""
        return self in (AccountType.INVESTMENT, AccountType.RETIREMENT)

    @property
    def supports_emergency_fund(self) -> bool:
        """Whether this type can represent an expense retained after income stops."""
        return self in {
            AccountType.EXPENSE,
            AccountType.LOAN,
            AccountType.LIABILITY,
            AccountType.ESCROW,
            AccountType.CREDIT,
        }


class GnuCashAccountType(str, Enum):
    """Exact source classification retained only for GnuCash interoperability."""

    ROOT = "ROOT"
    BANK = "BANK"
    CASH = "CASH"
    ASSET = "ASSET"
    CREDIT = "CREDIT"
    LIABILITY = "LIABILITY"
    STOCK = "STOCK"
    MUTUAL = "MUTUAL"
    CURRENCY = "CURRENCY"
    INCOME = "INCOME"
    EXPENSE = "EXPENSE"
    EQUITY = "EQUITY"
    RECEIVABLE = "RECEIVABLE"
    PAYABLE = "PAYABLE"
    TRADING = "TRADING"
    CHECKING = "CHECKING"
    SAVINGS = "SAVINGS"
    MONEYMRKT = "MONEYMRKT"
    CREDITLINE = "CREDITLINE"
    CD = "CD"

    @classmethod
    def recognize(cls, value: str) -> GnuCashAccountType | None:
        """Return a known source type without normalizing an unknown one."""
        try:
            return cls(str(value).strip().upper())
        except ValueError:
            return None

    @classmethod
    def parse(cls, value: str) -> GnuCashAccountType:
        return cls.recognize(value) or cls.ASSET

    @property
    def account_class(self) -> AccountClass:
        return _GNUCASH_CLASS_OF[self]

    def to_account_type(self) -> AccountType:
        return _BREADSCHED_TYPE_FOR_SOURCE[self]


_CLASS_OF: dict[AccountType, AccountClass] = {
    AccountType.ROOT: AccountClass.ROOT,
    AccountType.BANK: AccountClass.ASSET,
    AccountType.CASH: AccountClass.ASSET,
    AccountType.ASSET: AccountClass.ASSET,
    AccountType.INVESTMENT: AccountClass.ASSET,
    AccountType.RETIREMENT: AccountClass.ASSET,
    AccountType.FSA: AccountClass.ASSET,
    AccountType.ESCROW: AccountClass.ASSET,
    AccountType.CREDIT: AccountClass.LIABILITY,
    AccountType.LOAN: AccountClass.LIABILITY,
    AccountType.LIABILITY: AccountClass.LIABILITY,
    AccountType.INCOME: AccountClass.INCOME,
    AccountType.EXPENSE: AccountClass.EXPENSE,
    AccountType.EQUITY: AccountClass.EQUITY,
    AccountType.TECHNICAL: AccountClass.ASSET,
}

_GNUCASH_CLASS_OF: dict[GnuCashAccountType, AccountClass] = {
    GnuCashAccountType.ROOT: AccountClass.ROOT,
    GnuCashAccountType.BANK: AccountClass.ASSET,
    GnuCashAccountType.CASH: AccountClass.ASSET,
    GnuCashAccountType.ASSET: AccountClass.ASSET,
    GnuCashAccountType.STOCK: AccountClass.ASSET,
    GnuCashAccountType.MUTUAL: AccountClass.ASSET,
    GnuCashAccountType.CURRENCY: AccountClass.ASSET,
    GnuCashAccountType.RECEIVABLE: AccountClass.ASSET,
    GnuCashAccountType.TRADING: AccountClass.ASSET,
    GnuCashAccountType.CREDIT: AccountClass.LIABILITY,
    GnuCashAccountType.LIABILITY: AccountClass.LIABILITY,
    GnuCashAccountType.PAYABLE: AccountClass.LIABILITY,
    GnuCashAccountType.INCOME: AccountClass.INCOME,
    GnuCashAccountType.EXPENSE: AccountClass.EXPENSE,
    GnuCashAccountType.EQUITY: AccountClass.EQUITY,
    GnuCashAccountType.CHECKING: AccountClass.ASSET,
    GnuCashAccountType.SAVINGS: AccountClass.ASSET,
    GnuCashAccountType.MONEYMRKT: AccountClass.ASSET,
    GnuCashAccountType.CREDITLINE: AccountClass.LIABILITY,
    GnuCashAccountType.CD: AccountClass.ASSET,
}

_BREADSCHED_TYPE_FOR_SOURCE: dict[GnuCashAccountType, AccountType] = {
    GnuCashAccountType.ROOT: AccountType.ROOT,
    GnuCashAccountType.BANK: AccountType.BANK,
    GnuCashAccountType.CASH: AccountType.CASH,
    GnuCashAccountType.ASSET: AccountType.ASSET,
    GnuCashAccountType.STOCK: AccountType.INVESTMENT,
    GnuCashAccountType.MUTUAL: AccountType.INVESTMENT,
    GnuCashAccountType.CURRENCY: AccountType.ASSET,
    GnuCashAccountType.RECEIVABLE: AccountType.ASSET,
    GnuCashAccountType.TRADING: AccountType.TECHNICAL,
    GnuCashAccountType.CREDIT: AccountType.CREDIT,
    GnuCashAccountType.LIABILITY: AccountType.LIABILITY,
    GnuCashAccountType.PAYABLE: AccountType.LIABILITY,
    GnuCashAccountType.INCOME: AccountType.INCOME,
    GnuCashAccountType.EXPENSE: AccountType.EXPENSE,
    GnuCashAccountType.EQUITY: AccountType.EQUITY,
    GnuCashAccountType.CHECKING: AccountType.BANK,
    GnuCashAccountType.SAVINGS: AccountType.BANK,
    GnuCashAccountType.MONEYMRKT: AccountType.BANK,
    GnuCashAccountType.CREDITLINE: AccountType.CREDIT,
    GnuCashAccountType.CD: AccountType.ASSET,
}

SEPARATOR = ":"


@dataclass(frozen=True)
class GnuCashAccountField:
    """One read-only source field not promoted into BreadSched semantics."""

    name: str
    value_type: str
    value: str

    def serialize(self) -> dict[str, str]:
        return {"name": self.name, "value_type": self.value_type, "value": self.value}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GnuCashAccountField:
        return cls(
            name=str(data.get("name", "")),
            value_type=str(data.get("value_type", "")),
            value=str(data.get("value", "")),
        )


class Account(PrimaryObject):
    """A node in the chart of accounts."""

    TABLE = "account"

    def __init__(
        self,
        handle: str | None = None,
        name: str = "",
        atype: AccountType | str = AccountType.ASSET,
        parent: str | None = None,
        commodity: str | None = None,
        code: str = "",
        description: str = "",
        placeholder: bool = False,
        hidden: bool = False,
        commodity_scu: int | None = None,
    ) -> None:
        super().__init__(handle)
        self.name = name
        self.atype = AccountType.parse(atype) if not isinstance(atype, AccountType) else atype
        self.parent = parent
        self.commodity = commodity
        self.code = code
        self.description = description
        self.placeholder = placeholder
        self.hidden = hidden
        self.commodity_scu = commodity_scu
        self.notes = ""
        #: Last source-owned GnuCash type, distinct from BreadSched's account type.
        self.source_atype: GnuCashAccountType | None = None
        #: Exact imported identity. Adopted roots/top-level placeholders retain a
        #: BreadSched handle, so the source GUID cannot always be the object handle.
        self.source_guid: str | None = None
        #: Exact type text, including an unrecognized or historical source type.
        self.source_type: str = ""
        #: Typed, read-only GnuCash fields which BreadSched does not interpret.
        self.source_fields: list[GnuCashAccountField] = []
        self.fsa_years: list[FsaFundingYear] = []

        # Projection hints.  These are what turn a chart of accounts into a model.
        self.annual_return: Decimal = Decimal("0")  # investment growth, e.g. 0.06
        self.annual_interest: Decimal = Decimal("0")  # cost of a liability, e.g. 0.1899
        self.exclude_from_projection: bool = False

        #: Dashboard group this account belongs to, by name. Held on the account
        #: rather than only in the dashboard's configuration so that adding an
        #: account can place it immediately, and so the answer survives a group
        #: being renamed or rebuilt.
        self.group: str = ""
        #: For a loan: the asset it was borrowed against. A mortgage without its
        #: house is a debt with no counterpart, and reports equity that is wrong by
        #: the value of the property.
        self.linked_asset: str | None = None
        #: For a credit card: whether the balance is cleared every month. A card
        #: paid in full is a payment channel; one carrying a balance is a debt that
        #: compounds, and the two do not belong in a forecast together.
        self.pays_in_full: bool = True
        #: The payment usually made on a card carrying a balance.
        self.usual_payment: Money | None = None
        #: Day of the month a card payment is due, inferred on import where
        #: possible.
        self.payment_day: int | None = None
        #: Cash account normally used to pay this card. This is BreadSched-owned
        #: relationship data: GnuCash transactions imply it but do not store it on
        #: the card account itself.
        self.card_payment_account: str | None = None
        #: Optional household choice. None means the default (included) for an
        #: eligible type. Ineligible types are always excluded regardless of this
        #: retained preference.
        self.emergency_fund_override: bool | None = None

    # -------------------------------------------------------------- convenience

    @property
    def account_class(self) -> AccountClass:
        return self.atype.account_class

    @property
    def is_root(self) -> bool:
        return self.atype is AccountType.ROOT

    @property
    def carries_balance(self) -> bool:
        """A card that is not cleared monthly, and so accrues interest."""
        return self.atype is AccountType.CREDIT and not self.pays_in_full

    @property
    def is_spendable_cash(self) -> bool:
        return self.atype.is_cash_like

    @property
    def emergency_fund_eligible(self) -> bool:
        if not self.atype.supports_emergency_fund:
            return False
        return self.atype is not AccountType.CREDIT or self.carries_balance

    @property
    def emergency_fund_included(self) -> bool:
        """Whether positive economic activity contributes to fund sizing."""
        if not self.emergency_fund_eligible:
            return False
        return self.emergency_fund_override is not False

    def sign(self) -> int:
        """Multiplier that turns a raw split total into a displayed balance."""
        return 1 if self.atype.is_debit_balance else -1

    # ------------------------------------------------------------ serialisation

    def _serialize(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "atype": self.atype.value,
            "parent": self.parent,
            "commodity": self.commodity,
            "code": self.code,
            "description": self.description,
            "placeholder": self.placeholder,
            "hidden": self.hidden,
            "commodity_scu": self.commodity_scu,
            "notes": self.notes,
            "source_atype": self.source_atype.value if self.source_atype is not None else None,
            "source_guid": self.source_guid,
            "source_type": self.source_type,
            "source_fields": [field.serialize() for field in self.source_fields],
            "fsa_years": [year.serialize() for year in self.fsa_years],
            "annual_return": str(self.annual_return),
            "annual_interest": str(self.annual_interest),
            "exclude_from_projection": self.exclude_from_projection,
            "group": self.group,
            "linked_asset": self.linked_asset,
            "pays_in_full": self.pays_in_full,
            "usual_payment": (
                [self.usual_payment.numerator, self.usual_payment.denominator]
                if self.usual_payment is not None
                else None
            ),
            "payment_day": self.payment_day,
            "card_payment_account": self.card_payment_account,
            "emergency_fund": self.emergency_fund_override,
        }

    def _unserialize(self, data: dict[str, Any]) -> None:
        self.name = data["name"]
        self.atype = AccountType.parse(str(data["atype"]))
        self.parent = data.get("parent")
        self.commodity = data.get("commodity")
        self.code = data.get("code", "")
        self.description = data.get("description", "")
        self.placeholder = data.get("placeholder", False)
        self.hidden = data.get("hidden", False)
        raw_scu = data.get("commodity_scu")
        self.commodity_scu = int(raw_scu) if raw_scu is not None else None
        self.notes = data.get("notes", "")
        raw_source_type = data.get("source_atype")
        self.source_atype = GnuCashAccountType.parse(raw_source_type) if raw_source_type else None
        raw_source_guid = data.get("source_guid")
        self.source_guid = str(raw_source_guid) if raw_source_guid else None
        self.source_type = str(data.get("source_type", ""))
        if not self.source_type and self.source_atype is not None:
            self.source_type = self.source_atype.value
        self.source_fields = [
            GnuCashAccountField.from_dict(field)
            for field in data.get("source_fields", [])
            if isinstance(field, dict)
        ]
        self.fsa_years = [FsaFundingYear.from_dict(year) for year in data.get("fsa_years", [])]
        self.annual_return = Decimal(data.get("annual_return", "0"))
        self.annual_interest = Decimal(data.get("annual_interest", "0"))
        self.exclude_from_projection = data.get("exclude_from_projection", False)
        self.group = data.get("group", "")
        self.linked_asset = data.get("linked_asset")
        self.pays_in_full = data.get("pays_in_full", True)
        usual = data.get("usual_payment")
        self.usual_payment = Money(*usual) if usual else None
        self.payment_day = data.get("payment_day")
        payment_account = data.get("card_payment_account")
        self.card_payment_account = str(payment_account) if payment_account else None
        raw_emergency = data.get("emergency_fund")
        self.emergency_fund_override = bool(raw_emergency) if raw_emergency is not None else None

    def __repr__(self) -> str:
        return f"<Account {self.name!r} {self.atype.value}>"
