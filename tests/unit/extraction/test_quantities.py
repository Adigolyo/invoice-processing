"""Task 5 / USR-002-01 AC3: line-item quantities as printed -> Decimal (units stripped)."""

from decimal import Decimal

import pytest

from intake.extraction import parse_quantity


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("43 db", "43"),
        ("4 alkalom", "4"),
        ("38 óra", "38"),
        ("22 óra", "22"),
        ("1 200 db", "1200"),
        ("1 200 db", "1200"),
        ("2,5 óra", "2.5"),
        ("1.5 hours", "1.5"),
        ("12", "12"),
        ("  7 pcs  ", "7"),
        ("3 m²", "3"),
        ("10 h", "10"),
        ("0,75 nap", "0.75"),
        ("5 db.", "5"),
        ("1 234,5 kg", "1234.5"),
    ],
)
def test_quantity_with_unit_is_parsed(raw: str, expected: str) -> None:
    result = parse_quantity(raw)
    assert result == Decimal(expected)
    assert isinstance(result, Decimal)


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "db",  # unit with no number
        "kb. 40 óra",  # leading text: approximate, never guessed
        "40-45 óra",  # a range is not one quantity
        "1.200 db",  # lone separator + 3 digits: thousands or decimal? flag, never guess
        "1,200 db",
        "4 + 2 alkalom",
        "n/a",
    ],
)
def test_unparseable_quantity_is_none_not_guessed(raw: str | None) -> None:
    assert parse_quantity(raw) is None
