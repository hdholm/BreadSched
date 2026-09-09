"""Choose which accounts each dashboard group contains.

Groups are edited here rather than guessed, because only the household knows that
"Home Easton" means one asset account paired with one mortgage. A property group
is the one that needs saying explicitly: pairing a value with its loan is what
turns two unrelated balances into equity and a loan-to-value ratio.
"""

from __future__ import annotations

from ...gen.db.sqlite import DbSQLite
from ...gen.engine.dashboard import DashboardConfig, GroupConfig, default_config
from ..gi_setup import Gtk

__all__ = ["DashboardDialog"]

KINDS = ["liquid", "retirement", "asset", "property", "liability"]

_EXPLANATION = {
    "liquid": "Spendable now; the balance the liquidity check is made against",
    "retirement": "Long-term holdings, excluded from the liquidity check",
    "asset": "Anything else owned",
    "property": "A value and its loans together, reported as equity and LTV",
    "liability": "Owed",
}


class DashboardDialog(Gtk.Window):
    """Add, remove and populate the dashboard's groups."""

    def __init__(
        self,
        parent: Gtk.Window | None,
        db: DbSQLite,
        config: DashboardConfig | None = None,
    ) -> None:
        super().__init__(title="Dashboard groups", transient_for=parent, modal=True)
        self.db = db
        self.config = config or DashboardConfig.load(db)
        self.set_default_size(720, 560)

        self.accounts = sorted(
            (a for a in db.iter_accounts() if not a.is_root),
            key=db.full_name,
        )
        self.names = [db.full_name(a) for a in self.accounts]
        self.rows: list[dict] = []

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(18)
        self.set_child(box)

        box.append(
            Gtk.Label(
                label=(
                    "Each group is a line on the dashboard. A property group pairs "
                    "a value with its mortgage and reports equity and loan-to-value."
                ),
                xalign=0,
                wrap=True,
            )
        )

        self.group_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        scroller = Gtk.ScrolledWindow(child=self.group_box)
        scroller.set_vexpand(True)
        box.append(scroller)

        controls = Gtk.Box(spacing=8)
        add = Gtk.Button(label="Add group", icon_name="list-add-symbolic")
        add.connect("clicked", lambda *_: self.add_group())
        controls.append(add)
        reset = Gtk.Button(label="Suggest from accounts")
        reset.set_tooltip_text("Rebuild the groups from the chart of accounts")
        reset.connect("clicked", self._on_reset)
        controls.append(reset)
        box.append(controls)

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

    # ------------------------------------------------------------------- rows

    def add_group(self, group: GroupConfig | None = None) -> dict:
        frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        frame.add_css_class("card")

        header = Gtk.Box(spacing=8)
        name = Gtk.Entry(placeholder_text="Home Easton")
        name.set_hexpand(True)
        if group:
            name.set_text(group.name)
        header.append(name)

        kind = Gtk.DropDown.new_from_strings(
            [f"{k} - {_EXPLANATION[k]}" for k in KINDS]
        )
        kind.set_selected(KINDS.index(group.kind) if group else 0)
        header.append(kind)

        remove = Gtk.Button(icon_name="list-remove-symbolic")
        header.append(remove)
        frame.append(header)

        checks: list[Gtk.CheckButton] = []
        chooser = Gtk.FlowBox()
        chooser.set_selection_mode(Gtk.SelectionMode.NONE)
        chooser.set_max_children_per_line(3)
        chosen = set(group.accounts) if group else set()
        for account, label in zip(self.accounts, self.names, strict=False):
            check = Gtk.CheckButton(label=label or account.name)
            check.set_active(account.handle in chosen)
            checks.append(check)
            chooser.append(check)
        frame.append(chooser)

        row = {"frame": frame, "name": name, "kind": kind, "checks": checks}
        self.rows.append(row)
        self.group_box.append(frame)
        remove.connect("clicked", lambda *_: self._remove(row))
        return row

    def _remove(self, row: dict) -> None:
        self.rows.remove(row)
        self.group_box.remove(row["frame"])

    def _on_reset(self, _button) -> None:
        for row in list(self.rows):
            self._remove(row)
        for group in default_config(self.db).groups:
            self.add_group(group)

    # ----------------------------------------------------------------- saving

    def build(self) -> DashboardConfig:
        groups = []
        for row in self.rows:
            name = row["name"].get_text().strip()
            if not name:
                continue
            handles = [
                account.handle
                for account, check in zip(self.accounts, row["checks"], strict=False)
                if check.get_active()
            ]
            groups.append(GroupConfig(name, handles, KINDS[row["kind"].get_selected()]))
        self.config.groups = groups
        return self.config

    def _on_save(self, _button) -> None:
        self.build().save(self.db)
        self.close()
