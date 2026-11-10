# Commitments, estimates, goals, and matching

Part of the [BreadSched design](../../DESIGN.md); see
[Planning](planning.md#event-driven-plan) for the engine.

An actual is matched to the plan *by split and amount*. One split may count toward
one planned occurrence, several splits of one transaction toward several
occurrences, and several actuals toward one occurrence, each for part or all of its
amount. An estimate keeps what is left of it until its period ends, and then what is
left lapses: it does not carry into the next period. Matching is done in a window
that works like reconciling a statement, opened from the Plan, a schedule, or a
transaction, alongside the Review view.

The reason: a household buys groceries many times against one monthly allowance, and
one receipt often serves several purposes. A link from one whole transaction to one
planned item fits neither.

## What a household plans with

A household plans with four things. Three are made on purpose; the fourth, the
budget jar, is a way of looking at the other three.

| Use | When | What is left at the end |
| --- | --- | --- |
| **Commitment** (a scheduled transaction) | A particular payment or receipt you expect: rent, a paycheck, an insurance premium. | Stays expected until it is paid or skipped. |
| **Estimate** | An allowance for a category in each period: groceries, fuel, eating out. Many purchases count toward it. | Lapses when its period ends. |
| **Savings goal** | Money to set aside over months for a known purchase or reserve: a car, a holiday, an annual bill. | Carries forward until the goal is closed. |
| **Budget jar** | Not made: every commitment, estimate, and goal is a jar, showing the money income sets aside for it and what is drawn from it. | Follows the row above. |

Two questions choose among them:

- **Will one transaction settle it?** Then it is a commitment, and it usually posts
  itself. If many purchases fill it, it is an estimate.
- **Should unspent money still be there next period?** Then it is not a monthly
  estimate: save toward it with a goal, or estimate it over the period it really
  covers (an annual estimate for an annual expense).

So an estimate is a *use it or lose it* allowance, and a goal is a *keep it until
you need it* reserve. That difference is the whole difference between them. Both
are filled from income the same way and shown as jars the same way; they differ
only in what happens to what is left.

### What they share

- **One occurrence ledger.** Every commitment and estimate occurrence has an
  expected amount, an applied amount (the sum of the allocations naming it), a
  remaining amount, and an open or closed state. The engine computes these once,
  and Projection, the Dashboard's needs, Budget jars, and the Plan read them.
- **One filling rule.** Jars are filled from income for commitments, estimates, and
  goals alike (`engine.cash_flow`).
- **Closing decides the leftover.** A commitment closes when it is paid in full or
  skipped. An estimate closes when its period ends, and its jar is emptied: a
  leftover returns to what is available, and overspending stays as variance rather
  than becoming a debt the next period must repay. A goal closes when you close it
  or record its purchase, and until then its earmark stays.

Goals are not matched to actuals. A goal's earmark is released by closing it.

## Allocations: a split counts toward an occurrence for an amount

- **An allocation** is a split's claim on one planned occurrence:
  `(occurrence key, amount, planned date, expected amount)`. It is kept on the split,
  as `Split.plan_allocations`, a list, so a split may count toward more than one
  occurrence. The expected amount and planned date are kept at the time of matching,
  so editing a schedule later cannot
  rewrite past variance.
- **The amount** is in the split's own sign convention and currency, and is never
  more than the split's value. The default is all of it. A split allocated for part
  of its value leaves the rest unallocated.
- **An occurrence's applied amount** is the sum of the allocations naming it, in the
  occurrence's terms: each transaction counts the share it allocates of its plan legs
  (category legs, else its positive legs) times its gross, so a paycheck matched as a
  whole counts its gross with the withholding. Its
  remaining amount is expected minus applied, which can go negative (spent more than
  planned). Every interface shows those three figures for an occurrence, as a
  reconciliation shows the statement, the cleared total, and the difference.
- **A transaction's state** follows from its splits:
  - It is **matched** when every split with a category leg (income, expense, or a
    planning purpose) is fully allocated.
  - It is **partly matched** when some of those splits are.
  - It is **unmatched** when none are.
  - **Not planned** is a choice made for a split, or for the
    whole transaction, and takes that split out of what still needs matching.
  - *Historical*: transactions before the plan began need no
    matching.
- **Posting a schedule allocates.** A transaction posted from a schedule (Enter, or
  automatic posting) allocates every leg in full to its occurrence.

## Estimates keep what is left of them until their period ends

- **An estimate's period** runs from its occurrence date to the day before its next
  occurrence: a monthly estimate dated the 1st covers the month. An estimate with
  `count` 1 or *once* covers through its end date, or its own date if it has none.
- **What remains is still expected.** Within the period, the remaining amount (when
  positive) is what Projection, the Dashboard's needs, and Budget jars still count,
  instead of the whole estimate or nothing.
- **After the period ends, the remainder lapses.** The occurrence closes
  without an action. Its variance shows what happened (under or over budget), and
  nothing carries into the next period, in either direction:
  - Projection and the Dashboard stop counting it.
  - Its jar is emptied on the day after the period: a leftover is released as a
    *lapsed* event, and an overspent jar is brought back to zero the same way.
  - An allocation may still be added to a closed estimate, for a late purchase. It
    changes the occurrence's variance, not what is expected.
- **A commitment** (a scheduled transaction that posts) is closed when its applied
  amount reaches its expected amount, when it is skipped, or when a split from the
  transaction it posted is allocated. A partly paid bill keeps its remainder
  expected, and in its jar, until it is paid or skipped.
- **Close occurrence** closes an open occurrence early (a commitment that will not
  be paid in full, or an estimate you are done with), and **Reopen** undoes that.
  An estimate whose period has ended stays closed.

## Candidates ranked by category first

Matching proposes, for each unallocated split (or remaining part of one), the open
occurrences it could count toward, ranked by these rules, in this order. Each
proposal comes with the reasons that ranked it.

1. **Same category account.** An estimate or commitment whose category leg (an
   income or expense account) is the split's account ranks above anything else.
   Without one, the planning purpose: the same purpose on the same balance-sheet
   account.
2. **Estimate before commitment.** At the same category, an estimate ranks above a
   scheduled commitment, because commitments usually post themselves.
3. **Within the period.** The actual's date falls in the occurrence's period
   (estimates), or within seven days of its date (commitments).
4. **Fits what remains.** The split's amount is no more than the remaining amount,
   and then the closer the better.
5. **Same funding account,** then **description words in common.**

An occurrence that shares neither a category nor a funding account is never
proposed. The rules stay deterministic and explainable: no fuzzy or learned scoring
([Planning](planning.md#event-driven-plan)).

## Matching works like reconciling

A **Match window**, modeless like the reconcile window, opens from three places:

- **From a planned occurrence:** a Plan cell's detail, a row in Scheduled or
  Upcoming, or a Budget jar. It shows the occurrence's expected, applied, and
  remaining amounts at the top, and below, the candidate splits in its period,
  ranked and explained but none checked. Each candidate row shows:
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
  or partly matched. Choosing that count opens Review on them, grouped by category.
  **Not planned** is a choice for each split there and in the window from an actual.

Review is a main view for working through a backlog. It shows each
actual's splits with the account on the other side and the notes, and opens the
window from an actual for the one chosen. The browser has the same windows on the Plan, the
schedules, and its register; `breadsched plan-*` commands have allocation forms
(`plan-allocate TRANSACTION[:SPLIT] OCCURRENCE [--amount]`, and `plan-matches` with
remaining amounts).


## Where it is calculated and stored

- **Engine.** `engine.planning` computes an occurrence's applied and remaining
  amounts from allocations, and its open or closed state by the rules above.
  `match_candidates` ranks as above, and `review_explain` explains each proposal.
  Projection, the Dashboard, and Budget jars read the remaining amount from the
  engine rather than the whole expected amount or nothing.
- **Service.** `services.plan_matching` owns one write: allocate, change an
  allocation, remove one, mark a split or transaction not planned, close or reopen an
  occurrence. Each validates that the split exists, its allocations do not exceed
  its value, and the occurrence exists and shares the currency. A refusal leaves the
  transaction as it was, under a stable error code.
- **Book server.** The `BookApi` operations are `occurrence_matching(key)`,
  `transaction_matching(handle)`, `allocate`, `close_occurrence`,
  `reopen_occurrence`, and `unmatched_actuals(range)`. They need READ to read and
  WRITE to change ([Users and access](users-and-access.md)), with `/api/v1` routes and contract tests.
- **Storage.** `Split.plan_allocations` lives in the transaction JSON, and the
- **Storage.** `Split.plan_allocations` lives in the transaction JSON, and the
  compact form leaves it out when it is empty. Each closed occurrence is kept on its
  schedule as `closed_occurrences` (a list of occurrence dates), as `skipped` is.
  There is no table for either.

## Worked examples

Amounts are in dollars; each household
is paid $2,000 on the 1st and the 15th.

### A monthly groceries estimate filled by many purchases

1. Groceries is an estimate of $600 a month on the 1st. Its period is the month.
   Each paycheck fills its jar by $300.
2. On November 4 a $82 purchase at the market posts. In Review, or in the Match
   window from the November occurrence, the purchase's Groceries split is the top
   proposal (same category account). Allocating it leaves $518 remaining, and
   Projection now expects $518 more groceries in November, not $600.
3. Six more purchases through November 29 bring the applied amount to $571. The
   remaining $29 is still expected until November 30.
4. On December 1 the November occurrence closes. Its variance is $29 under budget,
   the jar releases the $29, and the December occurrence starts at $600. Nothing
   carries over.
5. If November's purchases had come to $640 instead, the variance is $40 over, the
   jar is brought back to zero, and December still starts at $600.

### A department store receipt with three purposes

1. A $150 receipt is entered with three splits: Groceries $60, Household goods $55,
   and Clothing $35, all paid from the credit card.
2. In the Match window from the transaction, each split lists its own proposals: the
   Groceries split the November groceries estimate, the Household goods split the
   household estimate. Clothing has no estimate.
3. Allocating the first two and choosing **Not planned** for the clothing split
   matches the whole transaction. Each estimate's remaining amount
   falls by its own split, and the $35 is unplanned spending in the Plan and in the
   Clothing jar's *Unmatched spending*.

### Money down on a bill, then the balance

1. A $1,200 furnace repair is a commitment due December 10.
2. On November 20 a $300 deposit is paid. Its split to Home repairs is allocated to
   the December 10 occurrence for $300. The commitment stays open with $900
   remaining, and Projection and its jar now expect $900 on December 10.
3. On December 10 the $900 balance posts and is allocated. Applied equals expected,
   so the occurrence closes. Had the final bill been $950, it would close with $50
   over.

### An annual expense saved for over the year

1. Car insurance of $1,080 is due every June 1.
2. **As a commitment** with a yearly recurrence: its jar fills from every paycheck of
   the year, $45 each, and is drawn when the premium posts. Because it is a
   commitment, nothing lapses while it waits.
3. **As a goal** instead, when the amount or date is uncertain: a goal of $1,080 in
   savings by June 1 sets aside a share of each paycheck as an earmark and holds it
   until the goal is closed.
4. It should not be a $90 **monthly estimate**: each month's unspent $90 would lapse,
   and nothing would be left in June.

## What follows from it

- **The Plan's category totals come from the ledger.** What matching decides is
  per-occurrence variance, what is still expected (Projection, Dashboard
  needs, Budget jars), and which actuals ask for attention.
- **Budget jars** draw an occurrence's allocated legs instead of the whole linked
  transaction, each on its own date. *Unmatched spending* holds what is left
  unallocated, so a half-allocated receipt is drawn exactly once.
  An estimate's jar is emptied by a *lapsed* event when its period ends, so its
  level never carries leftovers or overspending forward; a commitment's jar
  keeps its money until the commitment closes.
- **History-based estimates** leave out only the allocated part of a split from the
  unscheduled history, not the whole transaction.
- **Import and re-import** keep allocations (`engine.import_review`). A GnuCash re-import that changes a split's amount below
  what is allocated drops the allocations to fit and says so.
