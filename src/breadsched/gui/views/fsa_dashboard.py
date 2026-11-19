"""Dedicated FSA benefit-year and healthcare-claim dashboard.

Its commands follow the window's rule (``view_catalog``): **Edit FSA Claim** opens
the claim selected in the claims table (also by double-clicking it, or from its
right-click menu), and **Link Chosen Statement Lines** links the proposed lines
ticked; both are toolbar icons and Actions menu items. The ticks and the report's
grouping only choose what the view shows and what a command acts on.
"""

from __future__ import annotations

from ...api.local import book_server
from ...gen.services.claims import ClaimReportQuery
from ...presentation import claim_role_label, fsa_account_text, fsa_usage_text
from ..gi_setup import Gio, Gtk
from ..widgets.choice import bounded_dropdown
from ._base import BaseView, Row, attach_row_menu, column, sorted_model, table_section, unwrap

#: (grouping, label) choices for the claims report, in the order offered.
GROUPINGS = (
    ("status", "Status"),
    ("account", "FSA account"),
    ("year", "Funding year"),
    ("provider", "Provider"),
)

__all__ = ["FsaDashboardView"]


class FsaDashboardView(BaseView):
    """Benefit availability and open claims, separate from household liquidity."""

    KEY = "fsa-dashboard"
    WATCHES = (
        "database-changed",
        "account-add",
        "account-update",
        "account-delete",
        "transaction-add",
        "transaction-update",
        "transaction-delete",
        "fsa-claim-add",
        "fsa-claim-update",
        "fsa-claim-delete",
    )

    def __init__(self, manager) -> None:
        super().__init__(manager)
        bar = Gtk.Box(spacing=8)
        for side in ("top", "bottom", "start", "end"):
            getattr(bar, f"set_margin_{side}")(8)
        title = Gtk.Label(label="FSA Dashboard", xalign=0)
        title.add_css_class("category-title")
        title.set_hexpand(True)
        bar.append(title)
        # Edit FSA Claim, Link Chosen Statement Lines, and Manage FSA Claims are
        # toolbar icons and Actions menu items while this view is shown (#156).
        self.append_toolbar(bar)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.set_margin_bottom(12)
        scroller = Gtk.ScrolledWindow(child=content, vexpand=True)
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.append(scroller)

        self.attention_label = Gtk.Label(xalign=0, wrap=True)
        self.attention_label.add_css_class("negative")
        self.attention_label.set_margin_start(12)
        self.attention_label.set_margin_top(8)
        content.append(self.attention_label)

        # Statement lines that clearly belong on one claim, ready to link.
        self.proposal_heading = self._heading("Proposed claim links")
        content.append(self.proposal_heading)
        self.proposal_grid = self._grid()
        content.append(self.proposal_grid)
        proposal_actions = Gtk.Box(spacing=8)
        proposal_actions.set_margin_start(12)
        proposal_actions.set_margin_top(6)
        self.proposal_status = Gtk.Label(
            label="Untick any line that does not belong, then choose Link Lines.",
            xalign=0,
            wrap=True,
        )
        self.proposal_status.add_css_class("dim")
        proposal_actions.append(self.proposal_status)
        self.proposal_actions = proposal_actions
        content.append(proposal_actions)
        self._proposal_checks: list[tuple[Gtk.CheckButton, tuple[str, str, str]]] = []

        self.fsa_heading = self._heading("FSA benefit years")
        content.append(self.fsa_heading)
        self.fsa_grid = self._grid()
        content.append(self.fsa_grid)

        # Every claim of a benefit year that can still be claimed, settled or not,
        # so a new claim is easy to tell from one already filed (#366). Selecting
        # one is what Edit FSA Claim opens.
        self.claim_view = Gtk.ColumnView()
        self.claim_view.add_css_class("data-table")
        self.claim_view.set_show_row_separators(True)
        for title, bind, numeric, expand in (
            (
                "Service date",
                lambda line: line.summary.claim.service_date.isoformat(),
                False,
                False,
            ),
            ("Provider", lambda line: line.summary.claim.provider, False, True),
            ("Status", lambda line: line.summary.status.label, False, False),
            ("Net paid", lambda line: line.summary.net_paid.format(), True, False),
            ("Reimbursed", lambda line: line.summary.reimbursed.format(), True, False),
            ("Rejected", lambda line: line.summary.rejected.format(), True, False),
            ("Remaining", lambda line: line.summary.remaining_reimbursable.format(), True, False),
            ("Needs attention", _attention_text, False, True),
        ):
            self.claim_view.append_column(column(title, bind, expand=expand, numeric=numeric))
        self.claim_view.connect("activate", lambda *_a: self.edit_selected_claim())
        attach_row_menu(self, self.claim_view)
        self.claim_section = table_section(
            self.claim_view,
            "fsa-claims",
            getattr(self.manager.get_application(), "view_settings", None),
            title="FSA claims",
        )
        self.claim_heading = self.claim_section.heading
        for side in ("start", "end"):
            getattr(self.claim_section, f"set_margin_{side}")(12)
        self.claim_section.set_margin_top(12)
        # The whole dashboard scrolls, so the table is as tall as its claims.
        self.claim_section.scroller.set_propagate_natural_height(True)
        content.append(self.claim_section)
        self.claim_note = Gtk.Label(
            label=(
                "Every claim in a benefit year that can still be claimed, settled or "
                "not, and any older claim still open."
            ),
            xalign=0,
            wrap=True,
        )
        self.claim_note.add_css_class("dim")
        self.claim_note.set_margin_start(12)
        self.claim_section.insert_child_after(self.claim_note, self.claim_section.get_first_child())

        report_row = Gtk.Box(spacing=8)
        report_row.set_margin_start(12)
        report_row.set_margin_top(12)
        self.report_heading = Gtk.Label(label="Claims report", xalign=0)
        self.report_heading.add_css_class("total-row")
        report_row.append(self.report_heading)
        report_row.append(Gtk.Label(label="Group by"))
        self.report_by = bounded_dropdown([label for _key, label in GROUPINGS])
        self.report_by.connect("notify::selected", lambda *_a: self._regroup())
        report_row.append(self.report_by)
        self.report_row = report_row
        content.append(report_row)
        self.report_grid = self._grid()
        content.append(self.report_grid)

    @staticmethod
    def _heading(text: str) -> Gtk.Label:
        heading = Gtk.Label(label=text, xalign=0)
        heading.add_css_class("total-row")
        heading.set_margin_start(12)
        heading.set_margin_top(12)
        return heading

    @staticmethod
    def _grid() -> Gtk.Grid:
        grid = Gtk.Grid(column_spacing=18, row_spacing=3)
        grid.set_margin_start(12)
        grid.set_margin_end(12)
        return grid

    def refresh(self) -> None:
        if self.db is None:
            return
        self._show(self._read(book_server(self.db), self._grouping()))

    def background_refresh(self):
        """The claims and benefit years are read from a snapshot, off GTK's loop, so
        a large book's FSA Dashboard says it is updating rather than freezing (#397)."""
        if self.db is None:
            return None
        by = self._grouping()
        return (lambda book: self._read(book, by)), self._show

    def _grouping(self) -> str:
        return GROUPINGS[min(self.report_by.get_selected(), len(GROUPINGS) - 1)][0]

    @staticmethod
    def _read(book, by: str):
        """Everything the dashboard shows, read once; the claim report once for the
        claims and once more only when grouped differently."""
        claims = book.fsa_claim_report(ClaimReportQuery())
        grouped = (
            claims
            if by == ClaimReportQuery().by
            else book.fsa_claim_report(ClaimReportQuery(by=by))
        )
        return book.claim_link_proposals(), book.benefit_years(), claims, grouped

    def _show(self, data) -> None:
        proposals, years, claims, grouped = data
        self._render_proposals(proposals)
        self._render_years(years)
        self._render_claims(claims)
        self._render_report(grouped)

    def _render_proposals(self, proposals) -> None:
        _empty(self.proposal_grid)
        self._proposal_checks = []
        for widget in (self.proposal_heading, self.proposal_grid, self.proposal_actions):
            widget.set_visible(bool(proposals))
        if not proposals:
            return
        headings = ("", "Date", "Transaction", "Amount", "Claim", "As", "Why")
        for column_index, heading in enumerate(headings):
            label = Gtk.Label(label=heading, xalign=1 if column_index == 3 else 0)
            label.add_css_class("summary-label")
            self.proposal_grid.attach(label, column_index, 0, 1, 1)
        for row_index, item in enumerate(proposals, start=1):
            check = Gtk.CheckButton(active=True)
            check.connect("toggled", lambda *_a: self._update_commands())
            self.proposal_grid.attach(check, 0, row_index, 1, 1)
            self._proposal_checks.append((check, (item.claim, item.transaction, item.split)))
            values = (
                item.when.isoformat(),
                item.description,
                item.amount.format(),
                item.claim_label,
                claim_role_label(item.role),
                item.reason,
            )
            for column_index, value in enumerate(values, start=1):
                label = Gtk.Label(label=value, xalign=1 if column_index == 3 else 0)
                if column_index == 3:
                    label.add_css_class("numeric")
                self.proposal_grid.attach(label, column_index, row_index, 1, 1)
        self._update_commands()

    def _on_link_selected(self, _button=None) -> None:
        if self.db is None:
            return
        chosen = tuple(key for check, key in self._proposal_checks if check.get_active())
        if not chosen:
            return
        accepted = book_server(self.db).accept_claim_links(chosen)
        self.refresh()
        noun = "link" if accepted.linked == 1 else "links"
        text = f"Linked {accepted.linked} claim {noun}."
        if accepted.unchanged:
            text += f" {accepted.unchanged} no longer applied."
        self.proposal_status.set_label(text)
        self.proposal_actions.set_visible(True)

    def _render_years(self, years) -> None:
        _empty(self.fsa_grid)
        statuses = years.years
        self.fsa_heading.set_visible(bool(statuses))
        self.fsa_grid.set_visible(bool(statuses))
        if not statuses:
            return
        headings = (
            "Account",
            "Funding year",
            "Status",
            "Election",
            "Funded",
            "Used",
            "How used",
            "Remaining",
            "Carried in",
            "Carried over",
            "Forfeited",
        )
        for column_index, heading in enumerate(headings):
            text_column = column_index < 3 or column_index == 6
            label = Gtk.Label(label=heading, xalign=0 if text_column else 1)
            label.add_css_class("summary-label")
            self.fsa_grid.attach(label, column_index, 0, 1, 1)
        for row_index, status in enumerate(statuses, start=1):
            values = (
                fsa_account_text(years.name(status.account.handle), status.dependent_care),
                status.label,
                status.phase,
                status.year.election.format(),
                status.funded.format(),
                status.used.format(),
                fsa_usage_text(status),
                status.remaining.format(),
                status.carried_in.format(),
                status.carried_over.format(),
                status.forfeited.format(),
            )
            for column_index, value in enumerate(values):
                text_column = column_index < 3 or column_index == 6
                label = Gtk.Label(label=value, xalign=0 if text_column else 1)
                if not text_column:
                    label.add_css_class("numeric")
                self.fsa_grid.attach(label, column_index, row_index, 1, 1)

    def _render_claims(self, report) -> None:
        waiting = len(report.needing_attention)
        self.attention_label.set_label(
            f"{waiting} claim{' needs' if waiting == 1 else 's need'} attention." if waiting else ""
        )
        self.attention_label.set_visible(bool(waiting))
        lines = report.dashboard_lines
        self.claim_section.set_visible(bool(lines))
        # The claim that was selected (one just edited, say) stays selected.
        kept = self._selected_claim()
        store = Gio.ListStore.new(Row)
        for line in lines:
            store.append(Row(line))
        selection = Gtk.SingleSelection(model=sorted_model(self.claim_view, store))
        selection.connect("notify::selected", lambda *_a: self._update_commands())
        self.claim_view.set_model(selection)
        if kept is not None:
            for index in range(selection.get_n_items()):
                if unwrap(selection.get_item(index)).summary.claim.handle == kept:
                    selection.set_selected(index)
                    break
        self._update_commands()

    def _selected_claim(self) -> str | None:
        """The handle of the claim selected in the claims table, if any."""
        selection = self.claim_view.get_model()
        selected = selection.get_selected_item() if selection is not None else None
        return unwrap(selected).summary.claim.handle if selected is not None else None

    def edit_selected_claim(self, _button=None) -> None:
        """Open the selected claim in Manage FSA Claims."""
        handle = self._selected_claim()
        if handle is not None and self.db is not None:
            self._open_claim(handle)

    def _update_commands(self) -> None:
        self.enable_commands(
            edit_claim=self._selected_claim() is not None,
            link_proposals=any(check.get_active() for check, _key in self._proposal_checks),
        )

    def _regroup(self) -> None:
        if self.db is not None:
            report = book_server(self.db).fsa_claim_report(ClaimReportQuery(by=self._grouping()))
            self._render_report(report)

    def _render_report(self, report) -> None:
        _empty(self.report_grid)
        label = GROUPINGS[min(self.report_by.get_selected(), len(GROUPINGS) - 1)][1]
        self.report_row.set_visible(bool(report.lines))
        self.report_grid.set_visible(bool(report.lines))
        if not report.lines:
            return
        headings = (label, "Claims", "Net paid", "Reimbursed", "Rejected", "Remaining", "Attention")
        for column_index, heading in enumerate(headings):
            cell = Gtk.Label(label=heading, xalign=1 if column_index else 0)
            cell.add_css_class("summary-label")
            self.report_grid.attach(cell, column_index, 0, 1, 1)
        rows = (*report.groups, report.totals)
        for row_index, group in enumerate(rows, start=1):
            values = (
                group.label,
                str(group.claims),
                group.net_paid.format(),
                group.reimbursed.format(),
                group.rejected.format(),
                group.remaining.format(),
                str(group.attention) if group.attention else "",
            )
            for column_index, value in enumerate(values):
                cell = Gtk.Label(label=value, xalign=1 if column_index else 0)
                if column_index:
                    cell.add_css_class("numeric")
                if group is report.totals:
                    cell.add_css_class("total-row")
                self.report_grid.attach(cell, column_index, row_index, 1, 1)

    def _open_claim(self, claim_handle: str | None = None) -> None:
        from ..dialogs.fsa_claims_dialog import FsaClaimsDialog

        assert self.db is not None
        FsaClaimsDialog.present_for(self.get_root(), self.db, claim_handle)

    def _on_fsa_claims(self, _button) -> None:
        self._open_claim()


def _attention_text(line) -> str:
    """What a claim still needs, or a dash when nothing."""
    return "; ".join(item.text for item in line.attention) or "—"


def _empty(container: Gtk.Widget) -> None:
    child = container.get_first_child()
    while child is not None:
        container.remove(child)
        child = container.get_first_child()
