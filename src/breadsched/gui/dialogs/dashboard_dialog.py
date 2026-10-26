"""Configure the Dashboard: its horizons, sections, groups, and columns (#381).

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

from ...gen.db.sqlite import DbSQLite
from ...gen.engine.dashboard import (
    GROUP_KINDS,
    SECTIONS,
    DashboardConfig,
    GroupConfig,
    default_config,
)
from ...gen.services import DashboardGroupInput, SaveDashboardConfig, save_dashboard_config
from ...gen.services.dashboard_config import EMERGENCY_MONTHS_RANGE, LIQUIDITY_DAYS_RANGE
from ...presentation import service_error_message
from ..gi_setup import Gtk, Pango
from ..widgets.bounded import BoundedWindow
from ..widgets.choice import bounded_dropdown
from ..widgets.help import help_row

__all__ = ["DashboardDialog"]

KINDS = list(GROUP_KINDS)

_EXPLANATION = {
    "liquid": "Spendable now; the balance the liquidity check is made against",
    "retirement": "Long-term holdings, excluded from the liquidity check",
    "asset": "Anything else owned",
    "property": "A value and its loans together, reported as equity and LTV",
    "liability": "Owed",
}

#: The first entry of each group's account picker, which adds nothing.
_PICK = "Add an account…"


class DashboardDialog(BoundedWindow):
    """Every Dashboard setting in one place, a page per kind.

    ``tables`` are the Dashboard's tables as ``(title, view id, column view)``:
    their columns are shown and hidden here and remembered in ``settings``. It
    reads the book's configuration when it opens, so it shows each account's group
    as stored now.
    """

    def __init__(
        self,
        parent: Gtk.Window | None,
        db: DbSQLite,
        tables: Sequence[tuple[str, str, Gtk.ColumnView]] = (),
        settings=None,
    ) -> None:
        super().__init__(title="Configure Dashboard", transient_for=parent, modal=True)
        self.db = db
        self.settings = settings
        self.config = DashboardConfig.load(db)
        self.set_default_size(720, 580)

        self.accounts = sorted(
            (a for a in db.iter_accounts() if not a.is_root),
            key=db.full_name,
        )
        self.names = {a.handle: db.full_name(a) or a.name for a in self.accounts}
        self.rows: list[dict] = []

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(18)
        self.set_child(box)
        box.prepend(help_row("dashboard"))

        self.notebook = Gtk.Notebook()
        self.notebook.set_vexpand(True)
        box.append(self.notebook)
        self.notebook.append_page(self._general_page(), Gtk.Label(label="General"))
        self.notebook.append_page(self._groups_page(), Gtk.Label(label="Groups"))
        self.column_checks: dict[str, list[Gtk.CheckButton]] = {}
        if tables:
            self.notebook.append_page(self._columns_page(tables), Gtk.Label(label="Columns"))

        self.status = Gtk.Label(xalign=0, wrap=True)
        self.status.add_css_class("negative")
        box.append(self.status)

        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        buttons.append(cancel)
        save = Gtk.Button(label="Save")
        save.add_css_class("suggested-action")
        save.connect("clicked", self._on_save)
        buttons.append(save)
        box.append(buttons)

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

        page.append(self._heading("Sections"))
        self.section_checks: dict[str, Gtk.CheckButton] = {}
        for name, title in SECTIONS.items():
            check = Gtk.CheckButton(label=title)
            check.set_active(self.config.shows(name))
            self.section_checks[name] = check
            page.append(check)
        page.append(
            Gtk.Label(
                label=(
                    "The summary cards are always shown. Pending reimbursables lists "
                    "each reimbursable expense still owed, as Pending bills lists bills."
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
        reset = Gtk.Button(label="Suggest from accounts")
        reset.set_tooltip_text("Rebuild the groups from the chart of accounts")
        reset.connect("clicked", self._on_reset)
        controls.append(reset)
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

    def add_group(self, group: GroupConfig | None = None) -> dict:
        frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        frame.add_css_class("card")

        header = Gtk.Box(spacing=8)
        name = Gtk.Entry(placeholder_text="Investments:Plan A")
        name.set_hexpand(True)
        if group:
            name.set_text(group.name)
        header.append(name)

        kind = bounded_dropdown([f"{k} - {_EXPLANATION[k]}" for k in KINDS])
        kind.set_selected(KINDS.index(group.kind) if group else 0)
        header.append(kind)

        remove = Gtk.Button(icon_name="list-remove-symbolic")
        remove.set_tooltip_text("Remove this group")
        header.append(remove)
        frame.append(header)

        # The group's accounts, each with its own remove button, then a searchable
        # picker to add another: the whole chart is never listed under each group.
        members = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        frame.append(members)
        adder = Gtk.Box(spacing=8)
        picker = bounded_dropdown([_PICK, *(self.names[a.handle] for a in self.accounts)])
        picker.set_hexpand(True)
        adder.append(picker)
        add = Gtk.Button(label="Add")
        adder.append(add)
        frame.append(adder)

        row = {
            "frame": frame,
            "name": name,
            "kind": kind,
            "accounts": [],
            "members": members,
            "picker": picker,
        }
        self.rows.append(row)
        self.group_box.append(frame)
        remove.connect("clicked", lambda *_: self._remove(row))
        add.connect("clicked", lambda *_: self._add_picked(row))
        for handle in group.accounts if group else ():
            self.add_account(row, handle)
        return row

    def _add_picked(self, row: dict) -> None:
        index = row["picker"].get_selected()
        if index < 1:
            return
        self.add_account(row, self.accounts[index - 1].handle)
        row["picker"].set_selected(0)

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

    def remove_account(self, row: dict, handle: str) -> None:
        if handle not in row["accounts"]:
            return
        row["accounts"].remove(handle)
        child = row["members"].get_first_child()
        while child is not None:
            if getattr(child, "handle", None) == handle:
                row["members"].remove(child)
                break
            child = child.get_next_sibling()

    def _remove(self, row: dict) -> None:
        self.rows.remove(row)
        self.group_box.remove(row["frame"])

    def _on_reset(self, _button) -> None:
        for row in list(self.rows):
            self._remove(row)
        for group in default_config(self.db).groups:
            self.add_group(group)

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

    def save(self) -> bool:
        """Store the settings; False, with the reason shown, if they were refused."""
        result = save_dashboard_config(self.db, self.request())
        if not result.ok:
            self.status.set_text(service_error_message(result.errors[0]))
            return False
        assert result.value is not None
        self.config = result.value
        return True

    def _on_save(self, _button) -> None:
        if self.save():
            self.close()
