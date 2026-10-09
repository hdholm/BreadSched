"""Choose which lots a sale of shares sells (specific identification).

A sale normally sells by its account's method: first-in, first-out, or average
cost. Naming lots instead stores ``Split.lot_picks`` on the selling split; the
lots themselves stay derived (``engine.cost_basis``), so a pick names the
transaction that bought the lot and how many of its shares, as held at the sale,
the sale takes. Shares the picks do not cover still sell by the method.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from ..db.sqlite import DbSQLite
from ..engine.cost_basis import Lot, RealizedGain, cost_basis, lots_before_sale
from ..lib.account import Account
from ..lib.money import Money
from ..lib.transaction import LotPick, Split, Transaction
from .contracts import ServiceError, ServiceResult

__all__ = ["SaleLots", "SetSaleLots", "sale_lots", "set_sale_lots"]


@dataclass(frozen=True, slots=True)
class SetSaleLots:
    """Name the lots a sale sells; an empty ``picks`` returns it to the account's method.

    Each pick is (lot, shares): the transaction that acquired the lot, and the
    number of shares as decimal text.
    """

    transaction: str
    split: str
    picks: Sequence[tuple[str, str]] = ()


@dataclass(frozen=True, slots=True)
class SaleLots:
    """A sale, the lots open just before it, and the lots it currently names."""

    account: Account
    transaction: Transaction
    split: Split
    sale: RealizedGain
    offered: tuple[Lot, ...]
    picks: tuple[LotPick, ...]
    method: str


def _locate(
    db: DbSQLite, transaction: str, split: str
) -> tuple[Transaction, Split, Account, RealizedGain] | ServiceError:
    txn = db.get_transaction(transaction)
    if txn is None:
        return ServiceError("lots.transaction.not_found", ("transaction",))
    found = next((item for item in txn.splits if item.handle == split), None)
    if found is None:
        return ServiceError("lots.split.not_found", ("split",))
    account = db.get_account(found.account)
    holding = cost_basis(db, account) if account is not None else None
    sale = next(
        (item for item in (holding.sales if holding is not None else ()) if item.split == split),
        None,
    )
    if account is None or sale is None:
        return ServiceError("lots.split.not_sale", ("split",))
    return txn, found, account, sale


def sale_lots(db: DbSQLite, transaction: str, split: str) -> ServiceResult[SaleLots]:
    """The lots a sale could name, for a chooser."""
    located = _locate(db, transaction, split)
    if isinstance(located, ServiceError):
        return ServiceResult.failure(located)
    txn, found, account, sale = located
    return ServiceResult.success(
        SaleLots(
            account,
            txn,
            found,
            sale,
            lots_before_sale(db, account, split),
            found.lot_picks,
            account.cost_basis_method,
        )
    )


def set_sale_lots(db: DbSQLite, request: SetSaleLots) -> ServiceResult[SaleLots]:
    """Validate and store the lots a sale sells, as one undo step."""
    located = _locate(db, request.transaction, request.split)
    if isinstance(located, ServiceError):
        return ServiceResult.failure(located)
    txn, found, account, sale = located
    offered: dict[str, Money] = {}
    for open_lot in lots_before_sale(db, account, request.split):
        offered[open_lot.transaction] = (
            offered.get(open_lot.transaction, Money(0)) + open_lot.quantity
        )
    picks: list[LotPick] = []
    errors: list[ServiceError] = []
    seen: set[str] = set()
    for index, (lot, text) in enumerate(request.picks):
        path = f"picks.{index}"
        try:
            amount = Decimal(str(text).strip())
        except InvalidOperation:
            amount = Decimal(0)
        if not amount.is_finite() or amount <= 0:
            errors.append(ServiceError("lots.quantity.invalid", (f"{path}.quantity",)))
            continue
        if lot in seen:
            errors.append(ServiceError("lots.lot.duplicate", (f"{path}.lot",)))
            continue
        seen.add(lot)
        if lot not in offered:
            errors.append(ServiceError("lots.lot.unknown", (f"{path}.lot",)))
            continue
        quantity = Money(amount)
        if quantity > offered[lot]:
            errors.append(ServiceError("lots.quantity.exceeds_lot", (f"{path}.quantity",)))
            continue
        picks.append(LotPick(lot, quantity))
    if not errors and sum((pick.quantity for pick in picks), Money(0)) > sale.quantity:
        errors.append(ServiceError("lots.quantity.exceeds_sale", ("picks",)))
    if errors:
        return ServiceResult.failure(*errors)
    if tuple(picks) != found.lot_picks:
        found.lot_picks = tuple(picks)
        label = "Sell named lots" if picks else "Sell lots by the account's method"
        with db.transaction(label) as handle:
            db.commit_transaction(txn, handle)
    return sale_lots(db, request.transaction, request.split)
