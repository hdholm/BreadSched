"""Working out relationships an imported book does not state.

GnuCash records what happened, not what things mean. A mortgage and the house it
bought are two accounts that never mention each other; a credit card's due date
exists only as a pattern in a year of payments. Both matter here — equity needs the
pair, and a forecast needs the date — so they are inferred rather than demanded
from the user on the first run.

Every inference is a *suggestion* with a reason and a confidence, not a silent
change. A wrong guess that announces itself costs a moment; a wrong guess applied
quietly becomes a number somebody trusts.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date

from ..db.sqlite import DbSQLite
from ..lib.account import Account, AccountClass, AccountType
from ..lib.money import Money

__all__ = [
    "Suggestion",
    "infer_all",
    "infer_asset_links",
    "infer_card_settings",
    "apply_suggestions",
]


@dataclass
class Suggestion:
    """One proposed change to an account, with the evidence for it."""

    account: str
    field: str
    value: object
    reason: str
    confidence: float = 0.5
    account_name: str = ""
    value_label: str = ""

    def apply(self, account: Account) -> None:
        setattr(account, self.field, self.value)


@dataclass
class InferenceResult:
    suggestions: list[Suggestion] = field(default_factory=list)

    def for_field(self, name: str) -> list[Suggestion]:
        return [s for s in self.suggestions if s.field == name]

    def __len__(self) -> int:
        return len(self.suggestions)


# ------------------------------------------------------------------- linking


def infer_asset_links(db: DbSQLite) -> list[Suggestion]:
    """Pair each loan with the asset it was borrowed against.

    The evidence is the loan's own opening entry: money borrowed to buy a house
    lands in the house's account, so the asset that appears on the other side of
    the loan's largest transaction is almost always the thing it paid for.

    Where no transaction says so, the account names are compared — a "Mortgage
    Easton" beside a "Home Easton" is not a coincidence, but it is a weaker claim
    and is offered with lower confidence.
    """
    assets = [
        a
        for a in db.iter_accounts()
        if a.account_class is AccountClass.ASSET
        and not a.placeholder
        and not a.is_root
        and not a.atype.is_cash_like
    ]
    if not assets:
        return []

    suggestions: list[Suggestion] = []
    for loan in db.iter_accounts():
        if loan.account_class is not AccountClass.LIABILITY or loan.placeholder:
            continue
        if loan.linked_asset:
            continue

        paired: Counter[str] = Counter()
        largest = Money(0)
        for txn in db.iter_transactions(account=loan.handle):
            value = abs(txn.value_for(loan.handle))
            for split in txn.splits:
                if split.account == loan.handle:
                    continue
                other = db.get_account(split.account)
                if other is None or other not in assets:
                    continue
                paired[split.account] += 1
                if value > largest:
                    largest = value

        if paired:
            handle, count = paired.most_common(1)[0]
            asset = db.get_account(handle)
            if asset is None:
                continue
            suggestions.append(
                Suggestion(
                    account=loan.handle,
                    field="linked_asset",
                    value=handle,
                    reason=(
                        f"{count} transaction(s) move between this loan and {db.full_name(asset)}"
                    ),
                    confidence=0.9 if count > 1 else 0.7,
                    account_name=db.full_name(loan),
                    value_label=db.full_name(asset),
                )
            )
            continue

        match = _match_by_name(db, loan, assets)
        if match is not None:
            suggestions.append(
                Suggestion(
                    account=loan.handle,
                    field="linked_asset",
                    value=match.handle,
                    reason=(f"the names share a place: {loan.name!r} and {match.name!r}"),
                    confidence=0.4,
                    account_name=db.full_name(loan),
                    value_label=db.full_name(match),
                )
            )
    return suggestions


_IGNORED_WORDS = {
    "mortgage",
    "loan",
    "home",
    "house",
    "second",
    "the",
    "of",
    "equity",
    "line",
    "credit",
    "account",
    "property",
    "value",
}


def _match_by_name(db: DbSQLite, loan: Account, assets: list[Account]) -> Account | None:
    """Find an asset sharing a distinctive word with the loan's name."""

    def words(name: str) -> set[str]:
        return {
            word.strip(",.()").lower() for word in name.replace(":", " ").split()
        } - _IGNORED_WORDS

    loan_words = words(loan.name)
    if not loan_words:
        return None
    best, score = None, 0
    for asset in assets:
        shared = len(loan_words & words(asset.name))
        if shared > score:
            best, score = asset, shared
    return best if score else None


# -------------------------------------------------------------- credit cards


def infer_card_settings(db: DbSQLite, months: int = 12) -> list[Suggestion]:
    """Work out how a credit card is used, from how it has been paid.

    A card whose balance returns to nothing after each payment is a payment
    channel; one that never does is a debt. The distinction changes whether a
    forecast should compound interest on it, so it is worth getting from the
    history rather than asking.

    The payment day is the most common day of the month on which payments arrived.
    """
    suggestions: list[Suggestion] = []
    for card in db.iter_accounts():
        if card.atype is not AccountType.CREDIT or card.placeholder:
            continue

        payments: list[tuple[date, Money]] = []
        payment_accounts: Counter[str] = Counter()
        for txn in db.iter_transactions(account=card.handle):
            value = txn.value_for(card.handle)
            if value <= 0:  # a debit to a credit account reduces what is owed
                continue
            source_handles: set[str] = set()
            for split in txn.splits:
                if split.account == card.handle or split.value >= 0:
                    continue
                source = db.get_account(split.account)
                if source is not None and source.atype.is_cash_like:
                    source_handles.add(source.handle)
            if not source_handles:
                continue
            payments.append((txn.post_date, value))
            payment_accounts.update(source_handles)
        if not payments:
            continue

        if payment_accounts:
            source_handle, count = payment_accounts.most_common(1)[0]
            if card.card_payment_account != source_handle:
                source = db.get_account(source_handle)
                suggestions.append(
                    Suggestion(
                        account=card.handle,
                        field="card_payment_account",
                        value=source_handle,
                        reason=(
                            f"{count} of {len(payments)} identified payments came from "
                            f"{db.full_name(source) if source is not None else source_handle}"
                        ),
                        confidence=min(0.95, count / len(payments)),
                        account_name=db.full_name(card),
                        value_label=(db.full_name(source) if source is not None else source_handle),
                    )
                )

        days = Counter(when.day for when, _ in payments)
        day, count = days.most_common(1)[0]
        if count >= 2:
            suggestions.append(
                Suggestion(
                    account=card.handle,
                    field="payment_day",
                    value=day,
                    reason=f"{count} of {len(payments)} payments fell on day {day}",
                    confidence=min(0.95, count / len(payments)),
                    account_name=db.full_name(card),
                    value_label=f"day {day}",
                )
            )

        balance = _balance_after_last_payment(db, card.handle, payments)
        carries = balance > Money("1.00")
        if carries != (not card.pays_in_full):
            suggestions.append(
                Suggestion(
                    account=card.handle,
                    field="pays_in_full",
                    value=not carries,
                    reason=(
                        f"the balance after the last payment was {balance.format()}"
                        + (
                            "; the card carries a balance"
                            if carries
                            else "; the card is cleared each month"
                        )
                    ),
                    confidence=0.8,
                    account_name=db.full_name(card),
                    value_label="cleared monthly" if not carries else "carries a balance",
                )
            )

        if carries:
            typical = _typical_payment(payments)
            if typical and card.usual_payment != typical:
                suggestions.append(
                    Suggestion(
                        account=card.handle,
                        field="usual_payment",
                        value=typical,
                        reason=(
                            f"the usual payment over {len(payments)} payments was "
                            f"{typical.format()}"
                        ),
                        confidence=0.7,
                        account_name=db.full_name(card),
                        value_label=typical.format(),
                    )
                )
    return suggestions


def _balance_after_last_payment(db: DbSQLite, handle: str, payments) -> Money:
    from . import ledger

    last = max(when for when, _ in payments)
    return ledger.balance(db, handle, as_of=last)


def _typical_payment(payments) -> Money | None:
    """The median payment, which a single unusual month cannot skew."""
    amounts = sorted(amount.to_decimal() for _when, amount in payments)
    if not amounts:
        return None
    middle = len(amounts) // 2
    if len(amounts) % 2:
        return Money(amounts[middle])
    return Money((amounts[middle - 1] + amounts[middle]) / 2).quantize(100)


# ------------------------------------------------------------------ applying


def infer_all(db: DbSQLite) -> InferenceResult:
    result = InferenceResult()
    result.suggestions.extend(infer_asset_links(db))
    result.suggestions.extend(infer_card_settings(db))
    result.suggestions.sort(key=lambda s: -s.confidence)
    return result


def apply_suggestions(
    db: DbSQLite,
    suggestions: list[Suggestion],
    message: str = "Apply inferred account relationships",
) -> int:
    """Write the chosen suggestions to their accounts, in one undoable step."""
    if not suggestions:
        return 0
    applied = 0
    with db.transaction(message) as txn:
        for suggestion in suggestions:
            account = db.get_account(suggestion.account)
            if account is None:
                continue
            suggestion.apply(account)
            db.commit_account(account, txn)
            applied += 1
    return applied
