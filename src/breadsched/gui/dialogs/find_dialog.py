"""Find Transactions: search every register, then sort, group, and total (#382).

The register's filter searches one account; this window searches the whole book
through the book server's ``find`` (``services.transaction_search``), the same
search the browser's Find view and the command line's ``find`` run. Rows are
splits, so an amount or an account picks out the leg that has it. Subtotals nest
in the order chosen (account, month, year, tag, description, or notes), several
accounts can be searched at once, and the last line is the grand total, per
currency (#408). Activating a row opens that split's register. **Print…** prints the
result; opened from the Reports menu the same window is the Transaction Detail
report.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import InvalidOperation

from ...api import ApiError
from ...api.local import book_server
from ...gen.engine.transaction_query import DIRECTIONS, GROUPINGS, SORTS, TransactionQuery
from ...gen.lib import Money
from ...gen.services.transaction_search import FindResult, FoundGroup
from .. import printing
from ..gi_setup import Gio, Gtk
from ..remote_book import OpenBook
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
    "description": "Description",
    "notes": "Notes",
}
#: How many subtotals can be nested.
SUBTOTAL_LEVELS = 3


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
    """The rows as shown: each group's heading, its splits or inner groups, and its
    subtotal, nested in the order chosen, then the total."""
    lines: list[ResultLine] = []

    def splits(group: FoundGroup) -> None:
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

    def nested(group: FoundGroup) -> None:
        indent = "    " * group.depth
        if group.depth or group.children:
            lines.append(ResultLine("heading", description=f"{indent}{group.key}"))
        if group.children:
            for child in group.children:
                nested(child)
        else:
            splits(group)
        lines.append(
            ResultLine(
                "subtotal",
                description=f"{indent}Subtotal: {group.key}",
                amount=_totals(group.totals),
            )
        )

    for group in result.groups:
        if result.query.levels:
            nested(group)
        else:
            splits(group)
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
        db: OpenBook,
        open_account: Callable[[str], None] | None = None,
        *,
        report: bool = False,
    ) -> None:
        #: Opened from the Reports menu: the Transaction Detail report (#408).
        self.title = "Transaction Detail" if report else "Find transactions"
        super().__init__(title=self.title, transient_for=parent, modal=False)
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
        # Any collection of accounts (#408); none chosen searches every one.
        self.account_checks: dict[str, Gtk.CheckButton] = {}
        self.accounts_button = Gtk.MenuButton(label="All accounts")
        self.accounts_button.set_tooltip_text("Choose the accounts to search")
        picker = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        for side in ("top", "bottom", "start", "end"):
            getattr(picker, f"set_margin_{side}")(8)
        filter_entry = Gtk.SearchEntry(placeholder_text="Filter accounts")
        picker.append(filter_entry)
        listing = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        for record in records:
            check = Gtk.CheckButton(label=record.full_name)
            check.connect("toggled", lambda *_: self._show_chosen())
            self.account_checks[record.account.handle] = check
            listing.append(check)
        scroll = Gtk.ScrolledWindow(child=listing, propagate_natural_width=True)
        scroll.set_min_content_height(240)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        picker.append(scroll)
        clear = Gtk.Button(label="All accounts")
        clear.connect("clicked", lambda *_: self.choose_accounts(()))
        picker.append(clear)

        def narrow(entry) -> None:
            needle = entry.get_text().casefold()
            for check in self.account_checks.values():
                check.set_visible(needle in (check.get_label() or "").casefold())

        filter_entry.connect("search-changed", narrow)
        popover = Gtk.Popover(child=picker)
        self.accounts_button.set_popover(popover)
        form.add("Accounts", self.accounts_button)
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
        # Subtotals nest in the order chosen: by this, then by that (#408).
        subtotals = Gtk.Box(spacing=6)
        self.group_by = bounded_dropdown([_GROUP_LABELS[name] for name in GROUPINGS])
        subtotals.append(self.group_by)
        self.then_by: list[Gtk.DropDown] = []
        for _level in range(SUBTOTAL_LEVELS - 1):
            then = bounded_dropdown(["(no more)", *(_GROUP_LABELS[name] for name in GROUPINGS[1:])])
            then.update_property([Gtk.AccessibleProperty.LABEL], ["Then subtotal by"])
            self.then_by.append(then)
            subtotals.append(Gtk.Label(label="then"))
            subtotals.append(then)
        form.add("Subtotal by", subtotals)
        box.append(form)

        actions = Gtk.Box(spacing=8)
        self.status = Gtk.Label(xalign=0, wrap=True)
        self.status.set_hexpand(True)
        actions.append(self.status)
        find = Gtk.Button(label="Find")
        find.add_css_class("suggested-action")
        find.connect("clicked", lambda *_: self.find())
        actions.append(find)
        self.print_button = Gtk.Button(label="Print…")
        self.print_button.set_sensitive(False)
        self.print_button.connect("clicked", lambda *_: self.print_report())
        actions.append(self.print_button)
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

    def chosen_accounts(self) -> tuple[str, ...]:
        return tuple(handle for handle, check in self.account_checks.items() if check.get_active())

    def choose_accounts(self, handles) -> None:
        wanted = set(handles)
        for handle, check in self.account_checks.items():
            check.set_active(handle in wanted)
        self._show_chosen()

    def _show_chosen(self) -> None:
        chosen = [
            check.get_label() or "" for check in self.account_checks.values() if check.get_active()
        ]
        self.accounts_button.set_label(
            "All accounts"
            if not chosen
            else chosen[0]
            if len(chosen) == 1
            else f"{len(chosen)} accounts"
        )

    def subtotals(self) -> tuple[str, ...]:
        """The subtotals in the order chosen, each once."""
        first = GROUPINGS[self.group_by.get_selected()]
        if first == "none":
            return ()
        found = [first]
        for then in self.then_by:
            index = then.get_selected()
            if index == 0:
                break
            name = GROUPINGS[index]
            if name in found:
                raise ValueError(f"choose {_GROUP_LABELS[name].lower()} as a subtotal only once")
            found.append(name)
        return tuple(found)

    def query(self) -> TransactionQuery:
        """The criteria as typed; ``ValueError`` naming one that cannot be read."""
        accounts = self.chosen_accounts()
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
            subtotals=self.subtotals(),
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
        self.print_button.set_sensitive(True)
        return result

    def layout(self):
        """The last result as a printable report (#408)."""
        from ...plugins.export.report_layout import transaction_detail_layout

        assert self.result is not None
        return transaction_detail_layout(self.result, self.title)

    def print_report(self) -> None:
        if self.result is not None:
            printing.print_document(self, self.layout())

    def _on_activate(self, _view, position: int) -> None:
        line = self.lines[position] if position < len(self.lines) else None
        if line is not None and line.handle and self.open_account is not None:
            self.open_account(line.handle)
