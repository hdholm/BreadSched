"""List and edit one saved scenario's retirement drawdowns."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal, InvalidOperation

from ...gen.db.sqlite import DbSQLite
from ...gen.lib import Drawdown, Money, Scenario
from ...gen.services import SaveDrawdown, drawdown_accounts, remove_drawdown, save_drawdown
from ...presentation import service_error_message
from ..gi_setup import Gtk
from ..widgets.bounded import BoundedWindow, scroll_body
from ..widgets.choice import bounded_dropdown

__all__ = ["DrawdownEditDialog", "DrawdownsDialog", "drawdown_summary"]


def _percent(rate: Decimal) -> str:
    return f"{(rate * 100).normalize():f}"


def drawdown_summary(db: DbSQLite, drawdown: Drawdown) -> str:
    """One line naming the accounts, dates, and how much a drawdown withdraws."""
    if drawdown.annual_rate is not None:
        amount = f"{_percent(drawdown.annual_rate)}% of the balance a year"
    else:
        amount = f"{drawdown.annual_amount} a year"
        if drawdown.escalate:
            amount += ", rising with inflation"
    through = drawdown.end.isoformat() if drawdown.end is not None else "onward"
    return (
        f"{db.full_name(drawdown.account)} → {db.full_name(drawdown.into)}, "
        f"{drawdown.start.isoformat()} — {through}: {amount}"
    )


class DrawdownsDialog(BoundedWindow):
    """A saved scenario's drawdowns, with add, edit, and remove."""

    def __init__(
        self,
        parent: Gtk.Window,
        db: DbSQLite,
        scenario: Scenario,
        saved_callback: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(
            title=f"Retirement drawdowns — {scenario.name}", transient_for=parent, modal=True
        )
        self.db = db
        self.scenario = scenario
        self.saved_callback = saved_callback
        self.set_default_size(720, 360)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(18)
        self.set_child(box)
        box.append(
            Gtk.Label(
                label=(
                    "A drawdown withdraws from a retirement or investment account into a "
                    "cash account every month from its start date. It exists only in this "
                    "scenario's projection: nothing is posted and the Plan is unchanged."
                ),
                xalign=0,
                wrap=True,
            )
        )
        row = Gtk.Box(spacing=8)
        self.picker = bounded_dropdown()
        self.picker.set_hexpand(True)
        row.append(self.picker)
        self.add_button = Gtk.Button(label="Add…")
        self.add_button.connect("clicked", self._on_add)
        row.append(self.add_button)
        self.edit_button = Gtk.Button(label="Edit…")
        self.edit_button.connect("clicked", self._on_edit)
        row.append(self.edit_button)
        self.remove_button = Gtk.Button(label="Remove")
        self.remove_button.add_css_class("destructive-action")
        self.remove_button.connect("clicked", self._on_remove)
        row.append(self.remove_button)
        box.append(row)

        self.summary = Gtk.Label(xalign=0, wrap=True)
        self.summary.add_css_class("dim")
        box.append(self.summary)
        close = Gtk.Button(label="Close", halign=Gtk.Align.END)
        close.connect("clicked", lambda *_: self.close())
        box.append(close)
        self._reload()

    def _reload(self, selected: int = 0) -> None:
        model = Gtk.StringList()
        for drawdown in self.scenario.drawdowns:
            model.append(drawdown_summary(self.db, drawdown))
        self.picker.set_model(model)
        count = len(self.scenario.drawdowns)
        if count:
            self.picker.set_selected(min(selected, count - 1))
        self.edit_button.set_sensitive(bool(count))
        self.remove_button.set_sensitive(bool(count))
        sources, targets = drawdown_accounts(self.db)
        self.add_button.set_sensitive(bool(sources and targets))
        if not (sources and targets):
            self.summary.set_text(
                "A drawdown needs a retirement or investment account and a cash account."
            )
        else:
            self.summary.set_text(f"{count} drawdown(s).")

    def _selected(self) -> Drawdown | None:
        index = self.picker.get_selected()
        drawdowns = self.scenario.drawdowns
        return drawdowns[index] if index < len(drawdowns) else None

    def _on_add(self, _button) -> None:
        DrawdownEditDialog(self, self.db, self.scenario, None, self._after_commit).present()

    def _on_edit(self, _button) -> None:
        drawdown = self._selected()
        if drawdown is not None:
            DrawdownEditDialog(self, self.db, self.scenario, drawdown, self._after_commit).present()

    def _on_remove(self, _button) -> None:
        drawdown = self._selected()
        if drawdown is None:
            return
        result = remove_drawdown(self.db, self.scenario.handle, drawdown.handle)
        if result.ok:
            self._after_commit(None)
        else:
            self.summary.set_text(service_error_message(result.errors[0]))

    def _after_commit(self, handle: str | None) -> None:
        saved = self.db.get_scenario(self.scenario.handle)
        if saved is not None:
            self.scenario = saved
        handles = [item.handle for item in self.scenario.drawdowns]
        self._reload(handles.index(handle) if handle in handles else 0)
        if self.saved_callback is not None:
            self.saved_callback(self.scenario.handle)


class DrawdownEditDialog(BoundedWindow):
    """Add or change one drawdown; the service validates and saves it."""

    def __init__(
        self,
        parent: Gtk.Window,
        db: DbSQLite,
        scenario: Scenario,
        drawdown: Drawdown | None,
        callback: Callable[[str], None],
    ) -> None:
        super().__init__(
            title="Edit drawdown" if drawdown is not None else "Add drawdown",
            transient_for=parent,
            modal=True,
        )
        self.db = db
        self.scenario = scenario
        self.drawdown = drawdown
        self.callback = callback
        self.set_default_size(560, -1)
        sources, targets = drawdown_accounts(db)
        self._sources = sorted(sources, key=lambda handle: db.full_name(handle).casefold())
        self._targets = sorted(targets, key=lambda handle: db.full_name(handle).casefold())

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(18)
        self.set_child(box)
        grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        box.append(grid)

        grid.attach(Gtk.Label(label="Withdraw from", xalign=0), 0, 0, 1, 1)
        self.account_picker = self._account_dropdown(
            self._sources, drawdown.account if drawdown else None
        )
        grid.attach(self.account_picker, 1, 0, 1, 1)
        grid.attach(Gtk.Label(label="Pay into", xalign=0), 0, 1, 1, 1)
        self.into_picker = self._account_dropdown(
            self._targets, drawdown.into if drawdown else None
        )
        grid.attach(self.into_picker, 1, 1, 1, 1)
        grid.attach(Gtk.Label(label="Start (YYYY-MM-DD)", xalign=0), 0, 2, 1, 1)
        self.start_entry = Gtk.Entry()
        self.start_entry.set_text(
            drawdown.start.isoformat() if drawdown else scenario.start.isoformat()
        )
        grid.attach(self.start_entry, 1, 2, 1, 1)
        grid.attach(Gtk.Label(label="Through (blank = onward)", xalign=0), 0, 3, 1, 1)
        self.end_entry = Gtk.Entry()
        if drawdown is not None and drawdown.end is not None:
            self.end_entry.set_text(drawdown.end.isoformat())
        grid.attach(self.end_entry, 1, 3, 1, 1)

        by_rate = drawdown is not None and drawdown.annual_rate is not None
        self.amount_choice = Gtk.CheckButton(label="A fixed yearly amount")
        self.rate_choice = Gtk.CheckButton(label="A yearly share of the balance (%)")
        self.rate_choice.set_group(self.amount_choice)
        (self.rate_choice if by_rate else self.amount_choice).set_active(True)
        grid.attach(self.amount_choice, 0, 4, 1, 1)
        self.amount_entry = Gtk.Entry(placeholder_text="e.g. 40000")
        if drawdown is not None and drawdown.annual_amount is not None:
            self.amount_entry.set_text(str(drawdown.annual_amount.to_decimal()))
        grid.attach(self.amount_entry, 1, 4, 1, 1)
        self.escalate = Gtk.CheckButton(label="Rise with expense inflation each year")
        self.escalate.set_active(drawdown.escalate if drawdown is not None else True)
        grid.attach(self.escalate, 1, 5, 1, 1)
        grid.attach(self.rate_choice, 0, 6, 1, 1)
        self.rate_entry = Gtk.Entry(placeholder_text="e.g. 4")
        if by_rate:
            assert drawdown is not None and drawdown.annual_rate is not None
            self.rate_entry.set_text(_percent(drawdown.annual_rate))
        grid.attach(self.rate_entry, 1, 6, 1, 1)
        self.amount_choice.connect("toggled", self._sync_method)
        self._sync_method()

        note = Gtk.Label(
            label=(
                "Withdrawals happen monthly on the start date's day and never take more "
                "than the account holds."
            ),
            xalign=0,
            wrap=True,
        )
        note.add_css_class("dim")
        box.append(note)
        self.status = Gtk.Label(xalign=0, wrap=True)
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
        scroll_body(self)

    def _account_dropdown(self, handles: list[str], selected: str | None) -> Gtk.DropDown:
        model = Gtk.StringList()
        for handle in handles:
            model.append(self.db.full_name(handle))
        picker = bounded_dropdown()
        picker.set_model(model)
        picker.set_hexpand(True)
        if selected in handles:
            picker.set_selected(handles.index(selected))
        return picker

    def _sync_method(self, *_args) -> None:
        fixed = self.amount_choice.get_active()
        self.amount_entry.set_sensitive(fixed)
        self.escalate.set_sensitive(fixed)
        self.rate_entry.set_sensitive(not fixed)

    def _error(self, message: str) -> None:
        self.status.set_text(message)
        self.status.add_css_class("negative")

    @staticmethod
    def _pick(picker: Gtk.DropDown, handles: list[str]) -> str:
        index = picker.get_selected()
        return handles[index] if index < len(handles) else ""

    def _on_save(self, _button) -> None:
        fixed = self.amount_choice.get_active()
        try:
            start = date.fromisoformat(self.start_entry.get_text().strip())
            end_text = self.end_entry.get_text().strip()
            end = date.fromisoformat(end_text) if end_text else None
            amount_text = self.amount_entry.get_text().strip()
            rate_text = self.rate_entry.get_text().strip().rstrip("%").strip()
            amount = Money(amount_text) if fixed and amount_text else None
            rate = Decimal(rate_text) / 100 if not fixed and rate_text else None
        except (ValueError, InvalidOperation) as exc:
            self._error(f"Check the dates and amounts: {exc}")
            return
        result = save_drawdown(
            self.db,
            SaveDrawdown(
                self.scenario.handle,
                self._pick(self.account_picker, self._sources),
                self._pick(self.into_picker, self._targets),
                start,
                annual_amount=amount,
                annual_rate=rate.normalize() if rate is not None else None,
                end=end,
                escalate=self.escalate.get_active(),
                handle=self.drawdown.handle if self.drawdown is not None else None,
            ),
        )
        if not result.ok or result.value is None:
            self._error(service_error_message(result.errors[0]))
            return
        self.callback(result.value.handle)
        self.close()
