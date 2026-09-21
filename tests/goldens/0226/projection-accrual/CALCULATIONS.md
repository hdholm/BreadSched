# Projection accrual calculations

The opening stocks are $10,000 cash, $20,000 investment, and $5,000 debt. On January 16,
$1,000 moves from cash to the investment; on January 20, $500 cash repays principal.
Rates are intentionally simple annual effective rates: 3.65% cash, 7.30% investment, and
10.95% debt. The engine accrues between exact dated events using
`(1 + annual_rate)^(days / 365) - 1`, rounding each reporting result to cents.

The independently calculated January accruals are $28.33 cash interest, $123.14 investment
growth, and $42.61 debt interest. Thus cash closes at
$10,000 - $1,000 - $500 + $28.33 = $8,528.33; investments at $21,123.14; and debt at
$4,542.61. February adds $23.49, $114.48, and $36.35 respectively, producing $8,551.82
cash, $21,237.62 investments, and $4,578.96 debt. Every month reconciles independently.
