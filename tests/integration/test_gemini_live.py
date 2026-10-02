"""Live Gemini extraction accuracy against the demo TIG-pair invoices (Task 5 / USR-002-01).

Calls the real Gemini API, so it is marked ``live`` and skipped unless ``GEMINI_API_KEY``
is set and ``tests/fixtures/tig_pairs/answer_key.json`` exists. Only the synthetic demo
documents in ``tests/fixtures/tig_pairs/`` are sent. ``GEMINI_LIVE_MAX_PAIRS`` (default 2)
caps how many invoices are extracted per run; ``GEMINI_MODEL`` picks the model.

Run with: ``pytest -m live tests/integration/test_gemini_live.py``
"""

from __future__ import annotations

import json
import os
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from intake.extraction import GeminiExtractor, parse_quantity
from intake.models import InvoiceExtraction, StageStatus
from intake.normalization import normalize_amount

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "tig_pairs"
ANSWER_KEY = FIXTURES / "answer_key.json"
CURRENCY_MAP = {"Ft": "HUF", "€": "EUR"}

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.environ.get("GEMINI_API_KEY"), reason="GEMINI_API_KEY not set"),
    pytest.mark.skipif(not ANSWER_KEY.exists(), reason="answer_key.json not present"),
]

# answer-key key -> InvoiceExtraction attribute
HEADER_MAP = {
    "supplier": "supplier",
    "invoice_number": "invoice_number",
    "currency_raw": "currency",
    "net_raw": "net",
    "gross_raw": "gross",
    "issue_date_raw": "issue_date",
    "due_date_raw": "due_date",
    "supply_date_raw": "supply_date",
    "performance_date_raw": "performance_date",
    "supplier_country": "supplier_country",
}


def _squash(value: object) -> str | None:
    if value is None:
        return None
    return re.sub(r"\s+", " ", str(value)).strip() or None


def _number(value: object, *, quantity: bool = False) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, int | float):
        return Decimal(str(value))
    text = str(value)
    if quantity:
        return parse_quantity(text)
    parsed = normalize_amount(text, currency_iso="HUF" if "Ft" in text else None)
    return parsed if isinstance(parsed, Decimal) else None


def _invoice_cases() -> list[dict[str, Any]]:
    if not ANSWER_KEY.exists():
        return []
    pairs = json.loads(ANSWER_KEY.read_text(encoding="utf-8"))["pairs"]
    limit = int(os.environ.get("GEMINI_LIVE_MAX_PAIRS", "2"))
    return [pair["invoice"] for pair in pairs[:limit]]


def _invoice_pdf(invoice_number: str) -> Path:
    matches = sorted(FIXTURES.glob(f"*/{invoice_number}*.pdf"))
    matches = [m for m in matches if not m.name.startswith("TIG-")]
    assert matches, f"no fixture PDF for {invoice_number}"
    return matches[0]


@pytest.fixture(scope="module")
def extractor() -> GeminiExtractor:
    return GeminiExtractor.from_env(dict(os.environ), currency_map=CURRENCY_MAP)


@pytest.mark.parametrize(
    "expected", _invoice_cases(), ids=lambda inv: str(inv.get("invoice_number"))
)
def test_live_extraction_matches_answer_key(
    extractor: GeminiExtractor, expected: dict[str, Any]
) -> None:
    pdf = _invoice_pdf(expected["invoice_number"])

    result = extractor.extract(pdf.read_bytes(), "application/pdf")

    assert result.status is StageStatus.OK, result.detail
    actual = result.value
    assert isinstance(actual, InvoiceExtraction)
    for key, attr in HEADER_MAP.items():
        if key in expected:
            assert _squash(getattr(actual, attr)) == _squash(expected[key]), key

    expected_lines = expected.get("lines", [])
    assert len(actual.line_items) == len(expected_lines)
    for got, want in zip(actual.line_items, expected_lines, strict=True):
        assert _squash(got.description) == _squash(want["description"])
        assert got.quantity == _number(want["quantity"], quantity=True)
        assert got.unit_price == _number(want["unit_price"])
        assert got.net == _number(want["net"])
