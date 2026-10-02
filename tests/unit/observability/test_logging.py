"""Task 23: structured logging for Cloud Logging, Error Reporting fields and redaction.

Every test formats real ``logging.LogRecord`` objects through the project's formatters
and inspects the emitted text, so what is asserted is exactly what reaches Cloud Logging.
"""

from __future__ import annotations

import io
import json
import logging
import sys
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest

from intake.observability import logging as obs
from intake.observability.logging import (
    ALLOWED_FIELDS,
    ERROR_EVENT_TYPE,
    ReadableFormatter,
    StructuredFormatter,
    configure_logging,
    current_run_id,
    is_cloud_run,
    parse_trace_context,
    run_scope,
    safe_stack_trace,
    trace_scope,
)

# Values that must never reach a log line: an amount, a supplier name, document text,
# an e-mail address, an API key and an OAuth refresh token.
SUPPLIER = "Kővári Kft"
AMOUNT = "127 000 Ft"
SECRETS = (
    SUPPLIER,
    AMOUNT,
    "127000",
    "Fejlesztés 8 óra",
    "billing@contractor.example",
    "sk-ant-api03-SECRET",
    "1//refresh-SECRET",
)


def _record(
    msg: str = "stage done",
    *,
    level: int = logging.INFO,
    exc_info: Any = None,
    name: str = "intake.pipeline.orchestrator",
    **extra: Any,
) -> logging.LogRecord:
    record = logging.getLogger(name).makeRecord(
        name, level, __file__, 42, msg, (), exc_info, func="some_function", extra=extra or None
    )
    return record


def _json(record: logging.LogRecord, **kwargs: Any) -> dict[str, Any]:
    line = StructuredFormatter(**kwargs).format(record)
    assert "\n" not in line, "one JSON object per line"
    parsed: dict[str, Any] = json.loads(line)
    return parsed


def _raise_with_content() -> None:
    supplier, amount = SUPPLIER, Decimal("127000")  # locals must never be serialised
    raise ValueError(f"cannot book {supplier}: net {amount} ({AMOUNT})")


def _exc_info() -> Any:
    try:
        _raise_with_content()
    except ValueError:
        return sys.exc_info()
    raise AssertionError("unreachable")


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    root = logging.getLogger()
    intake = logging.getLogger("intake")
    saved = (list(root.handlers), root.level, intake.level)
    yield
    root.handlers[:] = saved[0]
    root.setLevel(saved[1])
    intake.setLevel(saved[2])


# --- JSON shape ---------------------------------------------------------------------------


def test_json_record_has_cloud_logging_special_fields() -> None:
    record = _record(
        "stage=route outcome=ok",
        stage="route",
        outcome="ok",
        message_id="18c2f0a1b2c3d4e5",
        duration_ms=12,
        reason=None,
    )

    payload = _json(record)

    assert payload["severity"] == "INFO"
    assert payload["message"] == "stage=route outcome=ok"
    assert payload["time"].endswith("Z") and "T" in payload["time"]
    assert payload["logger"] == "intake.pipeline.orchestrator"
    assert payload["stage"] == "route"
    assert payload["outcome"] == "ok"
    assert payload["message_id"] == "18c2f0a1b2c3d4e5"
    assert payload["duration_ms"] == 12
    assert "reason" not in payload  # None values are omitted
    location = payload["logging.googleapis.com/sourceLocation"]
    assert location["line"] == "42" and location["function"] == "some_function"


@pytest.mark.parametrize(
    ("level", "severity"),
    [
        (logging.DEBUG, "DEBUG"),
        (logging.INFO, "INFO"),
        (logging.WARNING, "WARNING"),
        (logging.ERROR, "ERROR"),
        (logging.CRITICAL, "CRITICAL"),
        (5, "DEFAULT"),
    ],
)
def test_severity_mapping(level: int, severity: str) -> None:
    assert _json(_record(level=level))["severity"] == severity


def test_run_summary_counts_are_top_level_integers() -> None:
    record = _record(
        "pipeline run finished",
        event="run_summary",
        candidates=4,
        processed=1,
        pending=1,
        needs_review=0,
        awaiting_tig=1,
        errors=1,
        skipped=0,
    )

    payload = _json(record)

    assert payload["event"] == "run_summary"
    assert {k: payload[k] for k in ("processed", "pending", "awaiting_tig", "errors")} == {
        "processed": 1,
        "pending": 1,
        "awaiting_tig": 1,
        "errors": 1,
    }
    assert payload["needs_review"] == 0 and payload["skipped"] == 0


# --- redaction ----------------------------------------------------------------------------


def test_non_allowlisted_extras_are_dropped_and_named() -> None:
    record = _record(
        provider=SUPPLIER,
        net=Decimal("127000"),
        body="Fejlesztés 8 óra",
        sender="billing@contractor.example",
        api_key="sk-ant-api03-SECRET",
        refresh_token="1//refresh-SECRET",
        stage="book",
    )

    line = StructuredFormatter().format(record)
    payload = json.loads(line)

    for secret in SECRETS:
        assert secret not in line
    assert payload["stage"] == "book"
    assert payload["redacted_fields"] == sorted(
        ["api_key", "body", "net", "provider", "refresh_token", "sender"]
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("reason", AMOUNT),  # free text with spaces is never a code
        ("reason", SUPPLIER),
        ("message_id", "billing@contractor.example"),
        ("duration_ms", Decimal("127000")),  # only plain ints for numeric fields
        ("duration_ms", "127000"),
        ("duration_ms", True),
        ("stage", ["a", "b"]),
        ("reason", "x" * 300),
    ],
)
def test_allowlisted_field_with_an_unsafe_value_is_dropped(field: str, value: Any) -> None:
    line = StructuredFormatter().format(_record(**{field: value}))
    payload = json.loads(line)

    assert field not in payload
    assert payload["redacted_fields"] == [field]
    assert str(value) not in line


def test_allowlist_covers_the_documented_schema() -> None:
    for name in (
        "event",
        "stage",
        "outcome",
        "reason",
        "message_id",
        "run_id",
        "duration_ms",
        "error_type",
        "processed",
        "pending",
        "needs_review",
        "awaiting_tig",
        "errors",
        "skipped",
        "candidates",
        "response_sha256",
    ):
        assert name in ALLOWED_FIELDS
    for forbidden in ("provider", "supplier", "net", "gross", "amount", "subject", "detail"):
        assert forbidden not in ALLOWED_FIELDS


# --- exceptions -----------------------------------------------------------------------------


def test_exception_message_and_locals_never_leak_into_error_records() -> None:
    record = _record("candidate aborted", level=logging.ERROR, exc_info=_exc_info())

    line = StructuredFormatter(service="kibit-intake-staging", version="rev-7").format(record)
    payload = json.loads(line)

    for secret in (*SECRETS, "cannot book"):
        assert secret not in line
    assert payload["error_type"] == "ValueError"
    stack = payload["stack_trace"]
    assert stack.startswith("Traceback (most recent call last):\n")
    assert "_raise_with_content" in stack  # frames are kept ...
    assert stack.rstrip().endswith("ValueError")  # ... the message is not


def test_error_records_with_a_stack_carry_error_reporting_fields() -> None:
    record = _record("run failed", level=logging.ERROR, exc_info=_exc_info())

    payload = _json(record, service="kibit-intake-staging", version="kibit-intake-00007")

    assert payload["@type"] == ERROR_EVENT_TYPE
    assert ERROR_EVENT_TYPE == (
        "type.googleapis.com/google.devtools.clouderrorreporting.v1beta1.ReportedErrorEvent"
    )
    assert payload["serviceContext"] == {
        "service": "kibit-intake-staging",
        "version": "kibit-intake-00007",
    }
    assert payload["severity"] == "ERROR"


def test_warning_with_exception_has_type_but_is_not_an_error_report() -> None:
    record = _record("retrying", level=logging.WARNING, exc_info=_exc_info())

    payload = _json(record)

    assert payload["error_type"] == "ValueError"
    assert "stack_trace" not in payload
    assert "@type" not in payload


def test_error_without_exception_is_not_forced_into_error_reporting() -> None:
    payload = _json(_record("misconfigured", level=logging.ERROR))
    assert "@type" not in payload and "stack_trace" not in payload


def test_chained_exceptions_keep_every_type_and_no_message() -> None:
    try:
        try:
            _raise_with_content()
        except ValueError as exc:
            raise RuntimeError(f"wrapped {AMOUNT}") from exc
    except RuntimeError as outer:
        stack = safe_stack_trace(outer)

    assert "ValueError" in stack and "RuntimeError" in stack
    assert "direct cause" in stack
    for secret in (*SECRETS, "wrapped", "cannot book"):
        assert secret not in stack


def test_implicit_context_is_reported_without_messages() -> None:
    try:
        try:
            _raise_with_content()
        except ValueError:
            raise KeyError(SUPPLIER)  # noqa: B904 - implicit context on purpose
    except KeyError as outer:
        stack = safe_stack_trace(outer)

    assert "During handling of the above exception" in stack
    assert SUPPLIER not in stack


def test_exception_without_traceback_still_reports_its_type() -> None:
    stack = safe_stack_trace(ValueError(AMOUNT))
    assert stack.strip().endswith("ValueError")
    assert AMOUNT not in stack


def test_non_builtin_exception_type_is_module_qualified() -> None:
    class LedgerError(Exception):
        pass

    stack = safe_stack_trace(LedgerError(SUPPLIER))
    assert stack.strip().endswith(f"{__name__}.LedgerError")


def test_message_args_are_rendered_but_exception_text_is_not_appended() -> None:
    record = _record("candidate aborted (%s)", level=logging.ERROR, exc_info=_exc_info())
    record.args = ("ValueError",)

    line = StructuredFormatter().format(record)

    assert json.loads(line)["message"] == "candidate aborted (ValueError)"
    assert "cannot book" not in line


# --- run and trace correlation --------------------------------------------------------------


def test_run_scope_sets_a_run_id_that_every_record_carries() -> None:
    assert current_run_id() is None
    with run_scope() as run_id:
        assert current_run_id() == run_id
        assert len(run_id) == 32 and int(run_id, 16) >= 0
        assert _json(_record())["run_id"] == run_id
        with run_scope() as nested:
            assert nested == run_id  # nested scopes join the outer run
    assert current_run_id() is None
    assert "run_id" not in _json(_record())


def test_run_scope_accepts_an_explicit_id() -> None:
    with run_scope("abc123") as run_id:
        assert run_id == "abc123"


@pytest.mark.parametrize(
    ("header", "traceparent", "expected"),
    [
        (
            "105445aa7843bc8bf206b12000100000/1;o=1",
            None,
            ("105445aa7843bc8bf206b12000100000", "1", True),
        ),
        (
            "105445aa7843bc8bf206b12000100000/77;o=0",
            None,
            ("105445aa7843bc8bf206b12000100000", "77", False),
        ),
        (
            "105445aa7843bc8bf206b12000100000",
            None,
            ("105445aa7843bc8bf206b12000100000", None, False),
        ),
        (
            None,
            "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
            ("4bf92f3577b34da6a3ce929d0e0e4736", "00f067aa0ba902b7", True),
        ),
        (None, None, None),
        ("not a trace", None, None),
        (None, "garbage", None),
    ],
)
def test_parse_trace_context(
    header: str | None, traceparent: str | None, expected: tuple[str, str | None, bool] | None
) -> None:
    assert parse_trace_context(header, traceparent) == expected


def test_trace_fields_only_when_a_trace_header_exists() -> None:
    assert "logging.googleapis.com/trace" not in _json(_record(), project_id="kibit-proj")

    with trace_scope("105445aa7843bc8bf206b12000100000/1;o=1"):
        payload = _json(_record(), project_id="kibit-proj")
        no_project = _json(_record())

    assert payload["logging.googleapis.com/trace"] == (
        "projects/kibit-proj/traces/105445aa7843bc8bf206b12000100000"
    )
    assert payload["logging.googleapis.com/spanId"] == "1"
    assert payload["logging.googleapis.com/trace_sampled"] is True
    assert "logging.googleapis.com/trace" not in no_project  # needs the project ID


# --- readable (local) format ----------------------------------------------------------------


def test_readable_format_shows_fields_and_redacts_like_json() -> None:
    record = _record(
        "stage=book outcome=error",
        level=logging.ERROR,
        exc_info=_exc_info(),
        stage="book",
        provider=SUPPLIER,
    )

    text = ReadableFormatter().format(record)

    assert "ERROR intake.pipeline.orchestrator: stage=book outcome=error" in text
    assert "stage=book" in text and "error_type=ValueError" in text
    assert "Traceback (most recent call last):" in text
    for secret in (*SECRETS, "cannot book"):
        assert secret not in text


# --- configure_logging ----------------------------------------------------------------------


def test_is_cloud_run_detects_k_service() -> None:
    assert is_cloud_run({"K_SERVICE": "kibit-intake-staging"}) is True
    assert is_cloud_run({"K_SERVICE": ""}) is False
    assert is_cloud_run({}) is False


def test_configure_logging_on_cloud_run_emits_json() -> None:
    stream = io.StringIO()
    env = {
        "K_SERVICE": "kibit-intake-staging",
        "K_REVISION": "kibit-intake-staging-00003",
        "GCP_PROJECT_ID": "kibit-proj",
    }

    handler = configure_logging(env=env, stream=stream)
    logging.getLogger("intake.test").info("hello", extra={"stage": "route"})
    try:
        _raise_with_content()
    except ValueError:
        logging.getLogger("intake.test").exception("boom")

    lines = stream.getvalue().splitlines()
    first, second = (json.loads(line) for line in lines)
    assert isinstance(handler.formatter, StructuredFormatter)
    assert first["message"] == "hello" and first["stage"] == "route"
    assert second["serviceContext"]["service"] == "kibit-intake-staging"
    assert second["serviceContext"]["version"] == "kibit-intake-staging-00003"
    assert "cannot book" not in stream.getvalue()


def test_configure_logging_locally_is_human_readable() -> None:
    stream = io.StringIO()

    handler = configure_logging(env={}, stream=stream)
    logging.getLogger("intake.test").info("hello", extra={"stage": "route"})

    assert isinstance(handler.formatter, ReadableFormatter)
    out = stream.getvalue()
    assert "INFO intake.test: hello" in out and "stage=route" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_configure_logging_explicit_flag_overrides_detection() -> None:
    stream = io.StringIO()
    configure_logging(json=True, env={}, stream=stream)
    logging.getLogger("intake.test").info("hello")
    assert json.loads(stream.getvalue())["message"] == "hello"


def test_configure_logging_is_idempotent_and_keeps_foreign_handlers() -> None:
    foreign = logging.NullHandler()
    logging.getLogger().addHandler(foreign)

    configure_logging(env={}, stream=io.StringIO())
    configure_logging(env={}, stream=io.StringIO())

    ours = [h for h in logging.getLogger().handlers if getattr(h, obs.HANDLER_MARK, False)]
    assert len(ours) == 1
    assert foreign in logging.getLogger().handlers
    assert logging.getLogger("intake").level == logging.INFO


def test_configure_logging_honours_log_level() -> None:
    configure_logging(env={"LOG_LEVEL": "DEBUG"}, stream=io.StringIO())
    assert logging.getLogger("intake").level == logging.DEBUG
    configure_logging(env={"LOG_LEVEL": "nonsense"}, stream=io.StringIO())
    assert logging.getLogger("intake").level == logging.INFO
