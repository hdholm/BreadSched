# 0005: Match actuals to plans by split and amount, like reconciling a statement

Part of the [BreadSched design](../../../DESIGN.md); see
[Planning](../planning.md#event-driven-plan).

- **Status:** proposed, 2026-10-30, for review (#397). Nothing is built yet; the
  review questions at the end need answers first.
- **Decision:** an actual is matched to the plan *by split and amount*. One split
  may count toward one planned occurrence, several splits of one transaction toward
  several occurrences, and several actuals toward one occurrence, each for part or
  all of its amount. An estimate keeps what is left of it until its period ends. You
  do the matching in a window that works like reconciling a statement, opened from
  the Plan, a schedule, or a transaction. It replaces the Review view.

## Context

Today an actual resolves the plan as a whole transaction:

- **One link per transaction.** `Transaction.planned_occurrence` names at most one
  schedule occurrence, with `planned_for` and `planned_amount` kept so later schedule
  edits cannot rewrite the variance (`engine.planning.actualize_transaction`). A
  transaction posted from a schedule is linked by `scheduled_from` and its date.
- **Each transaction has one state**, `planning_resolution`: unresolved, matched,
  unexpected, or historical. Review (`services.review.review_queue`) lists every
  unresolved one.
- **Candidates** (`engine.planning.match_candidates`) are expected occurrences within
  seven days that share an account and the currency, ranked by date distance, then
  amount difference, then accounts in common. `engine.review_explain` adds reasons
  and a *close* or *possible* confidence.
- **What a match changes.** The Plan's category totals come from the ledger whether
  or not anything is matched. A match changes the occurrence: it is no longer
  expected (so neither Projection, Upcoming, nor Budget jars count it again), its
  variance is the actual's, and history-based estimates leave a matched actual out
  of the unscheduled history.

That model fits a commitment that is paid as planned. It does not fit how
households use estimates (#397):

- **One purchase, several plans.** A store receipt split between groceries and
  household goods should count toward the groceries estimate and the household
  estimate. Today the transaction can resolve only one occurrence.
- **Estimates fill up.** A $600 monthly groceries estimate is met by many purchases,
  none of them $600. Today one purchase resolves the whole month's estimate, and the
  rest of the month's purchases have nothing left to match and stay in Review.
- **Matching is a chore without context.** Review shows a transaction's description
  and amount but not the account on its other side or its notes, which are often what
  says what it was for.
- **The candidates are ranked for commitments.** Date and amount closeness suits a
  bill paid on its date. For an estimate, the category is what matters: a grocery
  purchase belongs to the groceries estimate whatever its date or amount within the
  month. A scheduled commitment usually posts itself and rarely needs matching, so
  it should rank below an estimate.
- **Review stands apart.** It is a main view, separate from the Plan and the
  schedules whose occurrences it settles, while matching is really a reconciliation
  of a plan against what happened.

## Decision

### 1. Allocations: a split counts toward an occurrence for an amount

- **An allocation** is a split's claim on one planned occurrence:
  `(occurrence key, amount, planned date, expected amount)`. It is kept on the split,
  as `Split.plan_allocations`, a list, so a split may count toward more than one
  occurrence. The expected amount and planned date are kept at the time of matching,
  as `planned_amount` and `planned_for` are now, so editing a schedule later cannot
  rewrite past variance.
- **The amount** is in the split's own sign convention and currency, and is never
  more than the split's value. The default is all of it. A split allocated for part
  of its value leaves the rest unallocated.
- **An occurrence's applied amount** is the sum of the allocations naming it. Its
  remaining amount is expected minus applied, which can go negative (spent more than
  planned). Every interface shows those three figures for an occurrence, as a
  reconciliation shows the statement, the cleared total, and the difference.
- **A transaction's state** follows from its splits:
  - It is **matched** when every split with a category leg (income, expense, or a
    planning purpose) is fully allocated.
  - It is **partly matched** when some of those splits are.
  - It is **unmatched** when none are.
  - **Not planned** (today's *unexpected*) is a choice made for a split, or for the
    whole transaction, and takes that split out of what still needs matching.
  - *Historical* stays as it is: transactions before the plan began need no
    matching.
- **Posting a schedule allocates.** A transaction posted from a schedule (Enter, or
  automatic posting) allocates every leg in full to its occurrence, as the link by
  `scheduled_from` does now.

### 2. Estimates keep what is left of them until their period ends

- **An estimate's period** runs from its occurrence date to the day before its next
  occurrence: a monthly estimate dated the 1st covers the month. An estimate with
  `count` 1 or *once* covers through its end date, or its own date if it has none.
- **What remains is still expected.** Within the period, the remaining amount (when
  positive) is what Projection, the Dashboard's needs, and Budget jars still count,
  instead of the whole estimate or nothing.
- **After the period ends,** the remainder lapses. It is shown as the occurrence's
  variance (under budget), and the occurrence is closed without an action. An
  allocation may still be added to a closed estimate, for a late purchase.
- **A commitment** (a scheduled transaction that posts) is closed when its applied
  amount reaches its expected amount, when it is skipped, or when a split from the
  transaction it posted is allocated. A partly paid bill keeps its remainder
  expected, as an estimate does, until it is paid or skipped.

### 3. Candidates ranked by category first

Matching proposes, for each unallocated split (or remaining part of one), the open
occurrences it could count toward, ranked by these rules, in this order. Each
proposal comes with the reasons that ranked it, as Review's do now.

1. **Same category account.** An estimate or commitment whose category leg (an
   income or expense account) is the split's account ranks above anything else.
   Without one, the planning purpose: the same purpose on the same balance-sheet
   account.
2. **Estimate before commitment.** At the same category, an estimate ranks above a
   scheduled commitment, because commitments usually post themselves.
3. **Within the period.** The actual's date falls in the occurrence's period
   (estimates), or within seven days of its date (commitments, as now).
4. **Fits what remains.** The split's amount is no more than the remaining amount,
   and then the closer the better.
5. **Same funding account,** then **description words in common.**

An occurrence that shares neither a category nor a funding account is never
proposed. The rules stay deterministic and explainable: no fuzzy or learned scoring
(planning.md, *Event-driven Plan*).

### 4. Matching works like reconciling

A **Match window**, modeless like the reconcile window, opens from three places:

- **From a planned occurrence:** a Plan cell's detail, a row in Scheduled or
  Upcoming, or a Budget jar. It shows the occurrence's expected, applied, and
  remaining amounts at the top, and below, the candidate splits in its period,
  proposed ones checked. Each candidate row shows:
  - the date, number, description, and the account on the other side;
  - the transaction's notes and the split's memo;
  - the split amount, and the amount to allocate, which you can edit for a partial
    allocation.

  Checking a row allocates it, as clearing a reconciliation row does, and the totals
  update. **Done** closes the window: an estimate's remainder stays expected until
  its period ends, or **Close occurrence** lets it lapse now.
- **From an actual:** a register row's **Match to Plan…** or the transaction editor.
  It lists the transaction's splits, each with its ranked occurrences and how much
  remains of each, so a split receipt can count toward two estimates at once.
- **From what is left:** the Plan shows how many actuals in the range are unmatched
  or partly matched. Choosing that count opens the list, grouped by category,
  replacing today's Review view. **Not planned** is a choice there and in the window
  from an actual.

Review stops being a main view: its toolbar button and View menu item go, and its
work moves to these windows. The browser gets the same windows on the Plan, the
schedules, and its register; `breadsched plan-*` commands gain allocation forms
(`plan-allocate TRANSACTION[:SPLIT] OCCURRENCE [--amount]`, and `plan-matches` with
remaining amounts).

### 5. Where it is calculated and stored

- **Engine.** `engine.planning` computes an occurrence's applied and remaining
  amounts from allocations, and its open or closed state by the rules in section 2.
  `match_candidates` ranks by section 3, and `review_explain` explains each proposal.
  Projection, the Dashboard, and Budget jars read the remaining amount from the
  engine rather than the whole expected amount or nothing.
- **Service.** `services.plan_matching` owns one write: allocate, change an
  allocation, remove one, mark a split or transaction not planned, close or reopen an
  occurrence. Each validates that the split exists, its allocations do not exceed
  its value, and the occurrence exists and shares the currency. A refusal leaves the
  transaction as it was, under a stable error code.
- **Book server.** These are new `BookApi` operations: `occurrence_matching(key)`,
  `transaction_matching(handle)`, `allocate`, `close_occurrence`,
  `reopen_occurrence`, and `unmatched_actuals(range)`. They need READ to read and
  WRITE to change (decision 0004), with `/api/v1` routes and contract tests.
- **Storage.** `Split.plan_allocations` lives in the transaction JSON, and the
  compact form leaves it out when it is empty. Each closed occurrence is kept on its
  schedule as `closed_occurrences` (a list of occurrence keys), as `skipped` is.
  There is no new table. Whether the schema version rises is review question 5.
- **Migration.** On first open, an existing link becomes allocations:
  - A transaction with `planned_occurrence` (or posted from a schedule) allocates
    each of its category legs in full to that occurrence, keeping `planned_for` and
    `planned_amount`.
  - *Unexpected* becomes *not planned* on the whole transaction.
  - Nothing else changes, and a verification check confirms the transaction count,
    the links, and each occurrence's actual amount before and after.

## Ramifications

- **The Plan's category totals do not change:** they come from the ledger. What
  changes is per-occurrence variance, what is still expected (Projection, Dashboard
  needs, Budget jars), and which actuals ask for attention.
- **Budget jars** draw an occurrence's allocated legs instead of the whole linked
  transaction. *Unmatched spending* holds what is left unallocated, so a
  half-allocated receipt is no longer drawn twice or not at all.
- **History-based estimates** leave out only the allocated part of a split from the
  unscheduled history, not the whole transaction.
- **Import and re-import** keep allocations as they keep `planned_occurrence` now
  (`engine.import_review`). A GnuCash re-import that changes a split's amount below
  what is allocated drops the allocations to fit and says so.
- **Interfaces.** The GTK Review view, its toolbar button, and the browser's Review
  page go once the Match windows replace them. The command line keeps `review` as a
  read-only list of unmatched actuals.
- **Cost.** Large: a storage field and its migration, engine changes in planning,
  activity, projection, and jars, a new service and book-server operations, and new
  windows in GTK and the browser. It is built in phases, and the old resolution
  keeps working until each phase lands.

## Phases

1. **Allocations underneath.**
   - `Split.plan_allocations`, the migration, and the engine's applied, remaining,
     and closed state.
   - Existing matches, posting, and Review written as allocations.
   - Projection, Dashboard, and jars reading remaining amounts.
   - Behavior is unchanged except that a partly used estimate keeps its remainder.
2. **Ranking and context.** Category-first candidates with reasons, and the other
   account and notes in every interface's candidate rows.
3. **The Match windows in GTK.** From an occurrence (Plan detail, Scheduled,
   Upcoming, jars) and from a transaction (register, editor); the unmatched list from
   the Plan; Review retired.
4. **Browser and command line.** The same windows on the browser's Plan, schedules,
   and register; `plan-allocate` and remaining amounts in `plan-matches`.

## Review questions

1. **An estimate's period.** Is it right that an estimate covers the time until its
   next occurrence, and that the remainder lapses at the end (shown as under budget)?
   Or should an unspent remainder carry into the next period, as a budget jar's
   leftover does?
2. **Partly paid commitments.** Should a scheduled commitment also take partial
   allocations and keep its remainder expected, as proposed? Or should matching a
   commitment always settle it in full, as today?
3. **Proposals checked.** In the window from an occurrence, should the top-ranked
   candidates in its period start checked, as a reconciliation's cleared rows do, so
   one click accepts them? Or should every row start unchecked?
4. **Not planned.** Is *not planned* chosen per split, or only for a whole
   transaction?
5. **Schema version.** An earlier version reading a book with allocations ignores
   them; if it then saves such a transaction, it drops them silently. Should the
   schema version rise to 14, so earlier versions refuse to write the book? That is
   safer, but it is a one-way upgrade. Or is a documented risk enough, as with
   `Account.imported_values` in 0.2.0a306?
6. **Review's place.** Should Review go entirely once the Match windows exist, as
   proposed? Or should an "unmatched actuals" list stay as a main view for working
   through a backlog?
