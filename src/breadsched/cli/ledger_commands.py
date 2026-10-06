"""Commands over accounts, recorded transactions, balances, and exchange rates."""

from __future__ import annotations

import argparse
from datetime import date

from ..gen.db.sqlite import DbSQLite
from ..gen.engine import (
    ledger,
    valuation,
)
from ..gen.lib import (
    Account,
    AccountType,
    Amount,
    Money,
    Split,
    Transaction,
)
from ..gen.services import (
    DeleteAccount,
    DeleteTransaction,
    SaveAccount,
    SaveTransaction,
    TransactionInput,
    TransactionSplitInput,
    delete_account,
    delete_transaction,
    save_account,
    save_transaction,
    transaction_currency,
)
from ..presentation import (
    service_error_message,
)
from .common import (
    AddCommand,
    CommandError,
    boolean,
    emit,
    find_transaction,
    open_book,
    parse_date,
    resolve_account,
    resolve_currency,
    table,
)


def cmd_rate(args: argparse.Namespace) -> int:
    """Write a dated directional FX quote through the shared valuation contract."""
    quote_date = parse_date(args.date)
    if quote_date is None:
        raise CommandError("rate date is required")
    try:
        value = Money(args.value)
    except (ValueError, TypeError, ZeroDivisionError) as exc:
        raise CommandError("rate must be a positive exact number") from exc
    db = open_book(args.book)
    try:
        source = resolve_currency(db, args.source)
        target = resolve_currency(db, args.target)
        try:
            quote = valuation.save_currency_quote(
                db,
                source_handle=source,
                target_handle=target,
                quote_date=quote_date,
                value=value,
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
    finally:
        db.close()
    exact_rate = f"{quote.value.numerator}/{quote.value.denominator}"
    emit(
        {
            "handle": quote.handle,
            "from": args.source,
            "to": args.target,
            "date": quote.quote_date,
            "rate": exact_rate,
            "source": quote.source,
        },
        args,
        f"Saved {args.source} → {args.target}: {exact_rate} on {quote_date} ({quote.source})",
    )
    return 0


def cmd_accounts(args: argparse.Namespace) -> int:
    db = open_book(args.book, "r")
    try:
        as_of = parse_date(args.as_of)
        payload = []
        rows = []

        def walk(handle: str | None, depth: int) -> None:
            for account in db.child_accounts(handle):
                if account.hidden and not args.all:
                    continue
                result = valuation.aggregate_value(
                    db, accounts=[account, *db.descendants(account.handle)], as_of=as_of
                )
                valued = valuation.account_value(db, account, as_of=as_of)
                total = result.amount.value if result.amount is not None else None
                try:
                    book_balance = ledger.balance_recursive(db, account.handle, as_of=as_of)
                except TypeError as exc:
                    if "cannot combine unlike commodities" not in str(exc):
                        raise
                    book_balance = None
                payload.append(
                    {
                        "handle": account.handle,
                        "name": db.full_name(account),
                        "type": account.atype.value,
                        "code": account.code,
                        "description": account.description,
                        "notes": account.notes,
                        "source_notes": account.source_notes,
                        "placeholder": account.placeholder,
                        "hidden": account.hidden,
                        "commodity": account.commodity,
                        "commodity_scu": account.commodity_scu,
                        "source_guid": account.source_guid,
                        "source_type": account.source_type or None,
                        "source_fields": [field.serialize() for field in account.source_fields],
                        "balance": total,
                        "missing_quotes": list(result.missing_quotes),
                        "quote_date": valued.price_date,
                        "quote_source": valued.price_source,
                        "quote_age_days": valued.quote_age_days,
                        "conversion_path": valued.conversion_path,
                        "quote_evidence": valuation.quote_evidence(db, valued),
                        "book_balance": book_balance,
                    }
                )
                label = ("  " * depth) + account.name
                quote_evidence = valuation.quote_evidence(db, valued)
                rows.append(
                    [
                        label,
                        account.atype.value,
                        total.format(parens_negative=True)
                        if total is not None
                        else "Missing reporting-currency quote",
                        quote_evidence,
                    ]
                )
                walk(account.handle, depth + 1)

        root = db.root_account()
        walk(root.handle if root else None, 0)
        emit(payload, args, table(rows, ["account", "type", "balance", "quote"], right={2}))
        return 0
    finally:
        db.close()


def cmd_register(args: argparse.Namespace) -> int:
    db = open_book(args.book, "r")
    try:
        account = resolve_account(db, args.account)
        rows = ledger.register(
            db, account.handle, start=parse_date(args.start), end=parse_date(args.end)
        )
        if args.tag:
            wanted = " ".join(args.tag.split()).casefold()
            rows = [
                row for row in rows if any(t.casefold() == wanted for t in row.transaction.tags)
            ]
        if args.limit:
            rows = rows[-args.limit :]
        payload = [
            {
                "handle": row.transaction.handle,
                "date": row.post_date,
                "description": row.description,
                "transfer": row.transfer_label(db),
                "amount": row.amount,
                "balance": row.running,
                "tags": row.transaction.tags,
                "attachments": len(row.transaction.attachments)
                + (1 if row.transaction.source_link else 0),
            }
            for row in rows
        ]
        text = table(
            [
                [
                    row.transaction.handle[:8],
                    row.post_date.isoformat(),
                    row.description[:40],
                    row.transfer_label(db)[:30],
                    row.amount.format(parens_negative=True),
                    row.running.format(parens_negative=True),
                ]
                for row in rows
            ],
            ["id", "date", "description", "transfer", "amount", "balance"],
            right={4, 5},
        )
        print(f"{db.full_name(account)}  ({account.atype.value})") if not args.json else None
        emit(payload, args, text)
        return 0
    finally:
        db.close()


def cmd_balance(args: argparse.Namespace) -> int:
    db = open_book(args.book, "r")
    try:
        as_of = parse_date(args.as_of)
        if args.account:
            account = resolve_account(db, args.account)
            amount = (
                ledger.balance_recursive(db, account.handle, as_of=as_of)
                if args.recursive
                else ledger.balance(db, account.handle, as_of=as_of)
            )
            emit(
                {"account": db.full_name(account), "balance": amount},
                args,
                f"{db.full_name(account)}: {amount.format('', parens_negative=True)}",
            )
        else:
            net_worth = valuation.aggregate_value(db, as_of=as_of, net_worth=True)
            cash = valuation.aggregate_value(
                db,
                accounts=[
                    a for a in db.iter_accounts() if a.atype.is_cash_like and not a.placeholder
                ],
                as_of=as_of,
            )
            summary = {
                "cash": cash.amount.value if cash.amount is not None else None,
                "net_worth": net_worth.amount.value if net_worth.amount is not None else None,
            }
            payload = {
                **summary,
                "cash_missing_quotes": list(cash.missing_quotes),
                "net_worth_missing_quotes": list(net_worth.missing_quotes),
                "net_worth_incompatible_accounts": list(net_worth.incompatible_accounts),
            }
            emit(
                payload,
                args,
                table(
                    [
                        [
                            k.replace("_", " "),
                            v.format(parens_negative=True)
                            if v is not None
                            else "Missing reporting-currency quote",
                        ]
                        for k, v in summary.items()
                    ],
                    ["measure", "amount"],
                    right={1},
                ),
            )
        return 0
    finally:
        db.close()


def cmd_add(args: argparse.Namespace) -> int:
    db = open_book(args.book)
    try:
        debit = resolve_account(db, args.to)
        credit = resolve_account(db, getattr(args, "from"))
        when = parse_date(args.date) or date.today()
        amount = Money(args.amount)
        currency = transaction_currency(db)
        result = save_transaction(
            db,
            SaveTransaction(
                TransactionInput(
                    post_date=when,
                    description=args.description,
                    num=args.num,
                    currency=currency,
                    splits=(
                        TransactionSplitInput(
                            debit.handle, Amount(amount, currency), memo=args.memo
                        ),
                        TransactionSplitInput(
                            credit.handle, Amount(-amount, currency), memo=args.memo
                        ),
                    ),
                )
            ),
        )
        if not result.ok:
            raise CommandError(service_error_message(result.errors[0]))
        saved = result.value
        assert saved is not None
        emit(
            {"handle": saved.handle, "date": when, "amount": amount},
            args,
            f"Posted {amount.format()} {db.full_name(credit)} -> {db.full_name(debit)} on {when}",
        )
        return 0
    finally:
        db.close()


def cmd_edit(args: argparse.Namespace) -> int:
    """Change a transaction that is already recorded.

    Only the fields given are touched. Amounts are only editable on a two-split
    transaction: with three or more legs there is no single 'the amount', and
    guessing which one to move would silently rewrite someone's books.
    """
    db = open_book(args.book)
    try:
        target = find_transaction(db, args.transaction)
        before = target.describe()

        if args.date:
            parsed_date = parse_date(args.date)
            if parsed_date is not None:
                target.post_date = parsed_date
        if args.description is not None:
            target.description = args.description
        if args.num is not None:
            target.num = args.num
        if args.memo is not None:
            for split in target.splits:
                split.memo = args.memo

        if args.amount is not None:
            if len(target.splits) != 2:
                raise CommandError(
                    f"this transaction has {len(target.splits)} splits; amounts can "
                    "only be changed on a two-split transaction. Edit it in the "
                    "interface, or delete and re-enter it."
                )
            amount = Money(args.amount)
            positive = next((s for s in target.splits if s.value > 0), target.splits[0])
            for split in target.splits:
                split.value = amount if split is positive else -amount
                split.quantity = split.value

        result = save_transaction(
            db,
            SaveTransaction(
                _transaction_input(db, target),
                existing_handle=target.handle,
                source=target,
            ),
        )
        if not result.ok:
            raise CommandError(service_error_message(result.errors[0]))

        emit(
            {"handle": target.handle, "date": target.post_date, "description": target.description},
            args,
            f"Edited {before}\n     -> {target.describe()}",
        )
        return 0
    finally:
        db.close()


def cmd_delete(args: argparse.Namespace) -> int:
    db = open_book(args.book)
    try:
        target = find_transaction(db, args.transaction)
        description = target.describe()
        result = delete_transaction(db, DeleteTransaction(target.handle))
        if not result.ok:
            raise CommandError(service_error_message(result.errors[0]))
        emit({"deleted": target.handle}, args, f"Deleted {description}")
        return 0
    finally:
        db.close()


def _transaction_input(db: DbSQLite, transaction: Transaction) -> TransactionInput:
    """Translate one editable CLI model to the shared transaction contract."""
    currency = transaction_currency(db, transaction.currency)

    def split_input(split: Split) -> TransactionSplitInput:
        account = db.get_account(split.account)
        quantity_commodity = account.commodity if account is not None else None
        return TransactionSplitInput(
            split.account,
            Amount(split.value, currency),
            quantity=Amount(split.quantity, quantity_commodity or currency),
            handle=split.handle,
            memo=split.memo,
            planning_flow=split.planning_flow,
            investment_activity=split.investment_activity,
        )

    return TransactionInput(
        post_date=transaction.post_date,
        description=transaction.description,
        num=transaction.num,
        notes=transaction.notes,
        currency=currency,
        splits=tuple(split_input(split) for split in transaction.splits),
    )


def cmd_account(args: argparse.Namespace) -> int:
    """Add, edit or remove an account."""
    db = open_book(args.book, "r" if args.action == "list" else "w")
    try:
        if args.action == "list":
            rows = []
            for account in sorted(db.iter_accounts(), key=db.full_name):
                if account.is_root:
                    continue
                linked = db.get_account(account.linked_asset or "")
                payment_account = db.get_account(account.card_payment_account or "")
                rows.append(
                    [
                        db.full_name(account),
                        account.atype.value,
                        account.group,
                        db.full_name(linked) if linked else "",
                        ""
                        if account.atype is not AccountType.CREDIT
                        else ("monthly" if account.pays_in_full else "carries"),
                        db.full_name(payment_account) if payment_account else "",
                    ]
                )
            emit(
                [
                    {
                        "name": r[0],
                        "type": r[1],
                        "group": r[2],
                        "linked_asset": r[3],
                        "card": r[4],
                        "card_payment_account": r[5],
                    }
                    for r in rows
                ],
                args,
                table(
                    rows,
                    ["account", "type", "group", "linked asset", "card", "paid from"],
                ),
            )
            return 0

        if args.action == "add":
            if not args.name:
                raise CommandError("--name is required")
            parent = resolve_account(db, args.parent) if args.parent else db.root_account()
            if parent is None:
                raise CommandError("this book has no root account")
            account = Account(
                name=args.name,
                atype=AccountType.parse(args.type),
                parent=parent.handle,
                code=args.code or "",
                description=args.description or "",
                placeholder=args.placeholder,
            )
            _apply_account_options(db, account, args)
            result = save_account(
                db,
                SaveAccount(
                    account,
                    opening_balance=Money(args.opening) if args.opening else None,
                    opening_date=parse_date(args.opening_date) or date.today(),
                ),
            )
            if not result.ok:
                raise CommandError(service_error_message(result.errors[0]))
            emit(
                {"handle": account.handle, "name": db.full_name(account)},
                args,
                f"Added {db.full_name(account)} ({account.atype.value})",
            )
            return 0

        account = resolve_account(db, args.name or "")
        if args.action == "remove":
            result = delete_account(db, DeleteAccount(account.handle))
            if not result.ok:
                raise CommandError(service_error_message(result.errors[0]))
            emit({"removed": account.handle}, args, f"Removed {db.full_name(account)}")
            return 0

        # edit
        source = Account.from_dict(account.serialize())
        if args.rename:
            account.name = args.rename
        if args.type:
            account.atype = AccountType.parse(args.type)
        if args.description is not None:
            account.description = args.description
        if args.code is not None:
            account.code = args.code
        if args.parent:
            account.parent = resolve_account(db, args.parent).handle
        _apply_account_options(db, account, args)
        result = save_account(
            db,
            SaveAccount(account, existing_handle=account.handle, source=source),
        )
        if not result.ok:
            raise CommandError(service_error_message(result.errors[0]))
        emit(
            {"handle": account.handle, "name": db.full_name(account)},
            args,
            f"Edited {db.full_name(account)}",
        )
        return 0
    finally:
        db.close()


def _apply_account_options(db: DbSQLite, account: Account, args) -> None:
    """Shared between add and edit: the relationship fields."""
    if args.dependent_care is not None:
        if account.atype is not AccountType.FSA:
            raise CommandError("--dependent-care applies only to an FSA account")
        account.fsa_dependent_care = args.dependent_care
    if args.cost_basis is not None:
        if account.atype not in {AccountType.INVESTMENT, AccountType.RETIREMENT}:
            raise CommandError("--cost-basis applies only to an Investment or Retirement account")
        account.cost_basis_method = args.cost_basis
    if args.group is not None:
        account.group = args.group
    if args.linked_asset:
        account.linked_asset = resolve_account(db, args.linked_asset).handle
    if args.carries_balance is not None:
        account.pays_in_full = not args.carries_balance
    if args.usual_payment:
        account.usual_payment = Money(args.usual_payment)
    if args.payment_day:
        account.payment_day = args.payment_day
    if args.payment_account is not None:
        if account.atype is not AccountType.CREDIT:
            raise CommandError("--payment-account applies only to a Credit card account")
        if args.payment_account.strip().lower() == "none":
            account.card_payment_account = None
            return
        payment = resolve_account(db, args.payment_account)
        if not payment.atype.is_cash_like:
            raise CommandError("--payment-account must name a Bank or Cash account")
        account.card_payment_account = payment.handle
    elif account.atype is not AccountType.CREDIT:
        account.card_payment_account = None


def register(add: AddCommand) -> None:
    """Add the ledger subcommands."""
    accounts = add("accounts", "Show the chart of accounts with balances")
    accounts.add_argument("--as-of", help="balances as at this date (YYYY-MM-DD)")
    accounts.add_argument("--all", action="store_true", help="include hidden accounts")
    accounts.set_defaults(func=cmd_accounts)

    account = add("account", "Add, edit, list or remove accounts")
    account.add_argument("action", choices=["list", "add", "edit", "remove"])
    account.add_argument("--name", help="account name, or path when editing")
    account.add_argument("--rename")
    account.add_argument("--type", help="BANK, CREDIT CARD, LOAN, FSA, EXPENSE, ...")
    account.add_argument("--parent")
    account.add_argument("--description")
    account.add_argument("--code")
    account.add_argument("--placeholder", action="store_true")
    account.add_argument("--group", help="dashboard group this account belongs to")
    account.add_argument("--linked-asset", help="for a loan: the asset behind it")
    account.add_argument(
        "--carries-balance",
        type=boolean,
        metavar="yes|no",
        help="for a credit card: whether a balance is carried",
    )
    account.add_argument(
        "--dependent-care",
        type=boolean,
        metavar="yes|no",
        help="for an FSA: a dependent care FSA pays only what has been contributed, "
        "needs no EOB, and carries nothing over",
    )
    account.add_argument(
        "--cost-basis",
        choices=["fifo", "average"],
        help="for a security account: whether a sale's cost is its oldest shares' (fifo) "
        "or the average of every share held",
    )
    account.add_argument("--usual-payment", help="typical payment on a card")
    account.add_argument("--payment-day", type=int)
    account.add_argument(
        "--payment-account",
        help="Bank or Cash account used to pay a card; 'none' clears it",
    )
    account.add_argument("--opening", help="opening balance to post")
    account.add_argument("--opening-date")
    account.set_defaults(func=cmd_account)

    register = add("register", "Show one account's register with a running balance")
    register.add_argument("account", help="account name, full path, or handle")
    register.add_argument("--start")
    register.add_argument("--end")
    register.add_argument("--limit", type=int, default=0, help="show only the last N rows")
    register.add_argument(
        "--tag", help="only transactions with this tag (the running balance is unchanged)"
    )
    register.set_defaults(func=cmd_register)

    balance = add("balance", "Show a balance, or the book's headline totals")
    balance.add_argument("account", nargs="?")
    balance.add_argument("--as-of")
    balance.add_argument("--recursive", action="store_true", help="include child accounts")
    balance.set_defaults(func=cmd_balance)

    rate = add("rate", "Save a dated manual exchange rate (target units per source unit)")
    rate.add_argument("--from", dest="source", required=True, help="source currency code or handle")
    rate.add_argument("--to", dest="target", required=True, help="target currency code or handle")
    rate.add_argument("--date", required=True, help="quote date (YYYY-MM-DD)")
    rate.add_argument("--value", required=True, help="target units per one source unit")
    rate.set_defaults(func=cmd_rate)

    add_txn = add("add", "Post a two-split transaction")
    add_txn.add_argument("--date", default="today")
    add_txn.add_argument("--description", required=True)
    add_txn.add_argument("--from", required=True, dest="from", help="account the money leaves")
    add_txn.add_argument("--to", required=True, help="account the money arrives in")
    add_txn.add_argument("--amount", required=True)
    add_txn.add_argument("--memo", default="")
    add_txn.add_argument("--num", default="")
    add_txn.set_defaults(func=cmd_add)

    edit = add("edit", "Change a transaction that is already recorded")
    edit.add_argument("transaction", help="transaction handle, or a unique prefix")
    edit.add_argument("--date")
    edit.add_argument("--description")
    edit.add_argument("--num")
    edit.add_argument("--memo", help="set the memo on every split")
    edit.add_argument("--amount", help="two-split transactions only")
    edit.set_defaults(func=cmd_edit)

    delete = add("delete", "Remove a transaction")
    delete.add_argument("transaction", help="transaction handle, or a unique prefix")
    delete.set_defaults(func=cmd_delete)
