"""Task 12: the shared ``YYMM`` monthly-folder naming helper (also used by filing, Task 14)."""

from datetime import date, datetime

import pytest

from intake.registry.folder_naming import validate_yymm, yymm_folder_name


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (date(2024, 5, 31), "2405"),
        (date(2024, 12, 1), "2412"),
        (date(2030, 1, 15), "3001"),
        (date(2000, 10, 10), "0010"),
        (date(2109, 3, 3), "0903"),  # two-digit year, as in the registry number prefix
    ],
)
def test_folder_name_is_two_digit_year_and_zero_padded_month(value: date, expected: str) -> None:
    assert yymm_folder_name(value) == expected


def test_datetime_is_accepted_and_time_is_ignored() -> None:
    assert yymm_folder_name(datetime(2024, 6, 30, 23, 59)) == "2406"


def test_folder_name_is_always_a_valid_yymm() -> None:
    for month in range(1, 13):
        name = yymm_folder_name(date(2024, month, 1))
        assert validate_yymm(name) == name
        assert len(name) == 4


def test_rejects_non_date_input() -> None:
    with pytest.raises(TypeError):
        yymm_folder_name("2024-05-01")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "bad", ["", "245", "24050", "2400", "2413", "24-5", "ab05", " 2405", "2405\n", "２４０５"]
)
def test_validate_yymm_rejects_malformed_values(bad: str) -> None:
    with pytest.raises(ValueError):
        validate_yymm(bad)
