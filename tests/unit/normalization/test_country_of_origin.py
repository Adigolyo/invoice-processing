"""Task 6 / USR-002-02 (shared with USR-002-04, USR-002-05): country-of-origin determination.

Origin is derived from the supplier's country as printed on the invoice
(``InvoiceExtraction.supplier_country``). When that signal is missing or not a
recognisable country, the result is ``UNKNOWN`` so callers flag instead of guessing.
"""

import dataclasses

import pytest

from intake.models import InvoiceExtraction
from intake.normalization import Origin as OriginFromPackage
from intake.normalization import determine_origin as determine_origin_from_package
from intake.normalization.country_of_origin import Origin, determine_origin


def _extraction(country: str | None, **kwargs: str) -> InvoiceExtraction:
    return InvoiceExtraction(supplier_country=country, **kwargs)  # type: ignore[arg-type]


def test_origin_enum_has_exactly_the_four_design_values() -> None:
    assert {member.name for member in Origin} == {"DOMESTIC", "US", "OTHER_FOREIGN", "UNKNOWN"}


def test_package_reexports_shared_api() -> None:
    assert OriginFromPackage is Origin
    assert determine_origin_from_package is determine_origin


@pytest.mark.parametrize(
    "country",
    [
        "HU",
        "hu",
        "HUN",
        "Hungary",
        "hungary",
        "Magyarország",
        "MAGYARORSZÁG",
        "Magyarorszag",
        " HU ",
    ],
)
def test_hungarian_supplier_is_domestic(country: str) -> None:
    assert determine_origin(_extraction(country)) is Origin.DOMESTIC


@pytest.mark.parametrize(
    "country",
    [
        "US",
        "us",
        "USA",
        "U.S.",
        "U.S.A.",
        "United States",
        "United States of America",
        "Egyesült Államok",
    ],
)
def test_us_supplier_is_us(country: str) -> None:
    assert determine_origin(_extraction(country)) is Origin.US


@pytest.mark.parametrize(
    "country", ["DE", "Germany", "Németország", "GB", "United Kingdom", "AT", "Ireland", "FR"]
)
def test_any_other_recognised_country_is_other_foreign(country: str) -> None:
    assert determine_origin(_extraction(country)) is Origin.OTHER_FOREIGN


@pytest.mark.parametrize("country", [None, "", "   "])
def test_missing_country_is_unknown_not_guessed(country: str | None) -> None:
    assert determine_origin(_extraction(country)) is Origin.UNKNOWN


@pytest.mark.parametrize("country", ["Narnia", "XX", "ZZ", "EU", "001", "Europe", "Budapest 1051"])
def test_unrecognisable_country_is_unknown_not_guessed(country: str) -> None:
    assert determine_origin(_extraction(country)) is Origin.UNKNOWN


def test_other_signals_never_override_a_missing_country() -> None:
    """HUF currency or a Hungarian-looking supplier/date is not proof of origin."""
    extraction = _extraction(
        None, supplier="Kővári Kft", currency="Ft", issue_date="2024. március 5."
    )
    assert determine_origin(extraction) is Origin.UNKNOWN


def test_country_wins_over_currency() -> None:
    """A German supplier invoicing in HUF is still other-foreign."""
    assert determine_origin(_extraction("DE", currency="HUF")) is Origin.OTHER_FOREIGN


def test_is_foreign_property_groups_us_and_other_foreign() -> None:
    assert Origin.US.is_foreign
    assert Origin.OTHER_FOREIGN.is_foreign
    assert not Origin.DOMESTIC.is_foreign
    assert not Origin.UNKNOWN.is_foreign


def test_is_known_property() -> None:
    assert all(origin.is_known for origin in Origin if origin is not Origin.UNKNOWN)
    assert not Origin.UNKNOWN.is_known


def test_determination_is_deterministic() -> None:
    extraction = _extraction("United States")
    assert {determine_origin(extraction) for _ in range(50)} == {Origin.US}


def test_supplier_country_defaults_to_none_and_is_last_field() -> None:
    """The new model field is optional so existing callers keep working."""
    assert InvoiceExtraction().supplier_country is None
    assert dataclasses.fields(InvoiceExtraction)[-1].name == "supplier_country"
