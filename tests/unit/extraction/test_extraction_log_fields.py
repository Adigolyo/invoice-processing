"""Task 23: extraction log records pass the structured-logging allowlist unchanged.

Free-text ``detail`` (which can quote model output, e.g. unexpected JSON keys) stays in
the ``StageResult`` for the orchestrator; the log carries fixed codes only.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from intake.observability.logging import StructuredFormatter
from tests.unit.extraction.backends import claude_message
from tests.unit.extraction.test_claude_extractor import INVOICE_A, OK_TEXT, PAYLOAD, _extractor


def _payloads(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    formatter = StructuredFormatter()
    return [json.loads(formatter.format(r)) for r in caplog.records if r.name.startswith("intake")]


def test_complete_extraction_record_is_fully_allowlisted(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    extractor, _ = _extractor(claude_message(OK_TEXT))

    extractor.extract(INVOICE_A.read_bytes(), "application/pdf")

    (record,) = _payloads(caplog)
    assert "redacted_fields" not in record
    assert record["outcome"] == "ok"
    assert record["mime_type"] == "application/pdf"
    assert record["response_sha256"]
    assert record["line_item_count"] == 1


def test_incomplete_extraction_logs_missing_field_names(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    extractor, _ = _extractor(claude_message(json.dumps({**PAYLOAD, "due_date": None, "issue_date": None})))

    extractor.extract(INVOICE_A.read_bytes(), "application/pdf")

    (record,) = _payloads(caplog)
    assert "redacted_fields" not in record
    assert record["outcome"] == "incomplete"
    assert record["missing_fields"] == "due_date"


def test_rejected_response_logs_a_code_not_model_text(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    bad = json.dumps({**PAYLOAD, "Kővári Kft 127 000 Ft": "x"}, ensure_ascii=False)
    extractor, _ = _extractor(claude_message(bad))

    result = extractor.extract(INVOICE_A.read_bytes(), "application/pdf")

    assert result.detail is not None  # the detail is still available to the caller
    (record,) = _payloads(caplog)
    assert "redacted_fields" not in record
    assert record["outcome"] == "invalid_response"
    assert record["reason"] == "invalid_response"
    assert "Kővári" not in json.dumps(record, ensure_ascii=False)
    assert all("Kővári" not in str(v) for r in caplog.records for v in r.__dict__.values())


def test_non_end_turn_stop_logs_reason_code(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    extractor, _ = _extractor(claude_message(OK_TEXT, stop_reason="max_tokens"))

    extractor.extract(INVOICE_A.read_bytes(), "application/pdf")

    (record,) = _payloads(caplog)
    assert "redacted_fields" not in record
    assert record["reason"] == "stop_reason"
    assert record["stop_reason"] == "max_tokens"
