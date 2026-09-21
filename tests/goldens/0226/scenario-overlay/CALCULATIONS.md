# Scenario overlay calculations

Base has $2,000 monthly salary and $800 monthly rent, so its cash plan is +$1,200 in both
months. Alternative replaces salary with $1,800, suppresses rent, and adds a $600 February
trip. Its monthly cash plan is therefore +$1,800 and +$1,200 without mutating Base.

Alternative inherits Base's untouched assumptions, overrides cash interest from 1% to 2%,
and overrides the investment account from 7% to 4%. The February dated period changes
those two rates to 3% and 5%. Projection begins with $5,000 cash and $10,000 invested.
Exact event timing and actual/365 accrual give January interest/growth of $10.08/$33.37 and
February interest/growth of $16.58/$37.62. February closes with $8,026.65 cash and
$10,070.99 investments. Provenance remains Base for inherited global assumptions and
Alternative for both direct and dated overrides.
