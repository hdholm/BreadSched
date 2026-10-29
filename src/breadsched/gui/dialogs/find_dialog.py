"""Find Transactions: search every register, then sort, group, and total (#382).

The register's filter searches one account; this window searches the whole book
through the book server's ``find`` (``services.transaction_search``), the same
search the browser's Find view and the command line's ``find`` run. Rows are
splits, so an amount or an account picks out the leg that has it. Grouping gives
subtotals per account, month, year, or tag, and the last line is the grand total,
per currency. Activating a row opens that split's register.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import InvalidOperation

from ...api import ApiError
from ...api.local import book_server
from ...gen.db.sqlite import DbSQLite
from ...gen.engine.transaction_query import DIRECTIONS, GROUPINGS, SORTS, TransactionQuery
from ...gen.lib import Money
from ...gen.services.transaction_search import FindResult
from ..gi_setup import Gio, Gtk
from ..views._base import Row, column
from ..widgets.bounded import BoundedWindow
from ..widgets.choice import bounded_dropdown
from ..widgets.date_entry import attach_date_keys, parse_date_text
from ..widgets.form_pages import FormGrid
from ..widgets.help import help_row

__all__ = ["FindDialog", "ResultLine"]

_DIRECTION_LABELS = {
    "any": "In or out",
    "debit": "Money in (debit)",
    "credit": "Money out (credit)",
}
_SORT_LABELS = {
    "date": "Date",
    "amount": "Amount",
    "account": "Account",
    "description": "Description",
}
_GROUP_LABELS = {
    "none": "No groups",
    "account": "Account",
    "month": "Month",
    "year": "Year",
    "tag": "Tag",
}


@dataclass(frozen=True, slots=True)
class ResultLine:
    """One line of the results: a split, a group's subtotal, or the grand total."""

    kind: str  # "split", "subtotal", or "total"
    date: str = ""
    num: str = ""
    description: str = ""
    account: str = ""
    amount: str = ""
    handle: str | None = None  # the split's account, for a split line


def _totals(totals: dict[str, Money]) -> str:
    return (
        "\n".join(
            f"{amount.format(parens_negative=True)} {code}".strip()
            for code, amount in totals.items()
        )
        or Money(0).format()
    )


def result_lines(result: FindResult) -> list[ResultLine]:
    """The rows as shown: each group's splits and subtotal, then the total."""
    grouped = result.query.group_by != "none"
    lines: list[ResultLine] = []
    for group in result.groups:
        for row in group.rows:
            lines.append(
                ResultLine(
                    "split",
                    row.date.isoformat(),
                    row.num,
                    row.description,
                    row.account_name,
                    row.amount.format(parens_negative=True),
                    row.account,
                )
            )
        if grouped:
            lines.append(
                ResultLine(
                    "subtotal", description=f"Subtotal: {group.key}", amount=_totals(group.totals)
                )
            )
    lines.append(
        ResultLine(
            "total",
            description=(
                f"Total: {result.splits} split(s) in {result.transactions} transaction(s)"
            ),
            amount=_totals(result.totals),
        )
    )
    return lines


class FindDialog(BoundedWindow):
    """Criteria above, results below; Find runs the search."""

    def __init__(
        self,
        parent: Gtk.Window | None,
        db: DbSQLite,
        open_account: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(title="Find transactions", transient_for=parent, modal=False)
        self.db = db
        self.book = book_server(db)
        self.open_account = open_account
        self.result: FindResult | None = None
        self.set_default_size(820, 640)
        records = sorted(
            (record for record in self.book.accounts() if not record.account.is_root),
            key=lambda record: record.full_name,
        )
        self.accounts = [record.account for record in records]

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(12)
        self.set_child(box)
        box.append(help_row("find"))

        form = FormGrid()
        self.text_entry = Gtk.Entry(placeholder_text="Anything a transaction holds")
        self.text_entry.connect("activate", lambda *_: self.find())
        form.add("Text", self.text_entry)
        self.account = bounded_dropdown(["All accounts", *(record.full_name for record in records)])
        form.add("Account", self.account)
        self.subaccounts_check = Gtk.CheckButton(label="Include subaccounts")
        self.subaccounts_check.set_active(True)
        form.add(None, self.subaccounts_check)
        dates = Gtk.Box(spacing=6)
        self.start_entry = attach_date_keys(Gtk.Entry(placeholder_text="From YYYY-MM-DD"))
        self.end_entry = attach_date_keys(Gtk.Entry(placeholder_text="To YYYY-MM-DD"))
        dates.append(self.start_entry)
        dates.append(self.end_entry)
        form.add("Dates", dates)
        amounts = Gtk.Box(spacing=6)
        self.amount_entry = Gtk.Entry(placeholder_text="Exactly")
        self.min_entry = Gtk.Entry(placeholder_text="At least")
        self.max_entry = Gtk.Entry(placeholder_text="At most")
        for entry in (self.amount_entry, self.min_entry, self.max_entry):
            entry.connect("activate", lambda *_: self.find())
            amounts.append(entry)
        form.add(
            "Amount",
            amounts,
            tooltip="A split's size, either direction: 67 finds a 67.00 leg, never check 1067",
        )
        self.direction = bounded_dropdown([_DIRECTION_LABELS[name] for name in DIRECTIONS])
        form.add("Direction", self.direction)
        self.tag_entry = Gtk.Entry(placeholder_text="Comma-separated; every one must be present")
        form.add("Tags", self.tag_entry)
        order = Gtk.Box(spacing=6)
        self.sort = bounded_dropdown([_SORT_LABELS[name] for name in SORTS])
        self.descending_check = Gtk.CheckButton(label="Descending")
        order.append(self.sort)
        order.append(self.descending_check)
        form.add("Sort by", order)
        self.group_by = bounded_dropdown([_GROUP_LABELS[name] for name in GROUPINGS])
        form.add("Subtotal by", self.group_by)
        box.append(form)

        actions = Gtk.Box(spacing=8)
        self.status = Gtk.Label(xalign=0, wrap=True)
        self.status.set_hexpand(True)
        actions.append(self.status)
        find = Gtk.Button(label="Find")
        find.add_css_class("suggested-action")
        find.connect("clicked", lambda *_: self.find())
        actions.append(find)
        box.append(actions)

        self.results = Gtk.ColumnView()
        self.results.add_css_class("data-table")
        self.results.set_show_row_separators(True)
        self.results.connect("activate", self._on_activate)
        for title, attribute, expand, numeric in (
            ("Date", "date", False, False),
            ("Num", "num", False, False),
            ("Description", "description", True, False),
            ("Account", "account", True, False),
            ("Amount", "amount", False, True),
        ):
            self.results.append_column(
                column(title, lambda line, a=attribute: getattr(line, a), expand, numeric)
            )
        scroller = Gtk.ScrolledWindow(child=self.results)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        box.append(scroller)

        close = Gtk.Button(label="Close", halign=Gtk.Align.END)
        close.connect("clicked", lambda *_: self.close())
        box.append(close)
        self.lines: list[ResultLine] = []

    # ---------------------------------------------------------------- reading

    @staticmethod
    def _date(entry: Gtk.Entry, label: str) -> date | None:
        text = entry.get_text().strip()
        if not text:
            return None
        try:
            return parse_date_text(text)
        except ValueError:
            raise ValueError(f"check the {label} date (YYYY-MM-DD)") from None

    @staticmethod
    def _money(entry: Gtk.Entry, label: str) -> Money | None:
        text = entry.get_text().strip().replace(",", "")
        if not text:
            return None
        try:
            return Money(text)
        except (InvalidOperation, ValueError):
            raise ValueError(f"check the {label} amount") from None

    def query(self) -> TransactionQuery:
        """The criteria as typed; ``ValueError`` naming one that cannot be read."""
        chosen = self.account.get_selected()
        accounts = (self.accounts[chosen - 1].handle,) if chosen > 0 else ()
        tags = tuple(part.strip() for part in self.tag_entry.get_text().split(",") if part.strip())
        return TransactionQuery(
            text=self.text_entry.get_text().strip(),
            accounts=accounts,
            subaccounts=self.subaccounts_check.get_active(),
            start=self._date(self.start_entry, "first"),
            end=self._date(self.end_entry, "last"),
            amount=self._money(self.amount_entry, "exact"),
            amount_min=self._money(self.min_entry, "smallest"),
            amount_max=self._money(self.max_entry, "largest"),
            direction=DIRECTIONS[self.direction.get_selected()],
            tags=tags,
            sort=SORTS[self.sort.get_selected()],
            descending=self.descending_check.get_active(),
            group_by=GROUPINGS[self.group_by.get_selected()],
        )

    def find(self) -> FindResult | None:
        """Run the search and show it; None, with the reason shown, if it cannot run."""
        try:
            result = self.book.find(self.query())
        except (ApiError, ValueError) as exc:
            self.status.set_text(f"Cannot search: {exc}.")
            self.status.add_css_class("negative")
            return None
        self.status.remove_css_class("negative")
        self.status.set_text(
            f"{result.splits} split(s) in {result.transactions} transaction(s). "
            "Activate a row to open its register."
        )
        self.result = result
        self.lines = result_lines(result)
        store = Gio.ListStore.new(Row)
        for line in self.lines:
            store.append(Row(line))
        self.results.set_model(Gtk.SingleSelection(model=store))
        return result

    def _on_activate(self, _view, position: int) -> None:
        line = self.lines[position] if position < len(self.lines) else None
        if line is not None and line.handle and self.open_account is not None:
            self.open_account(line.handle)
