# 0227 historical-estimation golden book

This corpus fixes the behavior of the first versioned historical-estimation policy
without deriving its expected values from BreadSched. The declaration is materialized
as native SQLite, closed, reopened, and analyzed through the public estimator boundary.

`historical-rules` covers an isolated spike, an equal-frequency funding-account tie,
a repeated calendar-month profile, a material trend, confidence inputs, and stable
monthly anchors. `CALCULATIONS.md` records the independent arithmetic and
`expected-estimates.json` is the authored acceptance snapshot.

Future rule changes must use a new rule-set version and deliberately update or add
golden expectations. The tests also apply a stricter, separately named seasonal rule
to this same book to prove that changed policy is both behaviorally visible and
identified in structured evidence.
