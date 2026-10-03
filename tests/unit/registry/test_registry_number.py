"""Task 13 (USR-003-01): registry number assembly, ``[YYMM]_[seq]_[SUPPLIER8]``.

Scenarios are derived from USR-003-01 AC1-AC5, the execution plan's Task 13 row and the
QA strategy's "Registry Number Assembly" feature, plus negative/edge paths for every
component. The supplier component follows the merged USR-003-03 behaviour: 1-8
characters of ``[A-Z0-9]``, shorter names kept in full and unpadded (AC6 there), so the
total length is ``4 + width + len(supplier)``.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pytest

from intake.models import FlagReason, StageStatus
from intake.registry.folder_naming import yymm_folder_name
from intake.registry.registry_number import (
    RegistryNumberError,
    assemble,
    assemble_for_date,
)
from intake.registry.sequence import next_sequence, registry_filename_pattern
from intake.registry.supplier_id import SUPPLIER_ID_LENGTH, normalize_supplier

# --- AC1: order and padding -----------------------------------------------------------


def test_qa_happy_path_assembles_exact_registry_number() -> None:
    # YYMM 2405, sequence 7 (3-digit width), supplier ACMECORP -> 2405_007_ACMECORP
    # (underscores: project owner decision, 2026-10-03)
    assert assemble("2405", 7, "ACMECORP", 3) == "2405_007_ACMECORP"


@pytest.mark.parametrize(
    ("yymm", "seq", "supplier", "width", "expected"),
    [
        ("2405", 1, "KOVARIKF", 3, "2405_001_KOVARIKF"),
        ("2412", 42, "TELEKOM", 3, "2412_042_TELEKOM"),
        ("0001", 999, "A", 3, "0001_999_A"),
        ("2405", 0, "ACMECORP", 3, "2405_000_ACMECORP"),  # start value 0 is a valid seq
        ("2405", 7, "ACMECORP", 1, "2405_7_ACMECORP"),
        ("2405", 7, "ACMECORP", 5, "2405_00007_ACMECORP"),
        ("2405", 12345, "ACMECORP", 5, "2405_12345_ACMECORP"),  # exactly fills the width
    ],
)
def test_components_concatenated_in_order_with_fixed_width_padding(
    yymm: str, seq: int, supplier: str, width: int, expected: str
) -> None:
    assert assemble(yymm, seq, supplier, width) == expected


def test_supplier_starting_with_digits_stays_after_the_padded_sequence() -> None:
    assert assemble("2405", 3, "3M", 3) == "2405_003_3M"
    assert assemble("2405", 3, "7ELEVEN", 3) == "2405_003_7ELEVEN"


def test_supplier_from_normalize_supplier_is_used_verbatim() -> None:
    assert assemble("2405", 7, normalize_supplier("Kővári Kft"), 3) == "2405_007_KOVARIKF"
    assert assemble("2405", 8, normalize_supplier("Magyar Telekom Nyrt."), 3) == (
        "2405_008_TELEKOMN"
    )


# --- AC3: fixed total length ----------------------------------------------------------


@pytest.mark.parametrize("width", [1, 2, 3, 4, 6])
@pytest.mark.parametrize("supplier", ["A", "ACME", "TELEKOM", "ACMECORP"])
def test_length_is_four_plus_width_plus_supplier_plus_two_separators(
    width: int, supplier: str
) -> None:
    number = assemble("2405", 1, supplier, width)
    assert len(number) == 4 + 1 + width + 1 + len(supplier)


@pytest.mark.parametrize("width", [1, 3, 5])
def test_full_length_supplier_gives_four_plus_width_plus_eight(width: int) -> None:
    assert len(assemble("2405", 1, "ACMECORP", width)) == 4 + 1 + width + 1 + SUPPLIER_ID_LENGTH


def test_short_supplier_is_not_padded_to_eight() -> None:
    # USR-003-03 AC6: a short supplier ID is kept in full; assembly must not pad it.
    assert assemble("2405", 7, "ACME", 3) == "2405_007_ACME"


# --- Round trip with the sequence derivation pattern (Task 12) ------------------------


@pytest.mark.parametrize(
    ("yymm", "seq", "supplier", "width"),
    [
        ("2405", 7, "ACMECORP", 3),
        ("2405", 1, "A", 1),
        ("2412", 999, "3M", 3),
        ("0001", 0, "7ELEVEN", 4),
        ("9912", 12345, "KOVARIKF", 5),
    ],
)
def test_assembled_number_parses_back_through_registry_filename_pattern(
    yymm: str, seq: int, supplier: str, width: int
) -> None:
    number = assemble(yymm, seq, supplier, width)
    pattern = registry_filename_pattern(yymm, width)

    bare = pattern.fullmatch(number)
    assert bare is not None
    assert int(bare.group("seq")) == seq
    assert bare.group("supplier") == supplier
    assert bare.group("ext") is None

    filed = pattern.fullmatch(f"{number}.pdf")
    assert filed is not None
    assert (int(filed.group("seq")), filed.group("supplier"), filed.group("ext")) == (
        seq,
        supplier,
        "pdf",
    )


def test_filed_number_is_counted_by_next_sequence() -> None:
    number = assemble("2405", 7, "ACMECORP", 3)
    assert next_sequence([f"{number}.pdf"], yymm="2405", width=3, start=1) == 8


# --- AC2: any missing/invalid component -> no number at all ---------------------------


def _refused(**overrides: Any) -> RegistryNumberError:
    args: dict[str, Any] = {"yymm": "2405", "seq": 7, "supplier8": "ACMECORP", "width": 3}
    args.update(overrides)
    with pytest.raises(RegistryNumberError) as excinfo:
        assemble(**args)
    return excinfo.value


@pytest.mark.parametrize("component", ["yymm", "seq", "supplier8"])
def test_missing_component_produces_no_registry_number(component: str) -> None:
    error = _refused(**{component: None})
    assert error.component == component


@pytest.mark.parametrize("yymm", ["", "245", "24051", "2413", "2400", "24-5", "２４０５", 2405])
def test_invalid_yymm_is_refused(yymm: object) -> None:
    assert _refused(yymm=yymm).component == "yymm"


@pytest.mark.parametrize("seq", [-1, 1000, 1.0, "7", True, False])
def test_invalid_or_overflowing_sequence_is_refused(seq: object) -> None:
    # 1000 needs 4 digits at width 3: refuse rather than produce an over-long number.
    assert _refused(seq=seq).component == "seq"


@pytest.mark.parametrize(
    "supplier",
    ["", "ACMECORPX", "acmecorp", "ACME CORP", "KŐVÁRI", "ACME.", "../ETC", "AC\x00ME", 42],
)
def test_empty_overlong_or_unnormalised_supplier_is_refused(supplier: object) -> None:
    assert _refused(supplier8=supplier).component == "supplier8"


@pytest.mark.parametrize("width", [0, -3, None, 3.0, True, "3"])
def test_invalid_width_is_refused(width: object) -> None:
    assert _refused(width=width).component == "width"


def test_refusal_is_a_value_error_that_flags_incomplete_data() -> None:
    error = _refused(seq=None)
    assert isinstance(error, ValueError)
    assert error.reason is FlagReason.INCOMPLETE_DATA
    result = error.to_stage_result()
    assert result.status is StageStatus.INCOMPLETE
    assert result.flag_reason is FlagReason.INCOMPLETE_DATA
    assert result.detail is not None and "seq" in result.detail


# --- AC4 (unit-testable part): pure and deterministic ---------------------------------


def test_assembly_is_deterministic_and_side_effect_free() -> None:
    results = {assemble("2405", 7, "ACMECORP", 3) for _ in range(100)}
    assert results == {"2405_007_ACMECORP"}


# --- Convenience: YYMM from the performance date (Task 8) via yymm_folder_name ----------


def test_assemble_for_date_uses_the_shared_yymm_folder_name() -> None:
    performance = date(2024, 5, 31)
    number = assemble_for_date(performance, 7, "ACMECORP", 3)
    assert number == "2405_007_ACMECORP"
    assert number.startswith(yymm_folder_name(performance))
    assert number == assemble(yymm_folder_name(performance), 7, "ACMECORP", 3)


def test_assemble_for_date_ignores_time_of_datetime() -> None:
    assert assemble_for_date(datetime(2024, 12, 31, 23, 59), 1, "A", 3) == "2412_001_A"


@pytest.mark.parametrize("performance", [None, "2024-05-31", "2405", 20240531])
def test_assemble_for_date_refuses_missing_or_non_date(performance: object) -> None:
    with pytest.raises(RegistryNumberError) as excinfo:
        assemble_for_date(performance, 7, "ACMECORP", 3)  # type: ignore[arg-type]
    assert excinfo.value.component == "performance_date"


def test_assemble_for_date_still_validates_other_components() -> None:
    with pytest.raises(RegistryNumberError) as excinfo:
        assemble_for_date(date(2024, 5, 1), None, "ACMECORP", 3)
    assert excinfo.value.component == "seq"


# --- Underscore separators; legacy numbers still parse ---------------------------------


def test_legacy_numbers_without_separators_still_parse() -> None:
    # Files filed before the separator change must keep their sequence numbers taken.
    pattern = registry_filename_pattern("2611", 3)
    for name in ("2611015JKFT.pdf", "2611_015_JKFT.pdf"):
        match = pattern.fullmatch(name)
        assert match is not None
        assert (int(match.group("seq")), match.group("supplier")) == (15, "JKFT")


@pytest.mark.parametrize("name", ["2611_015JKFT.pdf", "2611015_JKFT.pdf", "2611__015_JKFT"])
def test_mixed_or_doubled_separators_do_not_parse(name: str) -> None:
    assert registry_filename_pattern("2611", 3).fullmatch(name) is None


def test_next_sequence_counts_old_and_new_numbers_together() -> None:
    names = ["2611014KKFT.pdf", "2611_015_JKFT.pdf"]
    assert next_sequence(names, yymm="2611", width=3, start=1) == 16
