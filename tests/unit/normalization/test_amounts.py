"""Task 7 / USR-002-03: monetary amount normalisation (Hungarian-locale notation)."""

from decimal import Decimal

import pytest

from intake.models import FlagReason, InvoiceExtraction
from intake.normalization import (
    NormalizedAmounts,
    format_hungarian_amount,
    normalize_amount,
    normalize_amounts,
)
from intake.normalization.amounts import AMOUNT_FIELDS, HU_THOUSANDS_SEPARATOR
from intake.normalization.result import NormalizationFailure

NBSP = " "
NARROW_NBSP = " "
THIN_SPACE = " "


def _assert_flagged(result: object) -> NormalizationFailure:
    assert isinstance(result, NormalizationFailure), result
    assert result.reason is FlagReason.INCOMPLETE_DATA
    assert result.detail
    return result


def _assert_amount(result: object, expected: str) -> None:
    assert isinstance(result, Decimal), result
    assert result == Decimal(expected)
    # Source precision is preserved: same digits after the decimal separator.
    assert result.as_tuple().exponent == Decimal(expected).as_tuple().exponent


# --- AC1: US format -------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1,234.56", "1234.56"),
        ("1,234,567.89", "1234567.89"),
        ("12,345.6", "12345.6"),
        ("999,999.99", "999999.99"),
        ("1,234.567", "1234.567"),
        ("1,234,567", "1234567"),
        ("1,000,000", "1000000"),
    ],
)
def test_us_format_keeps_its_magnitude(raw: str, expected: str) -> None:
    _assert_amount(normalize_amount(raw), expected)


def test_us_format_is_not_misread_as_hungarian_dot() -> None:
    """QA: the classic 1000x thousands/decimal confusion has a dedicated regression test."""
    result = normalize_amount("1,234.56")
    assert result == Decimal("1234.56")
    assert result != Decimal("1.23456")


# --- AC2: Hungarian dot-separated -----------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1.234,56", "1234.56"),
        ("1.234.567,89", "1234567.89"),
        ("12.345,6", "12345.6"),
        ("1.234,500", "1234.500"),
        ("1.234.567", "1234567"),
        ("125.000.000", "125000000"),
    ],
)
def test_hungarian_dot_thousands_are_not_decimal_points(raw: str, expected: str) -> None:
    _assert_amount(normalize_amount(raw), expected)


def test_hungarian_dot_amount_is_not_misread_as_a_decimal() -> None:
    result = normalize_amount("1.234,56")
    assert result == Decimal("1234.56")
    assert result != Decimal("1.234")


# --- AC3: Hungarian space-separated (incl. non-breaking / narrow spaces) -------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1 234,56", "1234.56"),
        (f"1{NBSP}234,56", "1234.56"),
        (f"1{NARROW_NBSP}234,56", "1234.56"),
        (f"1{THIN_SPACE}234,56", "1234.56"),
        ("1 234 567,89", "1234567.89"),
        (f"1{NBSP}234{NARROW_NBSP}567,8", "1234567.8"),
        ("1 234", "1234"),
        ("12 345 678", "12345678"),
        ("125 000,40", "125000.40"),
        ("1 234,567", "1234.567"),
    ],
)
def test_hungarian_space_thousands(raw: str, expected: str) -> None:
    _assert_amount(normalize_amount(raw), expected)


def test_space_dot_and_no_separator_variants_agree() -> None:
    """QA: '1 234,56' and '1234,56' resolve to the same value."""
    values = {normalize_amount(raw) for raw in ("1 234,56", "1234,56", "1.234,56", "1,234.56")}
    assert values == {Decimal("1234.56")}


# --- AC4: no thousands separator ------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1234,56", "1234.56"),
        ("1234.56", "1234.56"),
        ("1234", "1234"),
        ("0", "0"),
        ("0,5", "0.5"),
        ("0.99", "0.99"),
        ("12,5", "12.5"),
        ("1234,567", "1234.567"),
        ("1234.5678", "1234.5678"),
        ("0,123", "0.123"),
        ("0.125", "0.125"),
        ("125000", "125000"),
        ("1234.50", "1234.50"),
        ("1,2", "1.2"),
    ],
)
def test_no_thousands_separator(raw: str, expected: str) -> None:
    _assert_amount(normalize_amount(raw), expected)


def test_source_precision_is_preserved_not_rounded() -> None:
    assert str(normalize_amount("1.234,50")) == "1234.50"
    assert str(normalize_amount("125000,40")) == "125000.40"
    assert str(normalize_amount("0,0001")) == "0.0001"
    assert str(normalize_amount("1 000")) == "1000"


# --- Space thousands with dot decimal (SI style): space is never a decimal ------


def test_space_thousands_with_dot_decimal() -> None:
    _assert_amount(normalize_amount("1 234.56"), "1234.56")


# --- Hungarian ",-" whole-amount marker ------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("12 500,-", "12500"),
        ("1.234,-", "1234"),
        ("1234,-", "1234"),
        ("1234,–", "1234"),
        ("1,234.-", "1234"),
        ("12 500,- Ft", "12500"),
    ],
)
def test_whole_amount_dash_marker(raw: str, expected: str) -> None:
    _assert_amount(normalize_amount(raw), expected)


@pytest.mark.parametrize("raw", ["1,234,-", "1.234.-", "12,5-"])
def test_whole_amount_marker_must_use_the_decimal_separator(raw: str) -> None:
    _assert_flagged(normalize_amount(raw))


# --- Currency symbols / codes glued to the number --------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("€1,234.56", "1234.56"),
        ("€ 1.234,56", "1234.56"),
        ("1 234,56 Ft", "1234.56"),
        ("1 234,56Ft", "1234.56"),
        ("12 500 Ft.", "12500"),
        ("USD 1,234.56", "1234.56"),
        ("1.234,56 EUR", "1234.56"),
        ("HUF 12 500", "12500"),
        ("$12.50", "12.50"),
        ("US$ 5", "5"),
        ("12,50 zł", "12.50"),
        (f"1{NBSP}234,56{NBSP}Ft", "1234.56"),
        ("  1234,56  ", "1234.56"),
    ],
)
def test_currency_affix_is_ignored(raw: str, expected: str) -> None:
    _assert_amount(normalize_amount(raw), expected)


@pytest.mark.parametrize(
    "raw",
    [
        "Ft 1 234 Ft",  # two currency affixes
        "€ 12,50 EUR",
        "approx 100",  # a word, not a currency symbol/code
        "Total: 100",
        "100 forint",
        "12.5%",
        "#100",
    ],
)
def test_text_around_the_number_that_is_not_a_currency_is_flagged(raw: str) -> None:
    _assert_flagged(normalize_amount(raw))


# --- Negative amounts ------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("-1,234.56", "-1234.56"),
        ("-1 234,56", "-1234.56"),
        ("−1 234,56", "-1234.56"),  # U+2212 MINUS SIGN
        ("-€12.50", "-12.50"),
        ("€-12.50", "-12.50"),
        ("Ft -500", "-500"),
        ("- 500 Ft", "-500"),
    ],
)
def test_explicit_leading_minus_gives_a_negative_amount(raw: str, expected: str) -> None:
    _assert_amount(normalize_amount(raw), expected)


@pytest.mark.parametrize("raw", ["(100.00)", "100-", "--5", "+-5", "-", "1-2"])
def test_other_sign_notations_are_flagged_not_guessed(raw: str) -> None:
    _assert_flagged(normalize_amount(raw))


def test_negative_zero_is_plain_zero() -> None:
    result = normalize_amount("-0,00")
    assert result == Decimal("0.00")
    assert isinstance(result, Decimal)
    assert not result.is_signed()


# --- AC5: genuinely ambiguous / unclassifiable values are flagged ---------------


@pytest.mark.parametrize(
    "raw",
    [
        # one separator followed by exactly three digits: thousands (US/HU) or decimal?
        "1.234",
        "1,234",
        "12,345",
        "123.456",
        "-1.234",
        "$1,234",
        "1.234 Ft",
    ],
)
def test_single_separator_with_three_trailing_digits_is_ambiguous(raw: str) -> None:
    failure = _assert_flagged(normalize_amount(raw))
    assert "ambiguous" in failure.detail


@pytest.mark.parametrize(
    "raw",
    [
        "1,234.56.78",  # decimal separator twice
        "1.234,56,78",
        "1.234.56",  # invalid thousands group
        "1,23,456",  # Indian lakh grouping is not a supported convention
        "1 234.567,89",  # three separator kinds
        "12 34",  # space groups must be three digits
        "1,234 56",  # space can never be a decimal separator
        "1 2345",
        "1234 567",
        "0.123.456",  # grouping with a leading zero group
        "1..234",
        "1,,5",
        ",5",
        ".50",
        "1234.",
        "1234,",
        "1'234.56",  # Swiss apostrophe grouping is not supported
        "1e5",
        "NaN",
        "Infinity",
        "١٢٣",  # non-ASCII digits
        "12,5 34",
    ],
)
def test_unclassifiable_separator_patterns_are_flagged(raw: str) -> None:
    _assert_flagged(normalize_amount(raw))


@pytest.mark.parametrize("raw", [None, "", "   ", NBSP, "Ft", "€"])
def test_missing_amount_is_flagged(raw: str | None) -> None:
    _assert_flagged(normalize_amount(raw))


def test_failure_turns_into_incomplete_stage_result() -> None:
    failure = _assert_flagged(normalize_amount("1.234"))
    stage = failure.to_stage_result()
    assert not stage.is_ok
    assert stage.flag_reason is FlagReason.INCOMPLETE_DATA


def test_normalisation_is_deterministic() -> None:
    results = {normalize_amount("1 234,56") for _ in range(50)}
    assert results == {Decimal("1234.56")}


# --- Hungarian-locale output notation ---------------------------------------------


def test_thousands_separator_is_a_non_breaking_space() -> None:
    assert HU_THOUSANDS_SEPARATOR == NBSP


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        (Decimal("1234.56"), f"1{NBSP}234,56"),
        (Decimal("1234567.5"), f"1{NBSP}234{NBSP}567,5"),
        (Decimal("999"), "999"),
        (Decimal("1000"), f"1{NBSP}000"),
        (Decimal("-1234.50"), f"-1{NBSP}234,50"),
        (Decimal("0.001"), "0,001"),
        (Decimal("0"), "0"),
        (Decimal("1E+3"), f"1{NBSP}000"),
        (Decimal("125000.40"), f"125{NBSP}000,40"),
    ],
)
def test_format_hungarian_amount(amount: Decimal, expected: str) -> None:
    assert format_hungarian_amount(amount) == expected


@pytest.mark.parametrize("raw", ["1,234.56", "1.234,56", "1 234,56", "1234,56", "1234.56"])
def test_every_input_convention_yields_the_same_hungarian_notation(raw: str) -> None:
    """AC1-AC4: the output is Hungarian locale notation of the same numeric value."""
    amount = normalize_amount(raw)
    assert isinstance(amount, Decimal)
    assert format_hungarian_amount(amount) == f"1{NBSP}234,56"


@pytest.mark.parametrize(
    "amount", [Decimal("1234.56"), Decimal("-98765.4321"), Decimal("1000000"), Decimal("0.5")]
)
def test_hungarian_notation_round_trips(amount: Decimal) -> None:
    assert normalize_amount(format_hungarian_amount(amount)) == amount


@pytest.mark.parametrize("amount", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")])
def test_format_rejects_non_finite_amounts(amount: Decimal) -> None:
    with pytest.raises(ValueError, match="finite"):
        format_hungarian_amount(amount)


# --- AC6: every monetary field normalised independently with the same rules -----


def test_amount_fields() -> None:
    assert AMOUNT_FIELDS == ("net", "gross")


def test_all_monetary_fields_are_normalised_with_the_same_rules() -> None:
    extraction = InvoiceExtraction(net="1.234,56", gross="1 567,89 Ft")
    result = normalize_amounts(extraction)
    assert result == NormalizedAmounts(net=Decimal("1234.56"), gross=Decimal("1567.89"))
    assert result.failures == ()
    assert result.formatted() == {"net": f"1{NBSP}234,56", "gross": f"1{NBSP}567,89"}


def test_mixed_conventions_on_one_invoice_are_each_classified_independently() -> None:
    result = normalize_amounts(InvoiceExtraction(net="1,000.50", gross="1.270,64"))
    assert result.net == Decimal("1000.50")
    assert result.gross == Decimal("1270.64")


def test_one_failing_field_does_not_block_the_other() -> None:
    result = normalize_amounts(InvoiceExtraction(net="1.234", gross="1 567,89"))
    assert isinstance(result.net, NormalizationFailure)
    assert result.net.field == "net"
    assert result.gross == Decimal("1567.89")
    assert result.failures == (result.net,)
    assert result.formatted() == {"net": None, "gross": f"1{NBSP}567,89"}


def test_unextracted_fields_stay_none_and_are_not_failures() -> None:
    result = normalize_amounts(InvoiceExtraction(net=None, gross="100"))
    assert result.net is None
    assert result.gross == Decimal("100")
    assert result.failures == ()


def test_blank_extracted_field_is_a_failure() -> None:
    result = normalize_amounts(InvoiceExtraction(net="  ", gross="100"))
    assert isinstance(result.net, NormalizationFailure)
    assert result.net.field == "net"
