"""Dedicated FSA benefit-year and healthcare-claim dashboard."""

from __future__ import annotations

from ...gen.engine import fsa
from ...gen.engine.fsa_claim_report import claim_report
from ...gen.services import accept_claim_links, claim_link_proposals
from ...presentation import claim_role_label, fsa_usage_text
from ..gi_setup import Gtk
from ..widgets.choice import bounded_dropdown
from ._base import BaseView

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
        # "Manage FSA claims" is a toolbar icon while this view is shown (#156).
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
        self.link_selected = Gtk.Button(label="Link selected")
        self.link_selected.connect("clicked", self._on_link_selected)
        proposal_actions.append(self.link_selected)
        self.proposal_status = Gtk.Label(xalign=0, wrap=True)
        proposal_actions.append(self.proposal_status)
        self.proposal_actions = proposal_actions
        content.append(proposal_actions)
        self._proposal_checks: list[tuple[Gtk.CheckButton, tuple[str, str, str]]] = []

        self.fsa_heading = self._heading("FSA benefit years")
        content.append(self.fsa_heading)
        self.fsa_grid = self._grid()
        content.append(self.fsa_grid)

        self.claim_heading = self._heading("Open FSA claims")
        content.append(self.claim_heading)
        self.claim_grid = self._grid()
        content.append(self.claim_grid)

        report_row = Gtk.Box(spacing=8)
        report_row.set_margin_start(12)
        report_row.set_margin_top(12)
        self.report_heading = Gtk.Label(label="Claims report", xalign=0)
        self.report_heading.add_css_class("total-row")
        report_row.append(self.report_heading)
        report_row.append(Gtk.Label(label="Group by"))
        self.report_by = bounded_dropdown([label for _key, label in GROUPINGS])
        self.report_by.connect("notify::selected", lambda *_a: self._render_report())
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
        self._render_proposals()
        self._render_years()
        self._render_claims()
        self._render_report()

    def _render_proposals(self) -> None:
        _empty(self.proposal_grid)
        self._proposal_checks = []
        assert self.db is not None
        proposals = claim_link_proposals(self.db).value or ()
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

    def _on_link_selected(self, _button=None) -> None:
        if self.db is None:
            return
        chosen = tuple(key for check, key in self._proposal_checks if check.get_active())
        if not chosen:
            return
        result = accept_claim_links(self.db, chosen)
        accepted = result.value
        assert accepted is not None
        self.refresh()
        noun = "link" if accepted.linked == 1 else "links"
        text = f"Linked {accepted.linked} claim {noun}."
        if accepted.unchanged:
            text += f" {accepted.unchanged} no longer applied."
        self.proposal_status.set_label(text)
        self.proposal_actions.set_visible(True)

    def _render_years(self) -> None:
        _empty(self.fsa_grid)
        assert self.db is not None
        statuses = fsa.dashboard_statuses(self.db)
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
                self.db.full_name(status.account),
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

    def _render_claims(self) -> None:
        _empty(self.claim_grid)
        assert self.db is not None
        report = claim_report(self.db)
        waiting = len(report.needing_attention)
        self.attention_label.set_label(
            f"{waiting} claim{' needs' if waiting == 1 else 's need'} attention." if waiting else ""
        )
        self.attention_label.set_visible(bool(waiting))
        lines = [line for line in report.lines if line.attention or not line.summary.status.settled]
        self.claim_heading.set_visible(bool(lines))
        self.claim_grid.set_visible(bool(lines))
        if not lines:
            return
        headings = (
            "Service date",
            "Provider",
            "Status",
            "Net paid",
            "Reimbursed",
            "Rejected",
            "Remaining",
            "Needs attention",
            "Action",
        )
        for column_index, heading in enumerate(headings):
            label = Gtk.Label(label=heading, xalign=1 if 3 <= column_index <= 6 else 0)
            label.add_css_class("summary-label")
            self.claim_grid.attach(label, column_index, 0, 1, 1)
        for row_index, line in enumerate(lines, start=1):
            summary = line.summary
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
                self.claim_grid.attach(label, column_index, row_index, 1, 1)
            note = Gtk.Label(label="\n".join(item.text for item in line.attention) or "—", xalign=0)
            note.set_wrap(True)
            note.set_max_width_chars(40)
            if line.attention:
                note.add_css_class("negative")
            self.claim_grid.attach(note, 7, row_index, 1, 1)
            review = Gtk.Button(label="Review claim")
            review.set_valign(Gtk.Align.START)
            review.connect(
                "clicked",
                lambda _button, handle=summary.claim.handle: self._open_claim(handle),
            )
            self.claim_grid.attach(review, 8, row_index, 1, 1)

    def _render_report(self) -> None:
        _empty(self.report_grid)
        if self.db is None:
            return
        key, label = GROUPINGS[min(self.report_by.get_selected(), len(GROUPINGS) - 1)]
        report = claim_report(self.db, by=key)
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
        FsaClaimsDialog(self.get_root(), self.db, claim_handle=claim_handle).present()

    def _on_fsa_claims(self, _button) -> None:
        self._open_claim()


def _empty(container: Gtk.Widget) -> None:
    child = container.get_first_child()
    while child is not None:
        container.remove(child)
        child = container.get_first_child()
