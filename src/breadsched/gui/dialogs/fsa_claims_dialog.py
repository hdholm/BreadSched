"""Create and maintain FSA healthcare service episodes."""

from __future__ import annotations

from datetime import date

from ...gen.db.sqlite import DbSQLite
from ...gen.engine import fsa_claims
from ...gen.lib import (
    AccountClass,
    AccountPlanningRole,
    FsaClaim,
    FsaClaimAllocation,
    FsaClaimRejection,
    FsaClaimSplitLink,
    Money,
)
from ..gi_setup import Gtk

__all__ = ["FsaClaimsDialog"]


class _LinkList(Gtk.Box):
    def __init__(self, candidates: list[tuple[str, str, str]]) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        self._checks: list[tuple[Gtk.CheckButton, FsaClaimSplitLink]] = []
        for transaction, split, label in candidates:
            check = Gtk.CheckButton(label=label)
            self.append(check)
            self._checks.append((check, FsaClaimSplitLink(transaction, split)))

    def set_links(self, links: list[FsaClaimSplitLink]) -> None:
        selected = {(item.transaction, item.split) for item in links}
        for check, link in self._checks:
            check.set_active((link.transaction, link.split) in selected)

    def links(self) -> list[FsaClaimSplitLink]:
        return [link for check, link in self._checks if check.get_active()]


class _AllocationRow(Gtk.Frame):
    def __init__(self, db: DbSQLite, candidates, allocation=None) -> None:
        super().__init__()
        self.db = db
        self.accounts = [
            account for account in db.iter_accounts()
            if account.planning_role is AccountPlanningRole.FSA
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
            self.rejections.set_text("; ".join(
                f"{item.attempted_on.isoformat()} | {item.amount.to_decimal()} | {item.reason}"
                for item in allocation.rejections
            ))

    def _refresh_years(self, *_args) -> None:
        if not self.accounts:
            self.year.set_model(Gtk.StringList.new([]))
            return
        account = self.accounts[self.account.get_selected()]
        labels = [f"{year.start} – {year.through}" for year in account.fsa_years]
        self.year.set_model(Gtk.StringList.new(labels))

    def value(self) -> FsaClaimAllocation:
        if not self.accounts:
            raise ValueError("Create an FSA account and funding year first")
        account = self.accounts[self.account.get_selected()]
        if not account.fsa_years:
            raise ValueError("Selected FSA account has no funding year")
        year = account.fsa_years[self.year.get_selected()]
        target_text = self.target.get_text().strip()
        rejections: list[FsaClaimRejection] = []
        for raw in self.rejections.get_text().split(";"):
            raw = raw.strip()
            if not raw:
                continue
            parts = [part.strip() for part in raw.split("|", 2)]
            if len(parts) < 2:
                raise ValueError("Rejected attempts use date | amount | reason")
            rejections.append(FsaClaimRejection(
                date.fromisoformat(parts[0]), Money(parts[1]), parts[2] if len(parts) > 2 else ""
            ))
        return FsaClaimAllocation(
            account=account.handle,
            funding_year_start=year.start,
            target=Money(target_text) if target_text else None,
            reimbursements=self.reimburse.links(),
            rejections=rejections,
        )


class FsaClaimsDialog(Gtk.Window):
    """GTK maintenance surface for FSA claims and reconciliation links."""

    def __init__(self, parent: Gtk.Window, db: DbSQLite) -> None:
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
        for row, (label, widget) in enumerate((
            ("Service date", self.service), ("Provider", self.provider),
            ("Description", self.description), ("EOB responsibility", self.eob),
        )):
            fields.attach(Gtk.Label(label=label, xalign=0), 0, row, 1, 1)
            fields.attach(widget, 1, row, 1, 1)
        outer.append(fields)

        payments, refunds, reimbursements = self._candidates()
        outer.append(Gtk.Label(label="Healthcare payments", xalign=0))
        self.payments = _LinkList(payments)
        outer.append(Gtk.ScrolledWindow(child=self.payments, min_content_height=90))
        outer.append(Gtk.Label(label="Provider refunds / credits", xalign=0))
        self.refunds = _LinkList(refunds)
        outer.append(Gtk.ScrolledWindow(child=self.refunds, min_content_height=75))
        self.reimbursement_candidates = reimbursements

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
        self._load(self.claims[0] if self.claims else None)

    def _candidates(self):
        payments, refunds, reimbursements = [], [], []
        for transaction in self.db.iter_transactions():
            for split in transaction.splits:
                account = self.db.get_account(split.account)
                if account is None:
                    continue
                amount = abs(split.value).format()
                label = f"{transaction.post_date} {transaction.description} — {amount}"
                item = (transaction.handle, split.handle, label)
                if account.account_class is AccountClass.EXPENSE and split.value > 0:
                    payments.append(item)
                if account.account_class is AccountClass.EXPENSE and split.value < 0:
                    refunds.append(item)
                if account.planning_role is AccountPlanningRole.FSA and split.value < 0:
                    reimbursements.append(item)
        return payments[-250:], refunds[-250:], reimbursements[-250:]

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

    def _load(self, claim: FsaClaim | None) -> None:
        self.current = claim
        self.service.set_text(claim.service_date.isoformat() if claim else date.today().isoformat())
        self.provider.set_text(claim.provider if claim else "")
        self.description.set_text(claim.description if claim else "")
        self.eob.set_text(
            str(claim.eob_responsibility.to_decimal())
            if claim and claim.eob_responsibility is not None else ""
        )
        self.payments.set_links(claim.payments if claim else [])
        self.refunds.set_links(claim.refunds if claim else [])
        self._clear_allocations()
        if claim:
            for allocation in claim.allocations:
                self._add_allocation(allocation)

    def _save(self, _button) -> None:
        try:
            eob_text = self.eob.get_text().strip()
            claim = FsaClaim(
                handle=self.current.handle if self.current else None,
                service_date=date.fromisoformat(self.service.get_text().strip()),
                provider=self.provider.get_text().strip(),
                description=self.description.get_text().strip(),
                eob_responsibility=Money(eob_text) if eob_text else None,
                payments=self.payments.links(), refunds=self.refunds.links(),
                allocations=[row.value() for row in self._allocation_rows],
            ) if self.current else FsaClaim(
                service_date=date.fromisoformat(self.service.get_text().strip()),
                provider=self.provider.get_text().strip(),
                description=self.description.get_text().strip(),
                eob_responsibility=Money(eob_text) if eob_text else None,
                payments=self.payments.links(), refunds=self.refunds.links(),
                allocations=[row.value() for row in self._allocation_rows],
            )
            fsa_claims.save_claim(self.db, claim)
        except (ValueError, IndexError) as exc:
            self.status.set_text(str(exc))
            return
        self.current = claim
        self.status.set_text("Claim saved.")

    def _delete(self, _button) -> None:
        if self.current is None:
            return
        fsa_claims.delete_claim(self.db, self.current.handle)
        self.status.set_text("Claim deleted.")
        self._load(None)
