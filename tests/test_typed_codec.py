"""The book API's generic typed JSON form (#357).

**Experimental** (branch ``claude/experimental-book-server``). ``api.typed_codec``
writes the services' typed requests and results as JSON data and reads them back
from their types alone. A server reads what a client sends, so reading refuses an
unknown field, a missing one, or a value of the wrong type, and names where it is.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Literal

import pytest

from breadsched.api.typed_codec import from_data, to_data
from breadsched.gen.lib import Account, AccountType, Money
from breadsched.gen.lib.scenario import ReimbursementOverride


class Colour(Enum):
    RED = "red"
    BLUE = "blue"


@dataclass(frozen=True)
class Inner:
    when: date
    amount: Money | None = None


@dataclass(frozen=True)
class Outer:
    name: str
    count: int
    flag: bool
    colour: Colour
    inner: Inner
    many: tuple[Inner, ...]
    pair: tuple[str, int]
    names: dict[str, Money]
    choice: Literal["a", "b"]
    account: Account
    rate: Decimal
    at: datetime
    note: str = ""
    tags: list[str] = field(default_factory=list)


def _outer() -> Outer:
    return Outer(
        name="x",
        count=3,
        flag=True,
        colour=Colour.BLUE,
        inner=Inner(date(2026, 1, 2), Money("1/3")),
        many=(Inner(date(2026, 2, 3)),),
        pair=("p", 2),
        names={"rent": Money("1800.25")},
        choice="b",
        account=Account(name="Checking", atype=AccountType.BANK),
        rate=Decimal("0.0425"),
        at=datetime(2026, 1, 2, 3, 4, 5),
        tags=["t"],
    )


def test_a_value_round_trips_through_its_json_form():
    value = _outer()
    data = to_data(value)
    assert data["inner"] == {"when": "2026-01-02", "amount": "1/3"}
    assert data["names"] == {"rent": "1800.25"} and data["colour"] == "blue"
    assert data["account"] == value.account.serialize()
    restored = from_data(Outer, data)
    assert restored.account.serialize() == value.account.serialize()
    assert to_data(restored) == data
    # A record with its own stored form keeps it.
    override = ReimbursementOverride(Money("10"), date(2026, 5, 1))
    assert from_data(ReimbursementOverride, to_data(override)) == override


def test_a_mapping_reads_as_an_object():
    from breadsched.gen.services.payroll import PayChange

    change = PayChange("s", date(2026, 3, 1), Money("10"), amounts={"a": Money("2.50")})
    data = to_data(change)
    assert data["amounts"] == {"a": "2.50"} and data["scaled"] is None
    assert from_data(PayChange, data) == change
    with pytest.raises(ValueError, match=r"value\.amounts\.a must be an amount in text"):
        from_data(PayChange, {**data, "amounts": {"a": 2}})


def test_missing_fields_take_their_defaults():
    data = to_data(_outer())
    del data["note"], data["tags"]
    restored = from_data(Outer, data)
    assert (restored.note, restored.tags) == ("", [])


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"colour": "green"}, "value.colour must be one of red, blue"),
        ({"count": True}, "value.count must be a whole number"),
        ({"count": "3"}, "value.count must be a whole number"),
        ({"flag": 1}, "value.flag must be true or false"),
        ({"inner": {"when": "soon"}}, "value.inner.when must be a date"),
        ({"inner": {"when": "2026-01-02", "colour": "red"}}, "unknown field: colour"),
        ({"many": {}}, "value.many must be a list"),
        ({"pair": ["p"]}, "value.pair must be a list of 2 values"),
        ({"names": {"rent": 5}}, "value.names.rent must be an amount in text"),
        ({"choice": "c"}, "value.choice must be one of 'a', 'b'"),
        ({"account": "Checking"}, "value.account must be an object"),
        ({"account": {}}, "value.account is not a valid Account"),
        ({"rate": 0.04}, "value.rate must be a decimal number in text"),
        ({"surprise": 1}, "value has unknown field: surprise"),
    ],
)
def test_wrong_input_is_refused_with_where_it_is(change, message):
    data = {**to_data(_outer()), **change}
    with pytest.raises(ValueError, match=message.replace("(", r"\(").replace(")", r"\)")):
        from_data(Outer, data)


def test_a_required_field_must_be_given():
    data = to_data(_outer())
    del data["name"]
    with pytest.raises(ValueError, match=r"value\.name is required"):
        from_data(Outer, data)
    with pytest.raises(ValueError, match="must be an object"):
        from_data(Outer, [])
    with pytest.raises(ValueError, match=r"value\.when must be a date"):
        from_data(Inner, {"when": None})


def test_an_unsupported_value_is_a_programming_error():
    with pytest.raises(TypeError):
        to_data(object())
    with pytest.raises(TypeError):
        from_data(complex, "1j")
