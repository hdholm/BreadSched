"""Commands over projections, scenarios, net worth history, and the dashboard."""

from __future__ import annotations

import argparse
from datetime import date
from decimal import Decimal, InvalidOperation

from ..gen.db.sqlite import DbSQLite
from ..gen.engine import (
    activity,
    projection,
)
from ..gen.engine import (
    dashboard as dashboard_engine,
)
from ..gen.engine.projection_bridge import month_bridges, projection_bridges
from ..gen.lib import (
    Assumptions,
    Drawdown,
    Money,
    Scenario,
)
from ..gen.services import (
    DeleteScenario,
    SaveDrawdown,
    SaveScenario,
    SaveScenarioAssumptions,
    delete_scenario,
    remove_drawdown,
    save_drawdown,
    save_scenario,
    save_scenario_assumptions,
)
from ..presentation import (
    goal_milestone_text,
    projection_notes,
    reimbursement_outlook_text,
    service_error_message,
)
from .common import (
    AddCommand,
    CommandError,
    emit,
    open_book,
    parse_date,
    resolve_account,
    table,
)


def _due_cell(item: object, as_of: date) -> str:
    """A due date, or a grouped schedule's missed range and count."""
    if isinstance(item, dashboard_engine.MissedGroup):
        return item.missed_label(as_of)
    assert isinstance(item, dashboard_engine.BillRow)
    return item.next_due.isoformat()


def _missed_json(group: dashboard_engine.MissedGroup) -> dict[str, object]:
    return {
        "name": group.name,
        "schedule": group.schedule.handle if group.schedule is not None else None,
        "frequency": group.frequency,
        "missed": group.count,
        "first_due": group.next_due,
        "last_due": group.last_due,
        "total": group.amount,
        "occurrences": [{"date": when, "amount": amount} for when, amount in group.occurrences],
    }


def cmd_net_worth(args: argparse.Namespace) -> int:
    """Show market-valued net worth at each period end through the as-of date."""
    from dataclasses import asdict

    from ..gen.services import query_net_worth_history

    db = open_book(args.book, "r")
    try:
        start = parse_date(args.start)
        end = parse_date(args.end)
        if start is None or end is None:
            raise CommandError("net-worth requires --start and --end")
        result = query_net_worth_history(db, start, end, args.period, parse_date(args.as_of))
        if result.value is None:
            raise CommandError(service_error_message(result.errors[0]))
        history = result.value

        def money(value: Money | None) -> str:
            return "—" if value is None else value.format(parens_negative=True)

        rows = [
            [
                point.label,
                point.valued_on.isoformat(),
                money(point.assets),
                money(point.debts),
                money(point.net_worth),
                money(point.change),
                "; ".join(
                    part
                    for part in (
                        "to date" if point.partial else "",
                        f"missing quote: {', '.join(point.missing)}" if point.missing else "",
                    )
                    if part
                )
                or "—",
            ]
            for point in history.points
        ]
        text = table(
            rows,
            ["period", "valued on", "assets", "debts", "net worth", "change", "note"],
            right={2, 3, 4, 5},
        )
        # Each withheld point's evidence and corrective action (#236).
        for point in history.points:
            if not point.completeness.complete:
                text += f"\n{point.label} withheld: " + "; ".join(point.completeness.detail())
        emit(asdict(history), args, text)
        return 0
    finally:
        db.close()


def cmd_net_worth_change(args: argparse.Namespace) -> int:
    """List the postings behind a net worth change and the market movement beside them."""
    from dataclasses import asdict

    from ..gen.services import query_net_worth_change
    from ..plugins.export.csv_export import export_net_worth_change

    db = open_book(args.book, "r")
    try:
        start = parse_date(args.start)
        end = parse_date(args.end)
        if start is None or end is None:
            raise CommandError("net-worth-change requires --start and --end")
        result = query_net_worth_change(db, start, end, parse_date(args.as_of))
        if result.value is None:
            raise CommandError(service_error_message(result.errors[0]))
        change = result.value
        if args.csv:
            export_net_worth_change(change, args.csv)

        def money(value: Money | None) -> str:
            return "missing quote" if value is None else value.format(parens_negative=True)

        rows = [
            [
                posting.posted.isoformat(),
                posting.description,
                "; ".join(posting.accounts),
                posting.currency,
                money(posting.effect),
            ]
            for posting in change.postings
        ]
        rows += [
            ["", label, "", "", money(value)]
            for label, value in (
                (f"Opening net worth {change.opening_on.isoformat()}", change.opening),
                ("Postings", change.posted),
                ("Market and exchange-rate changes", change.revaluation),
                (f"Closing net worth {change.closing_on.isoformat()}", change.closing),
                ("Change", change.change),
            )
        ]
        text = table(rows, ["date", "description", "accounts", "currency", "effect"], right={4})
        if change.transfers:
            text += (
                f"\n{change.transfers} transfer(s) between your own accounts left out: "
                "they do not change net worth."
            )
        if change.missing:
            text += f"\nMissing quote: {', '.join(change.missing)}; totals withheld."
            text += "\n" + "\n".join(change.completeness.detail())
        emit(asdict(change), args, text)
        return 0
    finally:
        db.close()


def cmd_project(args: argparse.Namespace) -> int:
    db = open_book(args.book, "r")
    try:
        scenario = _scenario_for(db, args)
        result = projection.project(db, scenario)

        if args.csv:
            from ..plugins.export.csv_export import export_projection

            export_projection(result, args.csv)

        if args.monthly:
            rows = [
                [
                    row.label,
                    row.income.format(),
                    row.expense.format(),
                    row.cash_close.format(parens_negative=True),
                    row.holdings.format(),
                    row.net_worth.format(parens_negative=True),
                ]
                for row in result.rows
            ]
            text = table(
                rows,
                ["month", "income", "expense", "cash", "holdings", "net worth"],
                right={1, 2, 3, 4, 5},
            )
        else:
            years = result.year_end("net_worth")
            cash = result.year_end("cash_close")
            income = result.annual("income")
            expense = result.annual("expense")
            start_year = scenario.start.year
            text = table(
                [
                    [
                        f"Year {i + 1} ({start_year + i})",
                        income[i].format(),
                        expense[i].format(),
                        cash[i].format(parens_negative=True),
                        years[i].format(parens_negative=True),
                    ]
                    for i in range(len(years))
                ],
                ["period", "income", "expense", "cash", "net worth"],
                right={1, 2, 3, 4},
            )

        bridges = _projection_bridges(result, args.bridge) if args.bridge else None
        if bridges is not None:
            text += "\n\n" + _bridge_text(bridges[0], bridges[1])

        summary = result.summary()
        if not args.json:
            assumptions = scenario.effective_assumptions()
            print(f"Scenario: {scenario.name}  ({scenario.years} years from {scenario.start})")
            print(
                f"  income growth {assumptions.income_growth:.1%}   "
                f"expense inflation {assumptions.expense_inflation:.1%}   "
                f"investment return {assumptions.investment_return:.1%}"
            )
            print()
        milestones = [
            {**item.as_dict(), "text": goal_milestone_text(item)} for item in result.goal_milestones
        ]
        emit(
            {
                "summary": summary,
                "completeness": result.completeness,
                "rows": [
                    {**r.as_dict(), "completeness": result.month_completeness(index)}
                    for index, r in enumerate(result.rows)
                ],
                "goal_milestones": milestones,
                "reimbursements": [
                    {**item.as_dict(), "text": reimbursement_outlook_text(item)}
                    for item in result.reimbursements
                ],
                **(
                    {
                        "bridge_period": bridges[0],
                        "bridges": [item.as_dict() for item in bridges[1]],
                    }
                    if bridges is not None
                    else {}
                ),
            },
            args,
            text,
        )

        if not args.json:
            shortfall = result.first_shortfall()
            print()
            if shortfall:
                print(
                    f"  Cash runs out in {shortfall.label} "
                    f"({shortfall.cash_close.format(parens_negative=True)})"
                )
            else:
                print(f"  Lowest cash balance: {result.minimum_cash.format()}")
            for note in projection_notes(result):
                print(f"  {note}")
            if not result.completeness.complete:
                print(f"  {result.completeness.label}: the figures above leave out:")
                for line in result.completeness.detail():
                    print(f"    {line}")
            for warning in result.warnings:
                print(f"  warning: {warning}")
            if args.csv:
                print(f"  Wrote {args.csv}")
        return 0
    finally:
        db.close()


def _projection_bridges(result, which: str):
    """(period label, bridges) for ``which``: "all" or a month as YYYY-MM."""
    if which == "all":
        whole = projection_bridges(result)
        if whole is None:
            raise CommandError("the projection has no months")
        first, last = result.rows[0], result.rows[-1]
        return f"{first.label} through {last.label}", whole
    index = next((row.index for row in result.rows if f"{row.month:%Y-%m}" == which), None)
    if index is None:
        raise CommandError(f"no projected month {which!r}; use YYYY-MM within the projection")
    return result.rows[index].label, month_bridges(result, index)


def _bridge_text(period: str, bridges) -> str:
    lines = [f"How the projection reconciles, {period}:"]
    for bridge in bridges:
        rows = [[term.label, term.amount.format(parens_negative=True)] for term in bridge.terms]
        rows.append(
            [
                "Unexplained",
                bridge.unexplained.format(parens_negative=True)
                + ("" if bridge.reconciles else "  (does not reconcile)"),
            ]
        )
        lines.append("")
        lines.append(table(rows, [bridge.label, "amount"], right={1}))
    return "\n".join(lines)


def _scenario_for(db: DbSQLite, args: argparse.Namespace) -> Scenario:
    """Load a saved scenario, or build a throwaway one from the flags."""
    if args.scenario:
        scenario = db.get_scenario_by_name(args.scenario)
        if scenario is None:
            raise CommandError(f"no scenario named {args.scenario!r}")
        if args.years:
            scenario.years = args.years
        return scenario

    return Scenario(
        name=args.name or "ad hoc",
        start=parse_date(args.start) or date.today().replace(day=1),
        years=args.years or 5,
        assumptions=Assumptions(
            income_growth=args.income_growth,
            expense_inflation=args.inflation,
            investment_return=args.investment_return,
            cash_interest=args.cash_interest,
        ),
    )


def cmd_scenario(args: argparse.Namespace) -> int:
    if args.action == "list":
        db = open_book(args.book, "r")
        try:
            scenarios = list(db.iter_scenarios())
            effective = {s.handle: s.effective_assumptions() for s in scenarios}
            names: dict[str | None, str] = {
                None: "Base",
                **{s.handle: s.name for s in scenarios},
            }
            emit(
                [
                    {
                        "name": s.name,
                        "parent": names.get(s.parent_handle, "Base"),
                        "years": s.years,
                        "income_growth": effective[s.handle].income_growth,
                        "expense_inflation": effective[s.handle].expense_inflation,
                        "investment_return": effective[s.handle].investment_return,
                        "assumption_sources": s.assumption_sources(),
                    }
                    for s in scenarios
                ],
                args,
                table(
                    [
                        [
                            s.name,
                            names.get(s.parent_handle, "Base"),
                            str(s.years),
                            f"{effective[s.handle].income_growth:.1%}",
                            f"{effective[s.handle].expense_inflation:.1%}",
                            f"{effective[s.handle].investment_return:.1%}",
                        ]
                        for s in scenarios
                    ],
                    ["name", "parent", "years", "income", "inflation", "return"],
                    right={2, 3, 4, 5},
                ),
            )
            return 0
        finally:
            db.close()

    db = open_book(args.book)
    try:
        if args.action == "save":
            existing = db.get_scenario_by_name(args.name)
            scenario = existing or Scenario(name=args.name)
            scenario.years = args.years or scenario.years
            scenario.start = parse_date(args.start) or scenario.start
            updated = Assumptions(
                income_growth=args.income_growth,
                expense_inflation=args.inflation,
                investment_return=args.investment_return,
                cash_interest=args.cash_interest,
            )
            scenario.assumptions = updated
            scenario.assumption_overrides.update(
                {
                    "income_growth",
                    "expense_inflation",
                    "investment_return",
                    "cash_interest",
                    "liability_interest",
                }
            )
            result = save_scenario_assumptions(
                db,
                SaveScenarioAssumptions(
                    scenario,
                    existing_handle=existing.handle if existing is not None else None,
                ),
            )
            if not result.ok:
                raise CommandError(service_error_message(result.errors[0]))
            emit(
                {"name": scenario.name, "handle": scenario.handle},
                args,
                f"Saved scenario {scenario.name!r}",
            )
        elif args.action == "reparent":
            reparented = db.get_scenario_by_name(args.name)
            if reparented is None:
                raise CommandError(f"no scenario named {args.name!r}")
            parent_name = str(args.parent or "").strip()
            if not parent_name or parent_name.casefold() == "base":
                reparented.parent_handle = None
                resolved_parent = "Base"
            else:
                parent = db.get_scenario_by_name(parent_name)
                if parent is None:
                    raise CommandError(f"no scenario named {parent_name!r}")
                reparented.parent_handle = parent.handle
                resolved_parent = parent.name
            reparented.inherits_base_assumptions = True
            result = save_scenario(db, SaveScenario(reparented, existing_handle=reparented.handle))
            if not result.ok:
                raise CommandError(service_error_message(result.errors[0]))
            emit(
                {"name": reparented.name, "parent": resolved_parent},
                args,
                f"Scenario {reparented.name!r} now inherits from {resolved_parent!r}",
            )
        elif args.action == "delete":
            to_delete = db.get_scenario_by_name(args.name)
            if to_delete is None:
                raise CommandError(f"no scenario named {args.name!r}")
            result = delete_scenario(db, DeleteScenario(to_delete.handle))
            if not result.ok:
                raise CommandError(service_error_message(result.errors[0]))
            emit({"deleted": args.name}, args, f"Deleted scenario {args.name!r}")
        return 0
    finally:
        db.close()


def _drawdown_json(db: DbSQLite, drawdown: Drawdown) -> dict[str, object]:
    return {
        "handle": drawdown.handle,
        "account": db.full_name(drawdown.account),
        "into": db.full_name(drawdown.into),
        "start": drawdown.start,
        "end": drawdown.end,
        "annual_amount": drawdown.annual_amount,
        "annual_rate": drawdown.annual_rate,
        "escalate": drawdown.escalate,
    }


def _drawdown_rate(text: str | None) -> Decimal | None:
    if text is None:
        return None
    try:
        return Decimal(text.strip().rstrip("%")) / (100 if text.strip().endswith("%") else 1)
    except InvalidOperation as exc:
        raise CommandError(f"not a rate: {text!r}") from exc


def cmd_drawdown(args: argparse.Namespace) -> int:
    db = open_book(args.book, "r" if args.action == "list" else "w")
    try:
        scenario = db.get_scenario_by_name(args.scenario)
        if scenario is None:
            raise CommandError(f"no scenario named {args.scenario!r}")
        if args.action == "list":
            rows = [_drawdown_json(db, item) for item in scenario.drawdowns]
            emit(
                rows,
                args,
                table(
                    [
                        [
                            str(row["handle"])[:8],
                            str(row["account"]),
                            str(row["into"]),
                            str(row["start"]),
                            str(row["end"] or ""),
                            (
                                f"{row['annual_rate']:%}/yr of balance"
                                if row["annual_rate"] is not None
                                else f"{row['annual_amount']}/yr"
                                + (" + inflation" if row["escalate"] else "")
                            ),
                        ]
                        for row in rows
                    ],
                    ["handle", "from", "into", "start", "end", "withdraws"],
                ),
            )
            return 0
        if args.action == "remove":
            if not args.handle:
                raise CommandError("remove needs --handle")
            matches = [
                item.handle for item in scenario.drawdowns if item.handle.startswith(args.handle)
            ]
            if len(matches) != 1:
                raise CommandError(f"no single drawdown matches {args.handle!r}")
            removed = remove_drawdown(db, scenario.handle, matches[0])
            if not removed.ok:
                raise CommandError(service_error_message(removed.errors[0]))
            emit({"removed": matches[0]}, args, f"Removed drawdown {matches[0][:8]}")
            return 0
        if not (args.account and args.into and args.start):
            raise CommandError("save needs --account, --into and --start")
        handle = None
        if args.handle:
            matches = [
                item.handle for item in scenario.drawdowns if item.handle.startswith(args.handle)
            ]
            if len(matches) != 1:
                raise CommandError(f"no single drawdown matches {args.handle!r}")
            handle = matches[0]
        start = parse_date(args.start)
        assert start is not None
        result = save_drawdown(
            db,
            SaveDrawdown(
                scenario.handle,
                resolve_account(db, args.account).handle,
                resolve_account(db, args.into).handle,
                start,
                annual_amount=Money(args.annual_amount) if args.annual_amount else None,
                annual_rate=_drawdown_rate(args.annual_rate),
                end=parse_date(args.end),
                escalate=not args.level,
                handle=handle,
            ),
        )
        if not result.ok or result.value is None:
            raise CommandError(service_error_message(result.errors[0]))
        emit(
            _drawdown_json(db, result.value),
            args,
            f"Saved drawdown {result.value.handle[:8]} in {scenario.name!r}",
        )
        return 0
    finally:
        db.close()


def cmd_compare(args: argparse.Namespace) -> int:
    db = open_book(args.book, "r")
    try:
        left_scenario = db.get_scenario_by_name(args.base)
        right_scenario = db.get_scenario_by_name(args.other)
        if left_scenario is None:
            raise CommandError(f"no scenario named {args.base!r}")
        if right_scenario is None:
            raise CommandError(f"no scenario named {args.other!r}")
        left = projection.project(db, left_scenario)
        right = projection.project(db, right_scenario)
        rows = projection.compare(left, right)
        yearly = [row for row in rows if (row["index"] + 1) % 12 == 0]
        emit(
            yearly,
            args,
            table(
                [
                    [
                        row["label"],
                        row["base_net_worth"].format(parens_negative=True),
                        row["other_net_worth"].format(parens_negative=True),
                        row["net_worth_delta"].format(parens_negative=True),
                        row["completeness"].label,
                    ]
                    for row in yearly
                ],
                ["month", args.base, args.other, "difference", "coverage"],
                right={1, 2, 3},
            ),
        )
        return 0
    finally:
        db.close()


def cmd_dashboard(args: argparse.Namespace) -> int:
    """The overview: what is owned, what is owed, and what must stay liquid."""
    db = open_book(args.book, "r")
    try:
        config = dashboard_engine.DashboardConfig.load(db)
        if args.liquidity_days:
            config.liquidity_days = args.liquidity_days
        if args.emergency_months:
            config.emergency_months = args.emergency_months
        board = dashboard_engine.build(db, config, as_of=parse_date(args.as_of))
        summary = board.summary()
        report = board.report_summary()

        def shown(field: str, amount: Money) -> str:
            return (
                board.unavailable_reason(field)
                if report[field] is None
                else amount.format(parens_negative=True)
            )

        if args.json:
            emit(
                {
                    "summary": report,
                    "unavailable_reasons": {
                        key: board.unavailable_reason(key)
                        for key, value in report.items()
                        if value is None
                    },
                    "missing_quotes": list(board.missing_quotes),
                    "liquid_missing_quotes": list(board.liquid_missing_quotes),
                    "completeness": board.completeness,
                    "liquid_completeness": board.liquid_completeness,
                    "coverage_notes": list(board.coverage_notes),
                    "groups": [
                        {
                            "name": g.name,
                            "path": g.path,
                            "depth": g.depth,
                            "heading": g.heading,
                            "note": g.note,
                            "members": list(g.members),
                            "kind": g.kind,
                            "total": g.report_total,
                            "value": g.report_value,
                            "debt": g.report_debt,
                            "equity": g.report_equity,
                            "loan_to_value": g.report_loan_to_value,
                            "missing_quotes": list(g.missing_quotes),
                            "completeness": g.completeness,
                            "loan_end": g.loan_end,
                            "accounts": [
                                {
                                    "name": account.name,
                                    "balance": account.total,
                                    "source": account.source,
                                    "note": account.note,
                                }
                                for account in g.accounts
                            ],
                        }
                        for g in board.groups
                    ],
                    "bills": [
                        {
                            "name": item.name,
                            "next_due": item.next_due,
                            "frequency": item.frequency,
                            "cycle_months": item.cycle_months,
                            "amount": item.amount,
                            "monthly": item.monthly,
                            "annual": item.annual,
                            "hold": item.hold(board.as_of),
                            "reserve_for": item.reserve_for,
                            "estimate": item.estimate,
                            "generated": item.generated,
                        }
                        for item in board.bills
                    ],
                    "income": [
                        {
                            "name": item.name,
                            "next_due": item.next_due,
                            "frequency": item.frequency,
                            "cycle_months": item.cycle_months,
                            "amount": item.amount,
                            "monthly": item.monthly,
                            "annual": item.annual,
                        }
                        for item in board.incomes
                    ],
                    "missed_bills": [
                        _missed_json(item)
                        for item in board.display_bills
                        if isinstance(item, dashboard_engine.MissedGroup)
                    ],
                    "missed_income": [
                        _missed_json(item)
                        for item in board.display_incomes
                        if isinstance(item, dashboard_engine.MissedGroup)
                    ],
                },
                args,
            )
            return 0

        print(f"Dashboard as at {board.as_of}\n")
        rows = []
        for group in board.groups:
            group_label = f"{'  ' * group.depth}{group.name}"
            if group.note:
                group_label = f"{group_label} — {group.note}"
            loan_to_value = group.report_loan_to_value
            equity = group.report_equity
            if (
                not group.missing_quotes
                and loan_to_value is not None
                and group.report_value is not None
                and group.report_debt is not None
                and equity is not None
            ):
                rows.append(
                    [
                        group_label,
                        group.report_value.format(),
                        group.report_debt.format(),
                        equity.format(parens_negative=True),
                        f"{loan_to_value:.1%}",
                        str(group.loan_end or ""),
                    ]
                )
            else:
                rows.append(
                    [
                        group_label,
                        "",
                        "",
                        "Missing reporting-currency quote"
                        if group.missing_quotes
                        else group.total.format(parens_negative=True),
                        "",
                        str(group.loan_end or ""),
                    ]
                )
        print(
            table(
                rows,
                ["group", "value", "owed", "equity / total", "LTV", "loan end"],
                right={1, 2, 3, 4},
            )
        )

        print()
        for note in board.coverage_notes:
            print(note)
        if not board.completeness.complete:
            print("Net worth withheld: " + "; ".join(board.completeness.detail()))
        if board.coverage_notes or not board.completeness.complete:
            print()
        headline = [
            ["Net worth", shown("net_worth", summary["net_worth"])],
            ["Liquid", shown("liquid", summary["liquid"])],
            [
                f"Needed within {config.liquidity_days} days",
                summary["required_liquid"].format(parens_negative=True),
            ],
            ["Available", shown("available", summary["available"])],
            *(
                [
                    [
                        "Set aside for goals",
                        summary["goals_set_aside"].format()
                        + (
                            f" ({summary['goals_held'].format()} held from spendable cash)"
                            if summary["goals_held"] != summary["goals_set_aside"]
                            else ""
                        ),
                    ]
                ]
                if summary["goals_set_aside"] > 0
                else []
            ),
            *(
                [
                    [
                        "Reimbursements due",
                        summary["receivables_owed"].format()
                        + (
                            f" ({summary['receivables_attention'].format()} disputed or overdue)"
                            if summary["receivables_attention"] > 0
                            else ""
                        ),
                    ]
                ]
                if summary["receivables_owed"] > 0
                else []
            ),
            *(
                [
                    [
                        "FSA claims needing attention",
                        f"{summary['fsa_claims_attention']} (breadsched claims --attention)",
                    ]
                ]
                if summary["fsa_claims_attention"]
                else []
            ),
            [
                f"Emergency fund ({config.emergency_months} months)",
                shown("emergency_fund", summary["emergency_fund"]),
            ],
            [
                "Months covered",
                board.unavailable_reason("months_covered")
                if report["months_covered"] is None
                else f"{summary['months_covered']}",
            ],
            ["Committed outgoings, monthly", summary["monthly_outgoings"].format()],
            [
                "Outgoings including estimates, monthly",
                summary["monthly_outgoings_with_estimates"].format(),
            ],
            [
                "Committed emergency outgoings, monthly",
                summary["emergency_monthly_outgoings"].format(),
            ],
            [
                "Emergency outgoings including estimates, monthly",
                summary["emergency_monthly_outgoings_with_estimates"].format(),
            ],
            ["Committed income, monthly", summary["income_per_month"].format()],
            [
                "Income including estimates, monthly",
                summary["income_per_month_with_estimates"].format(),
            ],
        ]
        print(table(headline, ["measure", "amount"], right={1}))

        if board.bills:
            print()
            print("Pending bills")
            display_bills = board.display_bills
            bill_rows = [
                [
                    item.name[:32],
                    _due_cell(item, board.as_of),
                    item.frequency,
                    item.amount.format(),
                    "" if item.generated else item.monthly.format(),
                    item.held.format(),
                    "" if item.generated else item.annual.format(),
                    "account" if item.generated else "",
                ]
                for item in display_bills[: args.limit]
            ]
            print(
                table(
                    bill_rows,
                    [
                        "item",
                        "due",
                        "frequency",
                        "amount",
                        "monthly",
                        "hold",
                        "annual",
                        "",
                    ],
                    right={3, 4, 5, 6},
                )
            )
            if len(display_bills) > args.limit:
                print(f"... and {len(display_bills) - args.limit} more")
        if board.incomes:
            print()
            print("Expected income")
            display_incomes = board.display_incomes
            income_rows = [
                [
                    item.name[:32],
                    _due_cell(item, board.as_of),
                    item.frequency,
                    item.amount.format(),
                    item.monthly.format(),
                    item.annual.format(),
                ]
                for item in display_incomes[: args.limit]
            ]
            print(
                table(
                    income_rows,
                    ["item", "due", "frequency", "amount", "monthly", "annual"],
                    right={3, 4, 5},
                )
            )
            if len(display_incomes) > args.limit:
                print(f"... and {len(display_incomes) - args.limit} more")
        return 0
    finally:
        db.close()


def assumption_flags(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("--income-growth", default="0.03", help="annual, e.g. 0.03")
    sub.add_argument("--inflation", default="0.025", help="annual expense inflation")
    sub.add_argument("--investment-return", default="0.06", help="annual, nominal")
    sub.add_argument("--cash-interest", default="0.01")


def register(add: AddCommand) -> None:
    """Add the projection subcommands."""
    project_cmd = add("project", "Run a multi-year projection")
    project_cmd.add_argument("--scenario", help="use a saved scenario")
    project_cmd.add_argument("--name", help="label for an ad hoc scenario")
    project_cmd.add_argument("--years", type=int)
    project_cmd.add_argument("--start")
    project_cmd.add_argument(
        "--monthly", action="store_true", help="month by month instead of yearly"
    )
    project_cmd.add_argument("--csv", help="also write the monthly rows to this file")
    assumption_flags(project_cmd)
    project_cmd.add_argument(
        "--bridge",
        nargs="?",
        const="all",
        metavar="YYYY-MM",
        help="show how opening balances, planned events, and interest and performance "
        "reach each closing balance (the whole projection, or one month)",
    )
    project_cmd.set_defaults(func=cmd_project)

    scenario = add("scenario", "Save, list, reparent or delete projection scenarios")
    scenario.add_argument("action", choices=["list", "save", "reparent", "delete"])
    scenario.add_argument("--name")
    scenario.add_argument("--parent", help="parent scenario name, or Base")
    scenario.add_argument("--years", type=int)
    scenario.add_argument("--start")
    assumption_flags(scenario)
    scenario.set_defaults(func=cmd_scenario)

    drawdown = add("drawdown", "List, save or remove a scenario's retirement drawdowns")
    drawdown.add_argument("action", choices=["list", "save", "remove"])
    drawdown.add_argument("--scenario", required=True, help="saved scenario name")
    drawdown.add_argument("--account", help="holding account withdrawn from")
    drawdown.add_argument("--into", help="spendable cash account paid into")
    drawdown.add_argument("--start", help="first withdrawal; later ones fall on its day")
    drawdown.add_argument("--end", help="last day withdrawals happen")
    drawdown.add_argument("--annual-amount", help="fixed yearly amount, taken monthly")
    drawdown.add_argument("--annual-rate", help="yearly share of the balance, e.g. 0.04 or 4%%")
    drawdown.add_argument(
        "--level", action="store_true", help="do not grow a fixed amount with inflation"
    )
    drawdown.add_argument("--handle", help="drawdown to replace or remove (prefix)")
    drawdown.set_defaults(func=cmd_drawdown)

    compare = add("compare", "Compare two saved scenarios year by year")
    compare.add_argument("base")
    compare.add_argument("other")
    compare.set_defaults(func=cmd_compare)

    net_worth_cmd = add(
        "net-worth",
        "Market-valued net worth at each period end, with missing quotes named",
    )
    net_worth_cmd.add_argument("--start", required=True, help="first date (YYYY-MM-DD)")
    net_worth_cmd.add_argument("--end", required=True, help="last date (YYYY-MM-DD)")
    net_worth_cmd.add_argument(
        "--period",
        default="month",
        choices=[period.value for period in activity.ReportingPeriod],
        help="value the book at the end of each of these periods",
    )
    net_worth_cmd.add_argument(
        "--as-of",
        help="last date to value; later periods are omitted (default today)",
    )
    net_worth_cmd.set_defaults(func=cmd_net_worth)

    net_worth_change_cmd = add(
        "net-worth-change",
        "The postings behind a net worth change, reconciled to market movement",
    )
    net_worth_change_cmd.add_argument("--start", required=True, help="first date (YYYY-MM-DD)")
    net_worth_change_cmd.add_argument("--end", required=True, help="last date (YYYY-MM-DD)")
    net_worth_change_cmd.add_argument(
        "--as-of", help="value on this date instead when --end is later (default today)"
    )
    net_worth_change_cmd.add_argument(
        "--csv", metavar="PATH", help="also write the postings and totals as CSV"
    )
    net_worth_change_cmd.set_defaults(func=cmd_net_worth_change)

    dash = add("dashboard", "Overview of balances, bills and liquidity")
    dash.add_argument("--as-of")
    dash.add_argument(
        "--liquidity-days", type=int, help="days of bills that must be covered by cash"
    )
    dash.add_argument(
        "--emergency-months", type=int, help="months of outgoings the emergency fund should cover"
    )
    dash.add_argument("--limit", type=int, default=40, help="bills and income rows to list")
    dash.set_defaults(func=cmd_dashboard)
