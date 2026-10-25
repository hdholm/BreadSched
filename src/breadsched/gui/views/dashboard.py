"""The dashboard: the view a book opens on.

Three bands, in the order the questions get asked. What is owned and owed, as
group totals with equity and loan-to-value where a property is paired with its
mortgage. Then the verdict — liquid, spoken for, available, and how many months
the emergency fund would last. Then separate pending-bill and expected-income
lists, sortable and
scrollable, with recurring flows normalised to monthly and annual figures so a
quarterly fee and a fortnightly one can be compared at a glance.

Everything it can be told lives in **Configure Dashboard** (#381): the two
horizons (how much must stay liquid, and how long the fund should last), which
sections show, the account groups, and each table's columns. The cards name the
horizons they were judged against, so the numbers stay in sight.
"""

from __future__ import annotations

from ...api.local import book_server
from ...gen.engine import dashboard as engine
from ...gen.engine import receivables
from ...gen.lib.money import Money
from ...presentation import goal_status_text
from ..gi_setup import Gio, Gtk, Pango
from ._base import BaseView, Row, column, sorted_model, table_section

__all__ = ["DashboardView"]


class DashboardView(BaseView):
    """Balances, the liquidity verdict, pending bills, and expected income."""

    PRINTABLE = True

    WATCHES = (
        "database-changed",
        "transaction-add",
        "transaction-update",
        "transaction-delete",
        "scheduled-add",
        "scheduled-update",
        "scheduled-delete",
        "account-update",
        "savings-goal-add",
        "savings-goal-update",
        "savings-goal-delete",
    )

    def __init__(self, manager) -> None:
        super().__init__(manager)
        self.config: engine.DashboardConfig | None = None
        self.board: engine.Dashboard | None = None
        self._build()

    # ------------------------------------------------------------------ layout

    def _build(self) -> None:
        bar = Gtk.Box(spacing=8)
        for side in ("top", "bottom", "start", "end"):
            getattr(bar, f"set_margin_{side}")(8)

        title = Gtk.Label(label="Dashboard", xalign=0)
        title.add_css_class("category-title")
        title.set_hexpand(True)
        bar.append(title)

        # Configure Dashboard is a toolbar icon and Actions menu item (#156); the
        # horizons moved into it from here (#381).
        self._bar = bar
        self.append_toolbar(bar)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("start", "end", "bottom"):
            getattr(content, f"set_margin_{side}")(12)
        # Cards wrap onto more rows on a narrow window instead of widening it.
        self.cards = Gtk.FlowBox()
        self.cards.set_selection_mode(Gtk.SelectionMode.NONE)
        self.cards.set_homogeneous(False)
        self.cards.set_column_spacing(14)
        self.cards.set_row_spacing(10)
        self.cards.set_max_children_per_line(12)
        self.cards.set_valign(Gtk.Align.START)
        content.append(self.cards)

        # Groups, bills, and income are separate sections, each sized to its own
        # columns rather than stretched to the window; they sit side by side when
        # the window has room and stack when it does not (#152).
        self.sections = Gtk.FlowBox()
        self.sections.set_selection_mode(Gtk.SelectionMode.NONE)
        self.sections.set_homogeneous(False)
        self.sections.set_max_children_per_line(3)
        self.sections.set_column_spacing(24)
        self.sections.set_row_spacing(12)
        self.sections.set_valign(Gtk.Align.START)
        content.append(self.sections)

        self.groups = Gtk.Grid(column_spacing=18, row_spacing=3)
        self.groups_section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.groups_section.add_css_class("table-section")
        groups_heading = Gtk.Label(label="Account groups", xalign=0)
        groups_heading.add_css_class("total-row")
        self.groups_section.append(groups_heading)
        self.groups_section.append(self.groups)
        self._add_section(self.groups_section)

        outer = Gtk.ScrolledWindow(child=content)
        outer.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        outer.set_vexpand(True)
        self.append(outer)

        self.bills_view = Gtk.ColumnView()
        self.bills_view.add_css_class("data-table")
        self.bills_view.set_show_row_separators(True)
        # A bill is a scheduled transaction seen from another angle; activating a
        # row goes to the thing itself rather than making the user find it.
        self.bills_view.connect("activate", self._on_bill_activated)
        self.bills_view.append_column(
            column("Item", lambda b: b.name, expand=True, sort_key=lambda b: b.name)
        )
        self.bills_view.append_column(column("Due", _due_date, sort_key=lambda b: b.next_due))
        self.bills_view.append_column(
            column("Due in", self._due_in, sort_key=lambda b: b.days_until(self._today()))
        )
        # The schedule's own words, not a decimal month count: "every 3 months"
        # is what the user set, and 3.0000 reads as though it were the truth.
        self.bills_view.append_column(
            column("Frequency", lambda b: b.frequency, sort_key=lambda b: b.cycle_months)
        )
        self.bills_view.append_column(
            column(
                "Amount",
                lambda b: b.amount.format(),
                sort_key=lambda b: b.amount.to_decimal(),
                numeric=True,
            )
        )
        self.bills_view.append_column(
            column(
                "Monthly",
                lambda b: "" if b.generated else b.monthly.format(),
                sort_key=lambda b: b.monthly.to_decimal(),
                numeric=True,
            )
        )
        self.bills_view.append_column(
            column(
                "Hold now",
                lambda b: b.held.format(),
                sort_key=lambda b: b.held.to_decimal(),
                numeric=True,
            )
        )
        self.bills_view.append_column(
            column(
                "Annual",
                lambda b: "" if b.generated else b.annual.format(),
                sort_key=lambda b: b.annual.to_decimal(),
                numeric=True,
            )
        )
        self.bills_view.append_column(
            column(
                "Kind",
                lambda b: "Account payment" if b.generated else "Committed",
            )
        )
        self.bills_section = table_section(
            self.bills_view, "dashboard", self._settings(), title="Pending bills"
        )
        self._add_section(self.bills_section)

        self.income_view = Gtk.ColumnView()
        self.income_view.add_css_class("data-table")
        self.income_view.set_show_row_separators(True)
        self.income_view.connect("activate", self._on_bill_activated)
        self.income_view.append_column(
            column("Item", lambda item: item.name, expand=True, sort_key=lambda item: item.name)
        )
        self.income_view.append_column(
            column("Due", _due_date, sort_key=lambda item: item.next_due)
        )
        self.income_view.append_column(
            column(
                "Due in",
                self._due_in,
                sort_key=lambda item: item.days_until(self._today()),
            )
        )
        self.income_view.append_column(
            column(
                "Frequency", lambda item: item.frequency, sort_key=lambda item: item.cycle_months
            )
        )
        self.income_view.append_column(
            column(
                "Amount",
                lambda item: item.amount.format(),
                sort_key=lambda item: item.amount.to_decimal(),
                numeric=True,
            )
        )
        self.income_view.append_column(
            column("Monthly", lambda item: item.monthly.format(), numeric=True)
        )
        self.income_view.append_column(
            column("Annual", lambda item: item.annual.format(), numeric=True)
        )
        self.income_section = table_section(
            self.income_view, "dashboard-income", self._settings(), title="Expected income"
        )
        self._add_section(self.income_section)

        # One row per savings goal; activating one opens the goal editor.
        self.goals_view = Gtk.ColumnView()
        self.goals_view.add_css_class("data-table")
        self.goals_view.set_show_row_separators(True)
        self.goals_view.connect("activate", self._on_goal_activated)
        self.goals_view.append_column(
            column("Goal", lambda g: g.goal.name, expand=True, sort_key=lambda g: g.goal.name)
        )
        self.goals_view.append_column(
            column(
                "Target date",
                lambda g: g.goal.target_date.isoformat(),
                sort_key=lambda g: g.goal.target_date,
            )
        )
        for title, value in (
            ("Target", lambda g: g.target),
            ("Set aside", lambda g: g.set_aside),
            ("Remaining", lambda g: g.remaining),
        ):
            self.goals_view.append_column(
                column(
                    title,
                    lambda g, value=value: value(g).format(),
                    sort_key=lambda g, value=value: value(g).to_decimal(),
                    numeric=True,
                )
            )
        self.goals_view.append_column(column("Status", goal_status_text))
        self.goals_section = table_section(
            self.goals_view, "dashboard-goals", self._settings(), title="Savings goals"
        )
        self._add_section(self.goals_section)

        # Each reimbursable expense still owed, as the bills are listed (#381).
        self.reimbursables_view = Gtk.ColumnView()
        self.reimbursables_view.add_css_class("data-table")
        self.reimbursables_view.set_show_row_separators(True)
        self.reimbursables_view.connect("activate", self._on_reimbursable_activated)
        self.reimbursables_view.append_column(
            column(
                "Expense",
                lambda s: s.receivable.description or s.receivable.payer or "Reimbursable expense",
                expand=True,
                sort_key=lambda s: s.receivable.description,
            )
        )
        self.reimbursables_view.append_column(
            column("Payer", lambda s: s.receivable.payer, sort_key=lambda s: s.receivable.payer)
        )
        self.reimbursables_view.append_column(
            column(
                "Incurred",
                lambda s: s.receivable.incurred_date.isoformat(),
                sort_key=lambda s: s.receivable.incurred_date,
            )
        )
        self.reimbursables_view.append_column(
            column("Expected", self._expected_text, sort_key=engine.reimbursable_order)
        )
        self.reimbursables_view.append_column(
            column(
                "Still owed",
                lambda s: s.remaining.format(),
                sort_key=lambda s: s.remaining.to_decimal(),
                numeric=True,
            )
        )
        self.reimbursables_view.append_column(
            column("Status", lambda s: s.status.label, sort_key=lambda s: s.status.label)
        )
        self.reimbursables_section = table_section(
            self.reimbursables_view,
            "dashboard-reimbursables",
            self._settings(),
            title="Pending reimbursables",
        )
        self._add_section(self.reimbursables_section)

        # Columns are chosen in Configure Dashboard (#381), not in each heading.
        for section in self._tables_sections():
            section.chooser.set_visible(False)

    def _tables_sections(self) -> tuple[Gtk.Box, ...]:
        return (
            self.bills_section,
            self.income_section,
            self.reimbursables_section,
            self.goals_section,
        )

    def column_tables(self) -> tuple[tuple[str, str, Gtk.ColumnView], ...]:
        """The Dashboard's tables as Configure Dashboard lists their columns."""
        return (
            ("Pending bills", "dashboard", self.bills_view),
            ("Expected income", "dashboard-income", self.income_view),
            ("Pending reimbursables", "dashboard-reimbursables", self.reimbursables_view),
            ("Savings goals", "dashboard-goals", self.goals_view),
        )

    def _expected_text(self, summary: receivables.ReceivableSummary) -> str:
        expected = summary.receivable.expected_cash_date
        if expected is None:
            return "—"
        if expected < self._today():
            return f"{expected.isoformat()} (overdue)"
        return expected.isoformat()

    def _on_reimbursable_activated(self, _view, _position) -> None:
        self.manager.get_application().on_receivables(parent=self.get_root())

    def _on_goal_activated(self, _view, _position) -> None:
        self.manager.get_application().on_savings_goals()

    def _add_section(self, section: Gtk.Box) -> None:
        """Size a section to its content, bounded so no row can widen the window."""
        section.add_css_class("dashboard-section")
        section.set_halign(Gtk.Align.START)
        section.set_valign(Gtk.Align.START)
        section.set_hexpand(False)
        scroller = getattr(section, "scroller", None)
        if scroller is not None:
            scroller.set_propagate_natural_width(True)
            scroller.set_propagate_natural_height(True)
            scroller.set_max_content_width(_SECTION_MAX_WIDTH)
            scroller.set_max_content_height(_SECTION_MAX_HEIGHT)
        self.sections.append(section)

    def _settings(self):
        return getattr(self.manager.get_application(), "view_settings", None)

    def _today(self):
        from datetime import date

        return self.board.as_of if self.board else date.today()

    def _due_in(self, bill) -> str:
        days = bill.days_until(self._today())
        if days < 0:
            late = f"{-days} days overdue"
            # A schedule's missed dates are one grouped row; its count leads.
            return f"{bill.count} missed, {late}" if isinstance(bill, engine.MissedGroup) else late
        return "today" if days == 0 else f"{days} days"

    # ------------------------------------------------------------------- model

    def refresh(self) -> None:
        if self.db is None:
            return
        self.board = book_server(self.db).dashboard()
        self.config = self.board.config

        self._render_cards()
        self._render_groups()

        store = Gio.ListStore.new(Row)
        for item in self.board.display_bills:
            store.append(Row(item))
        self.bills_view.set_model(Gtk.SingleSelection(model=sorted_model(self.bills_view, store)))
        income_store = Gio.ListStore.new(Row)
        for item in self.board.display_incomes:
            income_store.append(Row(item))
        self.income_view.set_model(
            Gtk.SingleSelection(model=sorted_model(self.income_view, income_store))
        )
        goal_store = Gio.ListStore.new(Row)
        for goal in self.board.goals:
            goal_store.append(Row(goal))
        self.goals_view.set_model(
            Gtk.SingleSelection(model=sorted_model(self.goals_view, goal_store))
        )
        reimbursable_store = Gio.ListStore.new(Row)
        for summary in self.board.reimbursables:
            reimbursable_store.append(Row(summary))
        self.reimbursables_view.set_model(
            Gtk.SingleSelection(model=sorted_model(self.reimbursables_view, reimbursable_store))
        )
        shows = self.config.shows
        self.bills_section.set_visible(shows("bills"))
        self.income_section.set_visible(shows("income"))
        self.reimbursables_section.set_visible(shows("reimbursables"))
        self.goals_section.set_visible(shows("goals") and bool(self.board.goals))

    def printable_report(self):
        """The currently rendered Dashboard, laid out for printing."""
        if self.board is None:
            return None
        from ...plugins.export.report_layout import dashboard_layout

        return dashboard_layout(self.board, book_name=self.book_name())

    def printable_html(self) -> str | None:
        """The same report as a web page, for printing from a browser."""
        from ...plugins.export.html_report import render_html

        document = self.printable_report()
        return render_html(document) if document is not None else None

    def _render_cards(self) -> None:
        _empty(self.cards)
        board = self.board
        assert board is not None
        summary = board.summary()
        report = board.report_summary()
        missing_fields = {key for key, value in report.items() if value is None}
        shortfall = summary["emergency_shortfall"]

        cards = [
            ("Net worth", summary["net_worth"], False),
            ("Liquid", summary["liquid"], False),
            (f"Needed in {board.config.liquidity_days} days", summary["required_liquid"], False),
            ("Available", summary["available"], summary["available"] < 0),
            (
                f"Emergency fund ({board.config.emergency_months} mo)",
                summary["emergency_fund"],
                False,
            ),
            (
                "Committed emergency outgoings / mo",
                summary["emergency_monthly_outgoings"],
                False,
            ),
            (
                "Including estimates / mo",
                summary["emergency_monthly_outgoings_with_estimates"],
                False,
            ),
        ]
        for label, amount, alarm in cards:
            field = {"Net worth": "net_worth", "Liquid": "liquid", "Available": "available"}.get(
                label
            )
            rendered = (
                board.unavailable_reason(field)
                if field in missing_fields
                else amount.format(parens_negative=True)
            )
            if label.startswith("Emergency fund") and "emergency_fund" in missing_fields:
                rendered = board.unavailable_reason("emergency_fund")
            self.cards.append(
                _card(label, rendered, alarm if field not in missing_fields else False)
            )

        months = summary["months_covered"]
        self.cards.append(
            _card(
                "Months covered",
                f"{months:g}"
                if "months_covered" not in missing_fields
                else board.unavailable_reason("months_covered"),
                months < board.config.emergency_months and "months_covered" not in missing_fields,
            )
        )
        if shortfall > 0 and "emergency_shortfall" not in missing_fields:
            self.cards.append(_card("Short of the fund", shortfall.format(), True))
        owed, attention = summary["receivables_owed"], summary["receivables_attention"]
        if owed > 0:
            text = owed.format()
            if attention > 0:
                text += f" ({attention.format()} disputed or overdue)"
            self.cards.append(_card("Reimbursements due", text, attention > 0))
        if summary["fsa_claims_attention"]:
            self.cards.append(
                _card(
                    "FSA claims needing attention",
                    f"{summary['fsa_claims_attention']} — see the FSA Dashboard",
                    True,
                )
            )
        set_aside, held = summary["goals_set_aside"], summary["goals_held"]
        if set_aside > 0:
            text = set_aside.format()
            if held != set_aside:
                text += f" ({held.format()} held from spendable cash)"
            self.cards.append(_card("Set aside for goals", text, False))
        for note in board.coverage_notes:
            self.cards.append(_card("Coverage", note, False))
        # What each withheld total leaves out and how to include it (#236).
        for caption, coverage in (
            ("Net worth withheld", board.completeness),
            ("Liquid cash withheld", board.liquid_completeness),
        ):
            if coverage.complete or (
                coverage is board.liquid_completeness
                and coverage.excluded == board.completeness.excluded
            ):
                continue
            self.cards.append(_card(caption, "\n".join(coverage.detail()), True))

    def _render_groups(self) -> None:
        _empty(self.groups)
        board = self.board
        assert board is not None
        self.groups_section.set_visible(board.config.shows("groups") and bool(board.groups))

        headings = ("Group", "Value", "Owed", "Equity / total", "LTV", "Loan end")
        for position, heading in enumerate(headings):
            label = Gtk.Label(label=heading, xalign=1 if position else 0)
            label.add_css_class("summary-label")
            self.groups.attach(label, position, 0, 1, 1)

        for index, group in enumerate(board.groups, start=1):
            label = group.name
            if group.note:
                label = f"{label} — {group.note}"
            name = Gtk.Label(label=label, xalign=0)
            name.set_margin_start(group.depth * 18)
            if group.heading:
                name.add_css_class("total-row")
            # A combined group's note joins every account's quote details. An
            # ellipsized label still asks for its full natural width, so cap it
            # (issue #140) and keep the whole text in the tooltip.
            name.set_ellipsize(Pango.EllipsizeMode.END)
            name.set_max_width_chars(_GROUP_LABEL_CHARS)
            # The row names the group and its own valuation note; the tooltip lists
            # every account it covers, one per line (#150).
            tooltip = [group.path if group.path else group.name]
            if group.note:
                tooltip.append(group.note)
            tooltip.extend(group.members)
            tooltip.extend(group.completeness.detail())
            name.set_tooltip_text("\n".join(tooltip))
            self.groups.attach(name, 0, index, 1, 1)
            self.groups.attach(_amount(group.report_value), 1, index, 1, 1)
            self.groups.attach(_amount(group.report_debt), 2, index, 1, 1)
            self.groups.attach(
                _amount(
                    group.report_equity if group.report_equity is not None else group.report_total
                ),
                3,
                index,
                1,
                1,
            )
            ratio = group.report_loan_to_value
            self.groups.attach(
                Gtk.Label(label=f"{ratio:.1%}" if ratio is not None else "", xalign=1),
                4,
                index,
                1,
                1,
            )
            self.groups.attach(
                Gtk.Label(label=str(group.loan_end or ""), xalign=1),
                5,
                index,
                1,
                1,
            )

    # ----------------------------------------------------------------- actions

    def _on_bill_activated(self, view, position: int) -> None:
        selection = view.get_model()
        item = selection.get_item(position)
        if item is None:
            return
        bill = item.payload if hasattr(item, "payload") else item
        schedule = getattr(bill, "schedule", None)
        if schedule is not None:
            self.manager.open_schedule(schedule.handle)
        elif bill.generated and bill.account is not None and self.db is not None:
            account = self.db.get_account(bill.account)
            if account is None:
                return
            from ..dialogs.account_dialog import AccountDialog

            dialog = AccountDialog(self.get_root(), self.db, account=account)
            dialog.connect("close-request", self.refresh_on_close)
            dialog.present()
        elif bill.account is not None:
            self.manager.open_register(bill.account)

    def _on_setting_changed(self, *_args) -> None:
        if self._updating or self.db is None or self.config is None:
            return
        # Saved on the book, not in user settings: these name a household's own
        # circumstances, and carrying them to another book would mislead. The
        # service rereads the groups, so a group changed since this view was
        # drawn is kept as stored.
        from ...api import ApiError
        from ...gen.services import SaveDashboardConfig

        request = SaveDashboardConfig(
            liquidity_days=int(self.liquidity_spin.get_value()),
            emergency_months=int(self.emergency_spin.get_value()),
        )
        try:
            saved = book_server(self.db).save_dashboard_settings(request)
        except ApiError:
            saved = None
        if saved is not None:
            # The shown board reads its horizons from this object, so it answers
            # at once; the refresh below rereads the rest.
            self.config.liquidity_days = saved.liquidity_days
            self.config.emergency_months = saved.emergency_months
        # Rebuilt on the idle: this runs inside the spin button's own event, and
        # refresh() replaces the widgets around it.
        self.schedule_refresh()

    def _on_configure(self, _button) -> None:
        if self.db is None:
            return
        from ..dialogs.dashboard_dialog import DashboardDialog

        dialog = DashboardDialog(
            self.get_root(), self.db, tables=self.column_tables(), settings=self._settings()
        )
        dialog.connect("close-request", self.refresh_on_close)
        dialog.present()

    def _open_net_worth_history(self, _button) -> None:
        if self.db is None:
            return
        from ..dialogs.net_worth_history_dialog import NetWorthHistoryDialog

        NetWorthHistoryDialog(self.get_root(), self.db).present()


#: Widest a group name (with its note) may ask to be before it ellipsizes.
_GROUP_LABEL_CHARS = 60
#: Widest a headline card's text may ask to be before it wraps.
_CARD_TEXT_CHARS = 40
#: Upper bounds for a bills or income section's natural size; beyond them the
#: table scrolls inside its section instead of widening the window (#140, #152).
_SECTION_MAX_WIDTH = 900
_SECTION_MAX_HEIGHT = 420


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
    # Coverage notes can be long; wrap them instead of widening the window (#140).
    amount.set_wrap(True)
    amount.set_max_width_chars(_CARD_TEXT_CHARS)
    amount.add_css_class("summary-value")
    amount.add_css_class("numeric")
    if alarm or value.startswith(("(", "-")):
        amount.add_css_class("negative")
    box.append(label)
    box.append(amount)
    return box


def _due_date(item) -> str:
    """A due date, or the missed date range of a grouped schedule."""
    if isinstance(item, engine.MissedGroup):
        return f"{item.next_due.isoformat()} to {item.last_due.isoformat()}"
    return item.next_due.isoformat()
