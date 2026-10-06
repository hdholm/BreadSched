"""Commands over FSA claims, receivables and shared costs, and savings goals."""

from __future__ import annotations

import argparse
from datetime import date
from typing import Any

from ..gen.db.sqlite import DbSQLite
from ..gen.engine import activity
from ..gen.lib import (
    FsaClaim,
    FsaClaimEvent,
    Money,
    Receivable,
    Split,
    Transaction,
)
from ..gen.services import (
    CloseClaim,
    ReopenClaim,
    SetReimbursementOverride,
    close_claim,
    reopen_claim,
    set_reimbursement_override,
)
from ..gen.services.receivables import (
    RecordWriteOff,
    SaveReceivable,
    accept_reimbursements,
    attach_expense_split,
    attach_reimbursement_split,
    clear_dispute,
    delete_receivable,
    detach_split,
    list_receivables,
    mark_disputed,
    record_write_off,
    reimbursement_proposals,
    save_receivable,
    shared_costs,
)
from ..presentation import (
    goal_status_text,
    plan_reimbursable_text,
    service_error_message,
    shared_cost_text,
)
from .common import (
    AddCommand,
    CommandError,
    emit,
    find_transaction,
    open_book,
    parse_date,
    resolve_account,
    table,
)


def _find_receivable(db: DbSQLite, reference: str) -> Receivable:
    """Locate a receivable by handle, or by a unique prefix of one."""
    exact = db.get_receivable(reference)
    if exact is not None:
        return exact
    matches = [item for item in db.iter_receivables() if item.handle.startswith(reference)]
    if not matches:
        raise CommandError(f"no receivable matches {reference!r}")
    if len(matches) > 1:
        raise CommandError(f"{reference!r} matches {len(matches)} receivables; use more characters")
    return matches[0]


def _receivable_split(transaction: Transaction, index: int) -> Split:
    if not 1 <= index <= len(transaction.splits):
        raise CommandError(f"transaction has {len(transaction.splits)} split(s); no split {index}")
    return transaction.splits[index - 1]


def _resolve_goal(db: DbSQLite, reference: str) -> str:
    """A savings goal by exact handle, unique handle prefix, or exact name."""
    goals = list(db.iter_savings_goals())
    matches = [goal for goal in goals if goal.handle == reference]
    matches = matches or [goal for goal in goals if goal.handle.startswith(reference)]
    matches = matches or [goal for goal in goals if goal.name.casefold() == reference.casefold()]
    if len(matches) != 1:
        raise CommandError(
            f"no savings goal matches {reference!r}"
            if not matches
            else f"{reference!r} matches several savings goals"
        )
    return matches[0].handle


def cmd_goals(args: argparse.Namespace) -> int:
    """List savings goals and what each has set aside, or add, fund, or close one."""
    from ..gen.services import (
        AllocateToGoal,
        SaveSavingsGoal,
        allocate_to_goal,
        close_savings_goal,
        delete_savings_goal,
        query_savings_goals,
        reopen_savings_goal,
        save_savings_goal,
        set_goal_override,
    )
    from ..gen.services.savings_goals import SetGoalOverride

    writes = (args.add, args.allocate, args.close, args.reopen, args.delete, args.override)
    db = open_book(args.book, "w" if any(writes) else "r")
    try:
        on = parse_date(args.on) or date.today()

        def check(result: Any) -> Any:
            if result.value is None:
                raise CommandError(service_error_message(result.errors[0]))
            return result.value

        if args.add:
            if not (args.account and args.target and args.by):
                raise CommandError("--account, --target, and --by are required with --add")
            by = parse_date(args.by)
            assert by is not None
            goal = check(
                save_savings_goal(
                    db,
                    SaveSavingsGoal(
                        name=args.add,
                        account=resolve_account(db, args.account).handle,
                        target_amount=Money(args.target),
                        target_date=by,
                        start_date=parse_date(args.start) or date.today(),
                        description=args.description or "",
                    ),
                )
            )
            emit({"handle": goal.handle}, args, f"Added savings goal {goal.name} ({goal.handle})")
            return 0
        if args.override:
            if not args.scenario:
                raise CommandError("--scenario is required with --override")
            scenario = db.get_scenario_by_name(args.scenario)
            if scenario is None:
                raise CommandError(f"no scenario named {args.scenario!r}")
            handle = _resolve_goal(db, args.override)
            changed = check(
                set_goal_override(
                    db,
                    SetGoalOverride(
                        scenario.handle,
                        handle,
                        target_amount=Money(args.target) if args.target else None,
                        target_date=parse_date(args.by),
                        excluded=args.leave_out,
                        purchase_on=parse_date(args.buy_on),
                        purchase_account=(
                            resolve_account(db, args.buy_into).handle if args.buy_into else None
                        ),
                    ),
                )
            )
            override = changed.goal_overrides.get(handle)
            if override is None:
                text = f"{scenario.name} now follows the goal unchanged"
            elif override.excluded:
                text = f"{scenario.name} leaves the goal out"
            else:
                parts = [
                    f"target {override.target_amount.format()}" if override.target_amount else "",
                    f"by {override.target_date.isoformat()}" if override.target_date else "",
                    (
                        f"bought on {override.purchase_on.isoformat()} into "
                        f"{db.full_name(override.purchase_account)}"
                        if override.purchase_on and override.purchase_account
                        else ""
                    ),
                ]
                text = f"{scenario.name} changes the goal: " + ", ".join(p for p in parts if p)
            emit(
                {
                    "scenario": scenario.handle,
                    "goal": handle,
                    "override": override.serialize() if override is not None else None,
                },
                args,
                text,
            )
            return 0
        if args.allocate:
            if not args.amount:
                raise CommandError("--amount is required with --allocate")
            goal = check(
                allocate_to_goal(
                    db,
                    AllocateToGoal(
                        _resolve_goal(db, args.allocate), Money(args.amount), on, args.memo or ""
                    ),
                )
            )
            emit({"handle": goal.handle}, args, f"Allocated {args.amount} to {goal.name}")
            return 0
        if args.close:
            goal = check(close_savings_goal(db, _resolve_goal(db, args.close), on))
            emit({"handle": goal.handle}, args, f"Closed savings goal {goal.name}")
            return 0
        if args.reopen:
            goal = check(reopen_savings_goal(db, _resolve_goal(db, args.reopen)))
            emit({"handle": goal.handle}, args, f"Reopened savings goal {goal.name}")
            return 0
        if args.delete:
            check(delete_savings_goal(db, _resolve_goal(db, args.delete)))
            emit({"deleted": True}, args, "Deleted savings goal")
            return 0
        report = check(
            query_savings_goals(db, parse_date(args.as_of) or date.today(), include_closed=args.all)
        )
        rows = [
            [
                item.goal.name,
                item.account_name,
                item.goal.target_date.isoformat(),
                item.target.format(),
                item.set_aside.format(),
                item.remaining.format(),
                goal_status_text(item),
            ]
            for item in report.goals
        ]
        text = (
            table(
                rows,
                ["goal", "account", "by", "target", "set aside", "remaining", "status"],
                right={3, 4, 5},
            )
            + f"\nSet aside for goals: {report.set_aside.format()}; "
            f"held from spendable cash: {report.held.format()}"
            if rows
            else "No savings goals."
        )
        emit(
            {
                "as_of": report.as_of,
                "set_aside": report.set_aside,
                "held": report.held,
                "goals": [
                    {
                        "handle": item.goal.handle,
                        "name": item.goal.name,
                        "account": item.goal.account,
                        "account_name": item.account_name,
                        "start_date": item.goal.start_date,
                        "target_date": item.goal.target_date,
                        "target": item.target,
                        "allocated": item.allocated,
                        "from_income": item.from_income,
                        "set_aside": item.set_aside,
                        "remaining": item.remaining,
                        "status": item.status,
                        "basis": item.basis,
                    }
                    for item in report.goals
                ],
            },
            args,
            text,
        )
        return 0
    finally:
        db.close()


def _find_claim(db: DbSQLite, reference: str) -> FsaClaim:
    """Locate an FSA claim by handle, or by a unique prefix of one."""
    exact = db.get_fsa_claim(reference)
    if exact is not None:
        return exact
    matches = [item for item in db.iter_fsa_claims() if item.handle.startswith(reference)]
    if not matches:
        raise CommandError(f"no FSA claim matches {reference!r}")
    if len(matches) > 1:
        raise CommandError(f"{reference!r} matches {len(matches)} claims; use more characters")
    return matches[0]


def _claim_event_text(event: FsaClaimEvent) -> str:
    def amount(value: Money | None) -> str:
        return value.format() if value is not None else "none"

    if event.kind == FsaClaimEvent.EOB_CHANGED:
        text = f"EOB changed from {amount(event.previous)} to {amount(event.current)}"
    else:
        text = "Closed" if event.kind == FsaClaimEvent.CLOSED else "Reopened"
    return f"{event.on.isoformat()} {text}" + (f": {event.note}" if event.note else "")


def _claim_link_proposals(db, args: argparse.Namespace) -> int:
    """FSA statement lines that clearly belong on one claim; link them all if asked."""
    from ..gen.services import accept_claim_links, claim_link_proposals
    from ..presentation import claim_role_label

    account = resolve_account(db, args.account).handle if args.account else None
    proposals = claim_link_proposals(db, account=account).value or ()
    payload: dict[str, object] = {
        "proposals": [
            {
                "claim": item.claim,
                "claim_label": item.claim_label,
                "transaction": item.transaction,
                "split": item.split,
                "role": item.role,
                "date": item.when,
                "description": item.description,
                "amount": item.amount,
                "reason": item.reason,
            }
            for item in proposals
        ]
    }
    if args.link_proposals:
        chosen = tuple((item.claim, item.transaction, item.split) for item in proposals)
        accepted = accept_claim_links(db, chosen).value
        assert accepted is not None
        payload["linked"] = accepted.linked
        payload["unchanged"] = accepted.unchanged
        noun = "link" if accepted.linked == 1 else "links"
        emit(payload, args, f"Linked {accepted.linked} claim {noun}.")
        return 0
    if not proposals:
        emit(payload, args, "No proposed claim links.")
        return 0
    emit(
        payload,
        args,
        table(
            [
                [
                    item.when.isoformat(),
                    item.description,
                    item.amount.format(),
                    item.claim_label,
                    claim_role_label(item.role),
                    item.reason,
                ]
                for item in proposals
            ],
            ["Date", "Transaction", "Amount", "Claim", "As", "Why"],
            right={2},
        ),
    )
    return 0


def _fsa_years(db, args: argparse.Namespace) -> int:
    """Open and recently closed FSA benefit years, with how each was used."""
    from ..gen.engine import fsa
    from ..presentation import fsa_account_text, fsa_usage_text

    statuses = fsa.dashboard_statuses(db, as_of=parse_date(args.as_of) or date.today())
    if args.account:
        handle = resolve_account(db, args.account).handle
        statuses = [status for status in statuses if status.account.handle == handle]
    payload = [
        {
            "account": db.full_name(status.account),
            "dependent_care": status.dependent_care,
            "start": status.year.start,
            "through": status.year.through,
            "runout_through": status.year.runout_through,
            "phase": status.phase,
            "election": status.year.election,
            "funded": status.funded,
            "direct_payments": status.direct_payments,
            "reimbursements": status.reimbursements,
            "provider_refunds": status.provider_refunds,
            "repaid": status.repaid,
            "used": status.used,
            "remaining": status.remaining,
            "overage": status.overage,
            "forfeited": status.forfeited,
        }
        for status in statuses
    ]
    if not statuses:
        emit({"years": payload}, args, "No open FSA benefit years.")
        return 0
    rows = [
        [
            fsa_account_text(db.full_name(status.account), status.dependent_care),
            status.label,
            status.phase,
            status.year.election.format(),
            status.funded.format(),
            status.used.format(),
            status.remaining.format(),
            fsa_usage_text(status),
        ]
        for status in statuses
    ]
    emit(
        {"years": payload},
        args,
        table(
            rows,
            [
                "FSA account",
                "Plan year",
                "Phase",
                "Election",
                "Funded",
                "Used",
                "Remaining",
                "How used",
            ],
            right={3, 4, 5, 6},
        ),
    )
    return 0


def cmd_claims(args: argparse.Namespace) -> int:
    """FSA claims grouped by status, account, funding year, or provider."""
    from ..gen.engine.fsa_claim_report import claim_report
    from ..gen.engine.fsa_claims import FsaClaimStatus

    changing = bool(args.close or args.reopen)
    if args.close and args.reopen:
        raise CommandError("use --close or --reopen, not both")
    db = open_book(args.book, "w" if changing or args.link_proposals else "r")
    try:
        if changing:
            claim = _find_claim(db, args.close or args.reopen)
            on = parse_date(args.on) or date.today()
            if args.close:
                result = close_claim(db, CloseClaim(claim.handle, on, args.reason or ""))
            else:
                result = reopen_claim(db, ReopenClaim(claim.handle, on, args.note or ""))
            if result.value is None:
                raise CommandError(service_error_message(result.errors[0]))
            name = claim.provider or claim.description or "FSA claim"
            emit(
                {"handle": claim.handle},
                args,
                f"{'Closed' if args.close else 'Reopened'} the {name} claim "
                f"of {claim.service_date.isoformat()}",
            )
            return 0
        if args.years:
            return _fsa_years(db, args)
        if args.proposals or args.link_proposals:
            return _claim_link_proposals(db, args)
        if args.history:
            claim = _find_claim(db, args.history)
            emit(
                {"handle": claim.handle, "events": [event.serialize() for event in claim.events]},
                args,
                "\n".join(_claim_event_text(event) for event in claim.events)
                or "No EOB changes, closings, or reopenings.",
            )
            return 0
        status = None
        if args.status:
            try:
                status = FsaClaimStatus(args.status)
            except ValueError as exc:
                choices = ", ".join(item.value for item in FsaClaimStatus)
                raise CommandError(f"--status must be one of {choices}") from exc
        report = claim_report(
            db,
            as_of=parse_date(args.as_of) or date.today(),
            by=args.by,
            account=resolve_account(db, args.account).handle if args.account else None,
            funding_year=parse_date(args.year),
            provider=args.provider,
            status=status,
            attention_only=args.attention,
        )

        def money(value: Money) -> str:
            return value.format(parens_negative=True)

        payload = {
            "as_of": report.as_of,
            "by": report.by,
            "groups": [
                {
                    "label": group.label,
                    "claims": group.claims,
                    "net_paid": group.net_paid,
                    "reimbursable": group.reimbursable,
                    "reimbursed": group.reimbursed,
                    "rejected": group.rejected,
                    "remaining": group.remaining,
                    "attention": group.attention,
                }
                for group in report.groups
            ],
            "claims": [
                {
                    "handle": line.handle,
                    "service_date": line.summary.claim.service_date,
                    "provider": line.summary.claim.provider,
                    "account": line.account,
                    "funding_year": line.funding_year,
                    "status": line.summary.status.value,
                    "net_paid": line.summary.net_paid,
                    "reimbursed": line.summary.reimbursed,
                    "repaid": line.summary.repaid,
                    "over_reimbursed": line.summary.over_reimbursed,
                    "remaining": line.summary.remaining_reimbursable,
                    "forgone": line.summary.forgone,
                    "closed_on": line.summary.claim.closed_on,
                    "deadline": line.deadline,
                    "attention": [
                        {"code": item.code, "text": item.text} for item in line.attention
                    ],
                }
                for line in report.lines
            ],
        }
        if not report.lines:
            emit(payload, args, "No FSA claims match.")
            return 0
        heading = {
            "status": "Status",
            "account": "FSA account",
            "year": "Funding year",
            "provider": "Provider",
        }[report.by]
        groups = table(
            [
                [
                    group.label,
                    group.claims,
                    money(group.net_paid),
                    money(group.reimbursed),
                    money(group.rejected),
                    money(group.remaining),
                    group.attention or "",
                ]
                for group in (*report.groups, report.totals)
            ],
            [heading, "Claims", "Net paid", "Reimbursed", "Rejected", "Remaining", "Attention"],
            right={1, 2, 3, 4, 5, 6},
        )
        attention = [
            f"{line.summary.claim.service_date.isoformat()} "
            f"{line.summary.claim.provider or '(no provider)'}: {item.text}"
            for line in report.needing_attention
            for item in line.attention
        ]
        text = groups
        if attention:
            text += "\n\nNeeds attention:\n" + "\n".join(f"  {entry}" for entry in attention)
        emit(payload, args, text)
        return 0
    finally:
        db.close()


def _receivable_costs(db: DbSQLite, args: argparse.Namespace) -> int:
    """Each expense category's gross and net cost in a date range, as Plan shows it."""
    start, end = (parse_date(value) for value in args.costs)
    if start is None or end is None or end < start:
        raise CommandError("--costs needs a start date and an end date on or after it")
    report = activity.build_category_report(db, start, end)
    rows = report.reimbursable_categories
    payload = [
        {
            "account": row.account,
            "category": row.full_name,
            "gross": sum(row.gross, start=Money(0)).format(),
            "reimbursable": row.reimbursable_total.format(),
            "net": sum(row.actual, start=Money(0)).format(),
        }
        for row in rows
    ]
    text = (
        "\n".join(plan_reimbursable_text(row) for row in rows)
        if rows
        else "No expense category had reimbursements in that range."
    )
    emit(payload, args, text)
    return 0


def _scenario_expectation(db: DbSQLite, args: argparse.Namespace) -> int:
    """Change, in one scenario, what a payer is expected to reimburse and when."""
    if not args.scenario:
        raise CommandError("--scenario NAME is required with --expect")
    scenario = db.get_scenario_by_name(args.scenario)
    if scenario is None:
        raise CommandError(f"no scenario named {args.scenario!r}")
    receivable = _find_receivable(db, args.expect)
    result = set_reimbursement_override(
        db,
        SetReimbursementOverride(
            scenario.handle,
            receivable.handle,
            amount=Money(args.amount) if args.amount else None,
            on=date.fromisoformat(args.on) if args.on else None,
        ),
    )
    if not result.ok or result.value is None:
        raise CommandError(service_error_message(result.errors[0]))
    overrides = result.value.reimbursement_overrides
    change = overrides.get(receivable.handle)
    emit(
        {
            "scenario": result.value.name,
            "reimbursement_overrides": {
                handle: {"amount": item.amount, "on": item.on} for handle, item in overrides.items()
            },
        },
        args,
        f"{result.value.name}: {receivable.payer} "
        + (
            "reimbursement expected as the receivable says"
            if change is None
            else ", ".join(
                part
                for part in (
                    f"pays {change.amount.format()}" if change.amount is not None else "",
                    f"on {change.on.isoformat()}" if change.on is not None else "",
                )
                if part
            )
        ),
    )
    return 0


def cmd_receivables(args: argparse.Namespace) -> int:
    """List reimbursable expenses, add or resolve one, or link ledger splits."""
    read_only = not (
        args.add
        or args.attach_expense
        or args.attach_reimbursement
        or args.detach
        or args.dispute
        or args.clear_dispute
        or args.write_off
        or args.delete
        or args.accept_proposals
        or args.expect
    )
    db = open_book(args.book, "r" if read_only else "w")
    try:
        if args.costs:
            return _receivable_costs(db, args)
        if args.expect:
            return _scenario_expectation(db, args)
        if args.proposals or args.accept_proposals:
            proposals = reimbursement_proposals(db).value or ()
            if args.accept_proposals:
                accepted = accept_reimbursements(
                    db, tuple((p.receivable, p.transaction, p.split) for p in proposals)
                ).value
                assert accepted is not None
                emit(
                    {"linked": accepted.linked, "unchanged": accepted.unchanged},
                    args,
                    f"Linked {accepted.linked} reimbursement(s); "
                    f"{accepted.unchanged} left unchanged",
                )
                return 0
            emit(
                [
                    {
                        "receivable": item.receivable,
                        "payer": item.payer,
                        "transaction": item.transaction,
                        "split": item.split,
                        "date": item.when,
                        "description": item.description,
                        "amount": item.amount.format(),
                        "remaining_after": item.remaining_after.format(),
                        "reason": item.reason,
                    }
                    for item in proposals
                ],
                args,
                table(
                    [
                        [
                            item.when.isoformat(),
                            item.description,
                            item.payer,
                            item.amount.format(),
                            item.remaining_after.format(parens_negative=True),
                            item.reason,
                        ]
                        for item in proposals
                    ],
                    ["date", "description", "payer", "amount", "remaining after", "why"],
                    right={3, 4},
                )
                if proposals
                else "No unlinked credits clearly reimburse an open receivable.",
            )
            return 0
        if args.add:
            if not args.incurred:
                raise CommandError("--incurred DATE is required with --add")
            saved = save_receivable(
                db,
                SaveReceivable(
                    incurred_date=date.fromisoformat(args.incurred),
                    payer=args.add,
                    description=args.description or "",
                    expected_amount=Money(args.expected) if args.expected else None,
                    expected_cash_date=(
                        date.fromisoformat(args.expected_cash_date)
                        if args.expected_cash_date
                        else None
                    ),
                    account=resolve_account(db, args.account).handle if args.account else None,
                ),
            )
            if saved.value is None:
                raise CommandError(service_error_message(saved.errors[0]))
            receivable = saved.value
            emit(
                {"handle": receivable.handle, "payer": receivable.payer},
                args,
                f"Saved receivable for {receivable.payer}",
            )
            return 0
        if args.attach_expense or args.attach_reimbursement:
            receivable = _find_receivable(db, args.attach_expense or args.attach_reimbursement)
            if not args.transaction or not args.split_index:
                raise CommandError("--transaction and --split-index are required")
            transaction = find_transaction(db, args.transaction)
            split = _receivable_split(transaction, args.split_index)
            linker = attach_expense_split if args.attach_expense else attach_reimbursement_split
            result = linker(db, receivable.handle, transaction.handle, split.handle)
            if result.value is None:
                raise CommandError(service_error_message(result.errors[0]))
            emit(
                {"handle": receivable.handle},
                args,
                f"Linked {'expense' if args.attach_expense else 'reimbursement'} split "
                f"to receivable for {receivable.payer}",
            )
            return 0
        if args.detach:
            receivable = _find_receivable(db, args.detach)
            if not args.transaction or not args.split_index:
                raise CommandError("--transaction and --split-index are required")
            transaction = find_transaction(db, args.transaction)
            split = _receivable_split(transaction, args.split_index)
            result = detach_split(db, receivable.handle, transaction.handle, split.handle)
            if result.value is None:
                raise CommandError(service_error_message(result.errors[0]))
            emit({"handle": receivable.handle}, args, "Unlinked split from receivable")
            return 0
        if args.dispute:
            receivable = _find_receivable(db, args.dispute)
            if not args.on:
                raise CommandError("--on DATE is required with --dispute")
            result = mark_disputed(
                db, receivable.handle, date.fromisoformat(args.on), args.note or ""
            )
            if result.value is None:
                raise CommandError(service_error_message(result.errors[0]))
            emit({"handle": receivable.handle}, args, f"Disputed receivable for {receivable.payer}")
            return 0
        if args.clear_dispute:
            receivable = _find_receivable(db, args.clear_dispute)
            result = clear_dispute(db, receivable.handle)
            if result.value is None:
                raise CommandError(service_error_message(result.errors[0]))
            emit({"handle": receivable.handle}, args, "Cleared the dispute")
            return 0
        if args.write_off:
            receivable = _find_receivable(db, args.write_off)
            if not args.amount or not args.on:
                raise CommandError("--amount and --on DATE are required with --write-off")
            result = record_write_off(
                db,
                RecordWriteOff(
                    receivable=receivable.handle,
                    amount=Money(args.amount),
                    written_off_on=date.fromisoformat(args.on),
                    reason=args.reason or "",
                ),
            )
            if result.value is None:
                raise CommandError(service_error_message(result.errors[0]))
            emit(
                {"handle": receivable.handle},
                args,
                f"Wrote off {Money(args.amount).format()} for {receivable.payer}",
            )
            return 0
        if args.delete:
            receivable = _find_receivable(db, args.delete)
            deleted = delete_receivable(db, receivable.handle)
            if deleted.value is None:
                raise CommandError(service_error_message(deleted.errors[0]))
            emit({"deleted": deleted.value}, args, f"Deleted receivable for {receivable.payer}")
            return 0
        summaries = list_receivables(db).value or ()
        shared: dict[str, tuple] = {}
        for item in summaries:
            found = shared_costs(db, item.receivable.handle)
            if found.value is None:
                raise CommandError(service_error_message(found.errors[0]))
            shared[item.receivable.handle] = found.value
        emit(
            [
                {
                    "handle": item.receivable.handle,
                    "payer": item.receivable.payer,
                    "description": item.receivable.description,
                    "incurred_date": item.receivable.incurred_date,
                    "expense_total": item.expense_total.format(),
                    "reimbursed": item.reimbursed.format(),
                    "written_off": item.written_off.format(),
                    "owed": item.owed.format(),
                    "remaining": item.remaining.format(),
                    "age_days": item.age_days,
                    "status": item.status.value,
                    "account": (
                        db.full_name(item.receivable.account) if item.receivable.account else None
                    ),
                    "fsa_claims": list(item.fsa_claims),
                    "shared_costs": [
                        {
                            "claim": cost.claim,
                            "expense": cost.expense.format(),
                            "payer_share": cost.payer_share.format(),
                            "fsa_share": cost.fsa_share.format(),
                            "your_share": cost.your_share.format(),
                            "waiting_eob": cost.waiting_eob,
                            "needs_review": cost.needs_review,
                            "over_allocated": cost.over_allocated.format(),
                        }
                        for cost in shared[item.receivable.handle]
                    ],
                }
                for item in summaries
            ],
            args,
            table(
                [
                    [
                        item.receivable.incurred_date.isoformat(),
                        item.receivable.payer,
                        item.receivable.description,
                        item.remaining.format(parens_negative=True),
                        item.status.label,
                        item.age_days,
                    ]
                    for item in summaries
                ],
                ["incurred", "payer", "description", "remaining", "status", "age (days)"],
                right={5},
            )
            + "".join(
                f"\nWarning: {item.receivable.payer} {item.receivable.description}".rstrip()
                + " is also claimed from the FSA; check that it is not expected back twice."
                for item in summaries
                if item.fsa_claims
            )
            + "".join(
                f"\n{item.receivable.payer} {item.receivable.description}".rstrip()
                + f" and its FSA claim: {shared_cost_text(cost)}."
                for item in summaries
                for cost in shared[item.receivable.handle]
            )
            if summaries
            else "No receivables yet. Add one with --add PAYER --incurred DATE.",
        )
        return 0
    finally:
        db.close()


def register(add: AddCommand) -> None:
    """Add the benefit subcommands."""
    claims_cmd = add(
        "claims",
        "FSA claims grouped by status, account, funding year, or provider, "
        "with those needing attention",
    )
    claims_cmd.add_argument(
        "--by",
        choices=("status", "account", "year", "provider"),
        default="status",
        help="how to group claims (default status)",
    )
    claims_cmd.add_argument("--account", metavar="ACCOUNT", help="only this FSA account")
    claims_cmd.add_argument(
        "--year", metavar="DATE", help="only this funding year (its start date)"
    )
    claims_cmd.add_argument("--provider", help="only this provider (ignoring case)")
    claims_cmd.add_argument(
        "--status",
        help="only this status (waiting_eob, open, partial, fully_reimbursed, "
        "closed_no_funds, needs_review, over_reimbursed, closed)",
    )
    claims_cmd.add_argument(
        "--attention", action="store_true", help="only claims needing attention"
    )
    claims_cmd.add_argument("--as-of", help="report on this date (default today)")
    claims_cmd.add_argument(
        "--close",
        metavar="CLAIM",
        help="stop pursuing what is left to reimburse on a claim (with --on and --reason)",
    )
    claims_cmd.add_argument(
        "--reopen", metavar="CLAIM", help="pursue a closed claim again (with --on and --note)"
    )
    claims_cmd.add_argument("--on", metavar="DATE", help="date to close or reopen (default today)")
    claims_cmd.add_argument("--reason", help="why the claim is closed")
    claims_cmd.add_argument("--note", help="why the claim is reopened")
    claims_cmd.add_argument(
        "--proposals",
        action="store_true",
        help="list FSA transactions (such as imported statement lines) that clearly "
        "belong on one claim",
    )
    claims_cmd.add_argument(
        "--link-proposals", action="store_true", help="link every proposed claim link"
    )
    claims_cmd.add_argument(
        "--years",
        action="store_true",
        help="list open FSA benefit years: funded, used (paid from the card, reimbursed, "
        "refunded to the card), and remaining",
    )
    claims_cmd.add_argument(
        "--history", metavar="CLAIM", help="list a claim's EOB changes, closings, and reopenings"
    )
    claims_cmd.set_defaults(func=cmd_claims)

    receivables_cmd = add(
        "receivables",
        "List reimbursable expenses, add or resolve one, or link ledger splits",
    )
    receivables_cmd.add_argument(
        "--add", metavar="PAYER", help="create a receivable for this payer"
    )
    receivables_cmd.add_argument("--incurred", metavar="DATE", help="date the expense was incurred")
    receivables_cmd.add_argument(
        "--costs",
        nargs=2,
        metavar=("START", "END"),
        help="each expense category's gross cost, amount reimbursed or expected back, and "
        "net household cost between two dates, as the Plan shows them",
    )
    receivables_cmd.add_argument("--description", help="what the expense was for")
    receivables_cmd.add_argument("--expected", metavar="AMOUNT", help="amount expected back")
    receivables_cmd.add_argument(
        "--expected-cash-date", metavar="DATE", help="date the reimbursement is expected"
    )
    receivables_cmd.add_argument(
        "--account",
        metavar="ACCOUNT",
        help="Receivable account holding what is owed (default: one per currency)",
    )
    receivables_cmd.add_argument(
        "--attach-expense", metavar="RECEIVABLE", help="link the split recording the cost"
    )
    receivables_cmd.add_argument(
        "--attach-reimbursement",
        metavar="RECEIVABLE",
        help="link the split crediting money back",
    )
    receivables_cmd.add_argument(
        "--detach", metavar="RECEIVABLE", help="unlink a split (with --transaction --split-index)"
    )
    receivables_cmd.add_argument(
        "--transaction", metavar="TRANSACTION", help="transaction handle or unique prefix"
    )
    receivables_cmd.add_argument(
        "--split-index", type=int, metavar="N", help="1-based split position within --transaction"
    )
    receivables_cmd.add_argument(
        "--dispute", metavar="RECEIVABLE", help="mark a receivable as disputed (with --on)"
    )
    receivables_cmd.add_argument("--note", help="dispute note")
    receivables_cmd.add_argument(
        "--clear-dispute", metavar="RECEIVABLE", help="withdraw a receivable's dispute"
    )
    receivables_cmd.add_argument(
        "--write-off",
        metavar="RECEIVABLE",
        help="give up on collecting part or all of the balance (with --amount --on)",
    )
    receivables_cmd.add_argument("--amount", help="write-off or scenario amount")
    receivables_cmd.add_argument(
        "--on", metavar="DATE", help="date of the dispute or write-off, or the scenario's date"
    )
    receivables_cmd.add_argument("--reason", help="write-off reason")
    receivables_cmd.add_argument(
        "--proposals",
        action="store_true",
        help="list unlinked credits that clearly reimburse one open receivable",
    )
    receivables_cmd.add_argument(
        "--accept-proposals",
        action="store_true",
        help="link every current reimbursement proposal",
    )
    receivables_cmd.add_argument("--delete", metavar="RECEIVABLE", help="delete a receivable")
    receivables_cmd.add_argument(
        "--expect",
        metavar="RECEIVABLE",
        help="in --scenario, expect --amount back (0 for nothing) on --on DATE; with "
        "neither, the scenario expects what the receivable says",
    )
    receivables_cmd.add_argument("--scenario", metavar="NAME", help="saved scenario for --expect")
    receivables_cmd.set_defaults(func=cmd_receivables)

    goals_cmd = add(
        "goals",
        "List savings goals and what each has set aside, or add, fund, or close one",
    )
    goals_cmd.add_argument("--add", metavar="NAME", help="create a savings goal")
    goals_cmd.add_argument("--account", metavar="ACCOUNT", help="asset account holding the money")
    goals_cmd.add_argument("--target", metavar="AMOUNT", help="amount to have set aside")
    goals_cmd.add_argument("--by", metavar="DATE", help="target date")
    goals_cmd.add_argument(
        "--start", metavar="DATE", help="income from this date sets money aside (default today)"
    )
    goals_cmd.add_argument("--description", help="what the goal is for")
    goals_cmd.add_argument("--allocate", metavar="GOAL", help="set extra money aside (--amount)")
    goals_cmd.add_argument("--amount", help="amount to allocate")
    goals_cmd.add_argument("--memo", help="note for the allocation")
    goals_cmd.add_argument("--close", metavar="GOAL", help="release a goal's earmark (--on)")
    goals_cmd.add_argument("--reopen", metavar="GOAL", help="reopen a closed goal")
    goals_cmd.add_argument("--delete", metavar="GOAL", help="delete a savings goal")
    goals_cmd.add_argument("--on", metavar="DATE", help="date of an allocation or closing")
    goals_cmd.add_argument("--as-of", help="report progress on this date (default today)")
    goals_cmd.add_argument("--all", action="store_true", help="include closed goals")
    goals_cmd.add_argument(
        "--override",
        metavar="GOAL",
        help="change a goal in one scenario (--scenario, with --target/--by, --buy-on with "
        "--buy-into, or --leave-out; none of those clears the change)",
    )
    goals_cmd.add_argument("--scenario", metavar="NAME", help="scenario for --override")
    goals_cmd.add_argument(
        "--leave-out", action="store_true", help="with --override: leave the goal out"
    )
    goals_cmd.add_argument(
        "--buy-on",
        metavar="DATE",
        help="with --override: the scenario spends the target on this date (on or after the "
        "target date)",
    )
    goals_cmd.add_argument(
        "--buy-into",
        metavar="ACCOUNT",
        help="with --buy-on: the expense or asset account the purchase goes to",
    )
    goals_cmd.set_defaults(func=cmd_goals)
