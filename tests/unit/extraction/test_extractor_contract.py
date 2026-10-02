"""USR-002-01 / USR-005-01 extraction contract, held by EVERY backend (ADR 4).

The AC/QA coverage first written for the Gemini extractor (``test_gemini_extractor.py``,
kept unchanged) is ported here and parametrised over the Gemini and the Claude (AI
Compass) backends, so swapping the model cannot lower the bar. Each backend runs against
a recording fake client; see ``backends.py``.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from intake.extraction import (
    INVOICE_REQUIRED_FIELDS,
    DocumentKind,
    Extractor,
    UnsupportedDocumentError,
    extractor_from_env,
)
from intake.extraction.common import RESPONSE_JSON_SCHEMA
from intake.models import FlagReason, InvoiceExtraction, LineItem, StageStatus
from tests.unit.extraction.backends import BACKENDS, Backend

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "tig_pairs"
INVOICE_A = FIXTURES / "TIG-2026-09-KEK-A__INV_A-2026-01" / "INV_A-2026-01.pdf"
TIG_A = FIXTURES / "TIG-2026-09-KEK-A__INV_A-2026-01" / "TIG-2026-09-KEK-A.pdf"
INVOICE_K_EUR = (
    FIXTURES / "TIG-2026-10-MAGENTA-K__INV.K-2026-03_MAGENTA" / "INV.K-2026-03_MAGENTA.pdf"
)

CURRENCY_MAP = {"Ft": "HUF", "€": "EUR"}
API_KEY = "sk-SENTINEL-extraction-key"
HEADER_FIELDS = tuple(name for name in RESPONSE_JSON_SCHEMA["required"] if name != "line_items")

pytestmark = pytest.mark.parametrize("backend", BACKENDS, ids=lambda b: b.name)


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


def _ok(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _extractor(
    backend: Backend, *outcomes: Any, currency_map: dict[str, str] | None = None
) -> tuple[Extractor, Any]:
    return backend.extractor(
        *outcomes, currency_map=CURRENCY_MAP if currency_map is None else currency_map
    )


def _line(description: str, quantity: str, unit_price: str, net: str) -> dict[str, str]:
    return {"description": description, "quantity": quantity, "unit_price": unit_price, "net": net}


# --- AC1: core fields from a PDF --------------------------------------------------------


def test_ac1_pdf_core_fields_are_returned_exactly_as_printed(backend: Backend) -> None:
    extractor, _ = _extractor(backend, _ok(_payload()))

    result = extractor.extract(INVOICE_A.read_bytes(), "application/pdf")

    assert result.status is StageStatus.OK
    extraction = result.value
    assert isinstance(extraction, InvoiceExtraction)
    assert (extraction.supplier, extraction.invoice_number) == ("A Kft.", "INV_A-2026-01")
    assert (extraction.currency, extraction.net, extraction.gross) == (
        "Ft",
        "72 000 Ft",
        "72 000 Ft",
    )
    assert extraction.issue_date == "2026.10.05."
    assert extraction.due_date == "2026.11.04."
    assert extraction.performance_date == "2026.10.05."
    assert extraction.supply_date is None
    assert extraction.supplier_country == "HU"


def test_ac1_pdf_bytes_are_sent_whole_in_one_call(backend: Backend) -> None:
    pdf = INVOICE_A.read_bytes()
    extractor, client = _extractor(backend, _ok(_payload()))

    extractor.extract(pdf, "application/pdf")

    assert len(client.calls) == 1
    assert backend.document(client.calls[0]) == ("application/pdf", pdf)


def test_ac1_answer_is_constrained_by_the_shared_json_schema(backend: Backend) -> None:
    extractor, client = _extractor(backend, _ok(_payload()))

    extractor.extract(INVOICE_A.read_bytes(), "application/pdf")

    schema = backend.schema(client.calls[0])
    assert schema == RESPONSE_JSON_SCHEMA
    assert set(schema["required"]) >= set(HEADER_FIELDS) | {"line_items"}
    for name in HEADER_FIELDS:
        assert "null" in schema["properties"][name]["type"]
    item_schema = schema["properties"]["line_items"]["items"]
    assert set(item_schema["required"]) == {"description", "quantity", "unit_price", "net"}
    assert schema["additionalProperties"] is False
    assert item_schema["additionalProperties"] is False


def test_ac1_eur_invoice_keeps_comma_decimals_as_printed(backend: Backend) -> None:
    description = "Kertgondozás, zöldfelület-karbantartás – Magenta projekt, 2026. október"
    payload = _payload(
        supplier="K Kft.",
        invoice_number="INV.K-2026-03",
        currency="EUR",
        net="440,00 EUR",
        gross="440,00 EUR",
        line_items=[_line(description, "22 óra", "20,00 EUR", "440,00 EUR")],
    )
    extractor, _ = _extractor(backend, _ok(payload))

    result = extractor.extract(INVOICE_K_EUR.read_bytes(), "application/pdf")

    assert result.status is StageStatus.OK
    assert result.value is not None
    assert result.value.net == "440,00 EUR"
    assert result.value.line_items == (
        LineItem(description, Decimal("22"), Decimal("20.00"), Decimal("440.00")),
    )


def test_ac1_prompt_maps_hungarian_labels_and_forbids_guessing(backend: Backend) -> None:
    extractor, client = _extractor(backend, _ok(_payload()))

    extractor.extract(INVOICE_A.read_bytes(), "application/pdf")

    text = backend.instructions(client.calls[0])
    assert "Teljesítés kelte" in text
    assert "Fizetési határidő" in text
    assert "null" in text
    assert "exactly as printed" in text.lower()
    assert "ISO 3166-1 alpha-2" in text


def test_lowercase_supplier_country_is_upper_cased(backend: Backend) -> None:
    extractor, _ = _extractor(backend, _ok(_payload(supplier_country=" hu ")))

    result = extractor.extract(INVOICE_A.read_bytes(), "application/pdf")

    assert result.value is not None
    assert result.value.supplier_country == "HU"


# --- AC2 / AC6: image input, same schema ------------------------------------------------


def test_ac2_image_invoice_yields_the_same_extraction_as_the_pdf(backend: Backend) -> None:
    for mime_type in backend.image_mime_types:
        pdf_extractor, _ = _extractor(backend, _ok(_payload()))
        image_extractor, client = _extractor(backend, _ok(_payload()))
        image = b"\xff\xd8\xff\xe0 fake image bytes"

        from_pdf = pdf_extractor.extract(INVOICE_A.read_bytes(), "application/pdf")
        from_image = image_extractor.extract(image, mime_type)

        assert from_image.status is StageStatus.OK
        assert from_image.value == from_pdf.value
        assert backend.document(client.calls[0]) == (mime_type, image)


def test_ac6_schema_and_instructions_are_identical_regardless_of_format(
    backend: Backend,
) -> None:
    pdf_extractor, pdf_client = _extractor(backend, _ok(_payload()))
    image_extractor, image_client = _extractor(backend, _ok(_payload()))

    pdf_result = pdf_extractor.extract(INVOICE_A.read_bytes(), "application/pdf")
    image_result = image_extractor.extract(b"\x89PNG fake", "image/png")

    assert type(pdf_result.value) is type(image_result.value) is InvoiceExtraction
    assert backend.schema(pdf_client.calls[0]) == backend.schema(image_client.calls[0])
    assert backend.instructions(pdf_client.calls[0]) == backend.instructions(image_client.calls[0])


@pytest.mark.parametrize(
    ("given", "sent"),
    [
        ("image/jpg", "image/jpeg"),
        ("IMAGE/PNG", "image/png"),
        ("application/pdf; name=INV_A-2026-01.pdf", "application/pdf"),
    ],
)
def test_mime_type_variants_are_normalised(backend: Backend, given: str, sent: str) -> None:
    extractor, client = _extractor(backend, _ok(_payload()))

    extractor.extract(b"%PDF-1.7 bytes", given)

    assert backend.document(client.calls[0])[0] == sent


@pytest.mark.parametrize("mime_type", ["application/zip", "text/html", "application/x-msdownload"])
def test_unsupported_mime_type_is_rejected_before_any_api_call(
    backend: Backend, mime_type: str
) -> None:
    extractor, client = _extractor(backend)

    with pytest.raises(UnsupportedDocumentError):
        extractor.extract(b"MZ\x90\x00", mime_type)

    assert client.calls == []


def test_empty_document_is_rejected_before_any_api_call(backend: Backend) -> None:
    extractor, client = _extractor(backend)

    with pytest.raises(UnsupportedDocumentError):
        extractor.extract(b"", "application/pdf")

    assert client.calls == []


# --- AC3: line items --------------------------------------------------------------------


def test_ac3_line_items_are_structured_records_with_decimal_values(backend: Backend) -> None:
    payload = _payload(
        net="150 500 Ft",
        gross="150 500 Ft",
        line_items=[
            _line("Grafikai tervezés", "38 óra", "2 500 Ft", "95 000 Ft"),
            _line("Workshop", "4 alkalom", "10 000 Ft", "40 000 Ft"),
            _line("Nyomtatás", "43 db", "360,47 Ft", "15 500 Ft"),
        ],
    )
    extractor, _ = _extractor(backend, _ok(payload))

    result = extractor.extract(INVOICE_A.read_bytes(), "application/pdf")

    assert result.status is StageStatus.OK
    assert result.value is not None
    assert result.value.line_items == (
        LineItem("Grafikai tervezés", Decimal("38"), Decimal("2500"), Decimal("95000")),
        LineItem("Workshop", Decimal("4"), Decimal("10000"), Decimal("40000")),
        LineItem("Nyomtatás", Decimal("43"), Decimal("360.47"), Decimal("15500")),
    )


def test_ac3_huf_lone_dot_before_three_digits_is_read_as_thousands(backend: Backend) -> None:
    payload = _payload(line_items=[_line("Szolgáltatás", "1 db", "150.500 Ft", "150.500 Ft")])
    extractor, _ = _extractor(backend, _ok(payload))

    result = extractor.extract(INVOICE_A.read_bytes(), "application/pdf")

    assert result.value is not None
    (item,) = result.value.line_items
    assert (item.unit_price, item.net) == (Decimal("150500"), Decimal("150500"))


def test_ac3_ambiguous_amount_without_resolvable_currency_is_none(backend: Backend) -> None:
    payload = _payload(line_items=[_line("Szolgáltatás", "1 db", "150.500 Ft", "150 500 Ft")])
    extractor, _ = _extractor(backend, _ok(payload), currency_map={})

    result = extractor.extract(INVOICE_A.read_bytes(), "application/pdf")

    assert result.value is not None
    (item,) = result.value.line_items
    assert item.unit_price is None
    assert item.net == Decimal("150500")


def test_ac3_iso_currency_code_resolves_without_a_configured_map(backend: Backend) -> None:
    payload = _payload(
        currency="HUF", line_items=[_line("Szolgáltatás", "2 db", "1.250 HUF", "2.500 HUF")]
    )
    extractor, _ = _extractor(backend, _ok(payload), currency_map={})

    result = extractor.extract(INVOICE_A.read_bytes(), "application/pdf")

    assert result.value is not None
    (item,) = result.value.line_items
    assert (item.quantity, item.unit_price, item.net) == (
        Decimal("2"),
        Decimal("1250"),
        Decimal("2500"),
    )


def test_ac3_unreadable_line_values_are_none_and_the_line_is_kept(backend: Backend) -> None:
    payload = _payload(
        line_items=[
            {"description": "  ", "quantity": None, "unit_price": "illegible", "net": "72 000 Ft"}
        ]
    )
    extractor, _ = _extractor(backend, _ok(payload))

    result = extractor.extract(INVOICE_A.read_bytes(), "application/pdf")

    assert result.value is not None
    assert result.value.line_items == (LineItem(None, None, None, Decimal("72000")),)


def test_ac3_invoice_without_line_items_is_still_ok(backend: Backend) -> None:
    extractor, _ = _extractor(backend, _ok(_payload(line_items=[])))

    result = extractor.extract(INVOICE_A.read_bytes(), "application/pdf")

    assert result.status is StageStatus.OK
    assert result.value is not None and result.value.line_items == ()


# --- AC4: missing / illegible required fields -------------------------------------------


@pytest.mark.parametrize("field", INVOICE_REQUIRED_FIELDS)
def test_ac4_missing_required_field_is_none_and_flagged_incomplete(
    backend: Backend, field: str
) -> None:
    extractor, _ = _extractor(backend, _ok(_payload(**{field: None})))

    result = extractor.extract(INVOICE_A.read_bytes(), "application/pdf")

    assert result.status is StageStatus.INCOMPLETE
    assert result.flag_reason is FlagReason.INCOMPLETE_DATA
    assert result.detail is not None and field in result.detail
    assert result.value is not None
    assert getattr(result.value, field) is None
    assert result.value.supplier_country == "HU"


def test_ac4_illegible_due_date_returned_blank_is_treated_as_missing(backend: Backend) -> None:
    extractor, _ = _extractor(backend, _ok(_payload(due_date="   ", issue_date=None)))

    result = extractor.extract(INVOICE_A.read_bytes(), "application/pdf")

    assert result.status is StageStatus.INCOMPLETE
    assert result.value is not None and result.value.due_date is None
    assert result.detail is not None and "due_date" in result.detail


def test_ac4_several_missing_fields_are_all_named(backend: Backend) -> None:
    extractor, _ = _extractor(backend, _ok(_payload(net=None, due_date=None, issue_date=None)))

    result = extractor.extract(INVOICE_A.read_bytes(), "application/pdf")

    assert result.detail is not None
    assert "net" in result.detail and "due_date" in result.detail


@pytest.mark.parametrize(
    "field", ["performance_date", "issue_date", "supply_date", "supplier_country"]
)
def test_ac4_optional_fields_may_be_missing_without_flagging(backend: Backend, field: str) -> None:
    extractor, _ = _extractor(backend, _ok(_payload(**{field: None})))

    result = extractor.extract(INVOICE_A.read_bytes(), "application/pdf")

    assert result.status is StageStatus.OK
    assert result.value is not None and getattr(result.value, field) is None


# --- AC5: multi-page documents ----------------------------------------------------------


def test_ac5_line_items_from_every_page_are_kept_in_document_order(backend: Backend) -> None:
    pages = [
        _line(f"Tétel {n} (page {page})", f"{n} db", "1 000 Ft", f"{n} 000 Ft")
        for n, page in [(1, 1), (2, 2), (3, 2), (4, 3), (5, 3)]
    ]
    extractor, client = _extractor(
        backend, _ok(_payload(line_items=pages, net="15 000 Ft", gross="15 000 Ft"))
    )

    result = extractor.extract(b"%PDF-1.7 three pages", "application/pdf")

    assert result.value is not None
    assert [i.description for i in result.value.line_items] == [p["description"] for p in pages]
    assert [i.net for i in result.value.line_items] == [Decimal(n * 1000) for n in range(1, 6)]
    assert "all pages" in backend.instructions(client.calls[0]).lower()


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
        "```json\n" + json.dumps(_payload()) + "\n```",  # fenced: rejected, not repaired
        None,  # no text at all
    ],
)
def test_non_conforming_response_is_rejected_as_incomplete(
    backend: Backend, text: str | None
) -> None:
    extractor, _ = _extractor(backend, text)

    result = extractor.extract(INVOICE_A.read_bytes(), "application/pdf")

    assert result.status is StageStatus.INCOMPLETE
    assert result.flag_reason is FlagReason.INCOMPLETE_DATA
    assert result.value is None
    assert result.detail
    assert "A Kft." not in result.detail
    assert "72 000" not in result.detail


# --- TIG documents (USR-005-01) ---------------------------------------------------------


def _tig_payload(**overrides: Any) -> dict[str, Any]:
    payload = _payload(
        invoice_number="TIG-2026-09-KEK-A",
        net=None,
        gross=None,
        issue_date="2026. október 5.",
        due_date=None,
        performance_date=None,
        line_items=[_line("Nyomdai szolgáltatás (szórólap)", "1 200 db", "60 Ft", "72 000 Ft")],
    )
    payload.update(overrides)
    return payload


def test_tig_certificate_is_extracted_with_its_line_items(backend: Backend) -> None:
    extractor, client = _extractor(backend, _ok(_tig_payload()))

    result = extractor.extract(TIG_A.read_bytes(), "application/pdf", kind=DocumentKind.TIG)

    assert result.status is StageStatus.OK
    assert result.value is not None
    assert result.value.supplier == "A Kft."
    assert result.value.line_items == (
        LineItem(
            "Nyomdai szolgáltatás (szórólap)", Decimal("1200"), Decimal("60"), Decimal("72000")
        ),
    )
    assert "Megbízott" in backend.instructions(client.calls[0])
    assert backend.schema(client.calls[0]) == RESPONSE_JSON_SCHEMA


def test_tig_without_line_items_is_incomplete(backend: Backend) -> None:
    extractor, _ = _extractor(backend, _ok(_tig_payload(line_items=[])))

    result = extractor.extract(TIG_A.read_bytes(), "application/pdf", kind=DocumentKind.TIG)

    assert result.status is StageStatus.INCOMPLETE
    assert result.flag_reason is FlagReason.INCOMPLETE_DATA
    assert result.detail is not None and "line_items" in result.detail


def test_invoice_and_tig_prompts_differ(backend: Backend) -> None:
    inv_extractor, inv_client = _extractor(backend, _ok(_payload()))
    tig_extractor, tig_client = _extractor(backend, _ok(_tig_payload()))

    inv_extractor.extract(b"%PDF", "application/pdf")
    tig_extractor.extract(b"%PDF", "application/pdf", kind=DocumentKind.TIG)

    assert backend.instructions(inv_client.calls[0]) != backend.instructions(tig_client.calls[0])


# --- protocol and logging ---------------------------------------------------------------


def test_backend_implements_the_extractor_protocol(backend: Backend) -> None:
    extractor, _ = _extractor(backend)
    assert isinstance(extractor, Extractor)


def _all_logged_text(caplog: pytest.LogCaptureFixture) -> str:
    chunks: list[str] = []
    for record in caplog.records:
        chunks.append(record.getMessage())
        chunks.extend(str(value) for value in record.__dict__.values())
    return "\n".join(chunks)


@pytest.mark.parametrize(
    "text",
    [_ok(_payload()), _ok(_payload(due_date=None, issue_date=None)), "garbage A Kft. 72 000 Ft"],
)
def test_logs_never_contain_document_content_or_the_key(
    backend: Backend,
    text: str,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = backend.patch_from_env(monkeypatch, backend.make_client(text), API_KEY)
    caplog.set_level(logging.DEBUG)

    extractor = extractor_from_env(env)
    extractor.extract(INVOICE_A.read_bytes(), "application/pdf")

    logged = _all_logged_text(caplog)
    assert caplog.records, "the extraction outcome should be logged"
    for secret in (API_KEY, "A Kft.", "72 000", "INV_A-2026-01", "2026.11.04."):
        assert secret not in logged
    assert API_KEY not in repr(extractor)


def test_missing_due_date_is_not_flagged_when_an_issue_date_is_printed(backend: Backend) -> None:
    # Receipts paid on the spot print no due date; the issue date stands in for it.
    extractor, _ = _extractor(backend, _ok(_payload(due_date=None)))

    result = extractor.extract(INVOICE_A.read_bytes(), "application/pdf")

    assert result.status is StageStatus.OK
    assert result.value is not None and result.value.due_date is None


def test_missing_due_and_issue_date_flags_the_due_date(backend: Backend) -> None:
    extractor, _ = _extractor(backend, _ok(_payload(due_date=None, issue_date=None)))

    result = extractor.extract(INVOICE_A.read_bytes(), "application/pdf")

    assert result.status is StageStatus.INCOMPLETE
    assert result.detail is not None and "due_date" in result.detail
