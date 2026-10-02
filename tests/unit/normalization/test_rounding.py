"""Task 16 / USR-004-02: currency-based amount rounding at the booking step."""

from decimal import Decimal

import pytest

from intake.models import FlagReason, StageStatus
from intake.normalization import (
    NormalizedAmounts,
    normalize_amount,
    round_amounts_for_ledger,
    round_for_ledger,
)
from intake.normalization.result import NormalizationFailure


def _assert_flagged(result: object) -> NormalizationFailure:
    assert isinstance(result, NormalizationFailure), result
    assert result.reason is FlagReason.INCOMPLETE_DATA
    assert result.detail
    return result


def _assert_exact(result: object, expected: str) -> None:
    """Same value *and* same representation (exponent), so no hidden precision change."""
    assert isinstance(result, Decimal), result
    assert result == Decimal(expected)
    assert result.as_tuple() == Decimal(expected).as_tuple()


# --- AC1: HUF rounds to the nearest whole integer -----------------------------------


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        ("125000.40", "125000"),
        ("125000.49", "125000"),
        ("125000.51", "125001"),
        ("125000.6", "125001"),
        ("0.4", "0"),
        ("1234.499999", "1234"),
        ("1234.00", "1234"),
        ("1234", "1234"),
        ("-125000.40", "-125000"),
        ("-125000.6", "-125001"),
    ],
)
def test_huf_rounds_to_nearest_whole_integer(amount: str, expected: str) -> None:
    _assert_exact(round_for_ledger(Decimal(amount), "HUF"), expected)


def test_huf_result_has_no_decimal_places() -> None:
    """QA happy path: 125000.40 is booked as 125000 with no decimal places."""
    result = round_for_ledger(Decimal("125000.40"), "HUF")
    assert isinstance(result, Decimal)
    assert result.as_tuple().exponent == 0
    assert str(result) == "125000"


def test_huf_positive_exponent_is_rendered_as_plain_integer() -> None:
    result = round_for_ledger(Decimal("1.2E+3"), "HUF")
    _assert_exact(result, "1200")


@pytest.mark.parametrize("amount", ["-0.4", "-0.00", "0.00"])
def test_huf_rounding_to_zero_is_plain_zero(amount: str) -> None:
    result = round_for_ledger(Decimal(amount), "HUF")
    _assert_exact(result, "0")
    assert isinstance(result, Decimal) and not result.is_signed()


def test_huf_very_large_amount_is_rounded_exactly() -> None:
    # Beyond the default 28-digit context precision: must not raise or lose digits.
    amount = Decimal("123456789012345678901234567890.5")
    _assert_exact(round_for_ledger(amount, "HUF"), "123456789012345678901234567891")


# --- AC4: the .5 boundary rounds away from zero, deterministically ------------------


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        ("999.50", "1000"),
        ("999.5", "1000"),
        ("1234.50", "1235"),
        ("0.5", "1"),
        ("2.5", "3"),  # not banker's rounding (which would give 2)
        ("-0.5", "-1"),
        ("-999.50", "-1000"),
        ("-2.5", "-3"),
    ],
)
def test_huf_half_rounds_away_from_zero(amount: str, expected: str) -> None:
    _assert_exact(round_for_ledger(Decimal(amount), "HUF"), expected)


def test_huf_half_boundary_is_deterministic() -> None:
    """QA edge: 999.50 rounds to 1000 on every one of 50 runs."""
    results = {round_for_ledger(Decimal("999.50"), "HUF") for _ in range(50)}
    assert results == {Decimal("1000")}


# --- AC2: non-HUF amounts keep their precision unchanged ----------------------------


@pytest.mark.parametrize(
    ("amount", "currency"),
    [
        ("1234.56", "EUR"),
        ("1234.56", "USD"),
        ("12.345", "EUR"),  # three decimals stay three decimals: no rounding to 2 dp
        ("999.50", "EUR"),  # the HUF .5 boundary does not apply
        ("0.5", "GBP"),
        ("1234.50", "CHF"),  # trailing zero preserved
        ("1234", "USD"),  # no decimals are invented either
        ("-1234.56", "EUR"),
        ("1234.5678", "JPY"),  # only HUF is integer-booked, whatever the ISO minor unit
    ],
)
def test_non_huf_amount_is_passed_through_unchanged(amount: str, currency: str) -> None:
    _assert_exact(round_for_ledger(Decimal(amount), currency), amount)


def test_eur_amount_is_never_truncated_to_an_integer() -> None:
    """QA negative path: EUR 1234.56 stays 1234.56."""
    result = round_for_ledger(Decimal("1234.56"), "EUR")
    assert result == Decimal("1234.56")
    assert result != Decimal("1235")


# --- AC5: no rounding decision without a resolved ISO currency ----------------------


@pytest.mark.parametrize(
    "currency",
    [
        None,
        "",
        "   ",
        "Ft",  # a symbol, not yet resolved to an ISO code
        "€",
        "$",
        "huf",  # not the canonical form `to_iso_code` produces
        "Huf",
        " HUF",
        "HUF ",
        "HUF.",
        "HU",
        "HUFF",
        "H1F",
        "XYZ",  # three letters, but not an ISO 4217 currency
        "ＨＵＦ",  # full-width letters
    ],
)
def test_unresolved_currency_blocks_rounding(currency: str | None) -> None:
    failure = _assert_flagged(round_for_ledger(Decimal("125000.40"), currency))
    assert failure.field == "currency"


def test_unresolved_currency_does_not_default_to_either_convention() -> None:
    for amount in ("125000.40", "125000", "999.50"):
        result = round_for_ledger(Decimal(amount), None)
        assert not isinstance(result, Decimal)


def test_unresolved_currency_becomes_an_incomplete_stage_result() -> None:
    failure = _assert_flagged(round_for_ledger(Decimal("1"), ""))
    stage = failure.to_stage_result()
    assert stage.status is StageStatus.INCOMPLETE
    assert stage.flag_reason is FlagReason.INCOMPLETE_DATA


@pytest.mark.parametrize("amount", ["NaN", "sNaN", "Infinity", "-Infinity"])
@pytest.mark.parametrize("currency", ["HUF", "EUR"])
def test_non_finite_amount_is_flagged(amount: str, currency: str) -> None:
    _assert_flagged(round_for_ledger(Decimal(amount), currency))


# --- AC3: the rule is applied independently to Net and Gross ------------------------


def test_huf_net_and_gross_are_each_rounded() -> None:
    amounts = NormalizedAmounts(net=Decimal("100000.50"), gross=Decimal("127000.40"))
    rounded = round_amounts_for_ledger(amounts, "HUF")
    _assert_exact(rounded.net, "100001")
    _assert_exact(rounded.gross, "127000")
    assert rounded.failures == ()


def test_non_huf_net_and_gross_are_each_preserved() -> None:
    amounts = NormalizedAmounts(net=Decimal("1000.505"), gross=Decimal("1270.64"))
    rounded = round_amounts_for_ledger(amounts, "EUR")
    _assert_exact(rounded.net, "1000.505")
    _assert_exact(rounded.gross, "1270.64")


def test_net_and_gross_match_the_single_amount_rule() -> None:
    net, gross = Decimal("999.50"), Decimal("1269.365")
    for currency in ("HUF", "EUR"):
        rounded = round_amounts_for_ledger(NormalizedAmounts(net=net, gross=gross), currency)
        assert rounded.net == round_for_ledger(net, currency)
        assert rounded.gross == round_for_ledger(gross, currency)


def test_one_unusable_field_does_not_block_the_other() -> None:
    upstream = NormalizationFailure("unrecognised amount format 'abc'", field="net")
    amounts = NormalizedAmounts(net=upstream, gross=Decimal("127000.40"))
    rounded = round_amounts_for_ledger(amounts, "HUF")
    assert rounded.net is upstream
    _assert_exact(rounded.gross, "127000")


def test_unextracted_fields_stay_none() -> None:
    rounded = round_amounts_for_ledger(NormalizedAmounts(net=Decimal("1.5")), "HUF")
    _assert_exact(rounded.net, "2")
    assert rounded.gross is None
    assert rounded.failures == ()


def test_unresolved_currency_flags_both_fields_by_name() -> None:
    amounts = NormalizedAmounts(net=Decimal("100.5"), gross=Decimal("127.5"))
    rounded = round_amounts_for_ledger(amounts, None)
    failures = rounded.failures
    assert [failure.field for failure in failures] == ["net", "gross"]
    for failure in failures:
        _assert_flagged(failure)
        assert "currency" in failure.detail


def test_non_finite_field_is_flagged_by_name() -> None:
    amounts = NormalizedAmounts(net=Decimal("NaN"), gross=Decimal("127.5"))
    rounded = round_amounts_for_ledger(amounts, "HUF")
    assert [failure.field for failure in rounded.failures] == ["net"]
    _assert_exact(rounded.gross, "128")


def test_input_amounts_are_not_mutated() -> None:
    amounts = NormalizedAmounts(net=Decimal("100.5"), gross=Decimal("127.4"))
    round_amounts_for_ledger(amounts, "HUF")
    assert amounts == NormalizedAmounts(net=Decimal("100.5"), gross=Decimal("127.4"))


# --- Rounding happens only at the booking step --------------------------------------


def test_normalisation_still_preserves_huf_source_precision() -> None:
    """USR-002-03 is unchanged: rounding is a separate, later step."""
    normalized = normalize_amount("125 000,40 Ft")
    _assert_exact(normalized, "125000.40")
    assert isinstance(normalized, Decimal)
    _assert_exact(round_for_ledger(normalized, "HUF"), "125000")


@pytest.mark.parametrize(
    ("raw", "currency", "expected"),
    [
        ("1.234,50", "HUF", "1235"),
        ("1 234,49", "HUF", "1234"),
        ("12 500,-", "HUF", "12500"),
        ("1,234.56", "USD", "1234.56"),
        ("€1.234,56", "EUR", "1234.56"),
    ],
)
def test_normalise_then_round(raw: str, currency: str, expected: str) -> None:
    normalized = normalize_amount(raw)
    assert isinstance(normalized, Decimal)
    _assert_exact(round_for_ledger(normalized, currency), expected)
