# Historical-estimation calculations

Analysis date is 2026-01-20. Only the 24 completed months from January 2024 through
December 2025 are used. There is no future scheduled coverage, so gross and residual
activity are equal.

## Groceries

Eight active months contain seven ordinary `$100` totals and one isolated `$1,000`
total. Their median is `$100`; median absolute deviation is zero. The zero-MAD
threshold is `max($100 × 0.75, $1) = $75`, so the `$1,000` month is excluded and
seven observations remain. Once-per-month postings on day 10 produce a monthly
day-10 estimate of `$100`.

Checking and Brokerage each fund four transactions. Equal transaction counts invoke
the second tie-break, so spendable Checking wins over non-spendable Brokerage.

Confidence inputs are:

- coverage `8 / 24 = 1/3`;
- depth `7 / 12 = 7/12`;
- retained ratio `7 / 8`;
- consistency `1` because retained MAD is zero.

The score is `0.20 + (1/3 × 0.30) + (7/12 × 0.20) + (7/8 × 0.10) +
(1 × 0.20) = 0.704166…`.

## Utilities

Each calendar month appears twice with an identical amount. January, February, July,
and August are `$240`; the other months are `$100`. All 12 repeated months are stable,
and the peak-to-median ratio is `$240 / $100 = 2.40`, above the `1.35` seasonal
threshold. The proposal therefore starts monthly on day 5, has a `$100` ordinary
amount, and retains all 12 calendar-month amounts. Full coverage, depth, retention,
and consistency would total `1.00`; the rule-set cap makes confidence `0.95`.

## Salary

The six monthly values are `$1,000`, `$1,050`, `$1,100`, `$1,500`, `$1,550`, and
`$1,600`. The earlier median is `$1,050`; the later median is `$1,550`. The increase is
`($1,550 - $1,050) / $1,050 = 47.619…%`, above the 10% trend threshold, so the recent
three-month median `$1,550` becomes the day-15 monthly estimate.

For confidence, coverage is `6/24`, depth is `6/12`, retention is `1`, and amount
consistency is `1 - ($250 / $1,300) = 0.807692…`. The weighted score is
`0.20 + (0.25 × 0.30) + (0.5 × 0.20) + (1 × 0.10) +
(0.807692… × 0.20) = 0.636538…`.
