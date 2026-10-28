"""Create and maintain FSA healthcare service episodes."""

from __future__ import annotations

import weakref
from collections.abc import Callable, Sequence
from datetime import date

from ...api import ApiError
from ...api.local import book_server
from ...gen.db.sqlite import DbSQLite
from ...gen.engine import fsa_claims
from ...gen.lib import (
    FsaClaim,
    Money,
    SplitLink,
)
from ...gen.lib.fsa_claim import FsaClaimEvent
from ...gen.services import (
    ClaimAllocationInput,
    ClaimInput,
    ClaimLinkInput,
    ClaimRejectionInput,
    CloseClaim,
    ReopenClaim,
    SaveClaim,
)
from ...gen.utils.amount_input import parse_user_amount
from ...presentation import shared_cost_text
from ..gi_setup import GLib, Gtk
from ..widgets.bounded import BoundedWindow, scroll_body
from ..widgets.choice import bounded_dropdown
from ..widgets.confirm import delete_button
from ..widgets.help import help_row

__all__ = ["FsaClaimsDialog"]

#: The claim chooser's first entry, for entering a claim not yet saved.
NEW_CLAIM = "New claim"

#: The open claims window for each book.
_OPEN: weakref.WeakValueDictionary[int, FsaClaimsDialog] = weakref.WeakValueDictionary()


#: How the candidate transactions are limited (#366): near the anchor date (the
#: claim's service date), within the claim's benefit year, or not at all.
RANGES = (("near", "Within 60 days"), ("year", "Claim year"), ("all", "All dates"))
NEAR_DAYS = 60
#: At most this many candidates are listed at once; searching narrows the rest.
SHOWN_LIMIT = 200


class _LinkList(Gtk.Box):
    """Linked splits pinned on top, and a searchable, limited list to add from.

    A claim's linked transactions stay in view however long the candidate list
    is, each with **Remove**; the candidates below can be searched by date,
    description, or amount and limited to dates near the service date or within
    the claim's benefit year (#366). Earlier, every candidate since the first FSA
    year was a check box in one small scrolling box.
    """

    def __init__(self, candidates: list[tuple[str, str, date, str]]) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._candidates: list[tuple[ClaimLinkInput, date, str]] = []
        self._linked: list[ClaimLinkInput] = []
        self._anchor: date | None = None
        self._window: tuple[date, date] | None = None
        self.linked_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.linked_box.add_css_class("boxed-list")
        self.append(self.linked_box)
        filters = Gtk.Box(spacing=6)
        self.search = Gtk.SearchEntry(
            hexpand=True, placeholder_text="Search date, description, amount"
        )
        self.search.connect("search-changed", lambda *_: self._show_candidates())
        filters.append(self.search)
        self.range = bounded_dropdown([label for _key, label in RANGES])
        self.range.connect("notify::selected", lambda *_: self._show_candidates())
        filters.append(self.range)
        self.count = Gtk.Label(xalign=1)
        self.count.add_css_class("dim")
        filters.append(self.count)
        self.append(filters)
        self.candidate_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        scroll = Gtk.ScrolledWindow(child=self.candidate_box, min_content_height=140)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.append(scroll)
        self.set_candidates(candidates)

    def set_candidates(self, candidates: list[tuple[str, str, date, str]]) -> None:
        self._candidates = [
            (ClaimLinkInput(transaction, split), when, label)
            for transaction, split, when, label in candidates
        ]
        self._render()

    def set_anchor(self, when: date | None, window: tuple[date, date] | None = None) -> None:
        """The service date the candidates are near, and the claim's benefit year."""
        self._anchor = when
        self._window = window
        self._show_candidates()

    def set_links(self, links: Sequence[SplitLink | ClaimLinkInput]) -> None:
        self._linked = [ClaimLinkInput(item.transaction, item.split) for item in links]
        self._render()

    def links(self) -> list[ClaimLinkInput]:
        return list(self._linked)

    def add(self, link: ClaimLinkInput) -> None:
        if link not in self._linked:
            self._linked.append(link)
            self._render()

    def remove_link(self, link: ClaimLinkInput) -> None:
        if link in self._linked:
            self._linked.remove(link)
            self._render()

    def _label(self, link: ClaimLinkInput) -> str:
        return next(
            (label for item, _when, label in self._candidates if item == link),
            "(a transaction outside this list)",
        )

    def _render(self) -> None:
        self.linked_box.remove_all()
        for link in self._linked:
            row = Gtk.Box(spacing=6)
            text = Gtk.Label(label=self._label(link), xalign=0, hexpand=True, wrap=True)
            row.append(text)
            remove = Gtk.Button(label="Remove")
            remove.connect("clicked", lambda _b, item=link: self.remove_link(item))
            row.append(remove)
            self.linked_box.append(row)
        self.linked_box.set_visible(bool(self._linked))
        self._show_candidates()

    def visible_candidates(self) -> list[tuple[ClaimLinkInput, date, str]]:
        """The unlinked candidates the search and range let through, before the limit."""
        key = RANGES[min(self.range.get_selected(), len(RANGES) - 1)][0]
        words = self.search.get_text().casefold().split()
        shown = []
        for link, when, label in self._candidates:
            if link in self._linked:
                continue
            if key == "near" and self._anchor is not None:
                if abs((when - self._anchor).days) > NEAR_DAYS:
                    continue
            elif key == "year" and self._window is not None:
                if not self._window[0] <= when <= self._window[1]:
                    continue
            if any(word not in label.casefold() for word in words):
                continue
            shown.append((link, when, label))
        return shown

    def _show_candidates(self) -> None:
        self.candidate_box.remove_all()
        shown = self.visible_candidates()
        for link, _when, label in shown[:SHOWN_LIMIT]:
            row = Gtk.Box(spacing=6)
            row.append(Gtk.Label(label=label, xalign=0, hexpand=True, wrap=True))
            add = Gtk.Button(label="Add")
            add.connect("clicked", lambda _b, item=link: self.add(item))
            row.append(add)
            self.candidate_box.append(row)
        if len(shown) > SHOWN_LIMIT:
            self.count.set_text(f"First {SHOWN_LIMIT} of {len(shown)}; search to narrow")
        else:
            self.count.set_text(f"{len(shown)} to add")

    def __len__(self) -> int:
        return len(self._candidates)


def _event_text(event: FsaClaimEvent) -> str:
    def amount(value: Money | None) -> str:
        return value.format() if value is not None else "none"

    if event.kind == FsaClaimEvent.EOB_CHANGED:
        text = f"EOB changed from {amount(event.previous)} to {amount(event.current)}"
    else:
        text = "Closed" if event.kind == FsaClaimEvent.CLOSED else "Reopened"
    return f"{event.on.isoformat()} {text}" + (f": {event.note}" if event.note else "")


class _AllocationRow(Gtk.Frame):
    def __init__(self, fsa_accounts: tuple, candidates, allocation=None, repayments=()) -> None:
        super().__init__()
        #: The FSA accounts with their funding years, as the book server listed them.
        self.accounts = list(fsa_accounts)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.set_child(box)
        top = Gtk.Box(spacing=6)
        self.account = bounded_dropdown([account.name for account in self.accounts])
        self.account.connect("notify::selected", self._refresh_years)
        top.append(self.account)
        self.year = bounded_dropdown([])
        top.append(self.year)
        self.target = Gtk.Entry(placeholder_text="Target amount (optional)")
        top.append(self.target)
        box.append(top)
        box.append(Gtk.Label(label="Reimbursement/payment splits", xalign=0))
        self.reimburse = _LinkList(candidates)
        box.append(self.reimburse)
        box.append(Gtk.Label(label="Repaid to the FSA (money paid back into it)", xalign=0))
        self.repay = _LinkList(list(repayments))
        box.append(self.repay)
        box.append(Gtk.Label(label="Rejected/failed attempts (date | amount | reason)", xalign=0))
        self.rejections = Gtk.Entry(placeholder_text="2026-03-01 | 125.00 | receipt required")
        box.append(self.rejections)
        if allocation is not None:
            for index, account in enumerate(self.accounts):
                if account.handle == allocation.account:
                    self.account.set_selected(index)
                    break
        self._refresh_years()
        if allocation is not None:
            selected_account = self.accounts[self.account.get_selected()]
            for index, year in enumerate(selected_account.years):
                if year.start == allocation.funding_year_start:
                    self.year.set_selected(index)
                    break
            if allocation.target is not None:
                self.target.set_text(str(allocation.target.to_decimal()))
            self.reimburse.set_links(allocation.reimbursements)
            self.repay.set_links(allocation.repayments)
            self.rejections.set_text(
                "; ".join(
                    f"{item.attempted_on.isoformat()} | {item.amount.to_decimal()} | {item.reason}"
                    for item in allocation.rejections
                )
            )

    def _refresh_years(self, *_args) -> None:
        if not self.accounts:
            self.year.set_model(Gtk.StringList.new([]))
            return
        account = self.accounts[self.account.get_selected()]
        labels = [f"{year.start} – {year.through}" for year in account.years]
        self.year.set_model(Gtk.StringList.new(labels))

    def value(self) -> ClaimAllocationInput:
        if not self.accounts:
            raise ValueError("Create an FSA account and funding year first")
        account = self.accounts[self.account.get_selected()]
        if not account.years:
            raise ValueError("Selected FSA account has no funding year")
        year = account.years[self.year.get_selected()]
        target_text = self.target.get_text().strip()
        rejections: list[ClaimRejectionInput] = []
        for raw in self.rejections.get_text().split(";"):
            raw = raw.strip()
            if not raw:
                continue
            parts = [part.strip() for part in raw.split("|", 2)]
            if len(parts) < 2:
                raise ValueError("Rejected attempts use date | amount | reason")
            rejections.append(
                ClaimRejectionInput(
                    date.fromisoformat(parts[0]),
                    Money(parse_user_amount(parts[1])),
                    parts[2] if len(parts) > 2 else "",
                )
            )
        return ClaimAllocationInput(
            account=account.handle,
            funding_year_start=year.start,
            target=Money(parse_user_amount(target_text)) if target_text else None,
            reimbursements=tuple(self.reimburse.links()),
            rejections=tuple(rejections),
            repayments=tuple(self.repay.links()),
        )


class FsaClaimsDialog(BoundedWindow):
    """GTK maintenance surface for FSA claims and reconciliation links."""

    @classmethod
    def present_for(
        cls, parent: Gtk.Window | None, db: DbSQLite, claim_handle: str | None = None
    ) -> FsaClaimsDialog:
        """Show the book's claims window, opening one only if none is open (#366)."""
        existing = _OPEN.get(id(db))
        if existing is not None and existing.db is db:
            if claim_handle is not None:
                existing.show_claim(claim_handle)
            existing.present()
            return existing
        dialog = cls(parent, db, claim_handle)
        dialog.present()
        return dialog

    def __init__(self, parent: Gtk.Window, db: DbSQLite, claim_handle: str | None = None) -> None:
        super().__init__(title="FSA claims", transient_for=parent)
        self.db = db
        _OPEN[id(db)] = self
        self.set_default_size(860, 700)
        view = book_server(db).fsa_claims()
        self.claims = [summary.claim for summary in view.claims]
        #: FSA accounts with their years, and payers, as the book server listed them.
        self.fsa_accounts = view.candidates.fsa_accounts
        self.current: FsaClaim | None = None
        self._allocation_rows: list[_AllocationRow] = []
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for side in ("top", "bottom", "start", "end"):
            getattr(outer, f"set_margin_{side}")(12)
        self.set_child(outer)
        outer.prepend(help_row("fsa-claims"))

        top = Gtk.Box(spacing=6)
        # The first choice is a new claim, so New (or choosing it) never leaves an
        # old claim showing as chosen above an empty form (#366).
        self.claim_select = bounded_dropdown([NEW_CLAIM])
        self._choosing = False
        self.claim_select.connect("notify::selected", self._load_selected)
        top.append(self.claim_select)
        new = Gtk.Button(label="New")
        new.connect("clicked", lambda *_: self.new_claim())
        top.append(new)
        delete = delete_button(
            lambda: (
                f"the claim {self.current.description or self.current.provider}"
                if self.current is not None
                else "this claim"
            ),
            lambda: self._delete(None),
        )
        top.append(delete)
        outer.append(top)

        fields = Gtk.Grid(column_spacing=8, row_spacing=6)
        self.service = Gtk.Entry(placeholder_text="YYYY-MM-DD")
        self.provider = Gtk.Entry(placeholder_text="Provider")
        self.description = Gtk.Entry(placeholder_text="Description")
        self.eob = Gtk.Entry(placeholder_text="EOB patient responsibility")
        self.eob_note = Gtk.Entry(placeholder_text="Why the EOB changed (kept in the history)")
        # A payer (an insurer) covering part of this same expense leaves the FSA
        # only the rest (issue #192); linked only from here, never inferred.
        self.receivables = list(view.candidates.receivables)
        self.payer = bounded_dropdown(["None"] + [item.name for item in self.receivables])
        self.payer.set_tooltip_text(
            "A reimbursable expense whose payer covers part of this bill; "
            "the FSA then claims only the rest"
        )
        for row, (label, widget) in enumerate(
            (
                ("Service date", self.service),
                ("Provider", self.provider),
                ("Description", self.description),
                ("EOB responsibility", self.eob),
                ("EOB change note", self.eob_note),
                ("Payer covers part", self.payer),
            )
        ):
            fields.attach(Gtk.Label(label=label, xalign=0), 0, row, 1, 1)
            fields.attach(widget, 1, row, 1, 1)
        outer.append(fields)
        self.shared = Gtk.Label(xalign=0, wrap=True)
        self.shared.set_visible(False)
        outer.append(self.shared)

        payments, refunds, reimbursements, repayments = self._candidates()
        self.reimbursement_candidates = reimbursements
        self.repayment_candidates = repayments
        outer.append(Gtk.Label(label="Healthcare payments", xalign=0))
        self.payments = _LinkList(payments)
        outer.append(self.payments)
        outer.append(Gtk.Label(label="Provider refunds / credits", xalign=0))
        self.refunds = _LinkList(refunds)
        outer.append(self.refunds)
        self.service.connect("changed", self._service_changed)

        allocation_bar = Gtk.Box(spacing=6)
        allocation_bar.append(Gtk.Label(label="FSA allocations", xalign=0))
        add = Gtk.Button(label="Add allocation")
        add.connect("clicked", lambda *_: self._add_allocation())
        allocation_bar.append(add)
        outer.append(allocation_bar)
        self.allocations = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        outer.append(Gtk.ScrolledWindow(child=self.allocations, min_content_height=180))
        # Where the saved claim stands, and its corrections (EOB changes,
        # closings, reopenings), oldest first.
        self.standing = Gtk.Label(xalign=0, wrap=True)
        outer.append(self.standing)
        self.history = Gtk.Label(xalign=0, wrap=True)
        self.history.add_css_class("dim-label")
        outer.append(self.history)
        closing = Gtk.Box(spacing=6)
        self.close_reason = Gtk.Entry(placeholder_text="Why close, or why reopen", hexpand=True)
        closing.append(self.close_reason)
        self.close_button = Gtk.Button(label="Close claim")
        self.close_button.set_tooltip_text(
            "Stop pursuing what is left to reimburse; it is recorded as given up"
        )
        self.close_button.connect("clicked", self._close_claim)
        closing.append(self.close_button)
        self.reopen_button = Gtk.Button(label="Reopen claim")
        self.reopen_button.connect("clicked", self._reopen_claim)
        closing.append(self.reopen_button)
        outer.append(closing)
        self.status = Gtk.Label(xalign=0, wrap=True)
        outer.append(self.status)
        actions = Gtk.Box(spacing=6)
        save = Gtk.Button(label="Save claim")
        save.connect("clicked", self._save)
        actions.append(save)
        close = Gtk.Button(label="Close")
        close.connect("clicked", lambda *_: self.close())
        actions.append(close)
        outer.append(actions)
        # The form scrolls; its buttons stay on screen (dialog audit, #148).
        scroll_body(self)
        # The dialog is not modal: it can stay open beside a register, and the
        # transactions it offers follow the book (#366).
        self._refresh_source = 0
        self._unsubscribe: Callable[[], None] | None = book_server(db).subscribe(
            self._on_book_changed
        )
        self.connect("destroy", lambda *_: self._disconnect())
        self.connect("close-request", self._on_close_request)
        if claim_handle is None and self.claims:
            claim_handle = self.claims[0].handle
        self._reload_claims(claim_handle)
        self._load(self.current)

    # ----------------------------------------------------------------- life

    def _on_close_request(self, *_args) -> bool:
        self._disconnect()
        return False

    def _disconnect(self) -> None:
        if self._refresh_source:
            GLib.source_remove(self._refresh_source)
            self._refresh_source = 0
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        if _OPEN.get(id(self.db)) is self:
            del _OPEN[id(self.db)]

    def _on_book_changed(self, *_args) -> None:
        if not self._refresh_source:
            self._refresh_source = GLib.idle_add(self._idle_refresh)

    def _idle_refresh(self) -> bool:
        self._refresh_source = 0
        self.refresh_candidates()
        return False

    def refresh_candidates(self) -> None:
        """Offer transactions entered or changed elsewhere, keeping what is linked."""
        if not self.db.is_open:
            return
        payments, refunds, reimbursements, repayments = self._candidates()
        self.reimbursement_candidates = reimbursements
        self.repayment_candidates = repayments
        self.payments.set_candidates(payments)
        self.refunds.set_candidates(refunds)
        for row in self._allocation_rows:
            row.reimburse.set_candidates(reimbursements)
            row.repay.set_candidates(repayments)
        if self.current is not None:
            try:
                self.current = book_server(self.db).find_fsa_claim(self.current.handle)
            except ApiError:
                pass
        self._show_standing()

    def _reload_claims(self, chosen: str | None) -> None:
        """Rebuild the chooser after a save or delete, showing ``chosen``."""
        summaries = book_server(self.db).fsa_claims().claims
        self.claims = [summary.claim for summary in summaries]
        labels = [
            f"{summary.claim.service_date} {summary.claim.provider or 'FSA claim'}"
            f" — {summary.status.label}"
            for summary in summaries
        ]
        self.current = next((claim for claim in self.claims if claim.handle == chosen), None)
        index = next(
            (index + 1 for index, claim in enumerate(self.claims) if claim.handle == chosen), 0
        )
        self._choosing = True
        try:
            self.claim_select.set_model(Gtk.StringList.new([NEW_CLAIM, *labels]))
            self.claim_select.set_selected(index)
        finally:
            self._choosing = False

    def show_claim(self, handle: str) -> None:
        """Show a saved claim, as Review claim on the Dashboard asks."""
        self._reload_claims(handle)
        self._load(self.current)

    def new_claim(self) -> None:
        """Start a new claim: the chooser shows New claim and the form is empty."""
        self._choosing = True
        try:
            self.claim_select.set_selected(0)
        finally:
            self._choosing = False
        self._load(None)
        self.status.set_text("")

    def _years(self) -> list:
        return [year for account in self.fsa_accounts for year in account.years]

    def _candidates(self):
        """What a claim could link, by role, through the book server (#358)."""
        candidates = book_server(self.db).fsa_claims().candidates
        self.fsa_accounts = candidates.fsa_accounts

        def items(found) -> list:
            return [
                (
                    item.transaction,
                    item.split,
                    item.date,
                    f"{item.date} {item.description} — {item.amount.format()}",
                )
                for item in found
            ]

        return (
            items(candidates.payments),
            items(candidates.refunds),
            items(candidates.reimbursements),
            items(candidates.repayments),
        )

    def _link_lists(self) -> list[_LinkList]:
        lists = [self.payments, self.refunds]
        for row in self._allocation_rows:
            lists.extend((row.reimburse, row.repay))
        return lists

    def _anchor(self, service_date: date) -> None:
        """Limit every candidate list to around this service date and its year."""
        window = fsa_claims.year_window(self._years(), service_date)
        for links in self._link_lists():
            links.set_anchor(service_date, window)

    def _service_changed(self, _entry) -> None:
        try:
            service_date = date.fromisoformat(self.service.get_text().strip())
        except ValueError:
            return
        self._anchor(service_date)

    def _clear_allocations(self) -> None:
        child = self.allocations.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self.allocations.remove(child)
            child = following
        self._allocation_rows.clear()

    def _add_allocation(self, allocation=None) -> None:
        row = _AllocationRow(
            self.fsa_accounts, self.reimbursement_candidates, allocation, self.repayment_candidates
        )
        self.allocations.append(row)
        self._allocation_rows.append(row)
        try:
            service_date = date.fromisoformat(self.service.get_text().strip())
        except ValueError:
            return
        window = fsa_claims.year_window(self._years(), service_date)
        for links in (row.reimburse, row.repay):
            links.set_anchor(service_date, window)

    def _load_selected(self, *_args) -> None:
        if self._choosing:
            return
        index = self.claim_select.get_selected()
        self._load(self.claims[index - 1] if 0 < index <= len(self.claims) else None)

    def _load(self, claim: FsaClaim | None) -> None:
        self.current = claim
        self.service.set_text(claim.service_date.isoformat() if claim else date.today().isoformat())
        self.provider.set_text(claim.provider if claim else "")
        self.description.set_text(claim.description if claim else "")
        self.eob.set_text(
            str(claim.eob_responsibility.to_decimal())
            if claim and claim.eob_responsibility is not None
            else ""
        )
        self.eob_note.set_text("")
        self.close_reason.set_text("")
        service_date = claim.service_date if claim else date.fromisoformat(self.service.get_text())
        self.payments.set_links(claim.payments if claim else [])
        self.refunds.set_links(claim.refunds if claim else [])
        self._clear_allocations()
        if claim:
            for allocation in claim.allocations:
                self._add_allocation(allocation)
        self._anchor(service_date)
        self.payer.set_selected(
            next(
                (
                    index + 1
                    for index, item in enumerate(self.receivables)
                    if claim is not None and item.handle == claim.receivable
                ),
                0,
            )
        )
        self._show_shared()
        self._show_standing()

    def _show_standing(self) -> None:
        """The saved claim's status, what is owed either way, and its history."""
        claim = self.current
        self.close_button.set_sensitive(claim is not None and claim.closed_on is None)
        self.reopen_button.set_sensitive(claim is not None and claim.closed_on is not None)
        if claim is None:
            self.standing.set_text("")
            self.history.set_text("")
            return
        try:
            standing = book_server(self.db).fsa_claim_standing(claim.handle)
        except ApiError as error:
            # Deleted elsewhere since it was chosen.
            self.standing.set_text(str(error))
            return
        summary = standing.summary
        if summary is None:
            self.standing.set_text(standing.problem)
            return
        parts = [
            summary.status.label,
            f"reimbursed {summary.reimbursed.format()}",
            f"still to reimburse {summary.remaining_reimbursable.format()}",
        ]
        if summary.repaid > 0:
            parts.append(f"repaid to the FSA {summary.repaid.format()}")
        if summary.over_reimbursed > 0:
            parts.append(f"to repay to the FSA {summary.over_reimbursed.format()}")
        if claim.closed_on is not None:
            parts.append(
                f"closed {claim.closed_on.isoformat()}"
                + (f" ({claim.close_reason})" if claim.close_reason else "")
                + f", gave up {summary.forgone.format()}"
            )
        self.standing.set_text(" — ".join(parts))
        self.history.set_text("\n".join(_event_text(event) for event in claim.events))

    def _close_claim(self, _button) -> None:
        if self.current is None:
            return
        request = CloseClaim(
            self.current.handle, date.today(), self.close_reason.get_text().strip()
        )
        self._after_closing(lambda: book_server(self.db).close_fsa_claim(request), "Claim closed.")

    def _reopen_claim(self, _button) -> None:
        if self.current is None:
            return
        request = ReopenClaim(
            self.current.handle, date.today(), self.close_reason.get_text().strip()
        )
        self._after_closing(
            lambda: book_server(self.db).reopen_fsa_claim(request), "Claim reopened."
        )

    def _after_closing(self, change, message: str) -> None:
        try:
            saved = change()
        except ApiError as error:
            self.status.set_text(str(error))
            return
        self.current = book_server(self.db).find_fsa_claim(saved.handle)
        self.close_reason.set_text("")
        self.status.set_text(message)
        self._show_standing()

    def _show_shared(self) -> None:
        """The payer/FSA/you allocation of the stored claim, when it has a payer."""
        shared = None
        if self.current is not None and self.current.receivable is not None:
            try:
                standing = book_server(self.db).fsa_claim_standing(self.current.handle)
            except ApiError:
                standing = None
            if standing is None or standing.summary is None:
                self.shared.set_text(standing.problem if standing is not None else "")
                self.shared.set_visible(standing is not None)
                return
            shared = standing.summary.shared
        self.shared.set_text(shared_cost_text(shared) if shared is not None else "")
        self.shared.set_visible(shared is not None)

    def _selected_receivable(self) -> str | None:
        index = self.payer.get_selected()
        return self.receivables[index - 1].handle if 0 < index <= len(self.receivables) else None

    def _save(self, _button) -> None:
        try:
            eob_text = self.eob.get_text().strip()
            request = SaveClaim(
                ClaimInput(
                    service_date=date.fromisoformat(self.service.get_text().strip()),
                    provider=self.provider.get_text().strip(),
                    description=self.description.get_text().strip(),
                    eob_responsibility=Money(parse_user_amount(eob_text)) if eob_text else None,
                    payments=tuple(self.payments.links()),
                    refunds=tuple(self.refunds.links()),
                    allocations=tuple(row.value() for row in self._allocation_rows),
                    receivable=self._selected_receivable(),
                    eob_note=self.eob_note.get_text().strip(),
                ),
                existing_handle=self.current.handle if self.current else None,
            )
        except (ValueError, IndexError) as exc:
            self.status.set_text(str(exc))
            return
        try:
            saved = book_server(self.db).save_fsa_claim(request)
        except ApiError as error:
            self.status.set_text(str(error))
            return
        self._reload_claims(saved.handle)
        # Reload what was saved: a payment from the FSA card links its FSA side too.
        self._load(self.current)
        self.status.set_text("Claim saved.")

    def _delete(self, _button) -> None:
        if self.current is None:
            return
        try:
            book_server(self.db).delete_fsa_claim(self.current.handle)
        except ApiError as error:
            self.status.set_text(str(error))
            return
        self._reload_claims(None)
        self._load(None)
        self.status.set_text("Claim deleted.")
