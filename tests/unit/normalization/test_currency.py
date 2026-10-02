"""Task 7 / USR-002-05: currency captured as an ISO 4217 code."""

from collections.abc import Mapping
from types import MappingProxyType

import pytest

from intake.config import Config
from intake.models import FlagReason, InvoiceExtraction
from intake.normalization import Origin, determine_origin, to_iso_code
from intake.normalization.result import NormalizationFailure

# The seed table the spec names (HUF/EUR/USD), as the Config tab would hold it.
SEED_MAP: Mapping[str, str] = MappingProxyType({"Ft": "HUF", "€": "EUR", "$": "USD"})
ALL_ORIGINS = list(Origin)


def _assert_flagged(result: object) -> NormalizationFailure:
    assert isinstance(result, NormalizationFailure), result
    assert result.reason is FlagReason.INCOMPLETE_DATA
    assert result.field == "currency"
    assert result.detail
    return result


# --- AC1: symbols / local abbreviations map to ISO codes --------------------------


@pytest.mark.parametrize("origin", ALL_ORIGINS)
@pytest.mark.parametrize(("raw", "expected"), [("Ft", "HUF"), ("€", "EUR")])
def test_unambiguous_symbol_maps_to_its_iso_code_for_any_origin(
    raw: str, expected: str, origin: Origin
) -> None:
    assert to_iso_code(raw, origin, SEED_MAP) == expected


def test_dollar_on_a_us_invoice_is_usd() -> None:
    assert to_iso_code("$", Origin.US, SEED_MAP) == "USD"


def test_map_from_a_config_tab_string_is_used() -> None:
    config = Config.from_mapping(
        {
            "invoice_keywords": "számla",
            "attachment_mime_allowlist": "application/pdf",
            "contractor_identifiers": "contractor@example.com",
            "tig_subject_indicators": "TIG",
            "currency_map": "Ft=HUF, €=EUR, $=USD",
            "label_processed": "Kibit/Processed",
            "label_pending": "Kibit/Pending",
            "label_needs_review": "Kibit/NeedsReview",
            "label_awaiting_tig": "Kibit/AwaitingTIG",
            "sequence_start": "1",
            "sequence_width": "3",
            "rounding_tolerance": "0.01",
            "drive_root_folder_id": "root",
        }
    )
    assert to_iso_code("Ft", Origin.DOMESTIC, config.currency_map) == "HUF"
    assert to_iso_code("€", Origin.OTHER_FOREIGN, config.currency_map) == "EUR"
    assert to_iso_code("$", Origin.US, config.currency_map) == "USD"


def test_table_is_data_new_currencies_need_no_code_change() -> None:
    extended = {**SEED_MAP, "zł": "PLN", "CHF.": "CHF", "US$": "USD"}
    assert to_iso_code("zł", Origin.OTHER_FOREIGN, extended) == "PLN"
    assert to_iso_code("US$", Origin.OTHER_FOREIGN, extended) == "USD"
    # Without the entry the same symbol is unknown, not guessed.
    _assert_flagged(to_iso_code("zł", Origin.OTHER_FOREIGN, SEED_MAP))


def test_only_configured_symbols_are_recognised() -> None:
    _assert_flagged(to_iso_code("Ft", Origin.DOMESTIC, {"€": "EUR"}))


# --- AC2: ISO codes pass through unchanged ------------------------------------------


@pytest.mark.parametrize("origin", ALL_ORIGINS)
@pytest.mark.parametrize("raw", ["USD", "EUR", "HUF", "GBP", "CHF", "CAD"])
def test_iso_code_is_passed_through(raw: str, origin: Origin) -> None:
    assert to_iso_code(raw, origin, SEED_MAP) == raw


def test_iso_code_passes_through_even_with_an_empty_table() -> None:
    assert to_iso_code("USD", Origin.UNKNOWN, {}) == "USD"


# --- AC3: ambiguous symbols are resolved by country of origin -----------------------


@pytest.mark.parametrize("origin", [Origin.OTHER_FOREIGN, Origin.UNKNOWN, Origin.DOMESTIC])
def test_dollar_is_not_defaulted_when_origin_cannot_resolve_it(origin: Origin) -> None:
    failure = _assert_flagged(to_iso_code("$", origin, SEED_MAP))
    assert "ambiguous" in failure.detail


def test_ambiguous_symbol_needs_origin_to_confirm_the_configured_code() -> None:
    # "$" configured as CAD, but a US invoice's "$" is USD: the two disagree -> flag.
    _assert_flagged(to_iso_code("$", Origin.US, {"$": "CAD"}))


@pytest.mark.parametrize("origin", ALL_ORIGINS)
def test_configured_but_shared_symbols_are_ambiguous_without_a_matching_origin(
    origin: Origin,
) -> None:
    # "kr" is DKK/NOK/SEK; no Origin identifies those countries.
    _assert_flagged(to_iso_code("kr", origin, {"kr": "SEK"}))


@pytest.mark.parametrize(
    ("country", "expected"),
    [("USA", "USD"), ("United States", "USD"), ("US", "USD")],
)
def test_dollar_resolved_via_shared_determine_origin(country: str, expected: str) -> None:
    """Cross-module contract: currency uses the same origin as dates/performance date."""
    origin = determine_origin(InvoiceExtraction(supplier_country=country))
    assert to_iso_code("$", origin, SEED_MAP) == expected


@pytest.mark.parametrize("country", ["Canada", "Australia", None, "Atlantis"])
def test_dollar_from_a_non_us_or_unknown_country_is_flagged(country: str | None) -> None:
    origin = determine_origin(InvoiceExtraction(supplier_country=country))
    _assert_flagged(to_iso_code("$", origin, SEED_MAP))


# --- AC4: case and formatting variants -------------------------------------------


@pytest.mark.parametrize(
    "raw",
    ["HUF", "huf", "Huf", "HUF.", "huf.", " HUF ", "Ft", "Ft ", " ft", "FT", "Ft.", "\tFt\n"],
)
def test_case_and_formatting_noise_resolves_to_the_canonical_code(raw: str) -> None:
    assert to_iso_code(raw, Origin.DOMESTIC, SEED_MAP) == "HUF"


@pytest.mark.parametrize("raw", ["usd", "Usd.", " USD", "$", " $ ", "＄"])  # fullwidth $
def test_usd_variants(raw: str) -> None:
    assert to_iso_code(raw, Origin.US, SEED_MAP) == "USD"


@pytest.mark.parametrize("raw", ["€", " € ", "eur", "Eur.", "EUR"])
def test_eur_variants(raw: str) -> None:
    assert to_iso_code(raw, Origin.OTHER_FOREIGN, SEED_MAP) == "EUR"


def test_configured_keys_are_matched_with_the_same_cleaning() -> None:
    assert to_iso_code("ft", Origin.DOMESTIC, {" Ft. ": "HUF"}) == "HUF"


def test_conflicting_configured_variants_are_flagged() -> None:
    _assert_flagged(to_iso_code("ft", Origin.DOMESTIC, {"Ft": "HUF", "FT": "EUR"}))


def test_consistent_duplicate_variants_are_fine() -> None:
    assert to_iso_code("ft", Origin.DOMESTIC, {"Ft": "HUF", "FT": "HUF"}) == "HUF"


# --- AC5: unrecognisable values are flagged, not guessed ---------------------------


@pytest.mark.parametrize("origin", ALL_ORIGINS)
@pytest.mark.parametrize(
    "raw",
    [
        "XYZ",  # three letters but not an ISO 4217 code
        "ABC",
        "forint",
        "dollars",
        "Euro",
        "HUF/EUR",
        "€$",
        "US D",
        "HUFF",
    ],
)
def test_unknown_currency_is_flagged(raw: str, origin: Origin) -> None:
    _assert_flagged(to_iso_code(raw, origin, SEED_MAP))


@pytest.mark.parametrize("raw", [None, "", "   ", ".", " "])
@pytest.mark.parametrize("origin", ALL_ORIGINS)
def test_missing_currency_is_flagged_even_with_a_known_origin(
    raw: str | None, origin: Origin
) -> None:
    """A domestic supplier may invoice in EUR, so origin never supplies a missing currency."""
    _assert_flagged(to_iso_code(raw, origin, SEED_MAP))


def test_failure_turns_into_incomplete_stage_result() -> None:
    failure = _assert_flagged(to_iso_code("XYZ", Origin.UNKNOWN, SEED_MAP))
    stage = failure.to_stage_result()
    assert not stage.is_ok
    assert stage.flag_reason is FlagReason.INCOMPLETE_DATA
    assert stage.detail is not None
    assert stage.detail.startswith("currency: ")


# --- AC6: the ISO code is a distinct value with no amount embedded ----------------


@pytest.mark.parametrize("raw", ["1 234 Ft", "Ft 1234", "USD 12.50", "100€", "HUF1"])
def test_values_with_an_amount_embedded_are_not_currencies(raw: str) -> None:
    _assert_flagged(to_iso_code(raw, Origin.DOMESTIC, SEED_MAP))


@pytest.mark.parametrize(
    ("raw", "origin"),
    [("Ft", Origin.DOMESTIC), ("€", Origin.UNKNOWN), ("$", Origin.US), ("gbp", Origin.UNKNOWN)],
)
def test_result_is_a_bare_iso_code(raw: str, origin: Origin) -> None:
    result = to_iso_code(raw, origin, SEED_MAP)
    assert isinstance(result, str)
    assert len(result) == 3
    assert result.isascii()
    assert result.isalpha()
    assert result.isupper()


def test_currency_normalisation_is_deterministic() -> None:
    assert {to_iso_code("Ft ", Origin.DOMESTIC, SEED_MAP) for _ in range(50)} == {"HUF"}
