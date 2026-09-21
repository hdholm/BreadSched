# 0226 Plan and Projection golden books

These five small declarations are independent acceptance evidence for the shared Plan and
Projection engines. Each test creates a native SQLite book from `book.json`, closes it,
reopens it, and then calls the same service/engine boundary used by application surfaces.

`CALCULATIONS.md` records the arithmetic and financial interpretation. The expected JSON
files are deliberately static and hand-authored from those calculations; no product code or
fixture script generates them. Stable readable handles make a failure traceable back to the
declared account, transaction, schedule, or scenario.

The suite intentionally excludes import compatibility, formula schedules, multi-currency,
estimator confidence thresholds, and UI rendering. Those remain covered by their focused
tests and would obscure the financial invariants these books protect.
