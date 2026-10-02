"""Task 11 (USR-003-03): supplier name normalisation to an 8-character identifier.

Scenarios are derived from USR-003-03 AC1-AC6 and the QA strategy's
"Supplier Name Normalisation" feature, plus negative/edge paths.
"""

import pytest

from intake.registry import normalize_supplier
from intake.registry.supplier_id import SUPPLIER_ID_LENGTH

# --- AC1: accented characters are stripped to their base Latin letters --------


def test_qa_happy_path_kovari_kft() -> None:
    # QA scenario: "Kővári Kft" -> KOVARIKF (legal-form suffix kept, then truncated)
    assert normalize_supplier("Kővári Kft") == "KOVARIKF"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ő", "O"),  # Hungarian double acute
        ("ű", "U"),  # Hungarian double acute
        ("Ő", "O"),
        ("Ű", "U"),
        ("áéíóöú", "AEIOOU"),
        ("ÁÉÍÓÖÚÜ", "AEIOOUU"),
    ],
)
def test_ac1_hungarian_accents_become_base_letters(raw: str, expected: str) -> None:
    assert normalize_supplier(raw) == expected


def test_ac1_full_hungarian_alphabet_accents() -> None:
    assert normalize_supplier("Árvíztűrő") == "ARVIZTUR"
    assert normalize_supplier("tükörfúrógép") == "TUKORFUR"


def test_ac1_output_is_plain_ascii() -> None:
    result = normalize_supplier("Őrségi Űrkutató Zrt.")
    assert result.isascii()
    assert result == "ORSEGIUR"


def test_ac1_precomposed_and_decomposed_input_give_same_result() -> None:
    precomposed = "Kővári"
    decomposed = "Kővári"
    assert normalize_supplier(precomposed) == normalize_supplier(decomposed) == "KOVARI"


# --- AC2: upper-cased --------------------------------------------------------


@pytest.mark.parametrize("raw", ["acme", "ACME", "AcMe", "aCmE"])
def test_ac2_any_case_mix_is_upper_cased(raw: str) -> None:
    assert normalize_supplier(raw) == "ACME"


def test_ac2_result_is_entirely_upper_case() -> None:
    result = normalize_supplier("lowercase supplier bt")
    assert result == result.upper()
    assert result == "LOWERCAS"


# --- AC3: leading "Magyar" dropped, non-leading preserved ---------------------


def test_qa_edge_leading_magyar_dropped() -> None:
    assert normalize_supplier("Magyar Telekom") == "TELEKOM"


def test_qa_edge_non_leading_magyar_preserved() -> None:
    assert normalize_supplier("Új Magyar Posta") == "UJMAGYAR"


@pytest.mark.parametrize("raw", ["magyar Telekom", "MAGYAR TELEKOM", "MaGyAr telekom"])
def test_ac3_leading_magyar_is_case_insensitive(raw: str) -> None:
    assert normalize_supplier(raw) == "TELEKOM"


def test_ac3_leading_magyar_with_legal_form_suffix() -> None:
    assert normalize_supplier("Magyar Telekom Nyrt.") == "TELEKOMN"


def test_ac3_leading_whitespace_before_magyar_is_tolerated() -> None:
    assert normalize_supplier("   Magyar Telekom") == "TELEKOM"


def test_ac3_magyar_followed_by_punctuation_is_still_a_leading_word() -> None:
    assert normalize_supplier("Magyar-Telekom") == "TELEKOM"


def test_ac3_magyar_as_prefix_of_longer_word_is_not_stripped() -> None:
    # "Magyar" must be a whole word: "Magyarország" is not "Magyar" + "ország".
    assert normalize_supplier("Magyarország Kft") == "MAGYAROR"


def test_ac3_only_first_leading_magyar_is_dropped() -> None:
    assert normalize_supplier("Magyar Magyar Kft") == "MAGYARKF"


def test_ac3_trailing_magyar_preserved() -> None:
    assert normalize_supplier("Posta Magyar") == "POSTAMAG"


# --- AC4: spaces and punctuation removed before forming the identifier --------


def test_ac4_spaces_and_punctuation_removed() -> None:
    assert normalize_supplier("ABC Kft.") == "ABCKFT"


@pytest.mark.parametrize(
    "raw",
    [
        "A.B.C. Kft.",
        "A-B-C Kft",
        "A&B&C, Kft",
        "(ABC) Kft!",
        "A/B\\C Kft",
        "A\tB\nC  Kft",
        "A'B\"C Kft",
    ],
)
def test_ac4_various_punctuation_and_whitespace_removed(raw: str) -> None:
    assert normalize_supplier(raw) == "ABCKFT"


def test_ac4_removal_happens_before_truncation() -> None:
    # 8 characters are counted only after spaces/punctuation are gone.
    assert normalize_supplier("A B C D E F G H I") == "ABCDEFGH"


def test_ac4_digits_are_kept() -> None:
    assert normalize_supplier("4iG Nyrt.") == "4IGNYRT"


def test_ac4_path_traversal_characters_are_removed() -> None:
    result = normalize_supplier("../../etc/passwd\x00")
    assert result == "ETCPASSW"
    assert result.isalnum()


# --- AC5: truncated to exactly 8 characters ----------------------------------


def test_ac5_long_name_truncated_to_exactly_8() -> None:
    result = normalize_supplier("Telekommunikacios Szolgaltato Zrt")
    assert result == "TELEKOMM"
    assert len(result) == SUPPLIER_ID_LENGTH == 8


def test_ac5_exactly_8_character_name_is_unchanged() -> None:
    assert normalize_supplier("acmecorp") == "ACMECORP"


def test_ac5_first_8_characters_of_cleaned_name() -> None:
    assert normalize_supplier("Magyar Államkincstár") == "ALLAMKIN"


# --- AC6: short names used in full, no padding --------------------------------


def test_qa_negative_short_name_not_padded() -> None:
    assert normalize_supplier("ACME") == "ACME"


@pytest.mark.parametrize(("raw", "expected"), [("X", "X"), ("OTP", "OTP"), ("Bt.", "BT")])
def test_ac6_short_names_have_no_filler(raw: str, expected: str) -> None:
    result = normalize_supplier(raw)
    assert result == expected
    assert len(result) < SUPPLIER_ID_LENGTH
    assert " " not in result
    assert "_" not in result


def test_ac6_magyar_with_short_remainder() -> None:
    assert normalize_supplier("Magyar Posta") == "POSTA"


# --- Determinism constraint --------------------------------------------------


def test_same_input_always_produces_same_identifier() -> None:
    results = {normalize_supplier("Kővári Kft") for _ in range(100)}
    assert results == {"KOVARIKF"}


def test_spelling_variants_differing_only_in_case_spacing_accents_converge() -> None:
    variants = ["Kővári Kft", "KOVARI KFT", "kovari kft.", "Kővári  Kft."]
    assert {normalize_supplier(v) for v in variants} == {"KOVARIKF"}


# --- Negative: names that normalise to nothing are rejected, never "" ----------


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "...", "-- / --", "Magyar", "magyar", "  MAGYAR  ", "Magyar ."],
)
def test_empty_after_normalisation_raises(raw: str) -> None:
    with pytest.raises(ValueError, match="supplier"):
        normalize_supplier(raw)


def test_non_latin_only_name_raises_rather_than_returning_empty() -> None:
    with pytest.raises(ValueError):
        normalize_supplier("Газпром")


def test_non_latin_characters_are_dropped_alongside_latin_ones() -> None:
    assert normalize_supplier("ACME Газпром") == "ACME"
