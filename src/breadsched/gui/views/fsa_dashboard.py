"""Dedicated FSA benefit-year and healthcare-claim dashboard."""

from __future__ import annotations

from ...gen.engine import fsa, fsa_claims
from ..gi_setup import Gtk
from ._base import BaseView

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
        claims = Gtk.Button(label="Manage FSA claims…")
        claims.connect("clicked", self._on_fsa_claims)
        bar.append(claims)
        self.append(bar)

        self.fsa_heading = self._heading("FSA benefit years")
        self.append(self.fsa_heading)
        self.fsa_grid = self._grid()
        self.append(self.fsa_grid)

        self.claim_heading = self._heading("Open FSA claims")
        self.append(self.claim_heading)
        self.claim_grid = self._grid()
        self.append(self.claim_grid)

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
        self._render_years()
        self._render_claims()

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
            "Remaining",
            "Forfeited",
        )
        for column_index, heading in enumerate(headings):
            label = Gtk.Label(label=heading, xalign=1 if column_index >= 3 else 0)
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
                status.remaining.format(),
                status.forfeited.format(),
            )
            for column_index, value in enumerate(values):
                label = Gtk.Label(label=value, xalign=1 if column_index >= 3 else 0)
                if column_index >= 3:
                    label.add_css_class("numeric")
                self.fsa_grid.attach(label, column_index, row_index, 1, 1)

    def _render_claims(self) -> None:
        _empty(self.claim_grid)
        assert self.db is not None
        summaries = [
            fsa_claims.claim_summary(self.db, claim) for claim in fsa_claims.iter_claims(self.db)
        ]
        summaries = [
            summary
            for summary in summaries
            if summary.status is not fsa_claims.FsaClaimStatus.FULLY_REIMBURSED
        ]
        self.claim_heading.set_visible(bool(summaries))
        self.claim_grid.set_visible(bool(summaries))
        if not summaries:
            return
        headings = (
            "Service date",
            "Provider",
            "Status",
            "Net paid",
            "Reimbursed",
            "Rejected",
            "Remaining",
            "Action",
        )
        for column_index, heading in enumerate(headings):
            label = Gtk.Label(label=heading, xalign=1 if column_index >= 3 else 0)
            label.add_css_class("summary-label")
            self.claim_grid.attach(label, column_index, 0, 1, 1)
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
                self.claim_grid.attach(label, column_index, row_index, 1, 1)
            review = Gtk.Button(label="Review claim")
            review.connect(
                "clicked",
                lambda _button, handle=summary.claim.handle: self._open_claim(handle),
            )
            self.claim_grid.attach(review, 7, row_index, 1, 1)

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
