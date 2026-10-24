"""Commands over accounts, recorded transactions, balances, and exchange rates."""

from __future__ import annotations

import argparse
from dataclasses import replace
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
)
from ..gen.services import (
    BalanceQuery,
    DeleteAccount,
    RegisterQuery,
    SaveAccount,
    SaveTransaction,
    TransactionInput,
    TransactionSplitInput,
    delete_account,
    save_account,
)
from ..presentation import (
    service_error_message,
)
from .common import (
    AddCommand,
    CommandError,
    add_server_options,
    book_api,
    boolean,
    emit,
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
                    book_balance = ledger.balance_recursive_amount(
                        db, account.handle, as_of=as_of
                    ).value
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
    with book_api(args, "r") as book:
        account = book.find_account(args.account)
        page = book.register(
            RegisterQuery(
                account.handle,
                start=parse_date(args.start),
                end=parse_date(args.end),
                tag=args.tag,
                limit=args.limit or None,
            )
        )
    payload = [
        {
            "handle": line.transaction,
            "date": line.when,
            "description": line.description,
            "transfer": line.transfer,
            "amount": line.amount,
            "balance": line.balance,
            "tags": list(line.tags),
            "attachments": line.attachments,
        }
        for line in page.lines
    ]
    text = table(
        [
            [
                line.transaction[:8],
                line.when.isoformat(),
                line.description[:40],
                line.transfer[:30],
                line.amount.format(parens_negative=True),
                line.balance.format(parens_negative=True),
            ]
            for line in page.lines
        ],
        ["id", "date", "description", "transfer", "amount", "balance"],
        right={4, 5},
    )
    if not args.json:
        print(f"{page.account.full_name}  ({page.account.type})")
    emit(payload, args, text)
    return 0


def cmd_balance(args: argparse.Namespace) -> int:
    as_of = parse_date(args.as_of)
    with book_api(args, "r") as book:
        if args.account:
            account = book.find_account(args.account)
            found = book.account_balance(BalanceQuery(account.handle, as_of, args.recursive))
            name = found.account.full_name
            emit(
                {"account": name, "balance": found.amount},
                args,
                f"{name}: {found.amount.format('', parens_negative=True)}",
            )
            return 0
        totals = book.balance_summary(as_of)
    summary = {"cash": totals.cash, "net_worth": totals.net_worth}
    payload = {
        **summary,
        "cash_missing_quotes": list(totals.cash_missing_quotes),
        "net_worth_missing_quotes": list(totals.net_worth_missing_quotes),
        "net_worth_incompatible_accounts": list(totals.net_worth_incompatible_accounts),
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


def cmd_add(args: argparse.Namespace) -> int:
    with book_api(args, "w") as book:
        debit = book.find_account(args.to)
        credit = book.find_account(getattr(args, "from"))
        when = parse_date(args.date) or date.today()
        amount = Money(args.amount)
        currency = book.transaction_currency()
        saved = book.save_transaction(
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
    emit(
        {"handle": saved.handle, "date": when, "amount": amount},
        args,
        f"Posted {amount.format()} {credit.full_name} -> {debit.full_name} on {when}",
    )
    return 0


def cmd_edit(args: argparse.Namespace) -> int:
    """Change a transaction that is already recorded.

    Only the fields given are touched. Amounts are only editable on a two-split
    transaction: with three or more legs there is no single 'the amount', and
    guessing which one to move would silently rewrite someone's books.
    """
    with book_api(args, "w") as book:
        target = book.find_transaction(args.transaction)
        before = target.describe()
        edited = book.editable_transaction(target.handle)
        changes: dict[str, object] = {}
        if args.date and (parsed_date := parse_date(args.date)) is not None:
            changes["post_date"] = parsed_date
        if args.description is not None:
            changes["description"] = args.description
        if args.num is not None:
            changes["num"] = args.num
        splits = edited.splits
        if args.memo is not None:
            splits = tuple(replace(split, memo=args.memo) for split in splits)
        if args.amount is not None:
            if len(splits) != 2:
                raise CommandError(
                    f"this transaction has {len(splits)} splits; amounts can "
                    "only be changed on a two-split transaction. Edit it in the "
                    "interface, or delete and re-enter it."
                )
            amount = Money(args.amount)
            positive = next((s for s in splits if s.value.value > 0), splits[0])
            splits = tuple(_with_value(s, amount if s is positive else -amount) for s in splits)
        edited = replace(edited, splits=splits, **changes)  # type: ignore[arg-type]
        book.save_transaction(SaveTransaction(edited, existing_handle=target.handle, source=target))
        after = book.find_transaction(target.handle)
    emit(
        {"handle": after.handle, "date": after.post_date, "description": after.description},
        args,
        f"Edited {before}\n     -> {after.describe()}",
    )
    return 0


def _with_value(split: TransactionSplitInput, value: Money) -> TransactionSplitInput:
    """``split`` moving ``value``, its quantity following in the account's commodity."""
    quantity = split.quantity.commodity if split.quantity is not None else split.value.commodity
    return replace(
        split, value=Amount(value, split.value.commodity), quantity=Amount(value, quantity)
    )


def cmd_delete(args: argparse.Namespace) -> int:
    with book_api(args, "w") as book:
        target = book.find_transaction(args.transaction)
        book.delete_transaction(target.handle)
    emit({"deleted": target.handle}, args, f"Deleted {target.describe()}")
    return 0


def cmd_account(args: argparse.Namespace) -> int:
    """Add, edit or remove an account."""
    db = open_book(args.book, "r" if args.action == "list" else "w")
    try:
        if args.action == "list":
            rows = []
            for account in sorted(db.iter_accounts(), key=db.full_name):
                if account.is_root:
                    continue
                linked = db.get_account(account.liability.linked_asset or "")
                payment_account = db.get_account(account.card.payment_account or "")
                rows.append(
                    [
                        db.full_name(account),
                        account.atype.value,
                        account.group,
                        db.full_name(linked) if linked else "",
                        ""
                        if account.atype is not AccountType.CREDIT
                        else ("monthly" if account.card.pays_in_full else "carries"),
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
        account.fsa.dependent_care = args.dependent_care
    if args.cost_basis is not None:
        if account.atype not in {AccountType.INVESTMENT, AccountType.RETIREMENT}:
            raise CommandError("--cost-basis applies only to an Investment or Retirement account")
        account.investment.cost_basis_method = args.cost_basis
    if args.group is not None:
        account.group = args.group
    if args.linked_asset:
        account.liability.linked_asset = resolve_account(db, args.linked_asset).handle
    if args.carries_balance is not None:
        account.card.pays_in_full = not args.carries_balance
    if args.usual_payment:
        account.card.usual_payment = Money(args.usual_payment)
    if args.payment_day:
        account.card.payment_day = args.payment_day
    if args.payment_account is not None:
        if account.atype is not AccountType.CREDIT:
            raise CommandError("--payment-account applies only to a Credit card account")
        if args.payment_account.strip().lower() == "none":
            account.card.payment_account = None
            return
        payment = resolve_account(db, args.payment_account)
        if not payment.atype.is_cash_like:
            raise CommandError("--payment-account must name a Bank or Cash account")
        account.card.payment_account = payment.handle
    elif account.atype is not AccountType.CREDIT:
        account.card.payment_account = None


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
    add_server_options(register)
    register.set_defaults(func=cmd_register)

    balance = add("balance", "Show a balance, or the book's headline totals")
    balance.add_argument("account", nargs="?")
    balance.add_argument("--as-of")
    balance.add_argument("--recursive", action="store_true", help="include child accounts")
    add_server_options(balance)
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
    add_server_options(add_txn)
    add_txn.set_defaults(func=cmd_add)

    edit = add("edit", "Change a transaction that is already recorded")
    edit.add_argument("transaction", help="transaction handle, or a unique prefix")
    edit.add_argument("--date")
    edit.add_argument("--description")
    edit.add_argument("--num")
    edit.add_argument("--memo", help="set the memo on every split")
    edit.add_argument("--amount", help="two-split transactions only")
    add_server_options(edit)
    edit.set_defaults(func=cmd_edit)

    delete = add("delete", "Remove a transaction")
    delete.add_argument("transaction", help="transaction handle, or a unique prefix")
    add_server_options(delete)
    delete.set_defaults(func=cmd_delete)
