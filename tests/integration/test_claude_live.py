"""Live Claude (AI Compass) extraction accuracy against the demo TIG pairs.

Calls the real AI Compass gateway, so it is marked ``live`` and skipped unless
``AI_COMPASS_API_KEY`` is set and ``tests/fixtures/tig_pairs/answer_key.json`` exists.
Only the synthetic demo documents in ``tests/fixtures/tig_pairs/`` are sent, never
anything else. ``LIVE_MAX_PAIRS`` (default 3) caps how many pairs are extracted; both
the invoice and the TIG of each pair are extracted. ``AI_COMPASS_BASE_URL``,
``AI_COMPASS_MODEL`` and ``AI_COMPASS_EFFORT`` configure the call.

Each test collects every field mismatch before failing, so one run reports them all.

Run with: ``pytest -m live tests/integration/test_claude_live.py``
"""

from __future__ import annotations

import json
import os
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from intake.extraction import ClaudeExtractor, DocumentKind, parse_quantity
from intake.models import InvoiceExtraction, LineItem, StageStatus
from intake.normalization import normalize_amount

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "tig_pairs"
ANSWER_KEY = FIXTURES / "answer_key.json"
CURRENCY_MAP = {"Ft": "HUF", "€": "EUR"}

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.environ.get("AI_COMPASS_API_KEY"), reason="AI_COMPASS_API_KEY not set"
    ),
    pytest.mark.skipif(not ANSWER_KEY.exists(), reason="answer_key.json not present"),
]

# answer-key key -> InvoiceExtraction attribute
INVOICE_HEADER_MAP = {
    "supplier": "supplier",
    "invoice_number": "invoice_number",
    "net_raw": "net",
    "gross_raw": "gross",
    "due_date_raw": "due_date",
    "performance_date_raw": "performance_date",
    "supplier_country": "supplier_country",
}


def _squash(value: object) -> str | None:
    if value is None:
        return None
    return re.sub(r"\s+", " ", str(value)).strip() or None


def _decimal(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _pairs() -> list[dict[str, Any]]:
    if not ANSWER_KEY.exists():
        return []
    pairs: list[dict[str, Any]] = json.loads(ANSWER_KEY.read_text(encoding="utf-8"))["pairs"]
    return pairs[: int(os.environ.get("LIVE_MAX_PAIRS", "3"))]


def _pdf(pair: dict[str, Any], document: dict[str, Any]) -> Path:
    path = FIXTURES / pair["pair"] / document["file"]
    assert path.is_file(), f"no fixture PDF {path.name}"
    assert path.resolve().is_relative_to(FIXTURES.resolve())  # only the demo TIG pairs
    return path


def _line_mismatches(
    actual: tuple[LineItem, ...], expected: list[dict[str, Any]], where: str
) -> list[str]:
    if len(actual) != len(expected):
        return [f"{where}: {len(actual)} line(s), expected {len(expected)}"]
    problems: list[str] = []
    for index, (got, want) in enumerate(zip(actual, expected, strict=True)):
        for field, wanted in (
            ("quantity", _decimal(want["quantity"])),
            ("unit_price", _decimal(want["unit_price"])),
            ("net", _decimal(want["net"])),
        ):
            if getattr(got, field) != wanted:
                problems.append(f"{where}.lines[{index}].{field}: {getattr(got, field)} != {wanted}")
    return problems


@pytest.fixture(scope="module")
def extractor() -> ClaudeExtractor:
    return ClaudeExtractor.from_env(dict(os.environ), currency_map=CURRENCY_MAP)


@pytest.mark.parametrize("pair", _pairs(), ids=lambda p: str(p.get("pair")))
def test_live_invoice_extraction_matches_answer_key(
    extractor: ClaudeExtractor, pair: dict[str, Any]
) -> None:
    expected = pair["invoice"]

    result = extractor.extract(_pdf(pair, expected).read_bytes(), "application/pdf")

    assert result.status is StageStatus.OK, result.detail
    actual = result.value
    assert isinstance(actual, InvoiceExtraction)
    problems = [
        f"invoice.{key}: {getattr(actual, attr)!r} != {expected[key]!r}"
        for key, attr in INVOICE_HEADER_MAP.items()
        if _squash(getattr(actual, attr)) != _squash(expected[key])
    ]
    problems += _line_mismatches(actual.line_items, expected["lines"], "invoice")
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("pair", _pairs(), ids=lambda p: str(p.get("pair")))
def test_live_tig_extraction_matches_answer_key(
    extractor: ClaudeExtractor, pair: dict[str, Any]
) -> None:
    expected = pair["tig"]

    result = extractor.extract(
        _pdf(pair, expected).read_bytes(), "application/pdf", kind=DocumentKind.TIG
    )

    assert result.status is StageStatus.OK, result.detail
    assert result.value is not None
    problems = _line_mismatches(result.value.line_items, expected["lines"], "tig")
    assert not problems, "\n".join(problems)


def test_answer_key_amounts_parse_like_the_pipeline() -> None:
    # Sanity check that the answer key's normalised values agree with its raw strings,
    # so a mismatch above is the model's, not the key's.
    for pair in _pairs():
        for line in pair["invoice"]["lines"] + pair["tig"]["lines"]:
            assert parse_quantity(line["quantity_raw"]) == _decimal(line["quantity"])
            iso = "HUF" if "Ft" in line["net_raw"] else None
            assert normalize_amount(line["net_raw"], currency_iso=iso) == _decimal(line["net"])
