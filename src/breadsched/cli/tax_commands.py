"""Commands for one tax year: the report, and which accounts and tags it totals."""

from __future__ import annotations

import argparse

from ..gen.engine.cost_basis import shares_text
from ..gen.engine.tax_year import currency_labels, tax_year
from ..gen.services import SetTaxMarks, set_tax_marks, tax_marks
from ..presentation import service_error_message
from .common import AddCommand, CommandError, emit, open_book, resolve_account, table


def cmd_tax_year(args: argparse.Namespace) -> int:
    db = open_book(args.book, "r")
    try:
        report = tax_year(db, args.year)
        labels = currency_labels(db, report)

        def label(handle: str | None) -> str:
            return labels.get(handle, handle or "")

        payload = {
            "year": report.year,
            "gain_totals": [
                {
                    "term": total.term.value,
                    "currency": label(total.currency),
                    "lines": total.lines,
                    "proceeds": total.proceeds,
                    "cost": total.cost,
                    "gain": total.gain,
                }
                for total in report.gain_totals
            ],
            "gains": [line.as_dict(label(line.currency)) for line in report.gains],
            "accounts": [
                {
                    "account": item.account,
                    "name": item.full_name,
                    "currency": label(item.currency),
                    "amount": item.amount,
                    "transactions": item.transactions,
                    "marked_by": item.marked_by,
                }
                for item in report.accounts
            ],
            "tags": [
                {
                    "tag": item.tag,
                    "currency": label(item.currency),
                    "spent": item.spent,
                    "received": item.received,
                    "transactions": item.transactions,
                }
                for item in report.tags
            ],
            "income": [
                {
                    "account": item.account,
                    "name": item.full_name,
                    "currency": label(item.currency),
                    "amount": item.amount,
                    "transactions": item.transactions,
                }
                for item in report.income
            ],
            "income_totals": [
                {"currency": label(currency), "amount": total}
                for currency, total in report.income_totals
            ],
            "problems": list(report.problems),
        }
        parts = [
            f"Tax year {report.year} (calendar year; long-term means held over a year)",
            "Realized gains by term:",
            table(
                [
                    [
                        total.term.label,
                        label(total.currency),
                        total.proceeds.format(),
                        total.cost.format(),
                        total.gain.format(parens_negative=True),
                    ]
                    for total in report.gain_totals
                ],
                ["term", "currency", "proceeds", "cost", "gain"],
                right={2, 3, 4},
            )
            if report.gain_totals
            else "  No sales this year.",
        ]
        if report.gains:
            parts.append(
                table(
                    [
                        [
                            line.account_name,
                            shares_text(line.quantity),
                            line.acquired.isoformat() if line.acquired else "Various",
                            line.sold.isoformat(),
                            line.term.label,
                            line.proceeds.format(),
                            line.cost.format(),
                            line.gain.format(parens_negative=True),
                        ]
                        for line in report.gains
                    ],
                    ["security", "shares", "acquired", "sold", "term", "proceeds", "cost", "gain"],
                    right={1, 5, 6, 7},
                )
            )
        parts.append("Tax-relevant accounts:")
        parts.append(
            table(
                [
                    [
                        item.full_name,
                        label(item.currency),
                        str(item.transactions),
                        item.amount.format(parens_negative=True),
                        item.marked_by,
                    ]
                    for item in report.accounts
                ],
                ["account", "currency", "transactions", "total", "marked by"],
                right={2, 3},
            )
            if report.accounts
            else "  No account is marked tax-relevant."
        )
        parts.append("Tax-relevant tags:")
        parts.append(
            table(
                [
                    [
                        item.tag,
                        label(item.currency),
                        str(item.transactions),
                        item.spent.format(),
                        item.received.format(),
                    ]
                    for item in report.tags
                ],
                ["tag", "currency", "transactions", "spent", "received"],
                right={2, 3, 4},
            )
            if report.tags
            else "  No tag is marked tax-relevant."
        )
        parts.append("Income by source:")
        parts.append(
            table(
                [
                    *(
                        [
                            item.full_name,
                            label(item.currency),
                            str(item.transactions),
                            item.amount.format(parens_negative=True),
                        ]
                        for item in report.income
                    ),
                    *(
                        ["Total", label(currency), "", total.format(parens_negative=True)]
                        for currency, total in report.income_totals
                    ),
                ],
                ["source", "currency", "transactions", "amount"],
                right={2, 3},
            )
            if report.income
            else "  No income this year."
        )
        parts.extend(f"note: {problem}" for problem in report.problems)
        emit(payload, args, "\n".join(parts))
        return 0
    finally:
        db.close()


_ACCOUNT_MARKS = {"on": True, "off": False, "gnucash": None}


def cmd_tax_marks(args: argparse.Namespace) -> int:
    changes = bool(args.account or args.tag)
    db = open_book(args.book, "w" if changes else "r")
    try:
        if changes:
            accounts: dict[str, bool | None] = {}
            for item in args.account or ():
                name, _sep, mark = item.rpartition("=")
                if not name or mark not in _ACCOUNT_MARKS:
                    raise CommandError(f"--account takes ACCOUNT=on|off|gnucash, not {item!r}")
                accounts[resolve_account(db, name).handle] = _ACCOUNT_MARKS[mark]
            tags: dict[str, bool] = {}
            for item in args.tag or ():
                name, _sep, mark = item.rpartition("=")
                if not name or mark not in ("on", "off"):
                    raise CommandError(f"--tag takes TAG=on|off, not {item!r}")
                tags[name] = mark == "on"
            result = set_tax_marks(db, SetTaxMarks(accounts, tags))
            if result.value is None:
                raise CommandError(service_error_message(result.errors[0]))
            marks = result.value
        else:
            marks = tax_marks(db)
        payload = {
            "accounts": [
                {
                    "account": mark.account.handle,
                    "name": mark.full_name,
                    "relevant": mark.relevant,
                    "gnucash": mark.gnucash,
                    "breadsched": mark.override,
                }
                for mark in marks.accounts
                if mark.relevant or mark.override is not None or mark.gnucash
            ],
            "tags": [{"tag": tag, "relevant": marked} for tag, marked in marks.tags],
        }
        lines = ["Tax-relevant accounts:"]
        for mark in marks.accounts:
            if not (mark.relevant or mark.override is not None or mark.gnucash):
                continue
            source = "GnuCash" if mark.override is None else "BreadSched"
            state = "yes" if mark.relevant else "no"
            lines.append(f"  {mark.full_name}: {state} (marked by {source})")
        if len(lines) == 1:
            lines.append("  none")
        marked = [tag for tag, on in marks.tags if on]
        lines.append("Tax-relevant tags: " + (", ".join(marked) if marked else "none"))
        emit(payload, args, "\n".join(lines))
        return 0
    finally:
        db.close()


def register(add: AddCommand) -> None:
    """Add the tax-year subcommands."""
    report = add(
        "tax-year",
        "One calendar year's realized gains by term, tax-relevant totals, and income",
    )
    report.add_argument("--year", type=int, required=True, help="the calendar year")
    report.set_defaults(func=cmd_tax_year)

    marks = add("tax-marks", "Show or change which accounts and tags are tax-relevant")
    marks.add_argument(
        "--account",
        action="append",
        metavar="ACCOUNT=on|off|gnucash",
        help="mark an account (with those beneath it); gnucash follows GnuCash's mark",
    )
    marks.add_argument("--tag", action="append", metavar="TAG=on|off", help="mark a tag")
    marks.set_defaults(func=cmd_tax_marks)
