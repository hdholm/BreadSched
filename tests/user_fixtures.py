"""People with roles in a served book, and a transaction for them to save (decision 0004)."""

from __future__ import annotations

from datetime import date

from breadsched.api.http_client import HttpBookClient
from breadsched.api.users import UserDirectory


def person(directory: UserDirectory, name: str, role: str | None, book_id: str) -> str:
    """A user with their own password, the role given in the book, and a token."""
    directory.add_user(name, "temporary-pass")
    directory.set_password(name, f"{name}-own-password", current="temporary-pass")
    if role is not None:
        directory.grant(book_id, name, role)
    return directory.issue_token(directory.user(name), f"{name}'s phone")


def new_transaction(db, client: HttpBookClient):
    from breadsched.gen.lib import AccountType, Amount, Money
    from breadsched.gen.services.transactions import (
        SaveTransaction,
        TransactionInput,
        TransactionSplitInput,
    )

    def leaf(atype):
        return next(
            account.handle
            for account in db.iter_accounts()
            if account.atype is atype and not account.placeholder
        )

    currency = client.transaction_currency()
    amount = Money("7.25")
    return SaveTransaction(
        TransactionInput(
            post_date=date(2026, 10, 2),
            description="Lunch",
            currency=currency,
            splits=(
                TransactionSplitInput(leaf(AccountType.EXPENSE), Amount(amount, currency)),
                TransactionSplitInput(leaf(AccountType.BANK), Amount(-amount, currency)),
            ),
        )
    )


def own_preference(db, key: str) -> object:
    """What the computer account kept as its own ``key`` for the book (decision 0004)."""
    directory = UserDirectory()
    return directory.preference(directory.host_user(), db.get_metadata("book.id"), key)
