"""Choose which recurring flows a budget counts.

Membership is what lets two budgets genuinely differ rather than being copies of
one another: a "tighter" plan is usually the same book with three or four
subscriptions taken out of it.

An empty membership list means "every budget", which is how a schedule created
before budgets existed keeps counting. Turning that implicit answer into an
explicit one has to enumerate the other budgets first, or removing a flow from one
budget would quietly remove it from all of them — so the dialog always writes the
full list rather than a difference.
"""

from __future__ import annotations

from ...gen.db.sqlite import DbSQLite
from ...gen.lib.budget import Budget
from ..gi_setup import Gtk

__all__ = ["MembershipDialog"]


class MembershipDialog(Gtk.Window):
    """A checkbox per scheduled flow, for one budget."""

    def __init__(self, parent: Gtk.Window | None, db: DbSQLite, budget: Budget) -> None:
        super().__init__(
            title=f"Flows in {budget.name}", transient_for=parent, modal=True
        )
        self.db = db
        self.budget = budget
        self.schedules = list(db.iter_scheduled())
        self.checks: list[Gtk.CheckButton] = []
        self.set_default_size(620, 520)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(18)
        self.set_child(box)

        box.append(
            Gtk.Label(
                label=(
                    f"Which recurring flows does {budget.name!r} include? "
                    "Unticking one leaves it in every other budget."
                ),
                xalign=0,
                wrap=True,
            )
        )

        bulk = Gtk.Box(spacing=8)
        for label, value in (("Include all", True), ("Exclude all", False)):
            button = Gtk.Button(label=label)
            button.connect("clicked", lambda _b, v=value: self.set_all(v))
            bulk.append(button)
        box.append(bulk)

        grid = Gtk.Grid(column_spacing=14, row_spacing=4)
        for position, heading in enumerate(("In", "Flow", "Kind", "Frequency", "Amount")):
            label = Gtk.Label(label=heading, xalign=1 if position == 4 else 0)
            label.add_css_class("summary-label")
            grid.attach(label, position, 0, 1, 1)

        for index, sched in enumerate(self.schedules, start=1):
            check = Gtk.CheckButton()
            check.set_active(sched.in_budget(budget.handle))
            self.checks.append(check)
            grid.attach(check, 0, index, 1, 1)
            grid.attach(Gtk.Label(label=sched.name, xalign=0), 1, index, 1, 1)
            grid.attach(
                Gtk.Label(
                    label="Estimate" if sched.placeholder else "Commitment", xalign=0
                ),
                2, index, 1, 1,
            )
            grid.attach(
                Gtk.Label(label=sched.recurrence.describe(), xalign=0), 3, index, 1, 1
            )
            amount = Gtk.Label(label=sched.amount().format(), xalign=1)
            amount.add_css_class("numeric")
            grid.attach(amount, 4, index, 1, 1)

        scroller = Gtk.ScrolledWindow(child=grid)
        scroller.set_vexpand(True)
        box.append(scroller)

        self.status = Gtk.Label(xalign=0)
        self.status.add_css_class("dim")
        box.append(self.status)
        self._update_status()

        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        buttons.append(cancel)
        save = Gtk.Button(label="Save")
        save.add_css_class("suggested-action")
        save.connect("clicked", self._on_save)
        buttons.append(save)
        box.append(buttons)

    def set_all(self, included: bool) -> None:
        for check in self.checks:
            check.set_active(included)
        self._update_status()

    def _update_status(self) -> None:
        included = sum(1 for check in self.checks if check.get_active())
        self.status.set_text(
            f"{included} of {len(self.checks)} flow(s) included"
        )

    def apply(self) -> int:
        """Write the memberships. Returns how many flows are included."""
        handles = [budget.handle for budget in self.db.iter_budgets()]
        included = 0
        with self.db.transaction(f"Set the flows in {self.budget.name}") as txn:
            for sched, check in zip(self.schedules, self.checks, strict=False):
                if check.get_active():
                    sched.add_to_budget(self.budget.handle, handles)
                    included += 1
                else:
                    sched.remove_from_budget(self.budget.handle, handles)
                self.db.commit_scheduled(sched, txn)
        return included

    def _on_save(self, _button) -> None:
        self.apply()
        self.close()
