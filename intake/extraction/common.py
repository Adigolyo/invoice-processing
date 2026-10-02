"""Provider-neutral parts of document extraction (USR-002-01, USR-005-01, ADR 4).

Shared by every ``Extractor`` backend (Gemini, Claude via AI Compass) so they ask the
same question and are held to the same answer:

- the ``Extractor`` protocol, ``DocumentKind`` and ``UnsupportedDocumentError``;
- the JSON response schema (every value a raw string as printed, or ``null``);
- the invoice and TIG prompts;
- strict response validation (anything non-conforming is rejected, never repaired);
- conversion into ``InvoiceExtraction`` (line amounts through ``normalize_amount`` with
  the resolved currency, quantities through ``parse_quantity``);
- the required-field rules and the result contract (``interpret_response``);
- MIME normalisation and the per-environment secret-ID convention.

A backend only builds its request, calls its API and hands the response text to
``interpret_response``. API errors are not caught anywhere here.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import re
from collections.abc import Mapping
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final, Protocol, runtime_checkable

from intake.clients.auth import (
    ENVIRONMENT_ENV,
    PROJECT_ID_ENV,
    AuthConfigurationError,
    SecretAccessor,
    read_secret,
)
from intake.extraction.quantities import parse_quantity
from intake.models import FlagReason, InvoiceExtraction, LineItem, StageResult, StageStatus
from intake.normalization.amounts import normalize_amount
from intake.normalization.country_of_origin import determine_origin
from intake.normalization.currency import to_iso_code

SUPPORTED_MIME_TYPES: Final = frozenset(
    {
        "application/pdf",
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/heic",
        "image/heif",
    }
)
_MIME_ALIASES: Final = {"image/jpg": "image/jpeg", "image/pjpeg": "image/jpeg"}

_ENVIRONMENT_PATTERN: Final = re.compile(r"^[a-z][a-z0-9-]*$")

HEADER_FIELDS: Final = (
    "supplier",
    "invoice_number",
    "supplier_country",
    "currency",
    "net",
    "gross",
    "issue_date",
    "due_date",
    "supply_date",
    "performance_date",
)
LINE_ITEM_FIELDS: Final = ("description", "quantity", "unit_price", "net")

# USR-002-01 AC1 core fields. The performance date is not required here: foreign
# invoices often print none and USR-002-04 picks the earliest of issue/due/supply; a
# domestic invoice without one is flagged by ``select_performance_date``.
INVOICE_REQUIRED_FIELDS: Final = (
    "supplier",
    "invoice_number",
    "currency",
    "net",
    "gross",
    "due_date",
)


class DocumentKind(StrEnum):
    """Which kind of document is being extracted."""

    INVOICE = "invoice"
    TIG = "tig"  # teljesítésigazolás: a contractor's performance certificate


class UnsupportedDocumentError(ValueError):
    """The document is empty or its MIME type cannot be sent for extraction."""


@runtime_checkable
class Extractor(Protocol):
    """Turns one document into an ``InvoiceExtraction`` (ADR 4: swappable model)."""

    def extract(
        self, content: bytes, mime_type: str, *, kind: DocumentKind = DocumentKind.INVOICE
    ) -> StageResult[InvoiceExtraction]: ...


# --- response schema --------------------------------------------------------------------

_HEADER_DESCRIPTIONS: Final = {
    "supplier": "Name of the issuer/seller (the party that is paid), exactly as printed.",
    "invoice_number": "The document's own number, exactly as printed.",
    "supplier_country": (
        "ISO 3166-1 alpha-2 code of the supplier's country, from the supplier's address "
        "or tax ID (e.g. HU for a Hungarian address or an 8-1-2 digit adószám)."
    ),
    "currency": "Currency symbol, abbreviation or code exactly as printed (e.g. Ft, EUR, $).",
    "net": "Total net amount (without VAT) exactly as printed, including separators.",
    "gross": "Total gross amount payable exactly as printed, including separators.",
    "issue_date": "Issue date exactly as printed.",
    "due_date": "Payment due date exactly as printed.",
    "supply_date": "Supply / delivery / service date exactly as printed.",
    "performance_date": "Performance date exactly as printed.",
}
_LINE_DESCRIPTIONS: Final = {
    "description": "Item name/description, wrapped lines joined with single spaces.",
    "quantity": "Quantity exactly as printed, including its unit (e.g. '1 200 db').",
    "unit_price": "Net unit price exactly as printed.",
    "net": "Line net amount exactly as printed.",
}


def _nullable_string(description: str) -> dict[str, Any]:
    return {"type": ["string", "null"], "description": description}


RESPONSE_JSON_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        **{name: _nullable_string(_HEADER_DESCRIPTIONS[name]) for name in HEADER_FIELDS},
        "line_items": {
            "type": "array",
            "description": "Every itemised line from all pages, in document order.",
            "items": {
                "type": "object",
                "properties": {
                    name: _nullable_string(_LINE_DESCRIPTIONS[name]) for name in LINE_ITEM_FIELDS
                },
                "required": list(LINE_ITEM_FIELDS),
                "additionalProperties": False,
            },
        },
    },
    "required": [*HEADER_FIELDS, "line_items"],
    "additionalProperties": False,
}

# --- prompts ----------------------------------------------------------------------------

_COMMON_RULES: Final = """\
Rules:
- Read ALL pages of the document. Line items and totals may continue on later pages.
- Copy every value exactly as printed: keep the original digits, thousands and decimal
  separators, currency symbols and date punctuation (e.g. "150 500 Ft", "1.234,56 EUR",
  "2026.11.05.", "03/14/2026"). Do not convert, reformat, translate or compute anything.
- If a value is absent, illegible, obscured or you are not certain of it, return null.
  Never guess, never fabricate, never derive a value from other fields.
- supplier_country is the only exception to copying: give the ISO 3166-1 alpha-2 code of
  the supplier's country, determined from the supplier's address or tax ID; null if
  neither shows it.
- line_items: one entry per itemised line, from every page, in document order. Skip
  subtotal, total, VAT-summary and payment rows. quantity keeps its unit as printed.
- Answer with the JSON object only."""

_INVOICE_PROMPT: Final = f"""\
You extract data from a supplier invoice (Hungarian or foreign, any language).

Field meanings (Hungarian labels in parentheses):
- supplier: the issuer/seller ("Szállító", "Eladó"), never the buyer ("Vevő").
- invoice_number: "Számlaszám" / "Invoice No.".
- issue_date: "Kiállítás kelte" / "Invoice date".
- performance_date: "Teljesítés kelte" / "Teljesítés dátuma" (the date of performance);
  null if the document states none.
- due_date: "Fizetési határidő" / "Due date" / "Payment due".
- supply_date: "Date of supply" / "Delivery date" / "Service date" on foreign invoices;
  null if not printed separately.
- net: the total net amount ("Nettó összesen", "Net total", "Subtotal").
- gross: the total amount payable ("Bruttó összesen", "Fizetendő összesen", "Total due").
  When the invoice states it contains no VAT and that gross equals net (e.g. "Alanyi
  adómentes (AAM)", "VAT exempt") and prints only one total, return that printed total
  for both net and gross.
- currency: the currency as printed next to the amounts ("Ft", "HUF", "EUR", "€", "$").
- line_items: "Megnevezés" (description), "Mennyiség" (quantity), "(Nettó) egységár"
  (unit_price), "Ellenérték" / "Nettó ár" / "Net amount" (net).

{_COMMON_RULES}"""

_TIG_PROMPT: Final = f"""\
You extract data from a performance certificate ("teljesítésigazolás", TIG): a document
in which the client ("Megbízó") certifies the services a contractor ("Megbízott")
performed. It is not an invoice.

Field meanings:
- supplier: the contractor who performed the work ("Megbízott"), never the client
  ("Megbízó").
- invoice_number: the certificate's own number ("Sorszám").
- issue_date: the date the certificate was signed ("Kelt").
- performance_date: only if a single performance date is printed; otherwise null.
- net: the certificate's total net amount if printed; otherwise null.
- gross, due_date, supply_date: usually not printed on a certificate; return null unless
  printed.
- currency: the currency as printed next to the amounts ("Ft", "HUF", "EUR").
- line_items: "Megnevezés" (description), "Mennyiség" (quantity), "Egységár (nettó)"
  (unit_price), "Nettó ár" (net).

{_COMMON_RULES}"""

SYSTEM_PROMPTS: Final = {DocumentKind.INVOICE: _INVOICE_PROMPT, DocumentKind.TIG: _TIG_PROMPT}
USER_TEXT: Final = {
    DocumentKind.INVOICE: "Extract the invoice fields and line items from all pages of "
    "the attached document.",
    DocumentKind.TIG: "Extract the certificate fields and line items from all pages of "
    "the attached document.",
}


# --- configuration ----------------------------------------------------------------------


def secret_id(prefix: str, environment: str) -> str:
    """``<prefix>-<environment>``, e.g. ``kibit-gemini-api-key-staging``."""
    if not _ENVIRONMENT_PATTERN.fullmatch(environment):
        raise AuthConfigurationError(f"invalid environment name: {environment!r}")
    return f"{prefix}-{environment}"


def resolve_key(
    env: Mapping[str, str],
    *,
    local_env: str,
    secret_prefix: str,
    label: str,
    accessor: SecretAccessor | None = None,
) -> str:
    """An API key from ``env[local_env]`` (local dev) or Secret Manager.

    Without ``local_env``, the key is read from ``<secret_prefix>-<ENVIRONMENT>`` in
    project ``GCP_PROJECT_ID``. Errors never include the key.
    """
    local = env.get(local_env, "").strip()
    if local:
        return local
    missing = [key for key in (PROJECT_ID_ENV, ENVIRONMENT_ENV) if not env.get(key)]
    if missing:
        raise AuthConfigurationError(
            f"set {local_env} or {' and '.join(missing)} to locate the {label}"
        )
    return read_secret(
        env[PROJECT_ID_ENV], secret_id(secret_prefix, env[ENVIRONMENT_ENV]), accessor
    )


def normalise_mime(mime_type: str, supported: frozenset[str] = SUPPORTED_MIME_TYPES) -> str:
    """Base MIME type, lower-cased and de-aliased; ``UnsupportedDocumentError`` if unsent."""
    base = mime_type.split(";", 1)[0].strip().lower()
    base = _MIME_ALIASES.get(base, base)
    if base not in supported:
        raise UnsupportedDocumentError(f"unsupported document MIME type {base!r}")
    return base


def check_document(
    content: bytes, mime_type: str, supported: frozenset[str] = SUPPORTED_MIME_TYPES
) -> str:
    """Validate a document before any API call; return its normalised MIME type."""
    mime = normalise_mime(mime_type, supported)
    if not content:
        raise UnsupportedDocumentError("document is empty")
    return mime


# --- response validation ----------------------------------------------------------------


class InvalidResponse(Exception):
    """The model's answer does not conform to ``RESPONSE_JSON_SCHEMA``.

    Messages name keys and JSON types only, never values (they end up in logs).
    """


def _check_object(
    value: object, keys: tuple[str, ...], where: str, *, non_string: tuple[str, ...] = ()
) -> dict[str, Any]:
    """Exactly ``keys``; each a string or null unless listed in ``non_string``."""
    if not isinstance(value, dict):
        raise InvalidResponse(f"{where} is a {type(value).__name__}, not an object")
    missing = [key for key in keys if key not in value]
    extra = sorted(str(key) for key in value if key not in keys)
    if missing:
        raise InvalidResponse(f"{where} lacks key(s): {', '.join(missing)}")
    if extra:
        raise InvalidResponse(f"{where} has unexpected key(s): {', '.join(extra)}")
    for key in keys:
        if key not in non_string and value[key] is not None and not isinstance(value[key], str):
            raise InvalidResponse(
                f"{where}.{key} is a {type(value[key]).__name__}, not a string or null"
            )
    return value


def validate_response(text: str | None) -> dict[str, Any]:
    """Parse and strictly validate the model's JSON; raise ``InvalidResponse`` otherwise."""
    if text is None or not text.strip():
        raise InvalidResponse("response has no text")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InvalidResponse(f"response is not valid JSON ({exc.msg})") from None
    data = _check_object(
        parsed, (*HEADER_FIELDS, "line_items"), "response", non_string=("line_items",)
    )
    lines = data["line_items"]
    if not isinstance(lines, list):
        raise InvalidResponse(f"response.line_items is a {type(lines).__name__}, not an array")
    for index, line in enumerate(lines):
        _check_object(line, LINE_ITEM_FIELDS, f"response.line_items[{index}]")
    return data


# --- conversion -------------------------------------------------------------------------


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _clean_country(value: str | None) -> str | None:
    cleaned = _clean(value)
    if cleaned is not None and len(cleaned) == 2 and cleaned.isalpha():
        return cleaned.upper()
    return cleaned


def _amount(raw: str | None, currency_iso: str | None) -> Decimal | None:
    value = normalize_amount(_clean(raw), currency_iso=currency_iso)
    return value if isinstance(value, Decimal) else None


def _currency_iso(header: InvoiceExtraction, currency_map: Mapping[str, str]) -> str | None:
    if header.currency is None:
        return None
    iso = to_iso_code(header.currency, determine_origin(header), currency_map)
    return iso if isinstance(iso, str) else None


def build_extraction(data: Mapping[str, Any], currency_map: Mapping[str, str]) -> InvoiceExtraction:
    """Validated response data -> ``InvoiceExtraction`` (header raw, lines as Decimal)."""
    header = InvoiceExtraction(
        supplier=_clean(data["supplier"]),
        invoice_number=_clean(data["invoice_number"]),
        currency=_clean(data["currency"]),
        net=_clean(data["net"]),
        gross=_clean(data["gross"]),
        due_date=_clean(data["due_date"]),
        issue_date=_clean(data["issue_date"]),
        supply_date=_clean(data["supply_date"]),
        performance_date=_clean(data["performance_date"]),
        supplier_country=_clean_country(data["supplier_country"]),
    )
    currency_iso = _currency_iso(header, currency_map)
    lines = tuple(
        LineItem(
            description=_clean(line["description"]),
            quantity=parse_quantity(_clean(line["quantity"])),
            unit_price=_amount(line["unit_price"], currency_iso),
            net=_amount(line["net"], currency_iso),
        )
        for line in data["line_items"]
    )
    return dataclasses.replace(header, line_items=lines)


def missing_required(extraction: InvoiceExtraction, kind: DocumentKind) -> list[str]:
    """Required fields that are ``None`` (a TIG needs at least one line item)."""
    if kind is DocumentKind.TIG:
        return [] if extraction.line_items else ["line_items"]
    return [name for name in INVOICE_REQUIRED_FIELDS if getattr(extraction, name) is None]


# --- result contract --------------------------------------------------------------------


def response_sha256(text: str | None) -> str:
    """Fingerprint of the response text for logs (the text itself is never logged)."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def rejected(
    detail: str, *, logger: logging.Logger, log_fields: Mapping[str, object]
) -> StageResult[InvoiceExtraction]:
    """``incomplete`` with no value: nothing partial goes downstream."""
    logger.warning(
        "extraction response rejected",
        extra={**log_fields, "outcome": "invalid_response", "detail": detail},
    )
    return StageResult.incomplete(FlagReason.INCOMPLETE_DATA, detail)


def interpret_response(
    text: str | None,
    *,
    kind: DocumentKind,
    currency_map: Mapping[str, str],
    provider: str,
    logger: logging.Logger,
    log_fields: Mapping[str, object],
) -> StageResult[InvoiceExtraction]:
    """Validate, convert and check the model's answer (the ``Extractor`` result contract).

    - ``ok``: a conforming response with every required field present.
    - ``incomplete`` with ``value`` set: conforming, but a required field is ``None``;
      ``detail`` names the missing fields.
    - ``incomplete`` with ``value=None``: the response failed validation.
    """
    try:
        data = validate_response(text)
    except InvalidResponse as exc:
        return rejected(
            f"{provider} response rejected: {exc}", logger=logger, log_fields=log_fields
        )

    extraction = build_extraction(data, currency_map)
    missing = missing_required(extraction, kind)
    if missing:
        detail = f"missing required field(s): {', '.join(missing)}"
        logger.info(
            "extraction incomplete",
            extra={**log_fields, "outcome": "incomplete", "detail": detail},
        )
        return StageResult[InvoiceExtraction](
            StageStatus.INCOMPLETE,
            value=extraction,
            flag_reason=FlagReason.INCOMPLETE_DATA,
            detail=detail,
        )
    logger.info(
        "extraction complete",
        extra={**log_fields, "outcome": "ok", "line_item_count": len(extraction.line_items)},
    )
    return StageResult.ok(extraction)


__all__ = [
    "HEADER_FIELDS",
    "INVOICE_REQUIRED_FIELDS",
    "LINE_ITEM_FIELDS",
    "RESPONSE_JSON_SCHEMA",
    "SUPPORTED_MIME_TYPES",
    "SYSTEM_PROMPTS",
    "USER_TEXT",
    "DocumentKind",
    "Extractor",
    "InvalidResponse",
    "UnsupportedDocumentError",
    "build_extraction",
    "check_document",
    "interpret_response",
    "missing_required",
    "normalise_mime",
    "rejected",
    "resolve_key",
    "response_sha256",
    "secret_id",
    "validate_response",
]
