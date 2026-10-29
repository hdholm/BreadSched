"""Typed FSA claim construction and mutation contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..db.sqlite import DbSQLite
from ..engine import fsa, fsa_claim_proposals, fsa_claims
from ..engine.fsa import FsaYearStatus
from ..engine.fsa_claim_report import GROUPINGS, ClaimReport, claim_report
from ..engine.fsa_claims import FsaClaimStatus, FsaClaimSummary
from ..lib.account import AccountClass, AccountType, FsaFundingYear
from ..lib.base import create_handle
from ..lib.fsa_claim import (
    FsaClaim,
    FsaClaimAllocation,
    FsaClaimRejection,
)
from ..lib.money import Money
from ..lib.split_link import SplitLink
from .contracts import ServiceError, ServiceResult
from .savings_goals import NamedChoice


@dataclass(frozen=True, slots=True)
class ClaimLinkInput:
    transaction: str
    split: str


@dataclass(frozen=True, slots=True)
class ClaimRejectionInput:
    attempted_on: date
    amount: Money
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ClaimAllocationInput:
    account: str
    funding_year_start: date
    target: Money | None = None
    reimbursements: tuple[ClaimLinkInput, ...] = ()
    rejections: tuple[ClaimRejectionInput, ...] = ()
    #: Splits paying money back into the FSA for this funding year.
    repayments: tuple[ClaimLinkInput, ...] = ()


@dataclass(frozen=True, slots=True)
class ClaimInput:
    service_date: date
    provider: str = ""
    description: str = ""
    eob_responsibility: Money | None = None
    payments: tuple[ClaimLinkInput, ...] = ()
    refunds: tuple[ClaimLinkInput, ...] = ()
    allocations: tuple[ClaimAllocationInput, ...] = ()
    #: A receivable whose payer covers part of this expense (issue #192).
    receivable: str | None = None
    #: Why an EOB already entered changed (a corrected or late EOB); recorded
    #: in the claim's history only when the EOB does change.
    eob_note: str = ""


@dataclass(frozen=True, slots=True)
class SaveClaim:
    definition: ClaimInput
    existing_handle: str | None = None
    #: The date an EOB change is recorded on; today when omitted.
    changed_on: date | None = None


@dataclass(frozen=True, slots=True)
class SavedClaim:
    handle: str


@dataclass(frozen=True, slots=True)
class DeleteClaim:
    handle: str


@dataclass(frozen=True, slots=True)
class CloseClaim:
    """Stop pursuing what is left to reimburse on ``on``."""

    handle: str
    on: date
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ReopenClaim:
    """Pursue a closed claim again from ``on``."""

    handle: str
    on: date
    note: str = ""


def build_claim(request: SaveClaim) -> FsaClaim:
    definition = request.definition
    return FsaClaim(
        handle=request.existing_handle or create_handle(),
        service_date=definition.service_date,
        provider=definition.provider.strip(),
        description=definition.description.strip(),
        eob_responsibility=definition.eob_responsibility,
        payments=[SplitLink(item.transaction, item.split) for item in definition.payments],
        refunds=[SplitLink(item.transaction, item.split) for item in definition.refunds],
        allocations=[
            FsaClaimAllocation(
                item.account,
                item.funding_year_start,
                item.target,
                [SplitLink(link.transaction, link.split) for link in item.reimbursements],
                [
                    FsaClaimRejection(rejection.attempted_on, rejection.amount, rejection.reason)
                    for rejection in item.rejections
                ],
                [SplitLink(link.transaction, link.split) for link in item.repayments],
            )
            for item in definition.allocations
        ],
        receivable=definition.receivable or None,
    )


def save_claim(db: DbSQLite, request: SaveClaim) -> ServiceResult[SavedClaim]:
    """Validate and persist a complete claim through one atomic service boundary."""
    if request.existing_handle is not None and db.get_fsa_claim(request.existing_handle) is None:
        return ServiceResult.failure(ServiceError("claim.not_found", ("handle",)))
    candidate = build_claim(request)
    try:
        fsa_claims.save_claim(
            db, candidate, today=request.changed_on, eob_note=request.definition.eob_note
        )
    except fsa_claims.FsaClaimError as exc:
        return ServiceResult.failure(ServiceError(exc.code, exc.fields))
    return ServiceResult.success(SavedClaim(candidate.handle))


def delete_claim(db: DbSQLite, request: DeleteClaim) -> ServiceResult[SavedClaim]:
    try:
        fsa_claims.delete_claim(db, request.handle)
    except KeyError:
        return ServiceResult.failure(ServiceError("claim.not_found", ("handle",)))
    return ServiceResult.success(SavedClaim(request.handle))


def close_claim(db: DbSQLite, request: CloseClaim) -> ServiceResult[SavedClaim]:
    try:
        fsa_claims.close_claim(db, request.handle, on=request.on, reason=request.reason)
    except KeyError:
        return ServiceResult.failure(ServiceError("claim.not_found", ("handle",)))
    except fsa_claims.FsaClaimError as exc:
        return ServiceResult.failure(ServiceError(exc.code, exc.fields))
    return ServiceResult.success(SavedClaim(request.handle))


def reopen_claim(db: DbSQLite, request: ReopenClaim) -> ServiceResult[SavedClaim]:
    try:
        fsa_claims.reopen_claim(db, request.handle, on=request.on, note=request.note)
    except KeyError:
        return ServiceResult.failure(ServiceError("claim.not_found", ("handle",)))
    except fsa_claims.FsaClaimError as exc:
        return ServiceResult.failure(ServiceError(exc.code, exc.fields))
    return ServiceResult.success(SavedClaim(request.handle))


def claim_link_proposals(
    db: DbSQLite, *, account: str | None = None
) -> ServiceResult[tuple[fsa_claim_proposals.FsaClaimProposal, ...]]:
    """Unlinked FSA movements that clearly belong on one claim; writes nothing.

    ``account`` keeps only movements touching that account, such as the account
    being reconciled.
    """
    return ServiceResult.success(
        tuple(fsa_claim_proposals.propose_claim_links(db, account=account))
    )


@dataclass(frozen=True, slots=True)
class AcceptedClaimLinks:
    linked: int
    #: Chosen links that are no longer proposed (already linked, or changed).
    unchanged: int


def accept_claim_links(
    db: DbSQLite, chosen: tuple[tuple[str, str, str], ...]
) -> ServiceResult[AcceptedClaimLinks]:
    """Link the chosen (claim, transaction, split) proposals still on offer.

    Proposals are recomputed first, so a stale choice is skipped rather than
    linked on outdated evidence; each link goes through the claim's own checks.
    """
    current = {
        (item.claim, item.transaction, item.split): item
        for item in fsa_claim_proposals.propose_claim_links(db)
    }
    linked = 0
    for key in chosen:
        proposal = current.get(key)
        if proposal is None:
            continue
        try:
            fsa_claims.attach_transaction_to_claim(
                db,
                proposal.claim,
                proposal.transaction,
                role=proposal.role,
                split_handle=proposal.split,
            )
        except fsa_claims.FsaClaimError:
            continue
        linked += 1
    return ServiceResult.success(AcceptedClaimLinks(linked, len(chosen) - linked))


# --------------------------------------------------------------------- reads


@dataclass(frozen=True, slots=True)
class ClaimCandidate:
    """One split a claim could link, with what an editor shows for it."""

    transaction: str
    split: str
    date: date
    description: str
    account: str
    account_name: str
    #: The split's size, without its sign.
    amount: Money


@dataclass(frozen=True, slots=True)
class FsaAccountChoice:
    handle: str
    name: str
    years: tuple[FsaFundingYear, ...]


@dataclass(frozen=True, slots=True)
class ClaimCandidates:
    """Splits a claim could link, by role, and the accounts and payers to choose."""

    payments: tuple[ClaimCandidate, ...]
    refunds: tuple[ClaimCandidate, ...]
    reimbursements: tuple[ClaimCandidate, ...]
    #: Money paid back into an FSA, such as an over-reimbursement.
    repayments: tuple[ClaimCandidate, ...]
    fsa_accounts: tuple[FsaAccountChoice, ...]
    #: A payer covering part of a claim's expense (issue #192), labelled.
    receivables: tuple[NamedChoice, ...]


@dataclass(frozen=True, slots=True)
class FsaClaimsView:
    """Every claim's stored links and recomputed standing, and what it could link."""

    claims: tuple[FsaClaimSummary, ...]
    #: Full names of the allocation accounts, by handle.
    names: dict[str, str]
    candidates: ClaimCandidates

    def name(self, handle: str) -> str:
        return self.names.get(handle, handle)


def claim_candidates(db: DbSQLite) -> ClaimCandidates:
    """Splits from the first FSA year on that could be a claim's payment, refund,
    reimbursement, or repayment."""
    payments: list[ClaimCandidate] = []
    refunds: list[ClaimCandidate] = []
    reimbursements: list[ClaimCandidate] = []
    repayments: list[ClaimCandidate] = []
    fsa_accounts = [account for account in db.iter_accounts() if account.atype is AccountType.FSA]
    years = [year for account in fsa_accounts for year in account.fsa.years]
    start = min((year.start for year in years), default=None)
    for transaction in db.iter_transactions():
        if start is not None and transaction.post_date < start:
            continue
        for split in transaction.splits:
            account = db.get_account(split.account)
            if account is None:
                continue
            item = ClaimCandidate(
                transaction.handle,
                split.handle,
                transaction.post_date,
                transaction.description,
                account.handle,
                db.full_name(account) or account.name,
                abs(split.value),
            )
            if account.account_class is AccountClass.EXPENSE and split.value > 0:
                payments.append(item)
            if account.account_class is AccountClass.EXPENSE and split.value < 0:
                refunds.append(item)
            if account.atype is AccountType.FSA and split.value < 0:
                reimbursements.append(item)
            if account.atype is AccountType.FSA and split.value > 0:
                repayments.append(item)
    receivables = sorted(db.iter_receivables(), key=lambda item: (item.incurred_date, item.payer))
    return ClaimCandidates(
        tuple(payments),
        tuple(refunds),
        tuple(reimbursements),
        tuple(repayments),
        tuple(
            FsaAccountChoice(
                account.handle, db.full_name(account) or account.name, tuple(account.fsa.years)
            )
            for account in fsa_accounts
        ),
        tuple(
            NamedChoice(
                item.handle,
                f"{item.payer} — {item.description or 'expense'} "
                f"({item.incurred_date.isoformat()})",
            )
            for item in receivables
        ),
    )


def fsa_claims_view(db: DbSQLite, as_of: date | None = None) -> FsaClaimsView:
    summaries = tuple(
        fsa_claims.claim_summary(db, claim, as_of=as_of) for claim in fsa_claims.iter_claims(db)
    )
    accounts = {
        allocation.account for summary in summaries for allocation in summary.claim.allocations
    }
    return FsaClaimsView(
        summaries,
        {
            handle: db.full_name(handle) or handle
            for handle in sorted(accounts)
            if db.get_account(handle) is not None
        },
        claim_candidates(db),
    )


def find_claim(db: DbSQLite, reference: str) -> ServiceResult[FsaClaim]:
    """A claim by handle, or by a unique prefix of one."""
    exact = db.get_fsa_claim(reference) if reference else None
    if exact is not None:
        return ServiceResult.success(exact)
    matches = (
        [item for item in db.iter_fsa_claims() if item.handle.startswith(reference)]
        if reference
        else []
    )
    if not matches:
        return ServiceResult.failure(ServiceError("claim.not_found", ("claim",)))
    if len(matches) > 1:
        return ServiceResult.failure(ServiceError("claim.reference.ambiguous", ("claim",)))
    return ServiceResult.success(matches[0])


@dataclass(frozen=True, slots=True)
class ClaimReportQuery:
    as_of: date | None = None
    #: One of ``engine.fsa_claim_report.GROUPINGS``.
    by: str = "status"
    account: str | None = None
    funding_year: date | None = None
    provider: str | None = None
    status: FsaClaimStatus | None = None
    attention_only: bool = False


def fsa_claim_report(db: DbSQLite, query: ClaimReportQuery) -> ServiceResult[ClaimReport]:
    """Claims grouped and totalled, with the reasons any needs attention."""
    if query.by not in GROUPINGS:
        return ServiceResult.failure(ServiceError("claim.report.grouping", ("by",)))
    return ServiceResult.success(
        claim_report(
            db,
            as_of=query.as_of or date.today(),
            by=query.by,
            account=query.account,
            funding_year=query.funding_year,
            provider=query.provider,
            status=query.status,
            attention_only=query.attention_only,
        )
    )


@dataclass(frozen=True, slots=True)
class FsaYearsView:
    """Open and recently closed FSA benefit years, with how each was used."""

    years: tuple[FsaYearStatus, ...]
    #: Full names of the FSA accounts, by handle.
    names: dict[str, str]

    def name(self, handle: str) -> str:
        return self.names.get(handle, handle)


def fsa_years_view(db: DbSQLite, as_of: date | None = None) -> FsaYearsView:
    statuses = tuple(fsa.dashboard_statuses(db, as_of=as_of or date.today()))
    return FsaYearsView(
        statuses,
        {
            status.account.handle: db.full_name(status.account) or status.account.name
            for status in statuses
        },
    )


@dataclass(frozen=True, slots=True)
class ClaimStanding:
    """A stored claim's recomputed standing, or why it cannot be worked out."""

    summary: fsa_claims.FsaClaimSummary | None = None
    problem: str = ""


def claim_standing(db: DbSQLite, claim: str) -> ServiceResult[ClaimStanding]:
    """The standing an editor shows beside a claim (#358).

    A claim whose links no longer add up is reported with the engine's own words,
    not refused, so the editor can still show and correct it.
    """
    found = db.get_fsa_claim(claim)
    if found is None:
        return ServiceResult.failure(ServiceError("claim.not_found", ("handle",)))
    try:
        return ServiceResult.success(ClaimStanding(fsa_claims.claim_summary(db, found)))
    except (fsa_claims.FsaClaimError, ValueError) as exc:
        return ServiceResult.success(ClaimStanding(problem=str(exc)))
