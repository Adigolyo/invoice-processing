"""Task 5 / USR-002-01: Gemini multimodal extraction of invoice fields and line items.

The ``google.genai`` client is replaced by a recording fake, so these tests are hermetic
(no network, no API key). Response payloads mirror the committed demo fixtures in
``tests/fixtures/tig_pairs/`` (Hungarian AAM invoices: "72 000 Ft", "2026.10.05.").
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from google.genai import errors, types

from intake.clients.auth import AuthConfigurationError, SecretAccessError
from intake.extraction import (
    DEFAULT_GEMINI_MODEL,
    INVOICE_REQUIRED_FIELDS,
    SUPPORTED_MIME_TYPES,
    DocumentKind,
    Extractor,
    GeminiExtractor,
    UnsupportedDocumentError,
    gemini_api_key_secret_id,
    model_from_env,
    resolve_api_key,
)
from intake.extraction import gemini_extractor as module
from intake.models import FlagReason, InvoiceExtraction, LineItem, StageStatus

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "tig_pairs"
INVOICE_A = FIXTURES / "TIG-2026-09-KEK-A__INV_A-2026-01" / "INV_A-2026-01.pdf"
TIG_A = FIXTURES / "TIG-2026-09-KEK-A__INV_A-2026-01" / "TIG-2026-09-KEK-A.pdf"
INVOICE_K_EUR = (
    FIXTURES / "TIG-2026-10-MAGENTA-K__INV.K-2026-03_MAGENTA" / "INV.K-2026-03_MAGENTA.pdf"
)

CURRENCY_MAP = {"Ft": "HUF", "€": "EUR"}
API_KEY = "AIza-SENTINEL-gemini-key"

HEADER_FIELDS = (
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


# --- fakes ------------------------------------------------------------------------------


def _response(text: str | None) -> types.GenerateContentResponse:
    if text is None:
        return types.GenerateContentResponse(candidates=[])
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(content=types.Content(role="model", parts=[types.Part(text=text)]))
        ]
    )


class FakeModels:
    """Records every ``generate_content`` call and replays queued responses/errors."""

    def __init__(self, outcomes: list[types.GenerateContentResponse | Exception]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    def generate_content(self, *, model: str, contents: Any, config: Any = None) -> Any:
        self.calls.append({"model": model, "contents": contents, "config": config})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeClient:
    def __init__(self, *outcomes: types.GenerateContentResponse | Exception) -> None:
        self.models = FakeModels(list(outcomes))


def _payload(**overrides: Any) -> dict[str, Any]:
    """Model output for INV_A-2026-01 (A Kft., one line, 72 000 Ft, AAM: gross == net)."""
    payload: dict[str, Any] = {
        "supplier": "A Kft.",
        "invoice_number": "INV_A-2026-01",
        "supplier_country": "HU",
        "currency": "Ft",
        "net": "72 000 Ft",
        "gross": "72 000 Ft",
        "issue_date": "2026.10.05.",
        "due_date": "2026.11.04.",
        "supply_date": None,
        "performance_date": "2026.10.05.",
        "line_items": [
            {
                "description": "Nyomdai szolgáltatás (szórólap) – Kék projekt, 2026. szeptember",
                "quantity": "1 200 db",
                "unit_price": "60 Ft",
                "net": "72 000 Ft",
            }
        ],
    }
    payload.update(overrides)
    return payload


def _extractor(
    *outcomes: types.GenerateContentResponse | Exception,
    model: str = DEFAULT_GEMINI_MODEL,
    currency_map: dict[str, str] | None = None,
) -> tuple[GeminiExtractor, FakeClient]:
    client = FakeClient(*outcomes)
    extractor = GeminiExtractor(
        client,  # type: ignore[arg-type]
        model=model,
        currency_map=CURRENCY_MAP if currency_map is None else currency_map,
    )
    return extractor, client


def _ok(payload: dict[str, Any]) -> types.GenerateContentResponse:
    return _response(json.dumps(payload, ensure_ascii=False))


def _document_parts(call: dict[str, Any]) -> list[types.Part]:
    contents = call["contents"]
    flat: list[Any] = []
    for item in contents if isinstance(contents, list) else [contents]:
        if isinstance(item, types.Content):
            flat.extend(item.parts or [])
        else:
            flat.append(item)
    return [p for p in flat if isinstance(p, types.Part) and p.inline_data is not None]


def _instructions(call: dict[str, Any]) -> str:
    """All text the model is given: system instruction plus any text parts."""
    config: types.GenerateContentConfig = call["config"]
    texts: list[str] = []
    system = config.system_instruction
    if isinstance(system, str):
        texts.append(system)
    elif isinstance(system, types.Content):
        texts.extend(p.text or "" for p in system.parts or [])
    contents = call["contents"]
    for item in contents if isinstance(contents, list) else [contents]:
        if isinstance(item, str):
            texts.append(item)
        elif isinstance(item, types.Part) and item.text:
            texts.append(item.text)
        elif isinstance(item, types.Content):
            texts.extend(p.text or "" for p in item.parts or [] if p.text)
    return "\n".join(texts)


# --- AC1: core fields from a PDF --------------------------------------------------------


def test_ac1_pdf_core_fields_are_returned_exactly_as_printed() -> None:
    extractor, _ = _extractor(_ok(_payload()))

    result = extractor.extract(INVOICE_A.read_bytes(), "application/pdf")

    assert result.status is StageStatus.OK
    extraction = result.value
    assert isinstance(extraction, InvoiceExtraction)
    assert extraction.supplier == "A Kft."
    assert extraction.invoice_number == "INV_A-2026-01"
    assert extraction.currency == "Ft"
    assert extraction.net == "72 000 Ft"
    assert extraction.gross == "72 000 Ft"
    assert extraction.issue_date == "2026.10.05."
    assert extraction.due_date == "2026.11.04."
    assert extraction.performance_date == "2026.10.05."
    assert extraction.supply_date is None
    assert extraction.supplier_country == "HU"


def test_ac1_pdf_bytes_are_sent_whole_as_one_inline_document_part() -> None:
    pdf = INVOICE_A.read_bytes()
    extractor, client = _extractor(_ok(_payload()))

    extractor.extract(pdf, "application/pdf")

    assert len(client.models.calls) == 1
    (part,) = _document_parts(client.models.calls[0])
    assert part.inline_data is not None
    assert part.inline_data.mime_type == "application/pdf"
    assert part.inline_data.data == pdf


def test_ac1_call_uses_json_schema_constrained_structured_output() -> None:
    extractor, client = _extractor(_ok(_payload()))

    extractor.extract(INVOICE_A.read_bytes(), "application/pdf")

    call = client.models.calls[0]
    assert call["model"] == DEFAULT_GEMINI_MODEL
    config = call["config"]
    assert isinstance(config, types.GenerateContentConfig)
    assert config.response_mime_type == "application/json"
    schema = config.response_json_schema
    assert isinstance(schema, dict)
    # Every header key is required and nullable, so the model must answer each one
    # explicitly (null when absent) instead of silently dropping it.
    assert set(schema["required"]) >= set(HEADER_FIELDS) | {"line_items"}
    for name in HEADER_FIELDS:
        assert "null" in schema["properties"][name]["type"]
    item_schema = schema["properties"]["line_items"]["items"]
    assert set(item_schema["required"]) == {"description", "quantity", "unit_price", "net"}
    assert config.temperature == 0


def test_ac1_eur_invoice_keeps_comma_decimals_as_printed() -> None:
    payload = _payload(
        supplier="K Kft.",
        invoice_number="INV.K-2026-03",
        currency="EUR",
        net="440,00 EUR",
        gross="440,00 EUR",
        issue_date="2026.11.05.",
        due_date="2026.12.05.",
        performance_date="2026.11.05.",
        line_items=[
            {
                "description": "Kertgondozás, zöldfelület-karbantartás – Magenta projekt, "
                "2026. október",
                "quantity": "22 óra",
                "unit_price": "20,00 EUR",
                "net": "440,00 EUR",
            }
        ],
    )
    extractor, _ = _extractor(_ok(payload))

    result = extractor.extract(INVOICE_K_EUR.read_bytes(), "application/pdf")

    assert result.status is StageStatus.OK
    assert result.value is not None
    assert result.value.net == "440,00 EUR"
    assert result.value.currency == "EUR"
    assert result.value.line_items == (
        LineItem(
            description="Kertgondozás, zöldfelület-karbantartás – Magenta projekt, 2026. október",
            quantity=Decimal("22"),
            unit_price=Decimal("20.00"),
            net=Decimal("440.00"),
        ),
    )


def test_ac1_prompt_maps_hungarian_labels_and_forbids_guessing() -> None:
    extractor, client = _extractor(_ok(_payload()))

    extractor.extract(INVOICE_A.read_bytes(), "application/pdf")

    text = _instructions(client.models.calls[0])
    assert "Teljesítés kelte" in text  # performance date on Hungarian invoices
    assert "Fizetési határidő" in text  # due date
    assert "null" in text
    assert "exactly as printed" in text.lower()
    assert "ISO 3166-1 alpha-2" in text


def test_lowercase_supplier_country_is_upper_cased() -> None:
    extractor, _ = _extractor(_ok(_payload(supplier_country=" hu ")))

    result = extractor.extract(INVOICE_A.read_bytes(), "application/pdf")

    assert result.value is not None
    assert result.value.supplier_country == "HU"


# --- AC2 / AC6: image input, same schema ------------------------------------------------


@pytest.mark.parametrize("mime_type", ["image/jpeg", "image/png", "image/webp"])
def test_ac2_image_invoice_yields_the_same_extraction_as_the_pdf(mime_type: str) -> None:
    pdf_extractor, _ = _extractor(_ok(_payload()))
    image_extractor, client = _extractor(_ok(_payload()))

    from_pdf = pdf_extractor.extract(INVOICE_A.read_bytes(), "application/pdf")
    from_image = image_extractor.extract(b"\xff\xd8\xff\xe0 fake image bytes", mime_type)

    assert from_image.status is StageStatus.OK
    assert from_image.value == from_pdf.value
    (part,) = _document_parts(client.models.calls[0])
    assert part.inline_data is not None
    assert part.inline_data.mime_type == mime_type


def test_ac6_schema_is_identical_regardless_of_source_format() -> None:
    pdf_extractor, pdf_client = _extractor(_ok(_payload()))
    image_extractor, image_client = _extractor(_ok(_payload()))

    pdf_result = pdf_extractor.extract(INVOICE_A.read_bytes(), "application/pdf")
    image_result = image_extractor.extract(b"\x89PNG fake", "image/png")

    assert type(pdf_result.value) is type(image_result.value) is InvoiceExtraction
    pdf_schema = pdf_client.models.calls[0]["config"].response_json_schema
    image_schema = image_client.models.calls[0]["config"].response_json_schema
    assert pdf_schema == image_schema
    assert _instructions(pdf_client.models.calls[0]) == _instructions(image_client.models.calls[0])


@pytest.mark.parametrize(
    ("given", "sent"),
    [
        ("image/jpg", "image/jpeg"),
        ("IMAGE/PNG", "image/png"),
        ("application/pdf; name=INV_A-2026-01.pdf", "application/pdf"),
    ],
)
def test_mime_type_variants_are_normalised(given: str, sent: str) -> None:
    extractor, client = _extractor(_ok(_payload()))

    extractor.extract(b"%PDF-1.7 bytes", given)

    (part,) = _document_parts(client.models.calls[0])
    assert part.inline_data is not None
    assert part.inline_data.mime_type == sent


@pytest.mark.parametrize("mime_type", ["application/zip", "text/html", "application/x-msdownload"])
def test_unsupported_mime_type_is_rejected_before_any_api_call(mime_type: str) -> None:
    extractor, client = _extractor()

    with pytest.raises(UnsupportedDocumentError):
        extractor.extract(b"MZ\x90\x00", mime_type)

    assert client.models.calls == []


def test_empty_document_is_rejected_before_any_api_call() -> None:
    extractor, client = _extractor()

    with pytest.raises(UnsupportedDocumentError):
        extractor.extract(b"", "application/pdf")

    assert client.models.calls == []


def test_supported_mime_types_cover_pdf_and_common_images() -> None:
    assert {"application/pdf", "image/jpeg", "image/png"} <= SUPPORTED_MIME_TYPES


# --- AC3: line items --------------------------------------------------------------------


def test_ac3_line_items_are_structured_records_with_decimal_values() -> None:
    payload = _payload(
        net="150 500 Ft",
        gross="150 500 Ft",
        line_items=[
            {
                "description": "Grafikai tervezés",
                "quantity": "38 óra",
                "unit_price": "2 500 Ft",
                "net": "95 000 Ft",
            },
            {
                "description": "Workshop",
                "quantity": "4 alkalom",
                "unit_price": "10 000 Ft",
                "net": "40 000 Ft",
            },
            {
                "description": "Nyomtatás",
                "quantity": "43 db",
                "unit_price": "360,47 Ft",
                "net": "15 500 Ft",
            },
        ],
    )
    extractor, _ = _extractor(_ok(payload))

    result = extractor.extract(INVOICE_A.read_bytes(), "application/pdf")

    assert result.status is StageStatus.OK
    assert result.value is not None
    assert result.value.line_items == (
        LineItem("Grafikai tervezés", Decimal("38"), Decimal("2500"), Decimal("95000")),
        LineItem("Workshop", Decimal("4"), Decimal("10000"), Decimal("40000")),
        LineItem("Nyomtatás", Decimal("43"), Decimal("360.47"), Decimal("15500")),
    )


def test_ac3_huf_lone_dot_before_three_digits_is_read_as_thousands() -> None:
    payload = _payload(
        line_items=[
            {
                "description": "Szolgáltatás",
                "quantity": "1 db",
                "unit_price": "150.500 Ft",
                "net": "150.500 Ft",
            }
        ]
    )
    extractor, _ = _extractor(_ok(payload))

    result = extractor.extract(INVOICE_A.read_bytes(), "application/pdf")

    assert result.value is not None
    (item,) = result.value.line_items
    assert item.unit_price == Decimal("150500")
    assert item.net == Decimal("150500")


def test_ac3_ambiguous_amount_without_resolvable_currency_is_none_not_guessed() -> None:
    # "Ft" is not configured as a currency symbol, so the currency cannot be resolved
    # to HUF and "150.500" stays ambiguous (150500 or 150.5): never guessed.
    payload = _payload(
        line_items=[
            {
                "description": "Szolgáltatás",
                "quantity": "1 db",
                "unit_price": "150.500 Ft",
                "net": "150 500 Ft",
            }
        ]
    )
    extractor, _ = _extractor(_ok(payload), currency_map={})

    result = extractor.extract(INVOICE_A.read_bytes(), "application/pdf")

    assert result.value is not None
    (item,) = result.value.line_items
    assert item.unit_price is None
    assert item.net == Decimal("150500")  # space grouping is unambiguous


def test_ac3_iso_currency_code_resolves_without_a_configured_map() -> None:
    payload = _payload(
        currency="HUF",
        line_items=[
            {
                "description": "Szolgáltatás",
                "quantity": "2 db",
                "unit_price": "1.250 HUF",
                "net": "2.500 HUF",
            }
        ],
    )
    extractor, _ = _extractor(_ok(payload), currency_map={})

    result = extractor.extract(INVOICE_A.read_bytes(), "application/pdf")

    assert result.value is not None
    (item,) = result.value.line_items
    assert (item.quantity, item.unit_price, item.net) == (
        Decimal("2"),
        Decimal("1250"),
        Decimal("2500"),
    )


def test_ac3_unreadable_line_values_are_none_and_the_line_is_kept() -> None:
    payload = _payload(
        line_items=[
            {"description": "  ", "quantity": None, "unit_price": "illegible", "net": "72 000 Ft"}
        ]
    )
    extractor, _ = _extractor(_ok(payload))

    result = extractor.extract(INVOICE_A.read_bytes(), "application/pdf")

    assert result.value is not None
    assert result.value.line_items == (LineItem(None, None, None, Decimal("72000")),)


def test_ac3_invoice_without_line_items_is_still_ok() -> None:
    extractor, _ = _extractor(_ok(_payload(line_items=[])))

    result = extractor.extract(INVOICE_A.read_bytes(), "application/pdf")

    assert result.status is StageStatus.OK
    assert result.value is not None
    assert result.value.line_items == ()


# --- AC4: missing / illegible required fields -------------------------------------------


@pytest.mark.parametrize("field", INVOICE_REQUIRED_FIELDS)
def test_ac4_missing_required_field_is_none_and_flagged_incomplete(field: str) -> None:
    extractor, _ = _extractor(_ok(_payload(**{field: None})))

    result = extractor.extract(INVOICE_A.read_bytes(), "application/pdf")

    assert result.status is StageStatus.INCOMPLETE
    assert result.flag_reason is FlagReason.INCOMPLETE_DATA
    assert result.detail is not None and field in result.detail
    # The partial extraction is kept for diagnosis; the missing field is None, not guessed.
    assert result.value is not None
    assert getattr(result.value, field) is None
    assert result.value.supplier_country == "HU"


def test_ac4_illegible_due_date_returned_blank_is_treated_as_missing() -> None:
    extractor, _ = _extractor(_ok(_payload(due_date="   ")))

    result = extractor.extract(INVOICE_A.read_bytes(), "application/pdf")

    assert result.status is StageStatus.INCOMPLETE
    assert result.value is not None
    assert result.value.due_date is None
    assert result.detail is not None and "due_date" in result.detail


def test_ac4_several_missing_fields_are_all_named() -> None:
    extractor, _ = _extractor(_ok(_payload(net=None, due_date=None)))

    result = extractor.extract(INVOICE_A.read_bytes(), "application/pdf")

    assert result.detail is not None
    assert "net" in result.detail and "due_date" in result.detail


@pytest.mark.parametrize(
    "field", ["performance_date", "issue_date", "supply_date", "supplier_country"]
)
def test_ac4_optional_fields_may_be_missing_without_flagging(field: str) -> None:
    # Foreign invoices often print no performance date; USR-002-04 decides from the
    # remaining dates, so extraction does not flag it.
    extractor, _ = _extractor(_ok(_payload(**{field: None})))

    result = extractor.extract(INVOICE_A.read_bytes(), "application/pdf")

    assert result.status is StageStatus.OK
    assert result.value is not None
    assert getattr(result.value, field) is None


def test_required_fields_are_the_story_core_fields() -> None:
    assert set(INVOICE_REQUIRED_FIELDS) == {
        "supplier",
        "invoice_number",
        "currency",
        "net",
        "gross",
        "due_date",
    }


# --- AC5: multi-page documents ----------------------------------------------------------


def test_ac5_line_items_from_every_page_are_kept_in_document_order() -> None:
    pages = [
        {
            "description": f"Tétel {n} (page {page})",
            "quantity": f"{n} db",
            "unit_price": "1 000 Ft",
            "net": f"{n} 000 Ft",
        }
        for n, page in [(1, 1), (2, 2), (3, 2), (4, 3), (5, 3)]
    ]
    extractor, client = _extractor(
        _ok(_payload(line_items=pages, net="15 000 Ft", gross="15 000 Ft"))
    )

    result = extractor.extract(b"%PDF-1.7 three pages", "application/pdf")

    assert result.value is not None
    assert [item.description for item in result.value.line_items] == [
        p["description"] for p in pages
    ]
    assert [item.net for item in result.value.line_items] == [
        Decimal(n * 1000) for n in range(1, 6)
    ]
    assert "all pages" in _instructions(client.models.calls[0]).lower()


# --- malformed structured output (QA: flagged, never passed downstream) -----------------


@pytest.mark.parametrize(
    "text",
    [
        "not json at all: A Kft. 72 000 Ft",
        '{"supplier": "A Kft.", "net": "72 000 Ft"',  # truncated
        "[]",
        "null",
        json.dumps({k: v for k, v in _payload().items() if k != "due_date"}),  # key missing
        json.dumps(_payload(vat_rate="AAM")),  # unexpected key
        json.dumps(_payload(net=72000)),  # number instead of the printed string
        json.dumps(_payload(line_items="1 200 db")),
        json.dumps(_payload(line_items=[{"description": "A Kft.", "quantity": "1 db"}])),
        json.dumps(
            _payload(
                line_items=[{"description": 5, "quantity": None, "unit_price": None, "net": None}]
            )
        ),
        "",
    ],
)
def test_non_conforming_response_is_rejected_as_incomplete(text: str) -> None:
    extractor, _ = _extractor(_response(text))

    result = extractor.extract(INVOICE_A.read_bytes(), "application/pdf")

    assert result.status is StageStatus.INCOMPLETE
    assert result.flag_reason is FlagReason.INCOMPLETE_DATA
    assert result.value is None  # no partial/corrupt extraction goes downstream
    assert result.detail
    # The detail ends up in logs: it names the problem, never document content.
    assert "A Kft." not in result.detail
    assert "72 000" not in result.detail


def test_response_without_any_candidate_text_is_incomplete() -> None:
    extractor, _ = _extractor(_response(None))

    result = extractor.extract(INVOICE_A.read_bytes(), "application/pdf")

    assert result.status is StageStatus.INCOMPLETE
    assert result.value is None


def test_json_wrapped_in_a_markdown_fence_is_rejected_not_repaired() -> None:
    extractor, _ = _extractor(_response("```json\n" + json.dumps(_payload()) + "\n```"))

    result = extractor.extract(INVOICE_A.read_bytes(), "application/pdf")

    assert result.status is StageStatus.INCOMPLETE


# --- API errors propagate (orchestrator retries next run) -------------------------------


@pytest.mark.parametrize(
    "error",
    [
        errors.ServerError(503, {"error": {"message": "unavailable"}}),
        errors.ClientError(429, {"error": {"message": "quota"}}),
        TimeoutError("read timed out"),
    ],
)
def test_api_errors_propagate_unchanged(error: Exception) -> None:
    extractor, _ = _extractor(error)

    with pytest.raises(type(error)):
        extractor.extract(INVOICE_A.read_bytes(), "application/pdf")


# --- TIG documents (reused by Task 18 / USR-005-01) -------------------------------------


def _tig_payload(**overrides: Any) -> dict[str, Any]:
    payload = _payload(
        invoice_number="TIG-2026-09-KEK-A",
        net=None,
        gross=None,
        issue_date="2026. október 5.",
        due_date=None,
        performance_date=None,
        line_items=[
            {
                "description": "Nyomdai szolgáltatás (szórólap)",
                "quantity": "1 200 db",
                "unit_price": "60 Ft",
                "net": "72 000 Ft",
            }
        ],
    )
    payload.update(overrides)
    return payload


def test_tig_certificate_is_extracted_with_its_line_items() -> None:
    extractor, client = _extractor(_ok(_tig_payload()))

    result = extractor.extract(TIG_A.read_bytes(), "application/pdf", kind=DocumentKind.TIG)

    # Invoice-only fields (due date, gross) are legitimately absent on a TIG.
    assert result.status is StageStatus.OK
    assert result.value is not None
    assert result.value.supplier == "A Kft."
    assert result.value.line_items == (
        LineItem(
            "Nyomdai szolgáltatás (szórólap)", Decimal("1200"), Decimal("60"), Decimal("72000")
        ),
    )
    text = _instructions(client.models.calls[0])
    assert "Megbízott" in text  # the contractor, not the client (Megbízó), is the supplier
    assert client.models.calls[0]["config"].response_json_schema == _invoice_schema()


def _invoice_schema() -> Any:
    extractor, client = _extractor(_ok(_payload()))
    extractor.extract(b"%PDF", "application/pdf")
    return client.models.calls[0]["config"].response_json_schema


def test_tig_without_line_items_is_incomplete() -> None:
    extractor, _ = _extractor(_ok(_tig_payload(line_items=[])))

    result = extractor.extract(TIG_A.read_bytes(), "application/pdf", kind=DocumentKind.TIG)

    assert result.status is StageStatus.INCOMPLETE
    assert result.flag_reason is FlagReason.INCOMPLETE_DATA
    assert result.detail is not None and "line_items" in result.detail


def test_invoice_and_tig_prompts_differ() -> None:
    inv_extractor, inv_client = _extractor(_ok(_payload()))
    tig_extractor, tig_client = _extractor(_ok(_tig_payload()))

    inv_extractor.extract(b"%PDF", "application/pdf")
    tig_extractor.extract(b"%PDF", "application/pdf", kind=DocumentKind.TIG)

    assert _instructions(inv_client.models.calls[0]) != _instructions(tig_client.models.calls[0])


# --- Extractor protocol -----------------------------------------------------------------


def test_gemini_extractor_implements_the_extractor_protocol() -> None:
    extractor, _ = _extractor()
    assert isinstance(extractor, Extractor)


# --- model configuration ----------------------------------------------------------------


def test_default_model_is_the_current_gemini_flash() -> None:
    assert DEFAULT_GEMINI_MODEL == "gemini-3.8-flash"


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, DEFAULT_GEMINI_MODEL),
        ({"GEMINI_MODEL": "  "}, DEFAULT_GEMINI_MODEL),
        ({"GEMINI_MODEL": "gemini-3.5-flash-lite"}, "gemini-3.5-flash-lite"),
    ],
)
def test_model_is_configurable_through_env(env: dict[str, str], expected: str) -> None:
    assert model_from_env(env) == expected


def test_configured_model_is_used_for_the_call() -> None:
    extractor, client = _extractor(_ok(_payload()), model="gemini-3.5-flash")

    extractor.extract(b"%PDF", "application/pdf")

    assert client.models.calls[0]["model"] == "gemini-3.5-flash"


def test_blank_model_name_is_rejected() -> None:
    with pytest.raises(ValueError):
        GeminiExtractor(FakeClient(), model=" ")  # type: ignore[arg-type]


# --- API key resolution -----------------------------------------------------------------


class FakeAccessor:
    def __init__(self, store: dict[str, str]) -> None:
        self.store = store
        self.requested: list[str] = []

    def __call__(self, name: str) -> str:
        self.requested.append(name)
        if name not in self.store:
            raise LookupError("404")
        return self.store[name]


def test_secret_id_follows_the_per_environment_convention() -> None:
    assert gemini_api_key_secret_id("staging") == "kibit-gemini-api-key-staging"


def test_invalid_environment_name_is_rejected() -> None:
    with pytest.raises(AuthConfigurationError):
        gemini_api_key_secret_id("../prod")


def test_api_key_from_env_wins_for_local_dev() -> None:
    accessor = FakeAccessor({})

    key = resolve_api_key({"GEMINI_API_KEY": f" {API_KEY}\n"}, accessor=accessor)

    assert key == API_KEY
    assert accessor.requested == []


def test_api_key_is_read_from_secret_manager_otherwise() -> None:
    name = "projects/kibit-p/secrets/kibit-gemini-api-key-production/versions/latest"
    accessor = FakeAccessor({name: API_KEY + "\n"})

    key = resolve_api_key(
        {"GCP_PROJECT_ID": "kibit-p", "ENVIRONMENT": "production"}, accessor=accessor
    )

    assert key == API_KEY
    assert accessor.requested == [name]


def test_missing_project_or_environment_is_a_configuration_error() -> None:
    with pytest.raises(AuthConfigurationError) as excinfo:
        resolve_api_key({"ENVIRONMENT": "staging"}, accessor=FakeAccessor({}))
    assert "GCP_PROJECT_ID" in str(excinfo.value)


def test_unreadable_secret_raises_without_leaking_anything() -> None:
    with pytest.raises(SecretAccessError):
        resolve_api_key(
            {"GCP_PROJECT_ID": "kibit-p", "ENVIRONMENT": "staging"}, accessor=FakeAccessor({})
        )


def test_from_env_builds_a_client_with_the_resolved_key_and_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[dict[str, Any]] = []

    def fake_client(**kwargs: Any) -> FakeClient:
        created.append(kwargs)
        return FakeClient(_ok(_payload()))

    monkeypatch.setattr(module.genai, "Client", fake_client)

    extractor = GeminiExtractor.from_env(
        {"GEMINI_API_KEY": API_KEY, "GEMINI_MODEL": "gemini-3.5-flash"},
        currency_map=CURRENCY_MAP,
    )

    assert created[0]["api_key"] == API_KEY
    assert extractor.model == "gemini-3.5-flash"
    assert API_KEY not in repr(extractor)
    result = extractor.extract(INVOICE_A.read_bytes(), "application/pdf")
    assert result.status is StageStatus.OK


# --- no document content or key in logs -------------------------------------------------


def _all_logged_text(caplog: pytest.LogCaptureFixture) -> str:
    chunks: list[str] = []
    for record in caplog.records:
        chunks.append(record.getMessage())
        chunks.extend(str(value) for value in record.__dict__.values())
    return "\n".join(chunks)


@pytest.mark.parametrize(
    "make_response",
    [
        lambda: _ok(_payload()),
        lambda: _ok(_payload(due_date=None)),
        lambda: _response("garbage A Kft. 72 000 Ft"),
    ],
)
def test_logs_never_contain_document_content_or_the_key(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    make_response: Callable[[], types.GenerateContentResponse],
) -> None:
    monkeypatch.setattr(module.genai, "Client", lambda **_: FakeClient(make_response()))
    caplog.set_level(logging.DEBUG)

    extractor = GeminiExtractor.from_env({"GEMINI_API_KEY": API_KEY})
    extractor.extract(INVOICE_A.read_bytes(), "application/pdf")

    logged = _all_logged_text(caplog)
    assert caplog.records, "the extraction outcome should be logged"
    for secret in (API_KEY, "A Kft.", "72 000", "INV_A-2026-01", "2026.11.04."):
        assert secret not in logged
