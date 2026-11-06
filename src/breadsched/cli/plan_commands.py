"""Commands over the Plan: schedules, due and review queues, estimates, paychecks, and activity."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from decimal import Decimal, InvalidOperation

from ..api.local import lookup_schedule
from ..api.typed_codec import to_data
from ..api.views import (
    advance_days_json,
    due_review_json,
    next_occurrence_json,
    posted_due_json,
    resolution_json,
)
from ..gen.db.sqlite import DbSQLite
from ..gen.engine import (
    activity,
)
from ..gen.engine.payroll import PaycheckBreakdown, PayrollLine
from ..gen.lib import (
    Money,
    PeriodType,
    Recurrence,
    ScheduledTransaction,
)
from ..gen.services import (
    DueDecision,
    DueQuotes,
    FixedScheduleInput,
    LegPrice,
    PostDue,
    PostedDue,
    ResolveDue,
    ResolveNext,
    ReviewOccurrence,
    SaveFixedSchedule,
    SetAdvanceDays,
)
from ..gen.services.payroll import CreatePaycheck, PayChange, PayrollView, SavePayrollTemplate
from .common import (
    AddCommand,
    CommandError,
    add_server_options,
    book_api,
    emit,
    parse_date,
    table,
)


def _symbol_amounts(values: list[str] | None, option: str) -> dict[str, Money]:
    """``SYMBOL=AMOUNT`` pairs, keyed by upper-case symbol."""
    found: dict[str, Money] = {}
    for value in values or ():
        symbol, _, amount = value.partition("=")
        try:
            parsed = Money(Decimal(amount.strip().replace(",", "")))
        except (InvalidOperation, ValueError):
            raise CommandError(f"{option} needs SYMBOL=AMOUNT, not {value!r}") from None
        if not symbol.strip() or parsed <= 0:
            raise CommandError(f"{option} needs SYMBOL=AMOUNT above zero, not {value!r}")
        found[symbol.strip().upper()] = parsed
    return found


def _ask_share_price(leg) -> LegPrice | None:
    """Ask at the terminal for a leg's share price, or its shares as ``12.5 sh``."""
    prompt = (
        f"{leg.name} {leg.when.isoformat()}: {leg.value.format()} of {leg.mnemonic} "
        f"has no price. Share price in {leg.currency}, or shares as '12.5 sh': "
    )
    while True:
        answer = input(prompt).strip().lower()
        if not answer:
            return None
        shares = answer.endswith("sh")
        try:
            amount = Money(Decimal(answer.removesuffix("sh").strip().replace(",", "")))
        except (InvalidOperation, ValueError):
            print("Type a price such as 41.25, or shares such as 12.5 sh.", file=sys.stderr)
            continue
        if amount <= 0:
            print("Give an amount above zero.", file=sys.stderr)
            continue
        return LegPrice(
            leg.schedule,
            leg.when,
            leg.account,
            price=None if shares else amount,
            shares=amount if shares else None,
        )


def _share_prices(
    book, args: argparse.Namespace, occurrences: list[tuple[str, date]], as_of: date | None
) -> tuple[LegPrice, ...]:
    """Price every leg of these occurrences that buys or sells shares (#417).

    Online quotes are fetched first for commodities with a quote source.
    ``--share-price``/``--shares`` then price a leg of that symbol, over any stored
    price; a leg still without one is asked for at a terminal. A leg nothing prices
    stops the command, naming it.
    """
    if not occurrences:
        return ()
    request = DueQuotes(tuple(occurrences), as_of)
    if getattr(args, "online_quotes", True):
        from ..plugins.quotes import fetch_due_quotes
        from . import quote_commands

        needs, failures = fetch_due_quotes(book, request, quote_commands._fetcher())
        for failure in failures:
            print(f"{failure.symbol}: no online quote ({failure.reason})", file=sys.stderr)
    else:
        needs = book.due_quotes(request)
    prices = _symbol_amounts(getattr(args, "share_price", None), "--share-price")
    shares = _symbol_amounts(getattr(args, "shares", None), "--shares")
    given: list[LegPrice] = []
    missing: list[str] = []
    for leg in needs.legs:
        symbol = leg.mnemonic.upper()
        # A price or shares given on the command line wins over a stored price.
        if symbol in shares:
            given.append(LegPrice(leg.schedule, leg.when, leg.account, shares=shares[symbol]))
        elif symbol in prices:
            given.append(LegPrice(leg.schedule, leg.when, leg.account, price=prices[symbol]))
        elif leg.price is not None:
            continue
        elif sys.stdin.isatty() and not getattr(args, "json", False):
            asked = _ask_share_price(leg)
            if asked is not None:
                given.append(asked)
            else:
                missing.append(f"{leg.mnemonic} for {leg.name} on {leg.when.isoformat()}")
        else:
            missing.append(f"{leg.mnemonic} for {leg.name} on {leg.when.isoformat()}")
    if missing:
        raise CommandError(
            "no share price for "
            + "; ".join(missing)
            + ". Give --share-price SYMBOL=PRICE or --shares SYMBOL=COUNT"
        )
    return tuple(given)


def _due_references(reviews, reference: str) -> list[tuple[str, date]]:
    """Resolve ``SCHEDULE`` or ``SCHEDULE@YYYY-MM-DD`` against what is due."""
    name, _, raw_date = reference.partition("@")
    matches = [
        review
        for review in reviews
        if review.schedule == name or review.schedule.startswith(name) or review.name == name
    ]
    if not matches:
        raise CommandError(f"nothing due matches {name!r}")
    if len({review.schedule for review in matches}) > 1:
        raise CommandError(f"{name!r} matches more than one schedule; use more characters")
    review = matches[0]
    if not raw_date:
        return [(review.schedule, item.when) for item in review.items]
    try:
        when = date.fromisoformat(raw_date)
    except ValueError as exc:
        raise CommandError(f"invalid date in {reference!r}") from exc
    return [(review.schedule, when)]


def cmd_review(args: argparse.Namespace) -> int:
    """Unresolved transactions, the planned items Review offers, and why."""
    from ..presentation import REVIEW_ACTION_HELP, review_groups

    with book_api(args, "r") as book:
        # Grouped by category (decision 0005), each group oldest first.
        grouped = review_groups(book.review_queue())
        group_of = {item.handle: name for name, members in grouped for item in members}
        pending = [item for _name, members in grouped for item in members]
        if args.transaction:
            pending = [item for item in pending if item.handle.startswith(args.transaction)]
            if not pending:
                raise CommandError(f"no unresolved transaction matches {args.transaction!r}")
        items = []
        lines: list[str] = []
        heading = None
        for actual in pending:
            if group_of[actual.handle] != heading:
                heading = group_of[actual.handle]
                count = sum(1 for item in pending if group_of[item.handle] == heading)
                lines.append(f"== {heading} ({count}) ==")
            detail = book.review_detail(actual.handle)
            explained, hint = detail.candidates, detail.fsa_hint
            reason = detail.no_candidate_reason
            items.append(
                {
                    "handle": actual.handle,
                    "date": actual.date,
                    "description": actual.description,
                    "categories": list(actual.categories),
                    "funding": list(actual.funding),
                    "notes": actual.notes,
                    "group": group_of[actual.handle],
                    "fsa_hint": hint,
                    "no_candidate_reason": reason,
                    "candidates": [
                        {
                            "occurrence": item.candidate.event.key,
                            "date": item.candidate.event.planned_date,
                            "description": item.candidate.event.description,
                            "expected_amount": item.candidate.event.expected_amount,
                            "remaining_amount": item.candidate.event.remaining_amount,
                            "estimate": item.candidate.event.placeholder,
                            **item.as_dict(),
                        }
                        for item in explained
                    ],
                }
            )
            lines.append(f"{actual.date.isoformat()}  {actual.description}  [{actual.handle[:8]}]")
            accounts = " · ".join(
                part for part in (", ".join(actual.categories), ", ".join(actual.funding)) if part
            )
            if accounts:
                lines.append(f"  {accounts}")
            if actual.notes:
                lines.append(f"  Notes: {actual.notes}")
            if hint:
                lines.append(f"  {hint}")
            for item in explained:
                event = item.candidate.event
                lines.append(
                    f"  {item.label}: {event.planned_date.isoformat()} {event.description} "
                    f"(expected {event.expected_amount.format()}"
                    + (
                        f", {event.remaining_amount.format()} remains"
                        if event.placeholder or event.applied_amount
                        else ""
                    )
                    + ")"
                )
                lines.extend(f"    - {text}" for text in item.reasons)
            if reason:
                lines.append(f"  {reason}")
        if not pending:
            emit({"transactions": [], "actions": REVIEW_ACTION_HELP}, args, "Nothing to review.")
            return 0
        lines.append("")
        lines.extend(REVIEW_ACTION_HELP[key] for key in ("match", "reject", "skip", "unexpected"))
        emit({"transactions": items, "actions": REVIEW_ACTION_HELP}, args, "\n".join(lines))
        return 0


def cmd_due_review(args: argparse.Namespace) -> int:
    """List or decide due and missed scheduled occurrences, grouped by schedule."""
    if args.post_all and args.skip_all:
        raise CommandError("choose either --post-all or --skip-all, not both")
    as_of = parse_date(args.as_of) or date.today()
    with book_api(args, "w") as book:
        reviews = book.due_review(as_of)
        decisions: dict[tuple[str, date], DueDecision] = {}
        if args.post_all or args.skip_all:
            chosen = DueDecision.POST if args.post_all else DueDecision.SKIP
            # Reminders (remind in advance) are left for an explicit choice, as
            # GnuCash leaves them uncreated (#352).
            for review in reviews:
                decisions.update(
                    ((review.schedule, item.when), chosen)
                    for item in review.items
                    if item.stage != "reminder"
                )
        for references, decision in (
            (args.post, DueDecision.POST),
            (args.skip, DueDecision.SKIP),
        ):
            for reference in references or ():
                for key in _due_references(reviews, reference):
                    decisions[key] = decision
        if decisions:
            entering = [key for key, choice in decisions.items() if choice is DueDecision.POST]
            outcome = book.resolve_due(
                ResolveDue(
                    tuple((handle, when, choice) for (handle, when), choice in decisions.items()),
                    as_of=as_of,
                    prices=_share_prices(book, args, entering, as_of),
                )
            )
            emit(
                resolution_json(outcome),
                args,
                f"Posted {outcome.posted}; marked {outcome.skipped} as done",
            )
            return 0
        rows = [
            [
                review.name[:32],
                review.schedule[:8],
                str(len(review.items)),
                f"{review.items[0].when.isoformat()} to {review.items[-1].when.isoformat()}",
                review.frequency,
                review.total.format(),
            ]
            for review in reviews
        ]
        emit(
            due_review_json(reviews),
            args,
            table(rows, ["schedule", "id", "due", "dates", "frequency", "total"], right={2, 5})
            if rows
            else "Nothing is due.",
        )
        return 0


def cmd_scheduled_next(args: argparse.Namespace) -> int:
    """Show, enter, or skip a schedule's next occurrence before it is due (#351)."""
    decision = DueDecision.POST if args.enter else DueDecision.SKIP if args.skip else None
    as_of = parse_date(args.as_of)
    mode = "w" if decision is not None else "r"
    with book_api(args, mode) as book:
        sched = book.find_schedule(args.schedule)
        upcoming = book.next_occurrence(sched.handle, as_of=as_of)
        if decision is None:
            emit(
                next_occurrence_json(upcoming),
                args,
                f"{sched.name} has no occurrence to enter or skip"
                if upcoming is None
                else f"{sched.name}: next {upcoming.when.isoformat()} {upcoming.amount.format()}",
            )
            return 0
        entering = (
            [(sched.handle, upcoming.when)]
            if decision is DueDecision.POST and upcoming is not None
            else []
        )
        outcome = book.resolve_next(
            ResolveNext(
                sched.handle,
                decision,
                when=upcoming.when if upcoming is not None else None,
                as_of=as_of,
                prices=_share_prices(book, args, entering, as_of),
            )
        )
        assert upcoming is not None
        verb = "Entered" if decision is DueDecision.POST else "Skipped"
        emit(
            resolution_json(outcome),
            args,
            f"{verb} {sched.name} for {upcoming.when.isoformat()}",
        )
        return 0


def cmd_scheduled_advance(args: argparse.Namespace) -> int:
    """Show or set how many days ahead a schedule is entered or reminded (#352)."""
    writing = args.enter_days is not None or args.remind_days is not None
    with book_api(args, "w" if writing else "r") as book:
        sched = book.find_schedule(args.schedule)
        days = (
            book.set_advance_days(SetAdvanceDays(sched.handle, args.enter_days, args.remind_days))
            if writing
            else book.advance_days(sched.handle)
        )
        emit(
            advance_days_json(days),
            args,
            f"{days.name}: enter {days.advance_days} day(s) and remind "
            f"{days.remind_days} day(s) in advance",
        )
        return 0


def cmd_scheduled(args: argparse.Namespace) -> int:
    as_of = parse_date(args.as_of) or date.today()
    if args.post:
        # Through the book server, so it also works while one holds the book (#358).
        with book_api(args, "w") as book:
            # Online quotes first, so shares bought are priced; an occurrence
            # nothing prices is left due for the review to ask about (#417).
            if args.online_quotes:
                from ..plugins.quotes import fetch_due_quotes
                from . import quote_commands

                due = book.due_occurrences(as_of)
                fetch_due_quotes(
                    book,
                    DueQuotes(tuple((occ.schedule.handle, occ.when) for occ in due), as_of),
                    quote_commands._fetcher(),
                )
            posted = book.post_due(PostDue(as_of=as_of, only_automatic=not args.all)).posted
        emit(
            posted_due_json(PostedDue(posted)),
            args,
            f"Posted {len(posted)} scheduled transaction(s)"
            + (
                ""
                if not posted
                else ":\n" + "\n".join(f"  {item.when}  {item.description}" for item in posted)
            ),
        )
        return 0
    with book_api(args, "r") as book:
        occurrences = book.due_occurrences(as_of, args.days)
        payload = [
            {"date": occ.when, "name": occ.name, "amount": occ.amount} for occ in occurrences
        ]
        emit(
            payload,
            args,
            table(
                [[o.when.isoformat(), o.name, o.amount.format()] for o in occurrences],
                ["due", "schedule", "amount"],
                right={2},
            ),
        )
        return 0


def _find_schedule(db: DbSQLite, reference: str) -> ScheduledTransaction:
    """Locate a schedule by handle, unique handle prefix, or exact name."""
    return lookup_schedule(db, reference)


def _payroll_line_text(view: PayrollView, line: PayrollLine) -> str:
    amount = (
        f"{line.percent}% of gross"
        if line.percent is not None
        else line.amount.format()
        if line.amount is not None
        else ""
    )
    return f"{view.name(line.account)}: {amount}"


def _paycheck_text(breakdown: PaycheckBreakdown) -> str:
    rows = [[leg.kind.label, leg.name, leg.amount.format()] for leg in breakdown.legs]
    when = f" on {breakdown.when.isoformat()}" if breakdown.when else ""
    return (
        f"{breakdown.name}{when}: gross {breakdown.gross.format()}, take-home "
        f"{breakdown.net.format()} ({breakdown.take_home_percent}%)\n"
        + table(rows, ["line", "account", "amount"], right={2})
    )


def cmd_payroll(args: argparse.Namespace) -> int:
    """Show paychecks, manage payroll templates, create paychecks, and change pay."""
    writes = (
        args.save_template or args.delete_template or args.create or args.pay_change
    ) and not args.preview
    as_of = parse_date(args.as_of)
    with book_api(args, "w" if writes else "r") as book:
        if args.save_template:
            if not args.from_schedule:
                raise CommandError("--from-schedule is required with --save-template")
            source = book.find_schedule(args.from_schedule)
            template = book.template_from_schedule(source.handle, args.save_template, as_of)
            saved = book.save_payroll_template(SavePayrollTemplate(template))
            view = book.payroll(as_of)
            emit(
                saved.serialize(),
                args,
                f"Saved payroll template {saved.name!r} from {source.name!r}"
                + "".join(f"\n  {_payroll_line_text(view, line)}" for line in saved.lines),
            )
            return 0
        if args.delete_template:
            removed = book.delete_payroll_template(args.delete_template)
            emit({"deleted": removed.name}, args, f"Deleted payroll template {removed.name!r}")
            return 0
        if args.create:
            if not args.template:
                raise CommandError("--template is required with --create")
            recurrence = Recurrence(
                period=args.every,
                interval=args.interval,
                start=parse_date(args.start) or date.today(),
            )
            created = book.create_paycheck(
                CreatePaycheck(
                    args.template,
                    args.create,
                    recurrence,
                    gross=Money(args.gross) if args.gross else None,
                    auto_create=args.auto_create,
                )
            )
            breakdown = book.paycheck(created.handle, recurrence.start)
            emit(
                breakdown.as_dict(),
                args,
                f"Added paycheck {created.name!r} {recurrence.describe()}\n"
                + _paycheck_text(breakdown),
            )
            return 0
        if args.pay_change:
            if not args.gross or not args.start:
                raise CommandError("--gross and --start are required with --pay-change")
            schedule = book.find_schedule(args.pay_change)
            scaled = None
            if args.scale is not None:
                scaled = frozenset(book.find_account(item).handle for item in args.scale)
            amounts: dict[str, Money] = {}
            for item in args.set or ():
                account, _sep, amount = item.rpartition("=")
                if not account:
                    raise CommandError(f"--set takes ACCOUNT=AMOUNT, not {item!r}")
                amounts[book.find_account(account).handle] = Money(amount)
            request = PayChange(
                schedule.handle,
                parse_date(args.start) or date.today(),
                Money(args.gross),
                scaled=scaled,
                amounts=amounts,
            )
            plan = (
                book.preview_pay_change(request) if args.preview else book.apply_pay_change(request)
            )
            rows = [
                [line.kind.label, line.name, line.before.format(), line.after.format(), line.how]
                for line in plan.lines
            ]
            verb = "Pay change preview" if args.preview else "Saved pay change"
            emit(
                plan.as_dict(),
                args,
                f"{verb} for {schedule.name!r} from {plan.start.isoformat()}:\n"
                + table(rows, ["line", "account", "before", "after", "how"], right={2, 3}),
            )
            return 0
        if args.show:
            breakdown = book.paycheck(book.find_schedule(args.show).handle, as_of)
            emit(breakdown.as_dict(), args, _paycheck_text(breakdown))
            return 0
        view = book.payroll(as_of)
        found, templates = view.paychecks, view.templates
        emit(
            {
                "paychecks": [item.as_dict() for item in found],
                "templates": [item.serialize() for item in templates],
            },
            args,
            (
                table(
                    [
                        [
                            item.name,
                            item.gross.format(),
                            item.withheld.format(),
                            item.net.format(),
                            f"{item.take_home_percent}%",
                        ]
                        for item in found
                    ],
                    ["paycheck", "gross", "withheld", "net", "take-home"],
                    right={1, 2, 3, 4},
                )
                if found
                else "No schedule reads as a paycheck."
            )
            + "\n\n"
            + (
                "Payroll templates:\n"
                + "\n".join(
                    f"  {item.name}: gross {item.gross.format()}; "
                    + "; ".join(_payroll_line_text(view, line) for line in item.lines)
                    for item in templates
                )
                if templates
                else "No payroll templates. Save one with --save-template NAME "
                "--from-schedule SCHEDULE."
            ),
        )
        return 0


def cmd_activity(args: argparse.Namespace) -> int:
    """Show event-driven plan versus actuals in display-only time buckets."""
    start = parse_date(args.start)
    end = parse_date(args.end)
    if start is None or end is None:
        raise CommandError("activity requires --start and --end")
    with book_api(args, "r") as book:
        view = book.activity(
            start, end, activity.ReportingPeriod(args.period), parse_date(args.as_of)
        )
        report = view.report
        rows = [
            [
                period.label,
                period.planned_amount.format(),
                period.actual_amount.format(),
                period.amount_variance.format(parens_negative=True),
                period.planned_cash_change.format(parens_negative=True),
                period.actual_cash_change.format(parens_negative=True),
                len(period.unresolved),
                len(period.unresolved_actuals),
                len(period.unexpected),
            ]
            for period in report.periods
        ]
        text = table(
            rows,
            [
                "period",
                "planned",
                "actual",
                "variance",
                "planned cash",
                "actual cash",
                "expected due",
                "actual unresolved",
                "unexpected",
            ],
            right={1, 2, 3, 4, 5, 6, 7, 8},
        )
        if report.through_as_of_applies:
            assert report.as_of is not None

            def signed(value: Money | None) -> str:
                return value.format(parens_negative=True) if value is not None else "—"

            text += (
                f"\n\nThrough {report.as_of.isoformat()}: planned cash "
                f"{signed(report.planned_cash_through_as_of)}, actual cash "
                f"{signed(report.actual_cash_through_as_of)}, variance "
                f"{signed(report.cash_variance_through_as_of)}. Period columns include "
                "everything dated in each period, even after that date."
            )
        notes = view.currency_notes
        if not report.completeness.complete:
            # Totals are subtotals of what converted; say so beside them (#236).
            notes = (f"{report.completeness.label}: totals leave out the amounts below.", *notes)
        if notes:
            text = "\n".join((text, "", *notes))
        emit(report.as_dict(), args, text)
        return 0


def cmd_plan_unresolved(args: argparse.Namespace) -> int:
    """List unresolved scheduled expectations over an exact date horizon."""
    start = parse_date(args.start)
    end = parse_date(args.end)
    if start is None or end is None:
        raise CommandError("plan-unresolved requires --start and --end")
    with book_api(args, "r") as book:
        events = book.unresolved_occurrences(start, end)
        rows = [
            [
                event.key,
                event.planned_date.isoformat(),
                event.description[:40],
                event.expected_amount.format(),
            ]
            for event in events
        ]
        emit(
            [event.as_dict() for event in events],
            args,
            table(rows, ["occurrence", "planned", "description", "expected"], right={3}),
        )
        return 0


def cmd_plan_matches(args: argparse.Namespace) -> int:
    """Show the open occurrences one actual could count toward, best first, with what
    remains of each (decision 0005)."""
    with book_api(args, "r") as book:
        transaction = book.find_transaction(args.transaction)
        candidates = book.match_candidates(transaction.handle, args.window_days)
        payload = [
            {
                "occurrence": candidate.event.as_dict(),
                "date_distance_days": candidate.date_distance,
                "amount_difference": candidate.amount_difference,
                "common_accounts": candidate.common_accounts,
                "same_category": candidate.same_category,
                "same_funding": candidate.same_funding,
                "in_period": candidate.in_period,
                "fits": candidate.fits,
                "remaining_amount": candidate.event.remaining_amount,
            }
            for candidate in candidates
        ]
        rows = [
            [
                candidate.event.key,
                candidate.event.planned_date.isoformat(),
                candidate.event.description[:36],
                candidate.event.expected_amount.format(),
                candidate.event.remaining_amount.format(),
                candidate.date_distance,
                candidate.amount_difference.format(),
                candidate.common_accounts,
            ]
            for candidate in candidates
        ]
        text = table(
            rows,
            [
                "occurrence",
                "planned",
                "description",
                "expected",
                "remaining",
                "days",
                "amount diff",
                "accounts",
            ],
            right={3, 4, 5, 6, 7},
        )
        emit(payload, args, text)
        return 0


def _matching_lines(view) -> str:
    lines = [f"{view.date.isoformat()}  {view.description}  ({view.resolution.value})"]
    for index, leg in enumerate(view.legs):
        flag = "  not planned" if leg.not_planned else ""
        lines.append(
            f"  [{index}] {leg.account_name}  {leg.amount.format()}  "
            f"unallocated {leg.unallocated.format()}{flag}"
        )
        lines.extend(
            f"      → {item.occurrence}  {item.amount.format()}" for item in leg.allocations
        )
    return "\n".join(lines)


def cmd_plan_allocate(args: argparse.Namespace) -> int:
    """Count part or all of one split toward a planned occurrence (decision 0005)."""
    from ..gen.services.plan_matching import AllocateSplit

    reference, _, chosen = args.transaction.partition(":")
    amount = Money(args.amount) if args.amount is not None else None
    with book_api(args, "w") as book:
        transaction = book.find_transaction(reference)
        legs = book.transaction_matching(transaction.handle).legs
        if chosen.isdigit() and int(chosen) < len(legs):
            split = legs[int(chosen)].split
        elif chosen:
            found = [leg.split for leg in legs if leg.split.startswith(chosen)]
            if len(found) != 1:
                raise CommandError(f"no single plan leg of the transaction matches {chosen!r}")
            split = found[0]
        elif len(legs) == 1:
            split = legs[0].split
        else:
            raise CommandError(
                "the transaction has several plan legs; name one as TRANSACTION:INDEX"
            )
        view = book.allocate(AllocateSplit(transaction.handle, split, args.occurrence, amount))
        emit(to_data(view), args, _matching_lines(view))
        return 0


def cmd_plan_close(args: argparse.Namespace) -> int:
    """Close a planned occurrence early, or reopen it (decision 0005)."""
    with book_api(args, "w") as book:
        if args.reopen:
            view = book.reopen_occurrence(args.occurrence)
        else:
            view = book.close_occurrence(args.occurrence)
        state = "closed" if view.closed else "open"
        emit(
            to_data(view),
            args,
            f"{view.key}: {state}; expected {view.expected.format()}, applied "
            f"{view.applied.format()}, remaining {view.remaining.format()}",
        )
        return 0


def cmd_plan_resolve(args: argparse.Namespace) -> int:
    """Resolve one actual transaction against a selected scheduled occurrence."""
    with book_api(args, "w") as book:
        transaction = book.find_transaction(args.transaction)
        matched = book.match_review(ReviewOccurrence(transaction.handle, args.occurrence))
        assert matched.resolution is not None
        event = book.plan_occurrence(args.occurrence)
        emit(
            {
                "transaction": matched.transaction,
                "resolution": matched.resolution.value,
                "occurrence": event.key,
                "planned_for": event.planned_date,
                "planned_amount": event.expected_amount,
            },
            args,
            f"Matched {matched.transaction[:8]} to {event.key}",
        )
        return 0


def cmd_plan_reject(args: argparse.Namespace) -> int:
    """Persistently reject one suggested occurrence for an unresolved actual."""
    with book_api(args, "w") as book:
        transaction = book.find_transaction(args.transaction)
        book.reject_review(ReviewOccurrence(transaction.handle, args.occurrence))
        saved = book.find_transaction(transaction.handle)
        emit(
            {
                "transaction": saved.handle,
                "rejected_occurrence": args.occurrence,
                "rejected": list(saved.rejected_plan_occurrences),
            },
            args,
            f"Rejected {args.occurrence} for {saved.handle[:8]}",
        )
        return 0


def cmd_plan_unexpected(args: argparse.Namespace) -> int:
    """Explicitly declare an actual transaction to have no planned occurrence."""
    with book_api(args, "w") as book:
        transaction = book.find_transaction(args.transaction)
        marked = book.mark_review_unexpected(transaction.handle)
        assert marked.resolution is not None
        emit(
            {"transaction": marked.transaction, "resolution": marked.resolution.value},
            args,
            f"Marked {marked.transaction[:8]} as unexpected",
        )
        return 0


def _estimate_suggestions(args: argparse.Namespace) -> int:
    """Category estimates proposed from closed history, through the book server."""
    with book_api(args, "r") as book:
        proposals = book.historical_estimates(
            args.months, args.min_active_months, as_of=parse_date(args.as_of)
        ).proposals
        payload = [
            {
                "key": proposal.key,
                "purpose": proposal.purpose_name,
                "account": proposal.category_name,
                "funding": proposal.funding_name,
                "amount": proposal.display_amount,
                "frequency": proposal.recurrence.describe(),
                "confidence": proposal.confidence,
                "evidence": proposal.evidence.serialize(),
            }
            for proposal in proposals
        ]
        rows = [
            [
                proposal.purpose_name,
                proposal.display_amount.format(),
                proposal.recurrence.describe(),
                f"{proposal.confidence:.0%}",
            ]
            for proposal in proposals
        ]
        details = "\n\n".join(
            f"{proposal.purpose_name}\n" + "\n".join(proposal.evidence.summary_lines())
            for proposal in proposals
        )
        text = table(rows, ["purpose", "amount", "frequency", "confidence"], right={1})
        if details:
            text += "\n\n" + details
        emit(payload, args, text)
        return 0


def cmd_estimate(args: argparse.Namespace) -> int:
    """Manage placeholder flows: recurring estimates that are never posted."""
    if args.action == "suggest":
        return _estimate_suggestions(args)
    with book_api(args, "r" if args.action == "list" else "w") as book:
        estimates = [s for s in book.scheduled_page().schedules if s.placeholder]
        if args.action == "list":
            rows, payload = [], []
            for sched in estimates:
                rows.append(
                    [
                        sched.name,
                        sched.recurrence.describe(),
                        sched.amount().format(),
                        "yes" if sched.enabled else "no",
                    ]
                )
                payload.append(
                    {
                        "name": sched.name,
                        "frequency": sched.recurrence.describe(),
                        "amount": sched.amount(),
                        "enabled": sched.enabled,
                    }
                )
            emit(payload, args, table(rows, ["name", "frequency", "amount", "enabled"], right={2}))
            return 0

        if args.action == "remove":
            match = next((s for s in estimates if s.name == args.name), None)
            if match is None:
                raise CommandError(f"no estimate named {args.name!r}")
            book.delete_schedule(match.handle)
            emit({"removed": args.name}, args, f"Removed estimate {args.name!r}")
            return 0

        account = book.find_account(args.account)
        funding = book.find_account(args.funded_from)
        recurrence = Recurrence(
            period=args.every,
            interval=args.interval,
            start=parse_date(args.start) or date.today().replace(day=1),
        )
        value = Money(args.amount)
        if not value:
            raise CommandError("--amount must not be zero")
        # The amount goes to the account as given (a negative one for income), and
        # the funding account takes the other side.
        saved = book.save_fixed_schedule(
            SaveFixedSchedule(
                FixedScheduleInput(
                    name=args.name,
                    recurrence=recurrence,
                    category=account.handle,
                    funding=funding.handle,
                    amount=abs(value),
                    category_ledger_direction=1 if value > 0 else -1,
                    placeholder=True,
                )
            )
        )
        emit(
            {"name": args.name, "handle": saved.handle},
            args,
            f"Added estimate {args.name!r}: {value.format()} on "
            f"{account.full_name} {recurrence.describe()}",
        )
        return 0


def cmd_jars(args: argparse.Namespace) -> int:
    from ..gen.engine.budget_jars import jar_kind_label

    start = parse_date(args.start)
    end = parse_date(args.end)
    if start is None or end is None or end < start:
        raise CommandError("--start and --end are dates, and --end is not before --start")
    with book_api(args, "r") as book:
        view = book.budget_jars(
            start,
            end,
            activity.ReportingPeriod(args.period),
            parse_date(args.as_of) or date.today(),
        )
        report = view.report
        labels = view.labels()

        def period_data(item) -> dict[str, object]:
            return {
                "start": item.start,
                "end": item.end,
                "label": item.label,
                "filled": item.filled,
                "planned": item.planned,
                "actual": item.actual,
                "variance": item.variance,
                "lapsed": item.lapsed,
                "level": item.level,
            }

        payload = {
            "start": report.start,
            "end": report.end,
            "period": report.period.value,
            "accounts": [
                {
                    "account": bundle.account,
                    "name": bundle.account_name,
                    "currency": labels.get(bundle.currency, ""),
                    "periods": [period_data(item) for item in bundle.periods],
                    "jars": [
                        {
                            "key": jar.key,
                            "kind": jar.kind,
                            "name": jar.name,
                            "opening": jar.opening,
                            "periods": [period_data(item) for item in jar.periods],
                            "events": [
                                {
                                    "when": event.when,
                                    "kind": event.kind,
                                    "amount": event.amount,
                                    "transaction": event.transaction,
                                }
                                for event in jar.events
                            ],
                        }
                        for jar in bundle.jars
                    ],
                }
                for bundle in report.accounts
            ],
            "totals": [
                {
                    "currency": labels.get(total.currency, ""),
                    "periods": [period_data(item) for item in total.periods],
                }
                for total in report.totals
            ],
            "problems": list(report.problems),
        }
        lines: list[str] = []
        for bundle in report.accounts:
            label = labels.get(bundle.currency, "")
            lines.append(f"{bundle.account_name}{f' ({label})' if label else ''}")
            lines.append(
                "  jars: "
                + "; ".join(
                    f"{jar.name} ({jar_kind_label(jar.kind).lower()})" for jar in bundle.jars
                )
            )
            lines.append(
                table(
                    [
                        [
                            item.label,
                            item.filled.format(),
                            item.planned.format(),
                            item.actual.format(),
                            item.variance.format(parens_negative=True),
                            item.lapsed.format(parens_negative=True),
                            item.level.format(parens_negative=True),
                        ]
                        for item in bundle.periods
                    ],
                    ["period", "filled", "planned", "actual", "variance", "lapsed", "level"],
                    right={1, 2, 3, 4, 5, 6},
                )
            )
        lines.extend(f"note: {problem}" for problem in report.problems)
        emit(payload, args, "\n".join(lines) if lines else "No jars in this range.")
        return 0


def _add_share_price_options(command) -> None:
    command.add_argument(
        "--share-price",
        action="append",
        metavar="SYMBOL=PRICE",
        help="price of a share a schedule buys or sells, when no quote has one (repeatable)",
    )
    command.add_argument(
        "--shares",
        action="append",
        metavar="SYMBOL=COUNT",
        help="shares a schedule buys or sells, when no quote prices them (repeatable)",
    )
    command.add_argument(
        "--no-online-quotes",
        dest="online_quotes",
        action="store_false",
        help="do not fetch online quotes for shares before entering",
    )


def register(add: AddCommand) -> None:
    """Add the plan subcommands."""
    sched = add("scheduled", "List due scheduled transactions, or post them")
    sched.add_argument("--as-of")
    sched.add_argument("--days", type=int, default=30, help="look this far ahead")
    sched.add_argument("--post", action="store_true", help="write due occurrences")
    sched.add_argument("--all", action="store_true", help="with --post, include manual schedules")
    sched.add_argument(
        "--no-online-quotes",
        dest="online_quotes",
        action="store_false",
        help="with --post, do not fetch online quotes for shares first",
    )
    add_server_options(sched)
    sched.set_defaults(func=cmd_scheduled)

    next_cmd = add(
        "scheduled-next",
        "Show, enter, or skip a schedule's next occurrence, even before it is due",
    )
    next_cmd.add_argument("schedule", help="schedule id prefix or exact name")
    choice = next_cmd.add_mutually_exclusive_group()
    choice.add_argument("--enter", action="store_true", help="post the next occurrence now")
    choice.add_argument(
        "--skip", action="store_true", help="mark the next occurrence done without posting"
    )
    next_cmd.add_argument("--as-of", help="today, for testing (YYYY-MM-DD)")
    _add_share_price_options(next_cmd)
    add_server_options(next_cmd)
    next_cmd.set_defaults(func=cmd_scheduled_next)

    advance_cmd = add(
        "scheduled-advance",
        "Show or set how many days ahead a schedule is entered or reminded",
    )
    advance_cmd.add_argument("schedule", help="schedule id prefix or exact name")
    advance_cmd.add_argument(
        "--enter-days", type=int, help="days before its date an occurrence is entered"
    )
    advance_cmd.add_argument(
        "--remind-days", type=int, help="days before its date an occurrence is a reminder"
    )
    add_server_options(advance_cmd)
    advance_cmd.set_defaults(func=cmd_scheduled_advance)

    review_cmd = add(
        "review",
        "List unresolved transactions, the planned items Review offers for each, and why",
    )
    review_cmd.add_argument("--transaction", metavar="ID", help="only this transaction (id prefix)")
    add_server_options(review_cmd)
    review_cmd.set_defaults(func=cmd_review)

    due_review = add(
        "due-review",
        "List or decide due and missed scheduled transactions, grouped by schedule",
    )
    due_review.add_argument("--as-of", help="review date (YYYY-MM-DD, default today)")
    due_review.add_argument(
        "--post",
        action="append",
        metavar="SCHEDULE[@DATE]",
        help="post a schedule's due dates, or one date (repeatable; id prefix or exact name)",
    )
    due_review.add_argument(
        "--skip",
        action="append",
        metavar="SCHEDULE[@DATE]",
        help="mark a schedule's due dates, or one date, as done without posting (repeatable)",
    )
    due_review.add_argument(
        "--post-all", action="store_true", help="post every due date (not reminders)"
    )
    due_review.add_argument(
        "--skip-all",
        action="store_true",
        help="mark every due date (not reminders) as done without posting",
    )
    _add_share_price_options(due_review)
    add_server_options(due_review)
    due_review.set_defaults(func=cmd_due_review)

    plan_unresolved = add(
        "plan-unresolved",
        "List scheduled expectations that have not been resolved to actuals",
    )
    plan_unresolved.add_argument("--start", required=True, help="first date (YYYY-MM-DD)")
    plan_unresolved.add_argument("--end", required=True, help="last date (YYYY-MM-DD)")
    add_server_options(plan_unresolved)
    plan_unresolved.set_defaults(func=cmd_plan_unresolved)

    plan_matches = add(
        "plan-matches",
        "Show planned occurrences that could match an actual transaction",
    )
    plan_matches.add_argument("transaction", help="transaction handle, or a unique prefix")
    plan_matches.add_argument("--window-days", type=int, default=7)
    add_server_options(plan_matches)
    plan_matches.set_defaults(func=cmd_plan_matches)

    plan_resolve = add("plan-resolve", "Match an actual transaction to a planned occurrence")
    plan_resolve.add_argument("transaction", help="transaction handle, or a unique prefix")
    plan_resolve.add_argument("occurrence", help="stable scheduled occurrence key")
    add_server_options(plan_resolve)
    plan_resolve.set_defaults(func=cmd_plan_resolve)

    plan_allocate = add("plan-allocate", "Count part or all of a split toward a planned occurrence")
    plan_allocate.add_argument(
        "transaction",
        help="transaction handle or prefix, then :INDEX or :SPLIT for one of several plan legs",
    )
    plan_allocate.add_argument("occurrence", help="stable scheduled occurrence key")
    plan_allocate.add_argument(
        "--amount", help="how much of the split counts (default: all that is left; 0 removes)"
    )
    add_server_options(plan_allocate)
    plan_allocate.set_defaults(func=cmd_plan_allocate)

    plan_close = add("plan-close", "Close a planned occurrence early, so what remains lapses")
    plan_close.add_argument("occurrence", help="stable scheduled occurrence key")
    plan_close.add_argument("--reopen", action="store_true", help="reopen it instead")
    add_server_options(plan_close)
    plan_close.set_defaults(func=cmd_plan_close)

    plan_reject = add("plan-reject", "Reject a planned occurrence as a match candidate")
    plan_reject.add_argument("transaction", help="transaction handle, or a unique prefix")
    plan_reject.add_argument("occurrence", help="stable scheduled occurrence key")
    add_server_options(plan_reject)
    plan_reject.set_defaults(func=cmd_plan_reject)

    plan_unexpected = add(
        "plan-unexpected",
        "Declare an actual transaction to be intentionally outside the plan",
    )
    plan_unexpected.add_argument("transaction", help="transaction handle, or a unique prefix")
    add_server_options(plan_unexpected)
    plan_unexpected.set_defaults(func=cmd_plan_unexpected)

    jars = add("jars", "Budget jars: each schedule and goal filled from income and drawn")
    jars.add_argument("--start", required=True, help="first date (YYYY-MM-DD)")
    jars.add_argument("--end", required=True, help="last date (YYYY-MM-DD)")
    jars.add_argument(
        "--period",
        default="month",
        choices=[period.value for period in activity.ReportingPeriod],
        help="display grouping; does not change event dates",
    )
    jars.add_argument("--as-of", help="income after this date is expected (default today)")
    add_server_options(jars)
    jars.set_defaults(func=cmd_jars)

    activity_cmd = add(
        "activity",
        "Event-driven plan versus actuals, grouped only for display",
    )
    activity_cmd.add_argument("--start", required=True, help="first date (YYYY-MM-DD)")
    activity_cmd.add_argument("--end", required=True, help="last date (YYYY-MM-DD)")
    activity_cmd.add_argument(
        "--period",
        default="month",
        choices=[period.value for period in activity.ReportingPeriod],
        help="display grouping; does not change event dates",
    )
    activity_cmd.add_argument(
        "--as-of",
        help=(
            "date that ends the 'through' totals and whose exchange-rate quotes "
            "convert foreign amounts (default today)"
        ),
    )
    add_server_options(activity_cmd)
    activity_cmd.set_defaults(func=cmd_activity)

    estimate = add("estimate", "Recurring Plan estimates that never post")
    estimate.add_argument("action", choices=["list", "suggest", "add", "remove"])
    estimate.add_argument("--name")
    estimate.add_argument("--account", help="the income or expense account")
    estimate.add_argument(
        "--funded-from",
        help="the account the money moves to or from",
    )
    estimate.add_argument(
        "--amount",
        help="what each occurrence posts to --account: positive for an expense, "
        "negative for income",
    )
    estimate.add_argument(
        "--every",
        default="month",
        choices=[p.value for p in PeriodType],
        help="how often the flow recurs",
    )
    estimate.add_argument("--interval", type=int, default=1)
    estimate.add_argument("--start")
    estimate.add_argument("--as-of", help="analysis date for suggest (YYYY-MM-DD)")
    estimate.add_argument("--months", type=int, default=12, help="completed history months")
    estimate.add_argument("--min-active-months", type=int, default=3)
    add_server_options(estimate)  # used by suggest
    estimate.set_defaults(func=cmd_estimate)

    payroll_cmd = add(
        "payroll",
        "Show paychecks, manage payroll templates, create paychecks, and change pay",
    )
    payroll_cmd.add_argument("--show", metavar="SCHEDULE", help="one paycheck's lines")
    payroll_cmd.add_argument("--as-of", help="read paychecks as of this date (YYYY-MM-DD)")
    payroll_cmd.add_argument(
        "--save-template", metavar="NAME", help="save a template from --from-schedule"
    )
    payroll_cmd.add_argument("--from-schedule", metavar="SCHEDULE")
    payroll_cmd.add_argument("--delete-template", metavar="NAME")
    payroll_cmd.add_argument(
        "--create", metavar="NAME", help="add a paycheck schedule from --template"
    )
    payroll_cmd.add_argument("--template", metavar="NAME")
    payroll_cmd.add_argument("--gross", help="gross pay (default: the template's)")
    payroll_cmd.add_argument("--start", help="first paycheck, or the pay change's date")
    payroll_cmd.add_argument(
        "--every", default="week", choices=[p.value for p in PeriodType], help="pay period"
    )
    payroll_cmd.add_argument(
        "--interval", type=int, default=2, help="periods between paychecks (default: 2)"
    )
    payroll_cmd.add_argument("--auto-create", action="store_true")
    payroll_cmd.add_argument(
        "--pay-change", metavar="SCHEDULE", help="change gross pay from --start"
    )
    payroll_cmd.add_argument(
        "--scale",
        action="append",
        metavar="ACCOUNT",
        help="a line that scales with gross (repeatable; default: the taxes)",
    )
    payroll_cmd.add_argument(
        "--set", action="append", metavar="ACCOUNT=AMOUNT", help="a line's new amount"
    )
    payroll_cmd.add_argument(
        "--preview", action="store_true", help="show the pay change without saving it"
    )
    add_server_options(payroll_cmd)
    payroll_cmd.set_defaults(func=cmd_payroll)
