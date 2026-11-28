"""The Dashboard's settings: its horizons, sections, groups, and columns (#381).

They are the **Dashboard** tab of Edit → Preferences (:class:`DashboardSettings` is
that tab's content); all configuration lives in Preferences, so the Dashboard's
toolbar and menus no longer have a Configure Dashboard command of their own.

Groups are edited here rather than guessed, because only the household knows that
"Home Easton" means one asset account paired with one mortgage. A property group
is the one that needs saying explicitly: pairing a value with its loan is what
turns two unrelated balances into equity and a loan-to-value ratio.

The horizons, the sections shown, and the groups are the book's and are saved
together through ``save_dashboard_config``. Column choices are the user's own,
remembered in their view settings as each table's column menu always did, so they
apply at once and are not part of Save.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ...api import ApiError
from ...api.local import book_server
from ...gen.engine.dashboard import (
    GROUP_KINDS,
    SECTIONS,
)
from ...gen.services import DashboardGroupInput, SaveDashboardConfig
from ...gen.services.dashboard_config import (
    EMERGENCY_MONTHS_RANGE,
    LIQUIDITY_DAYS_RANGE,
    DashboardGroup,
)
from ..gi_setup import Gtk, Pango
from ..remote_book import OpenBook
from ..widgets.choice import bounded_dropdown

__all__ = ["DashboardSettings", "Unadded"]

KINDS = list(GROUP_KINDS)

_EXPLANATION = {
    "liquid": "Spendable now; the balance the liquidity check is made against",
    "retirement": "Long-term holdings, excluded from the liquidity check",
    "asset": "Anything else owned",
    "property": "A value and its loans together, reported as equity and LTV",
    "liability": "Owed",
}


@dataclass(frozen=True)
class Unadded:
    """An account checked to add to a group but never added with **Add checked**."""

    group: str
    account: str
    row: dict


class DashboardSettings(Gtk.Box):
    """Every Dashboard setting in one place, a page per kind: the content of the
    Preferences window's Dashboard tab.

    ``tables`` are the Dashboard's tables as ``(title, view id, column view)``:
    their columns are shown and hidden here and remembered in ``settings``. It
    reads the book's configuration when it is made, so it shows each account's
    group as stored now. :meth:`save` stores the rest; the window holding it saves
    and closes.
    """

    def __init__(
        self,
        db: OpenBook,
        tables: Sequence[tuple[str, str, Gtk.ColumnView]] = (),
        settings=None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.db = db
        self.settings = settings
        self.config = book_server(db).dashboard_settings()

        records = sorted(
            (record for record in book_server(db).accounts() if not record.account.is_root),
            key=lambda record: record.full_name,
        )
        self.accounts = [record.account for record in records]
        self.names = {
            record.account.handle: record.full_name or record.account.name for record in records
        }
        self.rows: list[dict] = []
        box = self

        self.notebook = Gtk.Notebook()
        self.notebook.set_vexpand(True)
        box.append(self.notebook)
        self.notebook.append_page(self._general_page(), Gtk.Label(label="Horizons and items"))
        self.notebook.append_page(self._groups_page(), Gtk.Label(label="Groups"))
        self.column_checks: dict[str, list[Gtk.CheckButton]] = {}
        if tables:
            self.notebook.append_page(self._columns_page(tables), Gtk.Label(label="Columns"))

        self.status = Gtk.Label(xalign=0, wrap=True)
        self.status.add_css_class("negative")
        box.append(self.status)

        for group in self.config.groups:
            self.add_group(group)

    # ------------------------------------------------------------------ pages

    @staticmethod
    def _page() -> Gtk.Box:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for side in ("top", "bottom", "start", "end"):
            getattr(page, f"set_margin_{side}")(12)
        return page

    @staticmethod
    def _heading(text: str) -> Gtk.Label:
        label = Gtk.Label(label=text, xalign=0)
        label.add_css_class("total-row")
        return label

    def _general_page(self) -> Gtk.Widget:
        page = self._page()
        page.append(self._heading("Horizons"))
        grid = Gtk.Grid(column_spacing=8, row_spacing=6)
        self.liquidity_spin = Gtk.SpinButton.new_with_range(*LIQUIDITY_DAYS_RANGE, 1)
        self.liquidity_spin.set_value(self.config.liquidity_days)
        self.liquidity_spin.set_tooltip_text("Days of bills that must be covered by cash on hand")
        self.emergency_spin = Gtk.SpinButton.new_with_range(*EMERGENCY_MONTHS_RANGE, 1)
        self.emergency_spin.set_value(self.config.emergency_months)
        self.emergency_spin.set_tooltip_text("Months of outgoings the emergency fund should cover")
        for row, (caption, spin, unit) in enumerate(
            (
                ("Liquid for", self.liquidity_spin, "days of bills"),
                ("Emergency fund covers", self.emergency_spin, "months of outgoings"),
            )
        ):
            grid.attach(Gtk.Label(label=caption, xalign=0), 0, row, 1, 1)
            grid.attach(spin, 1, row, 1, 1)
            grid.attach(Gtk.Label(label=unit, xalign=0), 2, row, 1, 1)
        page.append(grid)

        # Every item the Dashboard can show is a dashboard breadlet: its own items
        # first, in the order shown, and any others beside them, so the page stays
        # short enough for a laptop screen.
        columns = Gtk.Grid(column_spacing=32, row_spacing=4)
        columns.attach(self._heading("Shown on the Dashboard"), 0, 0, 1, 1)
        self.section_checks: dict[str, Gtk.CheckButton] = {}
        own = [item for item in self.config.breadlets if item.id in SECTIONS]
        others = [item for item in self.config.breadlets if item.id not in SECTIONS]
        if others:
            columns.attach(self._heading("More breadlets"), 2, 0, 1, 1)
        # The Dashboard's own items fill two columns; any others take a third.
        half = (len(own) + 1) // 2
        placed = [(index // half, index % half + 1, item) for index, item in enumerate(own)]
        placed += [(2, row, item) for row, item in enumerate(others, start=1)]
        for column, row, item in placed:
            check = Gtk.CheckButton(label=item.name, valign=Gtk.Align.START)
            check.set_tooltip_text(item.description)
            check.set_active(item.shown)
            self.section_checks[item.key] = check
            columns.attach(check, column, row, 1, 1)
        page.append(columns)
        page.append(
            Gtk.Label(
                label=(
                    "Pending reimbursables lists each reimbursable expense still owed, as "
                    "Pending bills lists bills; it is off until you turn it on."
                ),
                xalign=0,
                wrap=True,
            )
        )
        return page

    def _groups_page(self) -> Gtk.Widget:
        page = self._page()
        page.append(
            Gtk.Label(
                label=(
                    "Use colon-separated paths such as Investments:Plan A to create "
                    "totalled headings. Choosing a parent account includes its "
                    "subaccounts once. A property group pairs value with its loans."
                ),
                xalign=0,
                wrap=True,
            )
        )
        self.group_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        scroller = Gtk.ScrolledWindow(child=self.group_box)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        page.append(scroller)

        controls = Gtk.Box(spacing=8)
        add = Gtk.Button(label="Add group", icon_name="list-add-symbolic")
        add.connect("clicked", lambda *_: self.add_group())
        controls.append(add)
        page.append(controls)
        return page

    def _columns_page(self, tables: Sequence[tuple[str, str, Gtk.ColumnView]]) -> Gtk.Widget:
        page = self._page()
        page.append(
            Gtk.Label(
                label=(
                    "Columns are your own choice, remembered for you on this computer "
                    "rather than in the book, and change as soon as you tick them."
                ),
                xalign=0,
                wrap=True,
            )
        )
        for title, view_id, column_view in tables:
            page.append(self._heading(title))
            flow = Gtk.FlowBox()
            flow.set_selection_mode(Gtk.SelectionMode.NONE)
            flow.set_max_children_per_line(4)
            columns = column_view.get_columns()
            checks: list[Gtk.CheckButton] = []
            for index in range(columns.get_n_items()):
                column = columns.get_item(index)
                check = Gtk.CheckButton(label=column.get_title() or f"Column {index + 1}")
                check.set_active(column.get_visible())
                # The first column names the row; hiding it leaves nothing to read.
                check.set_sensitive(index != 0)
                check.connect("toggled", self._on_column_toggled, view_id, column_view, column)
                checks.append(check)
                flow.append(check)
            self.column_checks[view_id] = checks
            page.append(flow)
        scroller = Gtk.ScrolledWindow(child=page)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        return scroller

    def _on_column_toggled(self, button, view_id: str, column_view, column) -> None:
        column.set_visible(button.get_active())
        if self.settings is None:
            return
        # The same key and form as the table's column menu (``column_menu``).
        columns = column_view.get_columns()
        hidden = [
            columns.get_item(i).get_title()
            for i in range(columns.get_n_items())
            if not columns.get_item(i).get_visible()
        ]
        self.settings.set(f"columns:{view_id}", "hidden", hidden)
        self.settings.save()

    # ------------------------------------------------------------------- rows

    def add_group(self, group: DashboardGroup | None = None) -> dict:
        frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        frame.add_css_class("card")

        header = Gtk.Box(spacing=8)
        name = Gtk.Entry(placeholder_text="Investments:Plan A")
        name.set_hexpand(True)
        name.update_property([Gtk.AccessibleProperty.LABEL], ["Group name"])
        if group:
            name.set_text(group.name)
        header.append(name)

        kind = bounded_dropdown([f"{k} - {_EXPLANATION[k]}" for k in KINDS])
        kind.set_selected(KINDS.index(group.kind) if group else 0)
        header.append(kind)

        # The groups show on the Dashboard in this order.
        up = Gtk.Button(icon_name="go-up-symbolic")
        up.set_tooltip_text("Move this group up")
        header.append(up)
        down = Gtk.Button(icon_name="go-down-symbolic")
        down.set_tooltip_text("Move this group down")
        header.append(down)
        remove = Gtk.Button(icon_name="list-remove-symbolic")
        remove.set_tooltip_text("Remove this group")
        header.append(remove)
        frame.append(header)

        # The group's accounts, each with a check to remove several at once and its
        # own remove button.
        members = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        frame.append(members)
        drop_checked = Gtk.Button(label="Remove checked")
        drop_checked.set_halign(Gtk.Align.START)
        drop_checked.set_sensitive(False)
        frame.append(drop_checked)

        # Accounts to add: a searchable checklist, closed until wanted, so the whole
        # chart is never listed under every group. Check several (or every account
        # the search shows, then uncheck a few) and add them together.
        adder = Gtk.Expander(label="Add accounts…")
        chooser = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        search = Gtk.SearchEntry(placeholder_text="Search accounts")
        search.update_property([Gtk.AccessibleProperty.LABEL], ["Search accounts to add"])
        chooser.append(search)
        candidates = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        scroller = Gtk.ScrolledWindow(child=candidates)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_min_content_height(160)
        scroller.set_max_content_height(240)
        scroller.set_propagate_natural_height(True)
        chooser.append(scroller)
        buttons = Gtk.Box(spacing=8)
        check_shown = Gtk.Button(label="Check all shown")
        clear = Gtk.Button(label="Clear checks")
        add = Gtk.Button(label="Add checked")
        for button in (check_shown, clear, add):
            buttons.append(button)
        chooser.append(buttons)
        adder.set_child(chooser)
        frame.append(adder)

        row = {
            "frame": frame,
            "name": name,
            "kind": kind,
            "accounts": [],
            "members": members,
            "member_checks": {},
            "drop_checked": drop_checked,
            "adder": adder,
            "search": search,
            "candidates": candidates,
            "choices": {},
            "add": add,
            "up": up,
            "down": down,
        }
        self.rows.append(row)
        self.group_box.append(frame)
        up.connect("clicked", lambda *_: self.move_group(row, -1))
        down.connect("clicked", lambda *_: self.move_group(row, 1))
        remove.connect("clicked", lambda *_: self._remove(row))
        drop_checked.connect("clicked", lambda *_: self.remove_checked(row))
        adder.connect("notify::expanded", lambda *_: self._fill_choices(row))
        search.connect("search-changed", lambda *_: self._filter_choices(row))
        check_shown.connect("clicked", lambda *_: self.check_shown(row, True))
        clear.connect("clicked", lambda *_: self.check_shown(row, False, shown_only=False))
        add.connect("clicked", lambda *_: self._add_picked(row))
        for handle in group.accounts if group else ():
            self.add_account(row, handle)
        self._update_moves()
        return row

    # ------------------------------------------------------------- the order

    def move_group(self, row: dict, step: int) -> None:
        """Move a group up (-1) or down (1); the Dashboard lists them in this order."""
        index = self.rows.index(row)
        target = index + step
        if not 0 <= target < len(self.rows):
            return
        self.rows.insert(target, self.rows.pop(index))
        before = self.rows[target - 1]["frame"] if target else None
        self.group_box.reorder_child_after(row["frame"], before)
        self._update_moves()

    def _update_moves(self) -> None:
        for index, row in enumerate(self.rows):
            row["up"].set_sensitive(index > 0)
            row["down"].set_sensitive(index < len(self.rows) - 1)

    # ---------------------------------------------------------- the accounts

    def _fill_choices(self, row: dict) -> None:
        """The checklist is built the first time it is opened."""
        if row["choices"] or not row["adder"].get_expanded():
            return
        for account in self.accounts:
            check = Gtk.CheckButton(label=self.names[account.handle])
            check.connect("toggled", lambda *_: self._update_add(row))
            row["choices"][account.handle] = check
            row["candidates"].append(check)
        self._filter_choices(row)

    def _filter_choices(self, row: dict) -> None:
        """Show the accounts matching the search that are not in the group yet."""
        text = row["search"].get_text().strip().casefold()
        for handle, check in row["choices"].items():
            check.set_visible(
                handle not in row["accounts"]
                and (not text or text in self.names[handle].casefold())
            )
        self._update_add(row)

    def choose(self, row: dict, handle: str, chosen: bool = True) -> None:
        """Check (or uncheck) ``handle`` in the group's list of accounts to add."""
        if not row["choices"]:
            row["adder"].set_expanded(True)
            self._fill_choices(row)
        check = row["choices"].get(handle)
        if check is not None:
            check.set_active(chosen)

    def check_shown(self, row: dict, chosen: bool, *, shown_only: bool = True) -> None:
        """Check or uncheck every account the search shows (or every one)."""
        for check in row["choices"].values():
            if not shown_only or check.get_visible():
                check.set_active(chosen and check.get_visible())

    def checked(self, row: dict) -> list[str]:
        """The accounts checked to add that are not in the group yet, in chart order."""
        return [
            handle
            for handle, check in row["choices"].items()
            if check.get_active() and handle not in row["accounts"]
        ]

    def _update_add(self, row: dict) -> None:
        row["add"].set_sensitive(bool(self.checked(row)))

    def _add_picked(self, row: dict) -> None:
        """Add every checked account to the group, then clear the checks."""
        for handle in self.checked(row):
            self.add_account(row, handle)
        for check in row["choices"].values():
            check.set_active(False)
        self._filter_choices(row)

    def add_account(self, row: dict, handle: str) -> None:
        """Put ``handle`` in ``row``'s group, taking it out of any other group."""
        if handle in row["accounts"] or handle not in self.names:
            return
        self.status.set_text("")
        for other in self.rows:
            if other is not row and handle in other["accounts"]:
                self.remove_account(other, handle)
                group = other["name"].get_text().strip() or "another group"
                self.status.set_text(f"Moved {self.names[handle]} from {group}.")
        row["accounts"].append(handle)
        line = Gtk.Box(spacing=8)
        check = Gtk.CheckButton()
        check.update_property([Gtk.AccessibleProperty.LABEL], [f"Check {self.names[handle]}"])
        check.connect("toggled", lambda *_: self._update_drop(row))
        line.append(check)
        row["member_checks"][handle] = check
        label = Gtk.Label(label=self.names[handle], xalign=0)
        label.set_hexpand(True)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        line.append(label)
        drop = Gtk.Button(icon_name="window-close-symbolic")
        drop.add_css_class("flat")
        drop.set_tooltip_text(f"Remove {self.names[handle]} from this group")
        drop.connect("clicked", lambda *_: self.remove_account(row, handle))
        line.append(drop)
        line.handle = handle
        row["members"].append(line)
        self._filter_choices(row)

    def remove_account(self, row: dict, handle: str) -> None:
        if handle not in row["accounts"]:
            return
        row["accounts"].remove(handle)
        row["member_checks"].pop(handle, None)
        child = row["members"].get_first_child()
        while child is not None:
            if getattr(child, "handle", None) == handle:
                row["members"].remove(child)
                break
            child = child.get_next_sibling()
        self._filter_choices(row)
        self._update_drop(row)

    def remove_checked(self, row: dict) -> None:
        """Take every checked account out of the group."""
        for handle, check in list(row["member_checks"].items()):
            if check.get_active():
                self.remove_account(row, handle)

    def _update_drop(self, row: dict) -> None:
        row["drop_checked"].set_sensitive(
            any(check.get_active() for check in row["member_checks"].values())
        )

    def _remove(self, row: dict) -> None:
        self.rows.remove(row)
        self.group_box.remove(row["frame"])
        self._update_moves()

    # ----------------------------------------------------------------- saving

    def build(self) -> tuple[DashboardGroupInput, ...]:
        """The groups as edited; a row without a name is left out."""
        groups = []
        for row in self.rows:
            name = row["name"].get_text().strip()
            if not name:
                continue
            groups.append(
                DashboardGroupInput(name, KINDS[row["kind"].get_selected()], tuple(row["accounts"]))
            )
        return tuple(groups)

    def request(self) -> SaveDashboardConfig:
        """Everything the book keeps: groups, horizons, and sections."""
        return SaveDashboardConfig(
            groups=self.build(),
            liquidity_days=int(self.liquidity_spin.get_value()),
            emergency_months=int(self.emergency_spin.get_value()),
            sections={name: check.get_active() for name, check in self.section_checks.items()},
        )

    def unadded(self) -> list[Unadded]:
        """Accounts checked to add to a group but not yet added with **Add checked**.

        Saving or closing would otherwise lose them without a word, since only
        **Add checked** puts an account in the group.
        """
        found: list[Unadded] = []
        for row in self.rows:
            group = row["name"].get_text().strip() or "an unnamed group"
            found.extend(Unadded(group, self.names[handle], row) for handle in self.checked(row))
        return found

    def add_unadded(self) -> None:
        """Add every account checked but not yet added, as **Add checked** would."""
        for row in self.rows:
            if self.checked(row):
                self._add_picked(row)

    def save(self) -> bool:
        """Store the settings; False, with the reason shown, if they were refused."""
        try:
            book_server(self.db).save_dashboard_settings(self.request())
        except ApiError as error:
            self.status.set_text(str(error))
            return False
        self.config = book_server(self.db).dashboard_settings()
        return True
