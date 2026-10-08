# Household workflows

Part of the [BreadSched design](../../DESIGN.md).

## Descriptions are names

A transaction's description is its only name, as in GnuCash; there is no separate
payee. `engine/description_keys.match_key` casefolds and NFKC-normalizes a
description, splits on punctuation, and drops every word containing a digit, so
store numbers, card suffixes, and references do not split one merchant. Matching is
exact on that key, never fuzzy, so every match names the key that produced it.
Categorization rules, expected-reimbursement proposals, and entry autocomplete all
compare descriptions through it. Imports never rewrite a description.

Schema 11 removed the payees of earlier alphas (a `payee` table and
`Transaction.payee`). The 10→11 migration turns each rule that matched a payee into
one rule per description key that payee claimed, in the same position (the first
keeps the rule's handle, later ones add `-1`, `-2`, …), so it keeps matching what it
matched; a payee with no keys matched nothing and leaves no rule. It drops every
rule's `set_payee`, removes `payee` from transaction JSON, and drops the table.

## Categorization rules

Rules live in book metadata (`categorization_rules`) as one ordered list of
`CategoryRule(handle, category, key)`; rule edits are metadata writes inside a database
transaction and therefore undoable. `engine/categorization.py` owns
`placeholder_handles()` (the Uncategorized CSV/OFX accounts, also used by CSV
transfer review) and `propose_categories`, which considers only transactions with
exactly one split on a placeholder: a category chosen by the user or the source is
never proposed again, and split transactions are not guessed. A rule matches by
`match_key` of the description; the first matching rule in list order decides and
every later rule naming a different category is reported as a conflict with its
position (the service refuses a second rule for the same key, but a book migrated
from payee rules can hold one). `services/categorization.py` validates rules (a
non-empty key, a non-placeholder income or expense category, no duplicate key, a
valid position) and applies accepted proposals by
recomputing them and replacing only the placeholder split's account, keeping its
value, memo, and handle, in one undo step. GTK's Rules dialog, the web **Rules**
view (`web/rules_resource.py`), and `breadsched rules` are adapters over this
service.

## Tags and linked documents

Tags reuse `PrimaryObject.tags` on `Transaction`; linked documents are
`Transaction.attachments` (BreadSched-owned locations, in order) and
`Transaction.source_link` (GnuCash's `doclink`, or `assoc_uri` before GnuCash 4). Both
live in the transaction's JSON, so books without them load with empty defaults.

- **Ownership.** Re-import carries `tags` and `attachments` forward;
  `source_link` is source-owned and refreshed. The source link enters the import
  fingerprint only when set, so older imports still compare unchanged.
- **Locations.** `engine/attachments.py` resolves a web address (never fetched), a
  `file:` URI, an absolute path, or a relative path. BreadSched's relative locations
  resolve against the book's attachment folder (by default `<book stem> attachments`
  beside the book); GnuCash's resolve against the GnuCash linked-files folder. Links
  are kept as written and files are never copied between the two.
- **Missing files are a state, not damage.** Verification does not fail on a missing
  document; the link is kept so that it can be restored or relinked.
- **Writes.** `services/attachments.py` owns every change in one undoable
  transaction: tag normalization (whitespace collapsed, case-insensitive duplicates
  dropped, the book's existing spelling reused), linking, copying a file into the
  folder under a name that never overwrites, unlinking (files are never deleted), and
  relinking. `TransactionInput.tags` of `None` keeps the stored tags, so editors that
  do not show them never drop them.
- **Browser trust boundary.** Any page holding the token could call the web routes, so
  linking from the browser accepts only a web address or a location that resolves
  (symbolic links included) inside the attachment folder
  (`services.contained_location`); otherwise the routes would become a file reader.
  Uploads have a plain file name checked twice, content is served only for a location
  the transaction lists, every response carries `X-Content-Type-Options: nosniff`, and
  only PDF, image, and plain-text documents open in the page.
- **Not included.** Backup and restore cover the book, not the attachment folder, as in
  GnuCash, and nothing is written back to GnuCash.

## Reimbursable expenses

A `Receivable` (`gen/lib/receivable.py`, table `receivable`) tracks an out-of-pocket
expense and what an insurer, employer, or other payer is expected to reimburse, as
linked but distinct facts. It never rewrites the expense split it references, and a
reimbursement is never new income: like a refund, it is a negative split in the
*same* expense account, so it offsets net spending without touching the original.
`engine/receivables.receivable_summary` never stores a status; it recomputes it from
the linked splits plus any dispute or write-off: **written off** (a deliberate,
terminal decision), **settled**, **disputed**, **partial**, or **open**. What is owed
is the expected amount, or the whole linked expense, never more than the expense.

Expected receipts: `receivables.expected_receipt` gives an open or partly reimbursed
receivable with a receivable account, a reporting-currency posting, and an
`expected_cash_date` on or after today the amount still owed and the cash account
that paid its expense (a card's `card_payment_account` for a card-paid expense);
disputed, overdue, settled, and written-off receivables have none.
`planning.receivable_receipt_events` turns each into a placeholder `ONE_OFF` planned
event (cash +remaining, receivable account −remaining) in every scenario's events, so
Plan and Projection count it as cash only from its date, never escalate it, and leave
net worth unchanged.

Gross and net cost: an expense category's Plan actual is already the net household
cost, because the tracking posting moves what is owed out of the category and each
reimbursement credit is cancelled by its posting. `receivables.plan_adjustments`
names the receivables' owned postings and linked reimbursement splits;
`activity.build_activity_report` copies those splits (converted like the rest) into
`ActualActivity.reimbursable_splits`, and `category_report.build_category_report` negates their
expense-class values into `CategoryActivity.reimbursable`, rolled up like `actual`.
So `gross = actual + reimbursable` is what was spent, and `reimbursable` is what was
reimbursed or is still owed less write-offs; nothing is added twice, because a
reimbursement's credit and its posting cancel in both figures.
`own_reimbursable` (the account alone) selects `CategoryReport.reimbursable_categories`,
so a parent category is not listed beside its children. `plan_detail` sums the same
splits into `CategoryPeriodDetail.reimbursable`. `presentation.plan_reimbursable_text`
and `plan_detail_cost_text` give the sentences GTK, web, print, and
`receivables --costs` show; a range holding only a write-off of an earlier expense
reads as a raised net cost with no gross cost.

Scenario expectations: `Scenario.reimbursement_overrides` maps a receivable handle to
a `ReimbursementOverride` (what the payer pays in that scenario, zero for nothing,
and the expected date). `engine/reimbursement_outlook.scenario_receipts` applies it
to each `expected_receipt` (an amount is capped at what is still owed; a date before
today is ignored), and `planning.receivable_receipt_events` turns each into one
balanced placeholder event: cash +paid, receivable −owed, and the largest linked
expense split's account +shortfall (exactly where a recorded write-off posts). So
the receivable still empties on that date, and the shortfall is a projected expense
in Plan and Projection for that scenario only. `reimbursement_outlook` gives each
receipt in a projection's range its gross cost (`ReceivableSummary.expense_total`),
earlier reimbursements and write-offs, what the scenario expects, the shortfall,
and `net_cost = gross − reimbursed − expected`; `Projection.reimbursements` carries
it and `presentation.reimbursement_outlook_text` words it for GTK (projection
notes), the browser (`reimbursements` and `reimbursement_notes` on the projection
payload), print, and `project`. `services.scenarios.set_reimbursement_override`
refuses a receivable not currently expected, an amount outside zero to what is owed,
and a past date, and clears the change when both are empty; deleting a receivable
drops every scenario's change to it in the same undo step. The GTK receivables
dialog's **In scenario** row and the browser's (`/api/receivable/scenario`, with
`scenarios` and each receivable's `scenario_changes` on `/api/receivables`) only
parse input and call that service; `presentation.reimbursement_override_text` words
each change.

`services/receivables.py` validates every write: linked splits must be in
expense-class accounts, expense links positive and reimbursement links negative, and
no split linked twice. The database refuses to delete a transaction a receivable
links to (`receivable.missing_transaction`), as it does for FSA claims, and a
GnuCash re-import that would delete one is reported instead.

What is still owed is held in a **Receivable account** (`Receivable.account`): part
of net worth, never spendable, so it never counts toward liquidity. BreadSched owns
the reclassification transactions that put it there, planned by
`engine.receivables.planned_postings` from the receivable alone:

| Event | Posting | Date |
| --- | --- | --- |
| Tracking | receivable +owed, expense −owed (allocated over the linked expense splits) | incurred date |
| Reimbursement linked | expense +amount, receivable −amount | the credit's date |
| Write-off | expense +amount, receivable −amount (the largest expense split's account) | write-off date |
| Dispute | nothing | — |

Reimbursements and write-offs apply oldest first and never take the receivable
below zero; money back beyond what was owed stays a refund. Posting handles are
deterministic (UUID5 of the receivable and event), so every service write recomputes
and diffs the set inside the same database transaction, the transaction service
does the same when a linked split is edited, and `sync_all_receivables` catches up
after an import. Owned postings cannot be edited through the transaction service
(`transaction.receivable_posting`) and are never offered as link candidates. The
account must be a Receivable account in the linked splits' single currency; by
default the first BreadSched-native one in that currency is reused or "Reimbursements
Receivable" is created under Assets. A receivable without an account posts nothing
until its next change. The Dashboard sums open reporting-currency balances as
`receivables_owed`, with `receivables_attention` for disputed or overdue ones.

Reimbursement proposals (`engine.receivables.propose_reimbursements`) match an
unlinked credit to a receivable still owed something only when the credit is in an
expense account the receivable's expense used, dated on or after it, in the same
currency, and no larger than what remains; a credit that fits several receivables is
proposed only when exactly one payer's name appears in its description. Credits are
allocated oldest first, and acceptance recomputes the proposals and links only
choices still on offer. After an import and at the top of statement reconciliation,
one shared sentence (`presentation.reimbursement_notice`) points at waiting
proposals; it never links anything.

GTK's **Reimbursable Expenses** dialog, the register's **Track as Reimbursable…**,
the web **Reimbursables** page (`web/receivable_resource.py`), and `breadsched
receivables` gather input and render these results only.

## FSA accounts and claims

FSA benefit-year and claim detail belongs to the dedicated FSA Dashboard. The
shared FSA engines stay authoritative, and an FSA account placed in a Dashboard group
still contributes its benefit availability there. A Dashboard FSA figure is the
remaining election of every plan year applicable on the as-of date, never the
custodial ledger balance.

**Plan rules.** A funding year (`FsaFundingYear`, stored in the account's JSON) may
carry a `carryover_limit`, a `grace_through` date, or both; the two are not
exclusive. A grace period must end between the plan year's end and its run-out.
`engine.fsa.year_status` carries the unused election, up to the limit, into the
account's next funding year once the earlier year's run-out ends: the closed year
reports it as `carried_over` and forfeits only the rest, and the next year reports it
as `carried_in`. Nothing is carried before the run-out ends, because claims may still
use the money. A grace period extends `service_through`, so a grace-period service can
be claimed against either year and the household chooses; grace-period claims tagged
to the earlier year reduce what it can carry over.

**Dependent care.** `Account.fsa_dependent_care` marks a dependent care FSA. Its
`year_status` availability is the year's funding, at most the election, less what was
used, and it never carries in or over (`services.accounts` refuses a carryover limit
with `account.fsa.dependent_care.carryover`). A claim whose allocations are all on
dependent care FSAs takes what was paid as its responsibility when no EOB is entered,
so it never waits for an EOB, and it stays open rather than out of funds while a
funding year it draws on can still receive contributions.

**Flows.** `engine.fsa_flows.classify` gives every split on an FSA account one
`FsaFlowKind` from its sign and the other splits' accounts: positive and tagged with a
funding year is a claim's *repayment*; positive from an expense account (and no income
account) is a *provider refund*; any other positive split is *funding*; negative to an
expense account is a *direct payment*; any other negative split is a
*reimbursement*; a movement only between FSA accounts is a *transfer*.
`year_status` counts only funding dated in the plan year as `funded`, and sets
`used` to direct payments plus reimbursements less provider refunds and repayments,
each reported separately (`presentation.fsa_usage_text` words them for every
interface). The medical expense stays on its expense split, so a card charge later
paid from the bank and reimbursed by the FSA is one expense; the card payment is a
transfer and the reimbursement a balance-sheet movement. Plan infers every
non-transfer FSA flow as `BENEFIT_FUNDING`, so the benefit row is the account's net
movement and the cash bridge has no residual for FSA-paid expense.

**Claims.** An `FsaClaim` is a first-class transactional object (see
[Presentation settings and financial records](storage.md#presentation-settings-and-financial-records))
linking payments, provider refunds, and per-funding-year allocations with their
reimbursements, rejections, and repayments. `engine.fsa_claims.claim_summary`
recomputes paid, refunded, reimbursed, repaid, remaining, and status. FSA claims and
receivables share link resolution (`engine.split_links`); FSA reimbursements need no
reclassification because the money is already in the FSA asset, which is not
cash-like.

- **Payer and FSA together.** `FsaClaim.receivable` names the receivable whose payer
  covers part of the same expense; it is set only in the claim screens, never
  inferred. `claim_summary` then splits the net paid amount into the payer share (what
  is owed, or what was actually reimbursed if more), the FSA share (zero until an EOB
  responsibility is entered, then capped), and the household's share. An
  over-allocation is reported as *Needs review*; nothing is refused, and no posting
  depends on the split. Without that link, a receivable whose expense is also a claim
  payment produces a warning rather than a refusal, because such a split can be
  legitimate.
- **Proposed links.** `engine.fsa_claim_proposals.propose_claim_links` finds FSA
  splits no claim links whose flow needs a claim (direct payment, reimbursement,
  provider refund) and proposes the one claim that suggests it in that flow's role
  with an amount match, oldest first and at most one movement per claim per batch;
  anything ambiguous is left to Review. `services.claims.accept_claim_links`
  recomputes before linking, as `accept_reimbursements` does for receivables, and
  import and reconciliation surfaces report waiting proposals through
  `presentation.claim_link_notice`. `suggest_claims_for_transaction` considers a fully
  reimbursed claim only for refunds, which often arrive after the FSA has paid.
- **Paired roles.** `fsa_claims.attachment_roles` lists the roles a transaction can
  take, paired roles first. `direct_payment` links a direct FSA payment as both the
  claim's payment and its allocation's reimbursement; `direct_refund` links a provider
  refund to the FSA card as both a refund and a repayment, so the claim nets to what
  was actually used. Suggestions rank these ahead of single roles for the same
  transaction.
- **Repayments.** Splits paying money back into the allocation's FSA account are
  linked as repayments and tagged with the funding year. `year_status` takes a tagged
  positive split off `used` (reported as `repaid`) instead of treating it as payroll
  funding; only claim saves set that tag. More reimbursed than the claim allows is
  *Over-reimbursed*.
- **History and closing.** `FsaClaim.events` records EOB changes (with the previous
  and new responsibility and a note), closings, and reopenings. A save never replaces
  that history; `close_claim` and `reopen_claim` alone change it. A closed claim
  reports its unclaimed rest as `forgone`, but figures that disagree or money owed
  back keep it flagged.
- **Report and attention.** `engine.fsa_claim_report.claim_report` groups claims by
  status, account, funding year, or provider without writing, and gives each claim
  stable attention codes: `review`, `eob` (no EOB 30 days after service), `deadline`
  (money still to reimburse within 30 days of a run-out), `rejected`, `over`, and
  `reopened`. The Dashboard carries the claims needing attention as
  `claim_alerts`.

The GTK claims dialog, the web claim editor (`web/fsa_claim_resource.py`), and
`breadsched claims` only parse input and render these results.

## Statement reconciliation

A reconciliation is a persisted, account-scoped statement session, not transient UI
state. It owns an exact ending balance, statement date, checked split handles,
lifecycle status, timestamps, and an append-only lifecycle audit. Only asset and
liability posting accounts participate; income, expense, equity, roots, and
placeholders do not represent statement balances.

The shared reconciliation service derives the opening balance from previously
reconciled or frozen splits through the statement date. Non-void, unreconciled and
cleared splits through that date are candidates; Cleared candidates start checked.
The displayed difference is always statement ending balance minus the account's
natural-sign opening-plus-checked balance. GTK and web merely present this shared
calculation. Their mutations submit typed start, update, complete, cancel, and reopen
requests through the application-service boundary. Domain failures retain stable
codes and field paths while adapter-owned wording remains presentation-only.

Finishing is permitted only at an exact zero difference. It atomically changes every
checked split to Reconciled with the statement date and records completion of the
session. Cancelling records the abandoned session without changing ledger state.
Only the latest completed statement may be reopened, which atomically returns its
recorded splits to Cleared and preserves the lifecycle audit. This ordering prevents
a correction from invalidating later statement openings invisibly. Full book
verification reports missing, cross-account, duplicated, or subsequently changed
splits referenced by reconciliation history. Imported reconcile states remain
ledger facts and are included in opening balances rather than rewritten on import.

