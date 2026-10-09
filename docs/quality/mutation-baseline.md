# Bounded money mutation baseline

This gate checks two shared financial contracts: reporting-currency identity in
`src/breadsched/gen/engine/currency.py` and commodity-tagged arithmetic in
`src/breadsched/gen/lib/amount.py`. It selects `tests/test_money.py`,
`tests/test_currency.py`, `tests/test_valuation.py`, and `tests/test_activity.py`.
The selected tests include direct currency precedence, empty-book fallback,
commodity fractions of 1/10/100/1000, tagged amount operations and comparisons,
and the surrounding valuation and activity consumers.

The first measured pilot generated 123 mutants. Before the added direct currency
and Amount tests, 59 were killed, 40 survived, and 24 lacked an applicable test.
A new full run after those tests on Linux with mutmut 3.8.0 and Python 3.12, and
after `reporting_currency_label` (the code a chart's currency is labelled with) was
added to `currency.py` with direct tests for a configured code, the USD default,
and an empty legacy book, yielded:

| Result | Count |
| --- | ---: |
| Generated | 130 |
| Killed | 125 |
| Survived | 5 |
| No tests, skipped, suspicious, timeout, segfault | 0 |

The `reporting_fraction__mutmut_2` survivor changes
`commodity_fraction(db, reporting_currency_handle(db))` to
`commodity_fraction(db, None)`. Both select `book_currency(db)` and return its
fraction or the same default in a legacy empty book; it is equivalent under the
current contract. The gate exempts only this exact mutant and verifies that it is
still present as a survivor. The other four survivors change exception wording in
Amount validation. They remain counted, so the baseline is **125 killed out of 129
eligible mutants**. There are no platform-specific exemptions: this pure-Python
slice runs only on Linux because mutmut requires fork support.

`python scripts/check_mutation_baseline.py` uses a fresh temporary copy on every
run, pins the mutation tool in CI, and fails when the killed/eligible score falls
below 125/129 or any mutant has an incomplete result. The ten-minute CI timeout
bounds a stalled run. The measured local mutation phase took under five seconds;
the timeout includes installation and CI variability. When the two selected source
files or tests change, rerun and review the survivors before deliberately raising
the threshold. Expand the source selection in a separate measured change rather
than slowing the normal cross-platform matrix.
