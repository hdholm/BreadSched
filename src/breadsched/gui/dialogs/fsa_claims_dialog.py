"""Create and maintain FSA healthcare service episodes."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from ...gen.db.sqlite import DbSQLite
from ...gen.engine import fsa_claims
from ...gen.lib import (
    AccountClass,
    AccountType,
    FsaClaim,
    FsaClaimSplitLink,
    Money,
)
from ...gen.services import (
    ClaimAllocationInput,
    ClaimInput,
    ClaimLinkInput,
    ClaimRejectionInput,
    DeleteClaim,
    SaveClaim,
    delete_claim,
    save_claim,
)
from ...gen.utils.amount_input import parse_user_amount
from ...presentation import service_error_message
from ..gi_setup import GLib, Gtk

__all__ = ["FsaClaimsDialog"]


class _LinkList(Gtk.Box):
    def __init__(self, candidates: list[tuple[str, str, date, str]]) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        self._checks: list[tuple[Gtk.CheckButton, ClaimLinkInput, date]] = []
        self.set_candidates(candidates)

    def set_candidates(self, candidates: list[tuple[str, str, date, str]]) -> None:
        child = self.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self.remove(child)
            child = following
        self._checks.clear()
        for transaction, split, when, label in candidates:
            check = Gtk.CheckButton(label=label)
            self.append(check)
            self._checks.append((check, ClaimLinkInput(transaction, split), when))

    def set_links(self, links: Sequence[FsaClaimSplitLink | ClaimLinkInput]) -> None:
        selected = {(item.transaction, item.split) for item in links}
        for check, link, _when in self._checks:
            check.set_active((link.transaction, link.split) in selected)

    def links(self) -> list[ClaimLinkInput]:
        return [link for check, link, _when in self._checks if check.get_active()]

    def index_on_or_after(self, when: date) -> int:
        for index, (_check, _link, candidate_date) in enumerate(self._checks):
            if candidate_date >= when:
                return index
        return max(0, len(self._checks) - 1)

    def __len__(self) -> int:
        return len(self._checks)


class _AllocationRow(Gtk.Frame):
    def __init__(self, db: DbSQLite, candidates, allocation=None) -> None:
        super().__init__()
        self.db = db
        self.accounts = [
            account for account in db.iter_accounts() if account.atype is AccountType.FSA
        ]
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.set_child(box)
        top = Gtk.Box(spacing=6)
        self.account = Gtk.DropDown.new_from_strings(
            [db.full_name(account) for account in self.accounts]
        )
        self.account.connect("notify::selected", self._refresh_years)
        top.append(self.account)
        self.year = Gtk.DropDown.new_from_strings([])
        top.append(self.year)
        self.target = Gtk.Entry(placeholder_text="Target amount (optional)")
        top.append(self.target)
        box.append(top)
        box.append(Gtk.Label(label="Reimbursement/payment splits", xalign=0))
        self.reimburse = _LinkList(candidates)
        scroll = Gtk.ScrolledWindow(child=self.reimburse, min_content_height=90)
        box.append(scroll)
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
            for index, year in enumerate(selected_account.fsa_years):
                if year.start == allocation.funding_year_start:
                    self.year.set_selected(index)
                    break
            if allocation.target is not None:
                self.target.set_text(str(allocation.target.to_decimal()))
            self.reimburse.set_links(allocation.reimbursements)
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
        labels = [f"{year.start} – {year.through}" for year in account.fsa_years]
        self.year.set_model(Gtk.StringList.new(labels))

    def value(self) -> ClaimAllocationInput:
        if not self.accounts:
            raise ValueError("Create an FSA account and funding year first")
        account = self.accounts[self.account.get_selected()]
        if not account.fsa_years:
            raise ValueError("Selected FSA account has no funding year")
        year = account.fsa_years[self.year.get_selected()]
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
        )


class FsaClaimsDialog(Gtk.Window):
    """GTK maintenance surface for FSA claims and reconciliation links."""

    def __init__(self, parent: Gtk.Window, db: DbSQLite, claim_handle: str | None = None) -> None:
        super().__init__(title="FSA claims", transient_for=parent)
        self.db = db
        self.set_default_size(860, 700)
        self.claims = fsa_claims.iter_claims(db)
        self.current: FsaClaim | None = None
        self._allocation_rows: list[_AllocationRow] = []
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for side in ("top", "bottom", "start", "end"):
            getattr(outer, f"set_margin_{side}")(12)
        self.set_child(outer)

        top = Gtk.Box(spacing=6)
        self.claim_select = Gtk.DropDown.new_from_strings(
            [f"{claim.service_date} {claim.provider or 'FSA claim'}" for claim in self.claims]
        )
        self.claim_select.connect("notify::selected", self._load_selected)
        top.append(self.claim_select)
        new = Gtk.Button(label="New")
        new.connect("clicked", lambda *_: self._load(None))
        top.append(new)
        delete = Gtk.Button(label="Delete")
        delete.connect("clicked", self._delete)
        top.append(delete)
        outer.append(top)

        fields = Gtk.Grid(column_spacing=8, row_spacing=6)
        self.service = Gtk.Entry(placeholder_text="YYYY-MM-DD")
        self.provider = Gtk.Entry(placeholder_text="Provider")
        self.description = Gtk.Entry(placeholder_text="Description")
        self.eob = Gtk.Entry(placeholder_text="EOB patient responsibility")
        for row, (label, widget) in enumerate(
            (
                ("Service date", self.service),
                ("Provider", self.provider),
                ("Description", self.description),
                ("EOB responsibility", self.eob),
            )
        ):
            fields.attach(Gtk.Label(label=label, xalign=0), 0, row, 1, 1)
            fields.attach(widget, 1, row, 1, 1)
        outer.append(fields)

        payments, refunds, reimbursements = self._candidates()
        self._payment_candidates = payments
        self._refund_candidates = refunds
        self.reimbursement_candidates = reimbursements
        outer.append(Gtk.Label(label="Healthcare payments", xalign=0))
        self.payments = _LinkList(payments)
        self.payments_scroll = Gtk.ScrolledWindow(child=self.payments, min_content_height=90)
        outer.append(self.payments_scroll)
        outer.append(Gtk.Label(label="Provider refunds / credits", xalign=0))
        self.refunds = _LinkList(refunds)
        self.refunds_scroll = Gtk.ScrolledWindow(child=self.refunds, min_content_height=75)
        outer.append(self.refunds_scroll)
        self.service.connect("changed", self._service_changed)

        allocation_bar = Gtk.Box(spacing=6)
        allocation_bar.append(Gtk.Label(label="FSA allocations", xalign=0))
        add = Gtk.Button(label="Add allocation")
        add.connect("clicked", lambda *_: self._add_allocation())
        allocation_bar.append(add)
        outer.append(allocation_bar)
        self.allocations = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        outer.append(Gtk.ScrolledWindow(child=self.allocations, min_content_height=180))
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
        selected = 0
        if claim_handle is not None:
            selected = next(
                (index for index, claim in enumerate(self.claims) if claim.handle == claim_handle),
                0,
            )
        if self.claims:
            self.claim_select.set_selected(selected)
            self._load(self.claims[selected])
        else:
            self._load(None)

    def _candidates(self):
        payments, refunds, reimbursements = [], [], []
        fsa_years = [
            year
            for account in self.db.iter_accounts()
            if account.atype is AccountType.FSA
            for year in account.fsa_years
        ]
        candidate_start = min((year.start for year in fsa_years), default=None)
        for transaction in self.db.iter_transactions():
            if candidate_start is not None and transaction.post_date < candidate_start:
                continue
            for split in transaction.splits:
                account = self.db.get_account(split.account)
                if account is None:
                    continue
                amount = abs(split.value).format()
                label = f"{transaction.post_date} {transaction.description} — {amount}"
                item = (transaction.handle, split.handle, transaction.post_date, label)
                if account.account_class is AccountClass.EXPENSE and split.value > 0:
                    payments.append(item)
                if account.account_class is AccountClass.EXPENSE and split.value < 0:
                    refunds.append(item)
                if account.atype is AccountType.FSA and split.value < 0:
                    reimbursements.append(item)
        return payments, refunds, reimbursements

    def _claim_candidates(
        self,
        candidates: list[tuple[str, str, date, str]],
        service_date: date,
        selected: Sequence[FsaClaimSplitLink | ClaimLinkInput],
    ) -> list[tuple[str, str, date, str]]:
        window = fsa_claims.claim_year_window(self.db, service_date)
        if window is None:
            return candidates
        start, _through = window
        selected_keys = {(item.transaction, item.split) for item in selected}
        return [
            item for item in candidates if start <= item[2] or (item[0], item[1]) in selected_keys
        ]

    def _refresh_claim_candidates(
        self,
        service_date: date,
        payments: Sequence[FsaClaimSplitLink | ClaimLinkInput],
        refunds: Sequence[FsaClaimSplitLink | ClaimLinkInput],
    ) -> None:
        self.payments.set_candidates(
            self._claim_candidates(self._payment_candidates, service_date, payments)
        )
        self.refunds.set_candidates(
            self._claim_candidates(self._refund_candidates, service_date, refunds)
        )
        self.payments.set_links(payments)
        self.refunds.set_links(refunds)
        self._scroll_candidates_to(service_date)

    def _service_changed(self, _entry) -> None:
        try:
            service_date = date.fromisoformat(self.service.get_text().strip())
        except ValueError:
            return
        self._refresh_claim_candidates(service_date, self.payments.links(), self.refunds.links())

    def _clear_allocations(self) -> None:
        child = self.allocations.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self.allocations.remove(child)
            child = following
        self._allocation_rows.clear()

    def _add_allocation(self, allocation=None) -> None:
        row = _AllocationRow(self.db, self.reimbursement_candidates, allocation)
        self.allocations.append(row)
        self._allocation_rows.append(row)

    def _load_selected(self, *_args) -> None:
        if self.claims:
            self._load(self.claims[self.claim_select.get_selected()])

    def _scroll_candidates_to(self, when: date) -> None:
        def scroll() -> bool:
            for links, window in (
                (self.payments, self.payments_scroll),
                (self.refunds, self.refunds_scroll),
            ):
                if len(links) < 2:
                    continue
                index = links.index_on_or_after(when)
                adjustment = window.get_vadjustment()
                span = max(0.0, adjustment.get_upper() - adjustment.get_page_size())
                adjustment.set_value(span * index / (len(links) - 1))
            return False

        GLib.idle_add(scroll)

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
        service_date = claim.service_date if claim else date.fromisoformat(self.service.get_text())
        self._refresh_claim_candidates(
            service_date, claim.payments if claim else [], claim.refunds if claim else []
        )
        self._clear_allocations()
        if claim:
            for allocation in claim.allocations:
                self._add_allocation(allocation)

    def _save(self, _button) -> None:
        try:
            eob_text = self.eob.get_text().strip()
            result = save_claim(
                self.db,
                SaveClaim(
                    ClaimInput(
                        service_date=date.fromisoformat(self.service.get_text().strip()),
                        provider=self.provider.get_text().strip(),
                        description=self.description.get_text().strip(),
                        eob_responsibility=Money(parse_user_amount(eob_text)) if eob_text else None,
                        payments=tuple(self.payments.links()),
                        refunds=tuple(self.refunds.links()),
                        allocations=tuple(row.value() for row in self._allocation_rows),
                    ),
                    existing_handle=self.current.handle if self.current else None,
                ),
            )
        except (ValueError, IndexError) as exc:
            self.status.set_text(str(exc))
            return
        if result.value is None:
            self.status.set_text(service_error_message(result.errors[0]))
            return
        self.current = self.db.get_fsa_claim(result.value.handle)
        self.status.set_text("Claim saved.")

    def _delete(self, _button) -> None:
        if self.current is None:
            return
        result = delete_claim(self.db, DeleteClaim(self.current.handle))
        if result.value is None:
            self.status.set_text(service_error_message(result.errors[0]))
            return
        self.status.set_text("Claim deleted.")
        self._load(None)
