"""Accounts and the account-type taxonomy.

The ledger-type list is deliberately identical to GnuCash's
``accounts.account_type`` column so that an imported book keeps its source
classification instead of being flattened into something lossy. BreadSched's
independent account kind supplies household-planning behavior such as retirement,
FSA, debt, investment, and escrow treatment.
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
    "AccountKind",
    "AccountType",
    "FsaFundingYear",
]


class AccountClass(str, Enum):
    ASSET = "asset"
    LIABILITY = "liability"
    INCOME = "income"
    EXPENSE = "expense"
    EQUITY = "equity"
    ROOT = "root"


class AccountKind(str, Enum):
    """BreadSched account behavior, independent of its GnuCash ledger type."""

    ORDINARY = "ordinary"
    RETIREMENT = "retirement"
    FSA = "fsa"
    DEBT = "debt"
    INVESTMENT = "investment"
    ESCROW = "escrow"

    @property
    def label(self) -> str:
        return {
            AccountKind.ORDINARY: "Ordinary",
            AccountKind.RETIREMENT: "Retirement",
            AccountKind.FSA: "FSA / benefit",
            AccountKind.DEBT: "Loan / debt",
            AccountKind.INVESTMENT: "Investment",
            AccountKind.ESCROW: "Escrow",
        }[self]

    def supports(self, account_class: AccountClass) -> bool:
        if self is AccountKind.ORDINARY:
            return True
        if self is AccountKind.DEBT:
            return account_class is AccountClass.LIABILITY
        return account_class is AccountClass.ASSET


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

    @classmethod
    def parse(cls, value: str) -> AccountType:
        try:
            return cls(str(value).strip().upper())
        except ValueError:
            return cls.ASSET

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
        return self in (AccountType.STOCK, AccountType.MUTUAL)


_CLASS_OF: dict[AccountType, AccountClass] = {
    AccountType.ROOT: AccountClass.ROOT,
    AccountType.BANK: AccountClass.ASSET,
    AccountType.CASH: AccountClass.ASSET,
    AccountType.ASSET: AccountClass.ASSET,
    AccountType.STOCK: AccountClass.ASSET,
    AccountType.MUTUAL: AccountClass.ASSET,
    AccountType.CURRENCY: AccountClass.ASSET,
    AccountType.RECEIVABLE: AccountClass.ASSET,
    AccountType.TRADING: AccountClass.ASSET,
    AccountType.CREDIT: AccountClass.LIABILITY,
    AccountType.LIABILITY: AccountClass.LIABILITY,
    AccountType.PAYABLE: AccountClass.LIABILITY,
    AccountType.INCOME: AccountClass.INCOME,
    AccountType.EXPENSE: AccountClass.EXPENSE,
    AccountType.EQUITY: AccountClass.EQUITY,
}

SEPARATOR = ":"


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
        self.kind = AccountKind.ORDINARY
        #: Last source-owned GnuCash ledger type, distinct from BreadSched's kind.
        self.source_atype: AccountType | None = None
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
        return self.atype.is_cash_like and self.kind is AccountKind.ORDINARY

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
            "kind": self.kind.value,
            "source_atype": self.source_atype.value if self.source_atype is not None else None,
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
        }

    def _unserialize(self, data: dict[str, Any]) -> None:
        self.name = data["name"]
        self.atype = AccountType.parse(data["atype"])
        self.parent = data.get("parent")
        self.commodity = data.get("commodity")
        self.code = data.get("code", "")
        self.description = data.get("description", "")
        self.placeholder = data.get("placeholder", False)
        self.hidden = data.get("hidden", False)
        raw_scu = data.get("commodity_scu")
        self.commodity_scu = int(raw_scu) if raw_scu is not None else None
        self.notes = data.get("notes", "")
        self.kind = AccountKind(data.get("kind", data.get("planning_role", "ordinary")))
        raw_source_type = data.get("source_atype")
        self.source_atype = AccountType.parse(raw_source_type) if raw_source_type else None
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

    def __repr__(self) -> str:
        return f"<Account {self.name!r} {self.atype.value}>"
