"""Task 2: typed Config built from a plain mapping, with required-key validation."""

from decimal import Decimal
from typing import Any

import pytest

from intake.config import REQUIRED_KEYS, Config, ConfigError, LabelNames


def _valid_mapping() -> dict[str, Any]:
    return {
        "invoice_keywords": ["számla", "invoice"],
        "attachment_mime_allowlist": ["application/pdf", "image/jpeg", "image/png"],
        "tig_subject_indicators": ["TIG", "teljesítésigazolás"],
        "currency_map": {"Ft": "HUF", "€": "EUR", "$": "USD"},
        "label_processed": "Kibit/Processed",
        "label_pending": "Kibit/Pending",
        "label_needs_review": "Kibit/NeedsReview",
        "label_awaiting_tig": "Kibit/AwaitingTIG",
        "sequence_start": 1,
        "sequence_width": 3,
        "rounding_tolerance": "0.01",
        "drive_root_folder_id": "folder-123",
    }


def test_from_mapping_builds_typed_config() -> None:
    config = Config.from_mapping(_valid_mapping())

    assert config.invoice_keywords == ("számla", "invoice")
    assert config.attachment_mime_allowlist == ("application/pdf", "image/jpeg", "image/png")
    assert config.tig_subject_indicators == ("TIG", "teljesítésigazolás")
    assert dict(config.currency_map) == {"Ft": "HUF", "€": "EUR", "$": "USD"}
    assert config.labels == LabelNames(
        processed="Kibit/Processed",
        pending="Kibit/Pending",
        needs_review="Kibit/NeedsReview",
        awaiting_tig="Kibit/AwaitingTIG",
    )
    assert config.sequence_start == 1
    assert config.sequence_width == 3
    assert config.rounding_tolerance == Decimal("0.01")
    assert isinstance(config.rounding_tolerance, Decimal)
    assert config.drive_root_folder_id == "folder-123"


def test_from_mapping_accepts_sheet_style_string_cells() -> None:
    mapping = _valid_mapping()
    mapping.update(
        invoice_keywords=" számla , invoice ,, ",
        attachment_mime_allowlist="application/pdf,image/png",
        tig_subject_indicators="TIG",
        currency_map="Ft=HUF, €=EUR , huf = huf",
        sequence_start="5",
        sequence_width=" 4 ",
        rounding_tolerance=0.01,
    )
    config = Config.from_mapping(mapping)

    assert config.invoice_keywords == ("számla", "invoice")
    assert config.attachment_mime_allowlist == ("application/pdf", "image/png")
    assert dict(config.currency_map) == {"Ft": "HUF", "€": "EUR", "huf": "HUF"}
    assert config.sequence_start == 5
    assert config.sequence_width == 4
    assert config.rounding_tolerance == Decimal("0.01")


def test_config_equality_and_immutability() -> None:
    first = Config.from_mapping(_valid_mapping())
    second = Config.from_mapping(_valid_mapping())
    assert first == second
    with pytest.raises(AttributeError):
        first.sequence_width = 9  # type: ignore[misc]
    with pytest.raises(TypeError):
        first.currency_map["X"] = "XXX"  # type: ignore[index]


def test_unknown_keys_are_ignored() -> None:
    mapping = _valid_mapping()
    mapping["notes"] = "edited by bookkeeper"
    assert Config.from_mapping(mapping) == Config.from_mapping(_valid_mapping())


def test_required_keys_cover_every_config_entry() -> None:
    assert set(REQUIRED_KEYS) == set(_valid_mapping())


@pytest.mark.parametrize("key", sorted(_valid_mapping()))
def test_missing_required_key_raises_clear_error(key: str) -> None:
    mapping = _valid_mapping()
    del mapping[key]
    with pytest.raises(ConfigError, match=key) as excinfo:
        Config.from_mapping(mapping)
    assert "missing" in str(excinfo.value).lower()
    assert excinfo.value.missing_keys == (key,)


def test_all_missing_keys_are_reported_together() -> None:
    with pytest.raises(ConfigError) as excinfo:
        Config.from_mapping({})
    assert set(excinfo.value.missing_keys) == set(REQUIRED_KEYS)
    for key in REQUIRED_KEYS:
        assert key in str(excinfo.value)


@pytest.mark.parametrize(
    ("key", "blank"),
    [
        ("invoice_keywords", ""),
        ("invoice_keywords", " , ,"),
        ("attachment_mime_allowlist", []),
        ("currency_map", {}),
        ("label_processed", "   "),
        ("drive_root_folder_id", None),
        ("sequence_width", ""),
    ],
)
def test_blank_required_value_counts_as_missing(key: str, blank: object) -> None:
    mapping = _valid_mapping()
    mapping[key] = blank
    with pytest.raises(ConfigError, match=key) as excinfo:
        Config.from_mapping(mapping)
    assert excinfo.value.missing_keys == (key,)


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("sequence_width", "three", "integer"),
        ("sequence_width", True, "integer"),
        ("sequence_width", 0, ">= 1"),
        ("sequence_start", -1, ">= 0"),
        ("sequence_start", 1000, "does not fit"),
        ("rounding_tolerance", "abc", "decimal"),
        ("rounding_tolerance", "-0.01", ">= 0"),
        ("rounding_tolerance", "NaN", "decimal"),
        ("currency_map", "Ft", "symbol=ISO"),
        ("currency_map", {"Ft": "FORINT"}, "ISO 4217"),
        ("invoice_keywords", 42, "list"),
        ("invoice_keywords", ["ok", 3], "list"),
        ("label_pending", 7, "string"),
        ("currency_map", 12, "mapping"),
    ],
)
def test_invalid_values_raise_config_error_naming_the_key(
    key: str, value: object, message: str
) -> None:
    mapping = _valid_mapping()
    mapping[key] = value
    with pytest.raises(ConfigError, match=key) as excinfo:
        Config.from_mapping(mapping)
    assert message in str(excinfo.value)
    assert excinfo.value.missing_keys == ()


def test_config_error_is_a_value_error() -> None:
    assert issubclass(ConfigError, ValueError)


# --- Duplicate label (optional key) ------------------------------------------------------


def test_duplicate_label_defaults_when_the_key_is_absent() -> None:
    # Optional so existing Config tabs keep working; add the key to rename the label.
    assert Config.from_mapping(_valid_mapping()).labels.duplicate == "Kibit/Duplicate"


def test_duplicate_label_comes_from_config_when_set() -> None:
    raw = {**_valid_mapping(), "label_duplicate": "Acme/Dup"}
    assert Config.from_mapping(raw).labels.duplicate == "Acme/Dup"


def test_blank_duplicate_label_falls_back_to_the_default() -> None:
    raw = {**_valid_mapping(), "label_duplicate": "  "}
    assert Config.from_mapping(raw).labels.duplicate == "Kibit/Duplicate"


def test_duplicate_label_is_not_a_required_key() -> None:
    assert "label_duplicate" not in REQUIRED_KEYS


def test_contractor_identifiers_is_no_longer_a_setting() -> None:
    # Routing is decided by the thread (a reply to a TIG), never by a sender list. An old
    # Config tab that still has the row keeps working: unknown keys are ignored.
    assert "contractor_identifiers" not in REQUIRED_KEYS
    raw = {k: v for k, v in _valid_mapping().items() if k != "contractor_identifiers"}
    config = Config.from_mapping(raw)
    assert not hasattr(config, "contractor_identifiers")
    Config.from_mapping({**raw, "contractor_identifiers": "x.example"})
