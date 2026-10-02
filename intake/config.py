"""Typed runtime configuration, built from the ledger sheet's ``Config`` tab.

Nothing here talks to Sheets: ``Config.from_mapping`` takes a plain key/value mapping
(the Sheets loader that produces it lives in ``intake/clients/sheets_client.py``).
Values may be native Python types or the plain strings a spreadsheet cell holds:

- lists: a list/tuple of strings, or one comma-separated string;
- ``currency_map``: a mapping of symbol -> ISO code, or ``"Ft=HUF, €=EUR"``;
- integers / decimals: native numbers or their string form.

Every key in ``REQUIRED_KEYS`` must be present and non-blank, otherwise a single
``ConfigError`` names all missing keys. Unknown keys are ignored.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any

_ISO_4217 = re.compile(r"^[A-Z]{3}$")

LIST_KEYS = (
    "invoice_keywords",
    "attachment_mime_allowlist",
    "contractor_identifiers",
    "tig_subject_indicators",
)
LABEL_KEYS = (
    "label_processed",
    "label_pending",
    "label_needs_review",
    "label_awaiting_tig",
)
REQUIRED_KEYS: tuple[str, ...] = (
    *LIST_KEYS,
    "currency_map",
    *LABEL_KEYS,
    "sequence_start",
    "sequence_width",
    "rounding_tolerance",
    "drive_root_folder_id",
)


class ConfigError(ValueError):
    """Raised when the configuration is incomplete or malformed."""

    def __init__(self, message: str, missing_keys: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.missing_keys = missing_keys


@dataclass(frozen=True, slots=True)
class LabelNames:
    """Gmail label names for the four outcome states (ADR 3)."""

    processed: str
    pending: str
    needs_review: str
    awaiting_tig: str


@dataclass(frozen=True, slots=True)
class Config:
    invoice_keywords: tuple[str, ...]
    attachment_mime_allowlist: tuple[str, ...]
    contractor_identifiers: tuple[str, ...]
    tig_subject_indicators: tuple[str, ...]
    currency_map: Mapping[str, str]
    labels: LabelNames
    sequence_start: int
    sequence_width: int
    rounding_tolerance: Decimal
    drive_root_folder_id: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> Config:
        missing = tuple(key for key in REQUIRED_KEYS if _is_blank(raw.get(key)))
        if missing:
            raise ConfigError(
                f"Config is missing required key(s): {', '.join(missing)}",
                missing_keys=missing,
            )

        lists = {key: _parse_list(key, raw[key]) for key in LIST_KEYS}
        labels = {key: _parse_str(key, raw[key]) for key in LABEL_KEYS}
        sequence_start = _parse_int("sequence_start", raw["sequence_start"])
        sequence_width = _parse_int("sequence_width", raw["sequence_width"])
        rounding_tolerance = _parse_decimal("rounding_tolerance", raw["rounding_tolerance"])

        if sequence_width < 1:
            raise ConfigError(f"sequence_width must be >= 1, got {sequence_width}")
        if sequence_start < 0:
            raise ConfigError(f"sequence_start must be >= 0, got {sequence_start}")
        if len(str(sequence_start)) > sequence_width:
            raise ConfigError(
                f"sequence_start {sequence_start} does not fit sequence_width {sequence_width}"
            )
        if rounding_tolerance < 0:
            raise ConfigError(f"rounding_tolerance must be >= 0, got {rounding_tolerance}")

        return cls(
            invoice_keywords=lists["invoice_keywords"],
            attachment_mime_allowlist=lists["attachment_mime_allowlist"],
            contractor_identifiers=lists["contractor_identifiers"],
            tig_subject_indicators=lists["tig_subject_indicators"],
            currency_map=_parse_currency_map("currency_map", raw["currency_map"]),
            labels=LabelNames(
                processed=labels["label_processed"],
                pending=labels["label_pending"],
                needs_review=labels["label_needs_review"],
                awaiting_tig=labels["label_awaiting_tig"],
            ),
            sequence_start=sequence_start,
            sequence_width=sequence_width,
            rounding_tolerance=rounding_tolerance,
            drive_root_folder_id=_parse_str("drive_root_folder_id", raw["drive_root_folder_id"]),
        )


def _is_blank(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not [part for part in value.split(",") if part.strip()]
    if isinstance(value, list | tuple | Mapping):
        return len(value) == 0
    return False


def _parse_list(key: str, value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        items: list[object] = list(value.split(","))
    elif isinstance(value, list | tuple):
        items = list(value)
    else:
        raise ConfigError(f"{key} must be a list of strings or a comma-separated string")
    if not all(isinstance(item, str) for item in items):
        raise ConfigError(f"{key} must be a list of strings")
    return tuple(stripped for item in items if (stripped := str(item).strip()))


def _parse_str(key: str, value: object) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{key} must be a string, got {type(value).__name__}")
    return value.strip()


def _parse_int(key: str, value: object) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{key} must be an integer, got a boolean")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            pass
    raise ConfigError(f"{key} must be an integer, got {value!r}")


def _parse_decimal(key: str, value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, int | float | str | Decimal):
        raise ConfigError(f"{key} must be a decimal number, got {value!r}")
    try:
        # str() first so a float like 0.01 becomes Decimal("0.01"), not its binary expansion.
        result = Decimal(str(value).strip())
    except InvalidOperation:
        raise ConfigError(f"{key} must be a decimal number, got {value!r}") from None
    if not result.is_finite():
        raise ConfigError(f"{key} must be a finite decimal number, got {value!r}")
    return result


def _parse_currency_map(key: str, value: object) -> Mapping[str, str]:
    pairs: list[tuple[object, object]]
    if isinstance(value, Mapping):
        pairs = list(value.items())
    elif isinstance(value, str):
        pairs = []
        for entry in value.split(","):
            if not entry.strip():
                continue
            raw_symbol, sep, raw_iso = entry.partition("=")
            if not sep:
                raise ConfigError(f"{key} entries must look like symbol=ISO, got {entry.strip()!r}")
            pairs.append((raw_symbol, raw_iso))
    else:
        raise ConfigError(f"{key} must be a mapping or a 'symbol=ISO, ...' string")

    result: dict[str, str] = {}
    for symbol, iso in pairs:
        if not isinstance(symbol, str) or not isinstance(iso, str) or not symbol.strip():
            raise ConfigError(f"{key} must map non-empty symbol strings to ISO code strings")
        code = iso.strip().upper()
        if not _ISO_4217.match(code):
            raise ConfigError(
                f"{key} value for {symbol.strip()!r} is not an ISO 4217 code: {iso!r}"
            )
        result[symbol.strip()] = code
    return MappingProxyType(result)


__all__ = ["REQUIRED_KEYS", "Config", "ConfigError", "LabelNames"]
