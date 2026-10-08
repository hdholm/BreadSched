# Domain model and exact money

Part of the [BreadSched design](../../DESIGN.md).

## Double entry

Transactions are collections of splits and must balance atomically. Persistent
storage must never expose a partially written transaction. Imports that repair or
skip damaged source records must report what was changed rather than silently
inventing semantics.

## Money, rates, and amounts

Ledger values use exact rational arithmetic to preserve imported GnuCash numeric
values and avoid binary floating-point error. Commodity precision, account-specific
SCU, and multi-commodity valuation are distinct concerns from the exact
stored ledger fraction.

Rates are dimensionless and distinct from money amounts. The ``Rate`` domain type
wraps a finite ``Decimal`` for exact persistence and presentation rather than
subclassing it: direct and reflected arithmetic stays a ``Rate`` instead of silently
decaying to an untyped decimal. ``Money`` remains an exact rational ledger quantity.
Multiplying two monetary amounts is invalid; scaling a monetary amount requires a
dimensionless scalar/rate, and dividing one monetary amount by another yields an
exact dimensionless ratio.

``Amount`` pairs one exact ``Money`` scalar with a commodity identifier. Combining
or comparing two amounts requires identical commodity identity; scaling retains the
tag, and dividing like amounts produces an exact dimensionless ratio. Unlike
commodities become comparable only after an explicit dated conversion has returned
a new amount in the reporting currency.

Text becomes `Money` only at a boundary that knows its format. The core
constructor accepts one unambiguous numeric syntax; GTK and web entry go through the
shared amount-input boundary, which detects unambiguous decimal conventions from the
text, uses the GTK process locale only to break a tie, and takes the browser's
decimal convention explicitly from the web client. Entered amounts stay text until
exact server-side parsing, so JavaScript floating point never touches financial
input. Strict English thousands grouping is accepted; ambiguous comma-decimal forms
are rejected rather than silently re-scaled. Equality with Python numbers obeys
Python's equality and hash contract, and projection assumptions use `Rate`, so a
percentage cannot masquerade as a ledger amount.

Ledger reads preserve that identity internally. Account, recursive, class-total,
net-worth, cash-on-hand, and register-running arithmetic uses tagged transaction
values and refuses to combine material values with different currency identifiers.
The long-standing scalar APIs unwrap the checked result to ``Money`` for callers
that only present one reporting currency. An empty zero has no economic dimension
to net and therefore adopts the first material amount's commodity.

The reporting currency is explicit book metadata when configured, otherwise USD
when present, then the first currency commodity.

New native books contain a stable USD commodity and select it as their default
currency. A legacy native book without any currency remains readable; its first
transaction-service write creates that same default commodity and metadata inside
the transaction's atomic database change.

Rounding uses the selected commodity's declared ``fraction`` rather than assuming
cents. Scheduled posting uses the schedule currency; Projection, Dashboard, loan,
inference, and historical-estimate interfaces use the reporting currency; exact
allocation accepts the caller's commodity fraction. Stored ``Money`` remains
rational and unrounded until one of these minor-unit boundaries is crossed.

## Account types

An account has one user-visible, BreadSched-owned type. Its accounting class, debit
or credit display sign, liquidity, and household-planning behavior are derived from
that type. A separate user-editable planning role or account kind is deliberately
not part of the model: it exposed implementation detail, permitted combinations
with no distinct meaning, and required users to reconcile two classifications.

The visible balance-sheet types are Cash, Bank, Asset, Investment, Retirement,
FSA/benefit, Escrow, Receivable, Credit card, Loan, and Liability. Receivable is
money others owe back (GnuCash `RECEIVABLE` maps to it): an asset for net worth,
never cash-like, so it is excluded from liquidity and the emergency fund. Income, Expense, and Equity
retain their ledger meanings. Root is structural and Technical preserves imported
bookkeeping accounts that should not participate in ordinary household planning.
Cash and Bank deliberately share a liquid accounting class but remain distinct
workflow types: institutional accounts have statement, reconciliation, import, and
payment-source behavior that physical cash does not. Asset means non-liquid general
value. Credit card remains a revolving payment channel even when it carries a
balance; Loan is amortizing debt; Liability is the generic fallback that assumes
neither workflow.

Investment describes market-valued holdings. Retirement describes the restricted
or tax-advantaged wrapper and controls contribution/distribution planning. In a
hierarchical chart a Retirement parent may contain Investment children, whose
activity inherits retirement context; a flat retirement account simply has the
Retirement type. FSA and Escrow remain first-class types because neither can be
modelled correctly as a generic asset: FSA availability follows elections and
claims, while Escrow recognizes expense when funded and suppresses duplicate
expense recognition when disbursed.

An imported account records the exact latest GnuCash source type and source GUID.
The source GUID is separate from the BreadSched object handle because importing into
an initialized book adopts the existing root and compatible top-level placeholders.
That mapping is durable: subsequent imports resolve the retained source GUID before
falling back to a same-parent name/type match, so a source rename or move refreshes
the adopted account rather than creating a duplicate. Full verification reports two
accounts claiming the same source GUID.

Read-only source provenance also retains typed account fields that BreadSched does
not interpret, including SQLite account flags/slots and nested XML slot paths. This
prevents a smaller editor from destructively normalizing information merely because
it has no household workflow yet. GTK and web expose the provenance separately from
editable BreadSched configuration; CLI JSON carries the same fields. Native accounts
have no source identity or source fields.
Initial source mapping is conservative: BANK to Bank, CASH to Cash, ASSET/CURRENCY/
RECEIVABLE to Asset, CREDIT to Credit card, LIABILITY/PAYABLE to Liability,
STOCK/MUTUAL to Investment, the flow/equity/root types directly, and TRADING to
Technical. Historical CHECKING, SAVINGS, and MONEYMRKT types map to Bank,
CREDITLINE maps to Credit card, and CD maps to general Asset. A new unknown source
type is retained exactly but begins as Technical so it cannot acquire invented
planning semantics before review. GnuCash alone does not identify an FSA, Escrow,
Retirement account, or Loan; those types require explicit selection or stronger
reviewed evidence.

Inference precedence is:

1. explicit split planning-purpose override;
2. account type plus transaction direction/context;
3. ordinary Income/Expense behavior;
4. otherwise neutral.

Transfers that carry no household planning meaning should remain neutral.

An account is a chart root because its account type is `ROOT`, not merely because its
parent field is empty. This distinction prevents damaged/imported parentless ordinary
accounts from disappearing from engines that intentionally skip the root. Full book
verification reports parentless non-root accounts when an explicit root exists, while
lightweight rootless books remain valid for tests/tools that intentionally omit a chart
root.

## Security quantities and prices

A split has two exact rational dimensions: ``value`` is expressed in the
transaction currency and balances the double-entry transaction, while ``quantity``
is expressed in the account commodity. These must not be collapsed. Historical
ledger value remains an accounting fact even when a security's market price changes.
When `save_transaction` changes an existing split's value without being given a
quantity, the result depends on the account's commodity:

- In the transaction currency (or with no commodity), the new value is also the
  new quantity. That also repairs a quantity an earlier edit left behind.
- In another commodity, the change is refused with
  `transaction.quantity.conversion` rather than stored with a mismatched
  quantity.

An unchanged value keeps its stored quantity, so imported GnuCash detail
survives an edit that does not touch the amount.

A commodity price is a first-class dated object identifying the security, quote
currency, exact positive price, source, and quote type. As-of valuation selects the
latest direct quote on or before the requested date and converts a commodity-tagged
exact account quantity to a quote-currency-tagged amount. The price rejects units of
another commodity. Valuation quantizes only the resulting presentation value to
the quote currency fraction. Investment and Retirement accounts with a non-currency
commodity use this market value in current-value presentations; absent a compatible
quote, they explicitly fall back to ledger value. Ordinary accounts are never
silently revalued. Projection uses the as-of market value as its opening state, then
applies its explicit dated flows and return assumptions; a quote is not itself a
future return assumption.

## Investment activity

Investment activity is an optional split classification, separate from both the
account type and the planning-flow classification. The account type says what the
holding is; the investment activity says why one exact dated movement changed it;
a planning-flow classification says how a balance-sheet movement appears in Plan.
These dimensions may coexist on one split without changing its double-entry value
or commodity quantity.

The supported activity vocabulary is contribution, taxable withdrawal, retirement
distribution, reinvested dividend, reinvested interest, investment fee, and
retirement rollover. Contributions and reinvested income increase a holding;
withdrawals, distributions, and fees decrease it. Taxable withdrawals are invalid
inside a Retirement context, while retirement distributions require that context.
A rollover requires at least two distinct retirement-context accounts and its
classified investment legs must sum to zero. These rules are shared validation used
by GTK and web transaction, baseline-schedule, and scenario-schedule writes.

Projection records the signed holding movement once for reconciliation, then
attributes that same movement to the appropriate explanatory bucket. Contributions,
withdrawals, retirement distributions, investment income, fees, and gross rollover
amounts are therefore explanatory views of the state transition, not extra money
applied to it. Rollover legs net to zero in total holdings. Market growth remains a
separate accrual effect. This separation prevents contributions from being mistaken
for performance and prevents a rollover from being reported as a withdrawal plus a
new contribution.

For compatibility with existing unclassified books, a positive investment movement
is inferred as a contribution and a negative movement as a taxable withdrawal or
retirement distribution according to account context. That fallback cannot infer
dividends, interest, fees, or rollovers and must not invent tax or cost-basis facts.
Explicit classifications are BreadSched-owned. Stable transaction split identities
retain them across GnuCash re-import; scheduled splits have no stable source identity,
so re-import retains a classification only when the account occurs exactly once in
both the prior and incoming template.

