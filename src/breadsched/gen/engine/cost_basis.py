"""Cost basis and gains of security holdings, derived from their ledger splits.

Lots are never stored: like a receivable's status, they are recomputed from the
splits of each investment or retirement account that holds a security. Each split
that adds shares opens a lot at its value (what was paid, in the transaction's
currency); each split that removes shares sells the lots it names (specific
identification, ``Split.lot_picks``), and otherwise, or for any shares beyond those
named, first-in, first-out from the open lots (or, for an account set to average
cost, the same fraction of every lot). The gain realized is the proceeds less the
cost of the shares it took, and each sale keeps the lot parts it took, with their
purchase dates. Shares that
move to another security account of the same commodity are not sold: their lots,
with their purchase dates and cost, move with them. A change of shares in a
transaction where nothing carries value (any other leg is zero) is a share split:
every lot's shares change by the same factor and its cost stays. What is still held
is the open lots; its unrealized gain is its market value (from
``valuation.account_value``, with the quote it used) less that cost.

Nothing is approximated. Shares that arrive with no recorded value and nothing to
split (no shares held, or from outside the book's security accounts) open a lot at
zero cost and are named; a sale of more shares than the account's history bought is
named and its uncovered part has no realized gain; an account whose splits use more
than one currency, or whose quote is missing or in another currency than its cost,
has no unrealized gain.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from ..db.sqlite import DbSQLite
from ..lib.account import Account, AccountType
from ..lib.money import Money
from ..lib.transaction import LotPick, Transaction
from .chart_model import ChartModel
from .currency import reporting_currency_handle
from .valuation import AccountValuation, account_value

__all__ = [
    "HoldingCostBasis",
    "Lot",
    "LotMove",
    "RealizedGain",
    "cost_basis",
    "holdings_charts",
    "holdings_cost_basis",
    "lots_before_sale",
    "shares_text",
]


def shares_text(quantity: Money) -> str:
    """A share count as plain decimal text, without currency formatting."""
    return f"{quantity.to_decimal(8).normalize():f}"


_SECURITY_TYPES = {AccountType.INVESTMENT, AccountType.RETIREMENT}


@dataclass(frozen=True, slots=True)
class Lot:
    """Shares still held from one acquisition, and what they cost."""

    acquired: date
    quantity: Money
    cost: Money
    transaction: str


@dataclass(frozen=True, slots=True)
class RealizedGain:
    """One sale: shares sold, what they brought, what they cost, and the gain."""

    sold: date
    quantity: Money
    proceeds: Money
    cost: Money
    transaction: str
    #: Shares sold beyond the recorded purchases; their cost and gain are unknown.
    uncovered: Money = field(default_factory=lambda: Money(0))
    #: The lot parts this sale took, with their purchase dates and cost.
    lots: tuple[Lot, ...] = ()
    #: True when the sale named its lots (specific identification).
    specific: bool = False
    #: The selling split, which carries the lot choice.
    split: str = ""

    @property
    def gain(self) -> Money:
        return self.proceeds - self.cost


@dataclass(frozen=True, slots=True)
class LotMove:
    """Shares that changed without a sale: a transfer between accounts or a share split.

    A transfer carries its lots (purchase dates and cost) to the other account; a
    split changes every lot's shares by the same factor and keeps its cost.
    """

    when: date
    #: ``"transfer_in"``, ``"transfer_out"``, or ``"split"``.
    kind: str
    #: Shares added (positive) or removed (negative).
    quantity: Money
    #: Cost basis carried with the shares (zero for a split).
    cost: Money
    transaction: str
    other_account: str | None = None
    lots: tuple[Lot, ...] = ()
    #: Shares held just before a split.
    held: Money | None = None


@dataclass(frozen=True, slots=True)
class HoldingCostBasis:
    """One security account's open lots, realized gains, and unrealized gain."""

    account: Account
    currency: str | None
    lots: tuple[Lot, ...]
    sales: tuple[RealizedGain, ...]
    valuation: AccountValuation
    problems: tuple[str, ...] = ()
    moves: tuple[LotMove, ...] = ()

    @property
    def quantity(self) -> Money:
        return sum((lot.quantity for lot in self.lots), Money(0))

    @property
    def cost(self) -> Money:
        return sum((lot.cost for lot in self.lots), Money(0))

    @property
    def market_value(self) -> Money | None:
        """Market value in the cost's currency, or ``None`` when it cannot be compared."""
        valued = self.valuation
        if valued.source != "market" or valued.missing_quote or valued.total_amount is None:
            return None
        if self.currency is None or valued.total_amount.commodity != self.currency:
            return None
        return valued.total_amount.value

    @property
    def unrealized_gain(self) -> Money | None:
        market = self.market_value
        return None if market is None else market - self.cost

    def realized_by_year(self) -> dict[int, Money]:
        totals: dict[int, Money] = defaultdict(lambda: Money(0))
        for sale in self.sales:
            totals[sale.sold.year] = totals[sale.sold.year] + sale.gain
        return dict(sorted(totals.items()))

    @property
    def method(self) -> str:
        return self.account.cost_basis_method

    def as_dict(self) -> dict[str, object]:
        return {
            "account": self.account.handle,
            "method": self.method,
            "currency": self.currency,
            # Share counts as exact decimal text; amounts are money.
            "quantity": shares_text(self.quantity),
            "cost": self.cost,
            "market_value": self.market_value,
            "unrealized_gain": self.unrealized_gain,
            "price": self.valuation.price,
            "price_date": self.valuation.price_date,
            "lots": [
                {
                    "acquired": lot.acquired,
                    "quantity": shares_text(lot.quantity),
                    "cost": lot.cost,
                    "transaction": lot.transaction,
                }
                for lot in self.lots
            ],
            "sales": [
                {
                    "sold": sale.sold,
                    "quantity": shares_text(sale.quantity),
                    "proceeds": sale.proceeds,
                    "cost": sale.cost,
                    "gain": sale.gain,
                    "uncovered": shares_text(sale.uncovered),
                    "transaction": sale.transaction,
                    "split": sale.split,
                    "specific": sale.specific,
                    "lots": [
                        {
                            "acquired": lot.acquired,
                            "quantity": shares_text(lot.quantity),
                            "cost": lot.cost,
                            "lot": lot.transaction,
                        }
                        for lot in sale.lots
                    ],
                }
                for sale in self.sales
            ],
            "moves": [
                {
                    "when": move.when,
                    "kind": move.kind,
                    "quantity": shares_text(move.quantity),
                    "cost": move.cost,
                    "transaction": move.transaction,
                    "other_account": move.other_account,
                    "held": None if move.held is None else shares_text(move.held),
                }
                for move in self.moves
            ],
            "realized_by_year": {str(year): gain for year, gain in self.realized_by_year().items()},
            "problems": list(self.problems),
        }


def _is_security(db: DbSQLite, account: Account) -> bool:
    if account.atype not in _SECURITY_TYPES or account.placeholder or not account.commodity:
        return False
    commodity = db.get_commodity(account.commodity)
    return commodity is not None and not commodity.is_currency


def _take(open_lots: list[Lot], wanted: Money, method: str) -> tuple[list[Lot], list[Lot], Money]:
    """Remove ``wanted`` shares: the lots left, the parts taken, and the shares uncovered.

    First in, first out takes the oldest lots whole and splits the last one; average
    cost takes the same fraction of every lot, so each share taken costs the average.
    """
    held = sum((lot.quantity for lot in open_lots), Money(0))
    if not held:
        return open_lots, [], wanted
    covered = min(wanted, held)
    if method == "average":
        share = covered / held
        taken = [
            Lot(lot.acquired, lot.quantity * share, lot.cost * share, lot.transaction)
            for lot in open_lots
        ]
        left = [
            Lot(lot.acquired, lot.quantity - part.quantity, lot.cost - part.cost, lot.transaction)
            for lot, part in zip(open_lots, taken, strict=True)
            if lot.quantity - part.quantity > 0
        ]
        return left, taken, wanted - covered
    left = list(open_lots)
    taken = []
    remaining = covered
    while remaining > 0:
        lot = left[0]
        if lot.quantity <= remaining:
            taken.append(left.pop(0))
            remaining = remaining - lot.quantity
        else:
            part = lot.cost * (remaining / lot.quantity)
            taken.append(Lot(lot.acquired, remaining, part, lot.transaction))
            left[0] = Lot(lot.acquired, lot.quantity - remaining, lot.cost - part, lot.transaction)
            remaining = Money(0)
    return left, taken, wanted - covered


def _take_named(
    open_lots: list[Lot], picks: tuple[LotPick, ...], wanted: Money, method: str
) -> tuple[list[Lot], list[Lot], Money, list[str]]:
    """Take the named lots first, then any remaining shares by ``method``.

    Returns the lots left, the parts taken, the shares uncovered, and a description
    of each pick that could not be honoured in full (a lot already sold, or fewer
    shares left in it than named).
    """
    left = list(open_lots)
    taken: list[Lot] = []
    problems: list[str] = []
    remaining = wanted
    for pick in picks:
        want = min(pick.quantity, remaining)
        if want <= 0:
            if pick.quantity > 0:
                problems.append(f"lot {pick.lot[:8]} was named beyond the shares sold")
            continue
        got = Money(0)
        for index, lot in enumerate(left):
            if lot.transaction != pick.lot or got >= want:
                continue
            part = min(lot.quantity, want - got)
            cost = lot.cost if part == lot.quantity else lot.cost * (part / lot.quantity)
            taken.append(Lot(lot.acquired, part, cost, lot.transaction))
            left[index] = Lot(lot.acquired, lot.quantity - part, lot.cost - cost, lot.transaction)
            got = got + part
        left = [lot for lot in left if lot.quantity > 0]
        if got < want:
            problems.append(
                f"lot {pick.lot[:8]} had {shares_text(got)} of the {shares_text(want)} "
                "shares named; the rest were taken by the account's method"
            )
        remaining = remaining - got
    if remaining > 0:
        left, rest, uncovered = _take(left, remaining, method)
        taken.extend(rest)
    else:
        uncovered = Money(0)
    return left, taken, uncovered, problems


def _transfer_partner(
    db: DbSQLite, account: Account, txn: Transaction, quantity: Money
) -> Account | None:
    """The other security account of the same commodity this split's shares moved to or from."""
    for split in txn.splits:
        if split.account == account.handle or split.quantity != -quantity:
            continue
        other = db.get_account(split.account)
        if other is not None and other.commodity == account.commodity and _is_security(db, other):
            return other
    return None


def _is_share_split(account: Account, txn: Transaction, value: Money) -> bool:
    """A change of shares with no value anywhere: no other split moves money or shares."""
    return value == 0 and all(
        split.account == account.handle or (split.value == 0 and split.quantity == 0)
        for split in txn.splits
    )


def cost_basis(
    db: DbSQLite,
    account: Account | str,
    *,
    as_of: date | None = None,
    _visiting: frozenset[tuple[str, date | None]] = frozenset(),
    _before_split: str | None = None,
    _snapshot: list[tuple[Lot, ...]] | None = None,
) -> HoldingCostBasis | None:
    """The lots of one security account, by its method; ``None`` if it holds cash."""
    obj = db.get_account(account) if isinstance(account, str) else account
    if obj is None or not _is_security(db, obj):
        return None
    visiting = _visiting | {(obj.handle, as_of)}
    method = obj.cost_basis_method
    book = reporting_currency_handle(db)
    open_lots: list[Lot] = []
    sales: list[RealizedGain] = []
    moves: list[LotMove] = []
    currencies: set[str] = set()
    zero_cost = Money(0)
    uncovered_total = Money(0)
    moved_uncovered = Money(0)
    problems: list[str] = []
    # Within a day, shares arrive before any leave, so a same-day purchase and sale
    # (or a transfer back) never depends on the order of split handles.
    rows = sorted(
        db.split_rows(obj.handle, end=as_of),
        key=lambda row: (row["post_date"], row["quantity_num"] < 0),
    )
    txns = {row["txn"]: db.get_transaction(row["txn"]) for row in rows}
    partners: dict[int, Account] = {}
    directions: dict[tuple[str, str], set[bool]] = defaultdict(set)
    for index, row in enumerate(rows):
        txn = txns[row["txn"]]
        quantity = Money(row["quantity_num"], row["quantity_den"])
        found = None if txn is None or not quantity else _transfer_partner(db, obj, txn, quantity)
        if found is not None:
            partners[index] = found
            directions[(row["post_date"], found.handle)].add(quantity > 0)
    for index, row in enumerate(rows):
        quantity = Money(row["quantity_num"], row["quantity_den"])
        value = Money(row["value_num"], row["value_den"])
        when = date.fromisoformat(row["post_date"])
        currencies.add(row["currency"] or book)
        if not quantity:
            continue
        if _snapshot is not None and row["handle"] == _before_split:
            _snapshot.append(tuple(open_lots))
        txn = txns[row["txn"]]
        held = sum((lot.quantity for lot in open_lots), Money(0))
        if txn is not None and _is_share_split(obj, txn, value) and held and held + quantity > 0:
            # A share split (or reverse split) changes every lot's shares, not its cost.
            factor = (held + quantity) / held
            open_lots = [
                Lot(lot.acquired, lot.quantity * factor, lot.cost, lot.transaction)
                for lot in open_lots
            ]
            moves.append(LotMove(when, "split", quantity, Money(0), row["txn"], held=held))
            continue
        partner = partners.get(index)
        if partner is not None and len(directions[(row["post_date"], partner.handle)]) > 1:
            # Shares moved both ways between the same accounts on one day cannot be
            # matched to lots in either; each leg stays a sale or purchase at its value.
            problems.append(
                f"shares moved both ways with {db.full_name(partner)} on {when.isoformat()} "
                "count as a sale and a purchase at their recorded values"
            )
            partner = None
        if partner is not None and quantity < 0:
            # Shares moved to another account keep their cost; nothing is realized.
            open_lots, taken, uncovered = _take(open_lots, -quantity, method)
            moved_uncovered = moved_uncovered + uncovered
            moves.append(
                LotMove(
                    when,
                    "transfer_out",
                    quantity,
                    sum((lot.cost for lot in taken), Money(0)),
                    row["txn"],
                    partner.handle,
                    tuple(taken),
                )
            )
            continue
        if partner is not None and (partner.handle, when) not in visiting:
            source = cost_basis(db, partner, as_of=when, _visiting=visiting)
            sent = next(
                (
                    move
                    for move in (source.moves if source is not None else ())
                    if move.kind == "transfer_out" and move.transaction == row["txn"]
                ),
                None,
            )
            if sent is not None:
                arrived = list(sent.lots)
                carried = sum((lot.quantity for lot in arrived), Money(0))
                if carried < quantity:
                    zero_cost = zero_cost + quantity - carried
                    arrived.append(Lot(when, quantity - carried, Money(0), row["txn"]))
                # Moved lots keep their purchase dates, so they sell in that order.
                open_lots = sorted(open_lots + arrived, key=lambda lot: lot.acquired)
                moves.append(
                    LotMove(
                        when,
                        "transfer_in",
                        quantity,
                        sum((lot.cost for lot in arrived), Money(0)),
                        row["txn"],
                        partner.handle,
                        tuple(arrived),
                    )
                )
                continue
        if quantity > 0:
            if value == 0:
                zero_cost = zero_cost + quantity
            open_lots.append(Lot(when, quantity, value, row["txn"]))
        else:
            sold = -quantity
            picks = _lot_picks(txn, row["handle"])
            if picks:
                open_lots, taken, remaining, pick_problems = _take_named(
                    open_lots, picks, sold, method
                )
                problems.extend(f"sale on {when.isoformat()}: {text}" for text in pick_problems)
            else:
                open_lots, taken, remaining = _take(open_lots, sold, method)
            cost = sum((lot.cost for lot in taken), Money(0))
            proceeds = -value * ((sold - remaining) / sold)
            uncovered_total = uncovered_total + remaining
            sales.append(
                RealizedGain(
                    when,
                    sold,
                    proceeds,
                    cost,
                    row["txn"],
                    remaining,
                    tuple(taken),
                    bool(picks),
                    row["handle"],
                )
            )
    if zero_cost:
        problems.append(
            f"{shares_text(zero_cost)} shares arrived with no recorded cost (a transfer in "
            "or a share split); their cost basis is zero"
        )
    if uncovered_total:
        problems.append(
            f"{shares_text(uncovered_total)} shares were sold beyond the recorded purchases; "
            "their cost and gain are unknown"
        )
    if moved_uncovered:
        problems.append(
            f"{shares_text(moved_uncovered)} shares were moved out beyond the recorded "
            "purchases; the receiving account has no cost for them"
        )
    currency = next(iter(currencies)) if len(currencies) == 1 else None
    if len(currencies) > 1:
        problems.append("the account's transactions use more than one currency")
    valued = account_value(db, obj, as_of=as_of)
    if open_lots and currency is not None:
        if valued.missing_quote or valued.source != "market":
            problems.append("no market quote, so there is no unrealized gain")
        elif valued.total_amount is not None and valued.total_amount.commodity != currency:
            problems.append("its quote is in another currency than its cost")
    return HoldingCostBasis(
        obj,
        currency,
        tuple(open_lots),
        tuple(sales),
        valued,
        tuple(problems),
        tuple(moves),
    )


def _lot_picks(txn: Transaction | None, split_handle: str) -> tuple[LotPick, ...]:
    if txn is None:
        return ()
    for split in txn.splits:
        if split.handle == split_handle:
            return split.lot_picks
    return ()


def lots_before_sale(db: DbSQLite, account: Account | str, split_handle: str) -> tuple[Lot, ...]:
    """The lots open in ``account`` just before the sale made by ``split_handle``.

    These are the lots a sale may name. Shares bought earlier the same day count,
    as they do when the sale is computed. Empty when the split is not in the
    account's history.
    """
    snapshot: list[tuple[Lot, ...]] = []
    cost_basis(db, account, _before_split=split_handle, _snapshot=snapshot)
    return snapshot[0] if snapshot else ()


def holdings_cost_basis(db: DbSQLite, *, as_of: date | None = None) -> list[HoldingCostBasis]:
    """Every security account with any activity, ordered by full name."""
    found = [
        result
        for account in db.iter_accounts()
        if (result := cost_basis(db, account, as_of=as_of)) is not None
        and (result.lots or result.sales)
    ]
    return sorted(found, key=lambda item: db.full_name(item.account).casefold())


def holdings_charts(db: DbSQLite, holdings: list[HoldingCostBasis]) -> tuple[ChartModel, ...]:
    """Each holding's cost, market value, and unrealized gain, one chart per currency.

    The values are the holdings' own (``cost``, ``market_value``, ``unrealized_gain``);
    a holding whose market value cannot be compared with its cost shows its cost
    alone. A holding without shares or a cost currency is left out.
    """
    from .chart_model import BARS, ChartModel, ChartSeries

    by_currency: dict[str, list[HoldingCostBasis]] = {}
    for item in holdings:
        if item.currency is not None and (item.lots or item.market_value):
            by_currency.setdefault(item.currency, []).append(item)
    labels: dict[str, str] = {}
    for handle in by_currency:
        commodity = db.get_commodity(handle)
        labels[handle] = commodity.mnemonic if commodity is not None else handle
    charts = []
    for handle, items in by_currency.items():
        charts.append(
            ChartModel(
                f"holdings:{handle}",
                "Holdings: cost, market value, and unrealized gain",
                BARS,
                tuple(db.full_name(item.account) for item in items),
                (
                    ChartSeries("cost", "Cost", tuple(item.cost for item in items), 1),
                    ChartSeries(
                        "market", "Market value", tuple(item.market_value for item in items), 2
                    ),
                    ChartSeries(
                        "gain",
                        "Unrealized gain",
                        tuple(item.unrealized_gain for item in items),
                        3,
                    ),
                ),
                labels[handle],
            )
        )
    return tuple(charts)
