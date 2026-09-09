"""A locally hosted web interface.

Built on :mod:`http.server` with no framework, for the same reason the core has no
third-party dependencies: a finance tool that people run on their own machine for
years should not rot because a web framework moved on. The whole surface is a small
JSON API plus one static page.

It binds to the loopback address only. There is no authentication, because there is
no network exposure to authenticate against; if that ever changes, this docstring
is wrong and the change needs more than a new bind address.
"""

from __future__ import annotations

import json
import threading
from datetime import date
from decimal import Decimal
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..gen.db.sqlite import DbSQLite
from ..gen.engine import budgeting, cashflow, ledger, projection, schedule
from ..gen.lib import Money, ProjectionBasis, Scenario, Split, Transaction
from ..gen.utils.logs import get_logger

__all__ = ["serve", "build_handler", "api"]

LOG = get_logger(__name__)
STATIC = Path(__file__).parent / "static"


def _encode(value: object) -> object:
    if isinstance(value, Money):
        return str(value.to_decimal())
    if isinstance(value, (date, Decimal)):
        return str(value)
    raise TypeError(f"cannot serialise {type(value).__name__}")


class Api:
    """The JSON surface. Every method returns plain data, never a response object."""

    def __init__(self, db: DbSQLite) -> None:
        self.db = db

    # ---------------------------------------------------------------- reading

    def dashboard(self, liquidity_days: int | None, emergency_months: int | None) -> dict:
        """The overview: groups, the liquidity verdict, and the pending bills.

        Computed by the same engine the desktop dashboard uses, so the two cannot
        disagree about a household's position.
        """
        from ..gen.engine import dashboard as engine

        config = engine.DashboardConfig.load(self.db)
        if liquidity_days:
            config.liquidity_days = liquidity_days
        if emergency_months:
            config.emergency_months = emergency_months
        board = engine.build(self.db, config)

        return {
            "summary": _plain(board.summary()),
            "config": {
                "liquidity_days": config.liquidity_days,
                "emergency_months": config.emergency_months,
            },
            "groups": [
                {
                    "name": group.name,
                    "kind": group.kind,
                    "total": str(group.total.to_decimal()),
                    "value": str(group.value.to_decimal()) if group.value else None,
                    "debt": str(group.debt.to_decimal()) if group.debt else None,
                    "equity": (
                        str(group.equity.to_decimal())
                        if group.equity is not None else None
                    ),
                    "loan_to_value": (
                        float(group.loan_to_value)
                        if group.loan_to_value is not None else None
                    ),
                    "accounts": [
                        {"name": name, "balance": str(balance.to_decimal())}
                        for name, balance in group.accounts
                    ],
                }
                for group in board.groups
            ],
            "bills": [
                {
                    "name": bill.name,
                    "next_due": bill.next_due.isoformat(),
                    "days_until": bill.days_until(board.as_of),
                    "cycle_months": float(bill.cycle_months),
                    "amount": str(bill.amount.to_decimal()),
                    "monthly": str(bill.monthly.to_decimal()),
                    "annual": str(bill.annual.to_decimal()),
                    "hold": str(bill.hold(board.as_of).to_decimal()),
                    "estimate": bill.estimate,
                }
                for bill in board.bills
            ],
        }

    def summary(self) -> dict:
        counts = self.db.summary()
        return {
            "book": self.db.path,
            "accounts": counts["account"],
            "transactions": counts["txn"],
            "cash": ledger.cash_on_hand(self.db),
            "net_worth": ledger.net_worth(self.db),
            "budgets": [b.name for b in self.db.iter_budgets()],
            "scenarios": [s.name for s in self.db.iter_scenarios()],
        }

    def accounts(self) -> list[dict]:
        """The chart of accounts as a flat list carrying its own depth."""
        rows: list[dict] = []

        def walk(parent: str | None, depth: int) -> None:
            for account in self.db.child_accounts(parent):
                rows.append(
                    {
                        "handle": account.handle,
                        "name": account.name,
                        "full_name": self.db.full_name(account),
                        "type": account.atype.value,
                        "class": account.account_class.value,
                        "placeholder": account.placeholder,
                        "depth": depth,
                        "balance": ledger.balance_recursive(self.db, account.handle),
                        "own_balance": ledger.balance(self.db, account.handle),
                    }
                )
                walk(account.handle, depth + 1)

        root = self.db.root_account()
        walk(root.handle if root else None, 0)
        return rows

    def register(self, handle: str, limit: int = 250) -> dict:
        account = self.db.get_account(handle)
        if account is None:
            raise KeyError(handle)
        rows = ledger.register(self.db, handle)[-limit:]
        return {
            "account": self.db.full_name(account),
            "type": account.atype.value,
            "rows": [
                {
                    "date": row.post_date,
                    "num": row.transaction.num,
                    "description": row.description,
                    "transfer": row.transfer_label(self.db),
                    "amount": row.amount,
                    "balance": row.running,
                }
                for row in rows
            ],
        }

    def scheduled(self, days: int = 60) -> dict:
        occurrences = schedule.due_occurrences(self.db, horizon_days=days)
        return {
            "definitions": [
                {
                    "handle": s.handle,
                    "name": s.name,
                    "frequency": s.recurrence.describe(),
                    "amount": s.amount(),
                    "enabled": s.enabled,
                    "placeholder": s.placeholder,
                    "auto": s.auto_create,
                }
                for s in self.db.iter_scheduled()
            ],
            "upcoming": [
                {"date": o.when, "name": o.name, "amount": o.amount}
                for o in occurrences
            ],
        }

    def budget(self, name: str | None = None) -> dict:
        budgets = list(self.db.iter_budgets())
        if name:
            budgets = [b for b in budgets if b.name == name]
        if not budgets:
            return {"budget": None, "labels": [], "lines": []}
        report = cashflow.build_report(self.db, budgets[0])
        return {
            "budget": report.budget.name,
            "kind": report.budget.kind.value,
            "labels": report.labels,
            "shortfalls": report.shortfall_periods(),
            "net": [report.net_cash_flow(p) for p in range(report.budget.periods)],
            "net_actual": [
                report.net_cash_flow(p, actual=True)
                for p in range(report.budget.periods)
            ],
            "lines": [
                {
                    "account": line.name,
                    "class": line.account_class.value,
                    "budgeted": [p.budgeted for p in line.periods],
                    "actual": [p.actual for p in line.periods],
                    "total_budgeted": line.budgeted_total,
                    "total_actual": line.actual_total,
                    "variance": line.variance_total,
                }
                for line in report.lines
            ],
        }

    def coverage(self, name: str | None = None) -> list[dict]:
        budgets = list(self.db.iter_budgets())
        if name:
            budgets = [b for b in budgets if b.name == name]
        if not budgets:
            return []
        return [
            {
                "account": row.name,
                "budgeted": row.budgeted,
                "scheduled": row.scheduled,
                "unexplained": row.unexplained,
            }
            for row in budgeting.coverage(self.db, budgets[0])
        ]

    def projection(self, scenario_name: str | None = None, years: int = 5) -> dict:
        scenario = None
        if scenario_name:
            scenario = self.db.get_scenario_by_name(scenario_name)
        if scenario is None:
            budgets = list(self.db.iter_budgets())
            scenario = Scenario(
                name=scenario_name or "ad hoc",
                years=years,
                basis=ProjectionBasis.SCHEDULED,
                budget=budgets[0].handle if budgets else None,
            )
        result = projection.project(self.db, scenario)
        return {
            "scenario": scenario.name,
            "summary": result.summary(),
            "rows": [
                {
                    "label": row.label,
                    "income": row.income,
                    "expense": row.expense,
                    "cash": row.cash_close,
                    "holdings": row.holdings,
                    "liabilities": row.liabilities,
                    "net_worth": row.net_worth,
                }
                for row in result.rows
            ],
        }

    # ---------------------------------------------------------------- writing

    def add_transaction(self, payload: dict) -> dict:
        """Post a two-split transaction. The only write the web interface allows."""
        debit = self.db.get_account_by_name(payload["to"])
        credit = self.db.get_account_by_name(payload["from"])
        if debit is None or credit is None:
            raise KeyError("unknown account")
        when = date.fromisoformat(payload.get("date") or date.today().isoformat())
        amount = Money(str(payload["amount"]))
        txn = Transaction(
            post_date=when, description=payload.get("description", "").strip()
        )
        memo = payload.get("memo", "")
        txn.add_split(Split(debit.handle, amount, memo=memo))
        txn.add_split(Split(credit.handle, -amount, memo=memo))
        with self.db.transaction(f"Add {txn.description}") as batch:
            self.db.add_transaction(txn, batch)
        return {"handle": txn.handle, "date": when, "amount": amount}

    def post_scheduled(self) -> dict:
        posted = schedule.post_due(self.db, only_auto=False)
        return {
            "posted": len(posted),
            "transactions": [
                {"date": t.post_date, "description": t.description} for t in posted
            ],
        }


def api(db: DbSQLite) -> Api:
    return Api(db)


def _plain(values: dict) -> dict:
    """Convert Money and date values to strings the browser can read."""
    out = {}
    for key, value in values.items():
        if hasattr(value, "to_decimal"):
            out[key] = str(value.to_decimal())
        elif hasattr(value, "isoformat"):
            out[key] = value.isoformat()
        else:
            out[key] = str(value) if value is not None else None
    return out


def _plain(values: dict) -> dict:
    """Convert Money and date values to strings the browser can read."""
    out = {}
    for key, value in values.items():
        if hasattr(value, "to_decimal"):
            out[key] = str(value.to_decimal())
        elif hasattr(value, "isoformat"):
            out[key] = value.isoformat()
        else:
            out[key] = str(value) if value is not None else None
    return out


ROUTES = {
    "/api/dashboard": lambda a, q: a.dashboard(
        int(q.get("liquidity_days", ["0"])[0] or 0),
        int(q.get("emergency_months", ["0"])[0] or 0),
    ),
    "/api/summary": lambda a, q: a.summary(),
    "/api/accounts": lambda a, q: a.accounts(),
    "/api/register": lambda a, q: a.register(
        q.get("account", [""])[0], int(q.get("limit", ["250"])[0])
    ),
    "/api/scheduled": lambda a, q: a.scheduled(int(q.get("days", ["60"])[0])),
    "/api/budget": lambda a, q: a.budget(q.get("name", [None])[0]),
    "/api/coverage": lambda a, q: a.coverage(q.get("name", [None])[0]),
    "/api/projection": lambda a, q: a.projection(
        q.get("scenario", [None])[0], int(q.get("years", ["5"])[0])
    ),
}

POST_ROUTES = {
    "/api/transaction": lambda a, body: a.add_transaction(body),
    "/api/post-scheduled": lambda a, body: a.post_scheduled(),
}


class Handler(BaseHTTPRequestHandler):
    """Serves the single page and the JSON API. One database, guarded by a lock."""

    server_version = "CashPerspective"
    api_object: Api
    lock: threading.Lock

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - base class name
        LOG.debug("%s %s", self.address_string(), fmt % args)

    # ------------------------------------------------------------- responses

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # The page never loads anything remote; say so.
        self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline'")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: object) -> None:
        body = json.dumps(payload, default=_encode).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _static(self, name: str) -> None:
        path = (STATIC / name).resolve()
        if not path.is_file() or STATIC.resolve() not in path.parents:
            self._json(404, {"error": "not found"})
            return
        kind = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
        }.get(path.suffix, "application/octet-stream")
        self._send(200, path.read_bytes(), kind)

    # ---------------------------------------------------------------- routing

    def do_GET(self) -> None:  # noqa: N802 - required by the base class
        parsed = urlparse(self.path)
        route = ROUTES.get(parsed.path)
        if route is None:
            self._static("index.html" if parsed.path in ("/", "") else parsed.path[1:])
            return
        query = parse_qs(parsed.query)
        try:
            with self.lock:
                self._json(200, route(self.api_object, query))
        except KeyError as exc:
            self._json(404, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - a bad request must not kill the server
            LOG.exception("request failed: %s", self.path)
            self._json(500, {"error": str(exc)})

    def do_POST(self) -> None:  # noqa: N802 - required by the base class
        parsed = urlparse(self.path)
        route = POST_ROUTES.get(parsed.path)
        if route is None:
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "body was not valid JSON"})
            return
        try:
            with self.lock:
                self._json(200, route(self.api_object, body))
        except KeyError as exc:
            self._json(400, {"error": f"unknown account: {exc}"})
        except Exception as exc:  # noqa: BLE001
            LOG.exception("request failed: %s", self.path)
            self._json(400, {"error": str(exc)})


def build_handler(db: DbSQLite) -> type[Handler]:
    """A handler class bound to one database, with a lock around every request.

    SQLite connections are not safe to share across threads, and the server is
    threaded, so requests are serialised. For a single-user local tool that costs
    nothing and removes a whole category of intermittent corruption.
    """
    return type(
        "BoundHandler",
        (Handler,),
        {"api_object": Api(db), "lock": threading.Lock()},
    )


def serve(
    db: DbSQLite,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = False,
) -> ThreadingHTTPServer:
    """Start the server. Returns it without blocking; call ``serve_forever``."""
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError(
            "the web interface has no authentication and binds to loopback only"
        )
    server = ThreadingHTTPServer((host, port), build_handler(db))
    if open_browser:  # pragma: no cover - depends on a desktop session
        import webbrowser

        threading.Timer(
            0.5, partial(webbrowser.open, f"http://{host}:{server.server_port}/")
        ).start()
    return server
