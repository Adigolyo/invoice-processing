"""Task 23: what one pipeline run writes to Cloud Logging.

The orchestrator runs against the in-memory fakes and its records are formatted through
the production ``StructuredFormatter``, so the assertions hold for the emitted JSON.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest

from intake.observability.logging import StructuredFormatter, run_scope
from intake.pipeline.orchestrator import CandidateStatus
from tests.unit.pipeline.fakes import invoice_extraction, ok, tig_extraction
from tests.unit.pipeline.test_orchestrator import (
    INVOICE_PDF,
    INVOICE_PDF_2,
    Harness,
    _http_error,
)

# Content of the fake invoice/TIG that must never appear in any emitted line.
FINANCIAL = (
    "100 000",
    "100000",
    "80 000",
    "127 000",
    "127000",
    "INV-1",
    "Kővári",
    "KOVARI",
    "Fejlesztés",
    "billing@contractor.example",
)


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def capture() -> Iterator[_Capture]:
    handler = _Capture()
    logger = logging.getLogger("intake")
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    yield handler
    logger.removeHandler(handler)
    logger.setLevel(previous)


def _emitted(capture: _Capture) -> list[dict[str, Any]]:
    formatter = StructuredFormatter(project_id="p", service="kibit-intake-staging")
    lines = [formatter.format(r) for r in capture.records]
    return [json.loads(line) for line in lines]


def _all_text(capture: _Capture) -> str:
    formatter = StructuredFormatter(project_id="p", service="kibit-intake-staging")
    return "\n".join(formatter.format(r) for r in capture.records)


def _events(payloads: list[dict[str, Any]], event: str) -> list[dict[str, Any]]:
    return [p for p in payloads if p.get("event") == event]


def test_one_run_summary_with_counts_per_outcome(capture: _Capture) -> None:
    h = Harness()
    h.direct_invoice("m1")
    h.direct_invoice("m2", content=INVOICE_PDF_2, answer=RuntimeError(f"boom {FINANCIAL[0]}"))
    h.tig_thread(with_tig=False)  # no TIG in the thread -> processed

    h.run()

    payloads = _emitted(capture)
    (summary,) = _events(payloads, "run_summary")
    assert summary["severity"] == "INFO"
    assert {
        k: summary[k]
        for k in (
            "candidates",
            "processed",
            "pending",
            "needs_review",
            "awaiting_tig",
            "errors",
            "skipped",
        )
    } == {
        "candidates": 3,
        "processed": 2,
        "pending": 0,
        "needs_review": 0,
        "awaiting_tig": 0,
        "errors": 1,
        "skipped": 0,
    }
    (started,) = _events(payloads, "run_started")
    assert started["candidates"] == 3


def test_every_record_of_a_run_carries_the_same_run_id(capture: _Capture) -> None:
    h = Harness()
    h.direct_invoice("m1")

    h.run()

    payloads = _emitted(capture)
    run_ids = {p.get("run_id") for p in payloads}
    assert len(run_ids) == 1 and None not in run_ids


def test_an_outer_run_scope_is_reused(capture: _Capture) -> None:
    h = Harness()
    h.direct_invoice("m1")

    with run_scope("outer-run-1"):
        h.run()

    assert {p["run_id"] for p in _emitted(capture)} == {"outer-run-1"}


def test_one_candidate_outcome_record_per_candidate(capture: _Capture) -> None:
    h = Harness()
    h.direct_invoice("m1")
    h.tig_thread(with_tig=False)

    summary = h.run()

    outcomes = _events(_emitted(capture), "candidate_outcome")
    assert [(o["message_id"], o["outcome"]) for o in outcomes] == [
        (r.message_id, r.status.value) for r in summary.results
    ]
    for record in outcomes:
        assert record["severity"] == "INFO"
        assert isinstance(record["duration_ms"], int)
        assert record["reason"]


def test_error_outcome_is_logged_at_error_with_type_and_stack_only(capture: _Capture) -> None:
    h = Harness()
    h.direct_invoice("m1", answer=RuntimeError("Kővári Kft owes 127 000 Ft"))

    summary = h.run()

    assert summary.results[0].status is CandidateStatus.ERROR
    payloads = _emitted(capture)
    errors = [p for p in payloads if p["severity"] == "ERROR"]
    assert len(errors) == 1, "exactly one ERROR record per failed candidate"
    (error,) = errors
    assert error["event"] == "candidate_outcome"
    assert error["outcome"] == "error"
    assert error["reason"] == "RuntimeError"
    assert error["error_type"] == "RuntimeError"
    assert error["stage"] == "extract"
    assert error["message_id"] == "m1"
    assert error["@type"].endswith("ReportedErrorEvent")
    assert error["stack_trace"].rstrip().endswith("RuntimeError")
    text = _all_text(capture)
    for secret in (*FINANCIAL, "owes"):
        assert secret not in text


def test_failed_stage_record_is_a_warning(capture: _Capture) -> None:
    h = Harness()
    h.direct_invoice("m1")
    h.gmail.fail["apply_label"] = _http_error()

    h.run()

    stage = [p for p in _emitted(capture) if p.get("stage") == "finalise" and "event" not in p]
    assert [(p["outcome"], p["severity"], p["reason"]) for p in stage] == [
        ("error", "WARNING", "HttpError")
    ]
    (error,) = [p for p in _emitted(capture) if p["severity"] == "ERROR"]
    assert error["stage"] == "finalise"


def test_error_outside_any_stage_is_attributed_to_evaluate(
    capture: _Capture, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness()
    h.direct_invoice("m1")

    def explode(*_: object, **__: object) -> bool:
        raise KeyError("Kővári")

    monkeypatch.setattr("intake.pipeline.orchestrator.needs_evaluation", explode)
    h.run()

    (error,) = [p for p in _emitted(capture) if p["severity"] == "ERROR"]
    assert error["stage"] == "evaluate" and error["error_type"] == "KeyError"
    assert "Kővári" not in _all_text(capture)


def test_full_tig_run_emits_no_financial_content_in_any_record(capture: _Capture) -> None:
    h = Harness()
    h.tig_thread(tig=ok(tig_extraction(quantity="8", total="80 000 Ft")))

    h.run()

    payloads = _emitted(capture)
    stages = {p["stage"] for p in payloads if "stage" in p}
    assert stages >= {"route", "attachments", "extract", "normalise", "reconcile", "file"}
    for payload in payloads:
        assert "redacted_fields" not in payload, payload
    text = _all_text(capture)
    for secret in FINANCIAL:
        assert secret not in text


def test_duplicate_detection_does_not_log_the_provider(capture: _Capture) -> None:
    h = Harness()
    h.direct_invoice("m1")
    h.direct_invoice("m2", content=INVOICE_PDF_2)
    h.extractor.answers[INVOICE_PDF_2] = ok(invoice_extraction())

    h.run()

    raw = " ".join(str(r.__dict__) for r in capture.records)
    assert "Kővári" not in raw
    assert all("redacted_fields" not in p for p in _emitted(capture))


def test_records_carry_no_unexpected_fields_on_success(capture: _Capture) -> None:
    h = Harness()
    h.direct_invoice("m1", content=INVOICE_PDF)

    h.run()

    for payload in _emitted(capture):
        assert "redacted_fields" not in payload, payload
