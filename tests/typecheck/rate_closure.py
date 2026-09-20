"""Static contracts for dimensionally closed rate arithmetic."""

from decimal import Decimal

from breadsched.gen.lib.money import Rate

rate = Rate("0.05")
decimal = Decimal("0.10")

add: Rate = rate + decimal
reflected_add: Rate = decimal + rate
subtract: Rate = rate - 1
reflected_subtract: Rate = 1 - rate
multiply: Rate = rate * decimal
reflected_multiply: Rate = decimal * rate
divide: Rate = rate / 2
reflected_divide: Rate = decimal / rate
floor_divide: Rate = rate // decimal
remainder: Rate = decimal % rate
power: Rate = rate**2
reflected_power: Rate = decimal**rate
negative: Rate = -rate
absolute: Rate = abs(rate)
rounded: Rate = round(rate, 2)
quantized: Rate = rate.quantize(Decimal("0.01"))
quotient, divmod_remainder = divmod(rate, decimal)
divmod_quotient: Rate = quotient
divmod_modulus: Rate = divmod_remainder
