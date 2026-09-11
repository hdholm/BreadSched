"""The dashboard: the view a book opens on.

Three bands, in the order the questions get asked. What is owned and owed, as
group totals with equity and loan-to-value where a property is paired with its
mortgage. Then the verdict — liquid, spoken for, available, and how many months
the emergency fund would last. Then the bills, sortable and scrollable, each
normalised to a monthly and annual figure so a quarterly fee and a fortnightly one
can be compared at a glance.

The two horizons are on the toolbar rather than buried in a preferences dialog,
because they are the numbers a household argues about: how much must stay liquid,
and how long the fund should last.
"""

from __future__ import annotations

from ...gen.engine import dashboard as engine
from ...gen.lib.money import Money
from ..gi_setup import Gio, Gtk, Pango
from ._base import BaseView, Row, column, column_menu, sorted_model

__all__ = ["DashboardView"]


class DashboardView(BaseView):
    """Balances, the liquidity verdict, and the bills."""

    WATCHES = ("database-changed", "transaction-add", "transaction-update",
               "transaction-delete", "scheduled-add", "scheduled-update",
               "scheduled-delete", "account-update")

    def __init__(self, manager) -> None:
        super().__init__(manager)
        self.config: engine.DashboardConfig | None = None
        self.board: engine.Dashboard | None = None
        self._updating = False
        self._build()

    # ------------------------------------------------------------------ layout

    def _build(self) -> None:
        bar = Gtk.Box(spacing=8)
        for side in ("top", "bottom", "start", "end"):
            getattr(bar, f"set_margin_{side}")(8)

        self.budget_label = Gtk.Label(xalign=0)
        self.budget_label.add_css_class("dim")

        title = Gtk.Label(label="Dashboard", xalign=0)
        title.add_css_class("category-title")
        title.set_hexpand(False)
        bar.append(title)
        self.budget_label.set_hexpand(True)
        bar.append(self.budget_label)

        bar.append(Gtk.Label(label="Liquid for"))
        self.liquidity_spin = Gtk.SpinButton.new_with_range(7, 365, 1)
        self.liquidity_spin.set_value(30)
        self.liquidity_spin.set_tooltip_text(
            "Days of bills that must be covered by cash on hand"
        )
        self.liquidity_spin.connect("value-changed", self._on_setting_changed)
        bar.append(self.liquidity_spin)
        bar.append(Gtk.Label(label="days"))

        bar.append(Gtk.Label(label="Emergency"))
        self.emergency_spin = Gtk.SpinButton.new_with_range(1, 36, 1)
        self.emergency_spin.set_value(6)
        self.emergency_spin.set_tooltip_text(
            "Months of outgoings the emergency fund should cover"
        )
        self.emergency_spin.connect("value-changed", self._on_setting_changed)
        bar.append(self.emergency_spin)
        bar.append(Gtk.Label(label="months"))

        configure = Gtk.Button(label="Accounts…")
        configure.set_tooltip_text("Choose which accounts each group contains")
        configure.connect("clicked", self._on_configure)
        bar.append(configure)
        claims = Gtk.Button(label="FSA claims…")
        claims.connect("clicked", self._on_fsa_claims)
        bar.append(claims)
        self._bar = bar
        self.append(bar)

        self.cards = Gtk.Box(spacing=14)
        for side in ("start", "end"):
            getattr(self.cards, f"set_margin_{side}")(12)
        self.append(self.cards)

        self.groups = Gtk.Grid(column_spacing=18, row_spacing=3)
        for side in ("start", "end", "top"):
            getattr(self.groups, f"set_margin_{side}")(12)
        self.append(self.groups)

        self.fsa_heading = Gtk.Label(label="FSA benefit years", xalign=0)
        self.fsa_heading.add_css_class("total-row")
        for side in ("start", "top"):
            getattr(self.fsa_heading, f"set_margin_{side}")(12)
        self.append(self.fsa_heading)
        self.fsa_grid = Gtk.Grid(column_spacing=18, row_spacing=3)
        for side in ("start", "end"):
            getattr(self.fsa_grid, f"set_margin_{side}")(12)
        self.append(self.fsa_grid)

        self.fsa_claim_heading = Gtk.Label(label="Open FSA claims", xalign=0)
        self.fsa_claim_heading.add_css_class("total-row")
        for side in ("start", "top"):
            getattr(self.fsa_claim_heading, f"set_margin_{side}")(12)
        self.append(self.fsa_claim_heading)
        self.fsa_claim_grid = Gtk.Grid(column_spacing=18, row_spacing=3)
        for side in ("start", "end"):
            getattr(self.fsa_claim_grid, f"set_margin_{side}")(12)
        self.append(self.fsa_claim_grid)

        heading = Gtk.Label(label="Pending bills", xalign=0)
        heading.add_css_class("total-row")
        for side in ("start", "top"):
            getattr(heading, f"set_margin_{side}")(12)
        self.append(heading)

        self.bills_view = Gtk.ColumnView()
        self.bills_view.set_show_row_separators(True)
        # A bill is a scheduled transaction seen from another angle; activating a
        # row goes to the thing itself rather than making the user find it.
        self.bills_view.connect("activate", self._on_bill_activated)
        self.bills_view.append_column(
            column("Bill", lambda b: b.name, expand=True, sort_key=lambda b: b.name)
        )
        self.bills_view.append_column(
            column(
                "Next due",
                lambda b: b.next_due.isoformat(),
                sort_key=lambda b: b.next_due,
            )
        )
        self.bills_view.append_column(
            column("Due in", self._due_in, sort_key=lambda b: b.days_until(self._today()))
        )
        # The schedule's own words, not a decimal month count: "every 3 months"
        # is what the user set, and 3.0000 reads as though it were the truth.
        self.bills_view.append_column(
            column("Frequency", lambda b: b.frequency,
                   sort_key=lambda b: b.cycle_months)
        )
        self.bills_view.append_column(
            column("Amount", lambda b: b.amount.format(),
                   sort_key=lambda b: b.amount.to_decimal(), numeric=True)
        )
        self.bills_view.append_column(
            column("Monthly", lambda b: b.monthly.format(),
                   sort_key=lambda b: b.monthly.to_decimal(), numeric=True)
        )
        self.bills_view.append_column(
            column("Hold now", lambda b: b.hold(self._today()).format(),
                   sort_key=lambda b: b.hold(self._today()).to_decimal(), numeric=True)
        )
        self.bills_view.append_column(
            column("Annual", lambda b: b.annual.format(),
                   sort_key=lambda b: b.annual.to_decimal(), numeric=True)
        )
        self.bills_view.append_column(
            column("Kind", lambda b: "Estimate" if b.estimate else "Committed")
        )
        bar.append(column_menu("dashboard", self.bills_view, self._settings()))

        scroller = Gtk.ScrolledWindow(child=self.bills_view)
        scroller.set_vexpand(True)
        self.append(scroller)

    def _settings(self):
        return getattr(self.manager.get_application(), "view_settings", None)

    def _today(self):
        from datetime import date

        return self.board.as_of if self.board else date.today()

    def _due_in(self, bill) -> str:
        days = bill.days_until(self._today())
        if days < 0:
            return f"{-days} days overdue"
        return "today" if days == 0 else f"{days} days"

    # ------------------------------------------------------------------- model

    def refresh(self) -> None:
        if self.db is None:
            return
        self.config = engine.DashboardConfig.load(self.db)
        self._updating = True
        try:
            self.liquidity_spin.set_value(self.config.liquidity_days)
            self.emergency_spin.set_value(self.config.emergency_months)
        finally:
            self._updating = False

        self.board = engine.build(self.db, self.config)
        self.budget_label.set_text(
            f"following budget: {self.board.budget_name}"
            if self.board.budget_name else "no budget selected"
        )
        self._render_cards()
        self._render_groups()
        self._render_fsa()
        self._render_fsa_claims()

        store = Gio.ListStore.new(Row)
        for bill in self.board.bills:
            store.append(Row(bill))
        self.bills_view.set_model(
            Gtk.SingleSelection(model=sorted_model(self.bills_view, store))
        )

    def _render_cards(self) -> None:
        _empty(self.cards)
        board = self.board
        assert board is not None
        summary = board.summary()
        shortfall = summary["emergency_shortfall"]

        cards = [
            ("Net worth", summary["net_worth"], False),
            ("Liquid", summary["liquid"], False),
            (f"Needed in {board.config.liquidity_days} days",
             summary["required_liquid"], False),
            ("Available", summary["available"], summary["available"] < 0),
            (f"Emergency fund ({board.config.emergency_months} mo)",
             summary["emergency_fund"], False),
        ]
        for label, amount, alarm in cards:
            self.cards.append(_card(label, amount.format(parens_negative=True), alarm))

        months = summary["months_covered"]
        self.cards.append(
            _card(
                "Months covered",
                f"{months:g}",
                months < board.config.emergency_months,
            )
        )
        if shortfall > 0:
            self.cards.append(
                _card("Short of the fund", shortfall.format(), True)
            )

    def _render_groups(self) -> None:
        _empty(self.groups)
        board = self.board
        assert board is not None

        for position, heading in enumerate(
            ("Group", "Value", "Owed", "Equity / total", "LTV")
        ):
            label = Gtk.Label(label=heading, xalign=1 if position else 0)
            label.add_css_class("summary-label")
            self.groups.attach(label, position, 0, 1, 1)

        for index, group in enumerate(board.groups, start=1):
            name = Gtk.Label(label=group.name, xalign=0)
            name.set_ellipsize(Pango.EllipsizeMode.END)
            self.groups.attach(name, 0, index, 1, 1)
            self.groups.attach(
                _amount(group.value if group.value is not None else None), 1, index, 1, 1
            )
            self.groups.attach(
                _amount(group.debt if group.debt is not None else None), 2, index, 1, 1
            )
            self.groups.attach(
                _amount(group.equity if group.equity is not None else group.total),
                3, index, 1, 1,
            )
            ratio = group.loan_to_value
            self.groups.attach(
                Gtk.Label(label=f"{ratio:.1%}" if ratio is not None else "", xalign=1),
                4, index, 1, 1,
            )

    def _render_fsa(self) -> None:
        from ...gen.engine import fsa

        _empty(self.fsa_grid)
        assert self.db is not None
        statuses = fsa.dashboard_statuses(self.db, as_of=self._today())
        self.fsa_heading.set_visible(bool(statuses))
        self.fsa_grid.set_visible(bool(statuses))
        if not statuses:
            return
        headings = ("Account", "Funding year", "Status", "Election", "Funded",
                    "Used", "Remaining", "Forfeited")
        for column_index, heading in enumerate(headings):
            label = Gtk.Label(label=heading, xalign=1 if column_index >= 3 else 0)
            label.add_css_class("summary-label")
            self.fsa_grid.attach(label, column_index, 0, 1, 1)
        for row_index, status in enumerate(statuses, start=1):
            values = (
                self.db.full_name(status.account), status.label, status.phase,
                status.year.election.format(), status.funded.format(), status.used.format(),
                status.remaining.format(), status.forfeited.format(),
            )
            for column_index, value in enumerate(values):
                label = Gtk.Label(label=value, xalign=1 if column_index >= 3 else 0)
                if column_index >= 3:
                    label.add_css_class("numeric")
                self.fsa_grid.attach(label, column_index, row_index, 1, 1)

    def _render_fsa_claims(self) -> None:
        from ...gen.engine import fsa_claims

        _empty(self.fsa_claim_grid)
        assert self.db is not None
        summaries = [
            fsa_claims.claim_summary(self.db, claim, as_of=self._today())
            for claim in fsa_claims.iter_claims(self.db)
        ]
        summaries = [
            summary for summary in summaries
            if summary.status is not fsa_claims.FsaClaimStatus.FULLY_REIMBURSED
        ]
        self.fsa_claim_heading.set_visible(bool(summaries))
        self.fsa_claim_grid.set_visible(bool(summaries))
        if not summaries:
            return
        headings = (
            "Service date", "Provider", "Status", "Net paid", "Reimbursed",
            "Rejected", "Remaining", "Action",
        )
        for column_index, heading in enumerate(headings):
            label = Gtk.Label(label=heading, xalign=1 if column_index >= 3 else 0)
            label.add_css_class("summary-label")
            self.fsa_claim_grid.attach(label, column_index, 0, 1, 1)
        for row_index, summary in enumerate(summaries, start=1):
            values = (
                summary.claim.service_date.isoformat(),
                summary.claim.provider,
                summary.status.label,
                summary.net_paid.format(),
                summary.reimbursed.format(),
                summary.rejected.format(),
                summary.remaining_reimbursable.format(),
            )
            for column_index, value in enumerate(values):
                label = Gtk.Label(label=value, xalign=1 if column_index >= 3 else 0)
                if column_index >= 3:
                    label.add_css_class("numeric")
                self.fsa_claim_grid.attach(label, column_index, row_index, 1, 1)
            review = Gtk.Button(label="Review claim")
            review.connect(
                "clicked",
                lambda _button, handle=summary.claim.handle: self._open_fsa_claim(handle),
            )
            self.fsa_claim_grid.attach(review, 7, row_index, 1, 1)

    # ----------------------------------------------------------------- actions


    def _open_fsa_claim(self, claim_handle: str | None = None) -> None:
        from ..dialogs.fsa_claims_dialog import FsaClaimsDialog

        assert self.db is not None
        FsaClaimsDialog(self.get_root(), self.db, claim_handle=claim_handle).present()

    def _on_fsa_claims(self, _button) -> None:
        self._open_fsa_claim()

    def _on_bill_activated(self, _view, position: int) -> None:
        selection = self.bills_view.get_model()
        item = selection.get_item(position)
        if item is None:
            return
        bill = item.payload if hasattr(item, "payload") else item
        schedule = getattr(bill, "schedule", None)
        if schedule is not None:
            self.manager.open_schedule(schedule.handle)

    def _on_setting_changed(self, *_args) -> None:
        if self._updating or self.db is None or self.config is None:
            return
        self.config.liquidity_days = int(self.liquidity_spin.get_value())
        self.config.emergency_months = int(self.emergency_spin.get_value())
        # Saved on the book, not in user settings: these name a household's own
        # circumstances, and carrying them to another book would mislead.
        self.config.save(self.db)
        # Rebuilt on the idle: this runs inside the spin button's own event, and
        # refresh() replaces the widgets around it.
        self.schedule_refresh()

    def _on_configure(self, _button) -> None:
        if self.db is None:
            return
        from ..dialogs.dashboard_dialog import DashboardDialog

        dialog = DashboardDialog(self.get_root(), self.db, self.config)
        dialog.connect("close-request", lambda *_: (self.refresh(), False)[1])
        dialog.present()


def _empty(container: Gtk.Widget) -> None:
    child = container.get_first_child()
    while child is not None:
        container.remove(child)
        child = container.get_first_child()


def _amount(value: Money | None) -> Gtk.Label:
    label = Gtk.Label(
        label=value.format(parens_negative=True) if value is not None else "", xalign=1
    )
    label.add_css_class("numeric")
    if value is not None and value < 0:
        label.add_css_class("negative")
    return label


def _card(caption: str, value: str, alarm: bool) -> Gtk.Widget:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    box.add_css_class("card")
    label = Gtk.Label(label=caption, xalign=0)
    label.add_css_class("summary-label")
    amount = Gtk.Label(label=value, xalign=0)
    amount.add_css_class("summary-value")
    amount.add_css_class("numeric")
    if alarm or value.startswith(("(", "-")):
        amount.add_css_class("negative")
    box.append(label)
    box.append(amount)
    return box
