"""Static contracts for commodity-tagged amount arithmetic."""

from decimal import Decimal
from fractions import Fraction

from breadsched.gen.lib.amount import Amount
from breadsched.gen.lib.money import Money, Rate

amount = Amount(Money("10"), "USD")
other = Amount(Money("2"), "USD")

added: Amount = amount + other
subtracted: Amount = amount - other
scaled: Amount = amount * Rate("1.5")
reflected_scaled: Amount = Decimal("1.5") * amount
divided: Amount = amount / 2
ratio: Fraction = amount / other
negative: Amount = -amount
absolute: Amount = abs(amount)
quantized: Amount = amount.quantize(100)
