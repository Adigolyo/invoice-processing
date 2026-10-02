"""Structured logging for Cloud Logging and Cloud Error Reporting, with a redaction guard.

On Cloud Run (``K_SERVICE`` set) every record is written as **one JSON object per line**
to stderr, which Cloud Run's logging agent turns into a structured ``LogEntry``
(https://cloud.google.com/logging/docs/structured-logging): ``severity``, ``message`` and
``time`` become the entry's own fields, ``logging.googleapis.com/sourceLocation`` /
``trace`` / ``spanId`` / ``trace_sampled`` are lifted into the entry, and every other key
lands in ``jsonPayload``. Locally the same fields are rendered as one readable line.

Redaction guard (both formats):

- Only the field names in ``ALLOWED_FIELDS`` are ever serialised from a record's
  ``extra``. Any other name (``provider``, ``net``, ``body``, ``api_key``, ...) is dropped
  and only its *name* is listed in ``redacted_fields`` so the leak is visible and fixable.
- An allowlisted field must also hold a safe value: ``code`` fields a short token of
  ``[A-Za-z0-9_.:,/+=-]`` (no spaces, no ``@``: free text, amounts written with spaces,
  names and e-mail addresses fail), ``int`` fields a plain ``int``. Anything else is
  dropped the same way. ``None`` values are omitted.
- Exceptions are reduced to their type: ``error_type`` always, and on ERROR or worse a
  ``stack_trace`` in Python's traceback layout whose frames carry only file, line and
  function. Exception messages, source lines and local variables are never serialised,
  including those of chained causes. ``message`` is the rendered log message, which the
  codebase builds only from fixed text and codes.

Error Reporting: an ERROR (or CRITICAL) record carrying an exception gets ``stack_trace``
plus ``@type`` = ``ReportedErrorEvent`` and ``serviceContext`` (``K_SERVICE`` /
``K_REVISION``), the documented structured-log format
(https://cloud.google.com/error-reporting/docs/formatting-error-messages), so each one
becomes an Error Reporting event grouped by its stack.

Correlation: ``run_scope`` gives every record of one polling cycle the same ``run_id``;
``trace_scope`` adds ``logging.googleapis.com/trace`` only when the request carried an
``X-Cloud-Trace-Context`` or ``traceparent`` header (and the project ID is known).
"""

from __future__ import annotations

import contextvars
import json as jsonlib
import logging
import os
import re
import sys
import traceback
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Final, Literal, TextIO

ERROR_EVENT_TYPE: Final = (
    "type.googleapis.com/google.devtools.clouderrorreporting.v1beta1.ReportedErrorEvent"
)
DEFAULT_SERVICE: Final = "kibit-intake"
HANDLER_MARK: Final = "_kibit_structured_handler"

FieldKind = Literal["code", "int"]

ALLOWED_FIELDS: Final[Mapping[str, FieldKind]] = {
    # correlation and pipeline state
    "event": "code",
    "run_id": "code",
    "message_id": "code",
    "thread_id": "code",
    "stage": "code",
    "outcome": "code",
    "reason": "code",
    "duration_ms": "int",
    "error_type": "code",
    # run summary (counts per outcome) and run start
    "candidates": "int",
    "processed": "int",
    "pending": "int",
    "needs_review": "int",
    "awaiting_tig": "int",
    "errors": "int",
    "skipped": "int",
    # extraction call metadata (never the document or the response text)
    "document_kind": "code",
    "mime_type": "code",
    "size_bytes": "int",
    "model": "code",
    "effort": "code",
    "stop_reason": "code",
    "input_tokens": "int",
    "output_tokens": "int",
    "response_sha256": "code",
    "line_item_count": "int",
    "missing_fields": "code",
    # miscellaneous operational codes
    "environment": "code",
    "draft_status": "code",
    "config_keys": "int",
    "status_code": "int",
}

_CODE: Final = re.compile(r"[A-Za-z0-9_.:,/+=\-]{0,200}")
_SEVERITY: Final = {
    logging.DEBUG: "DEBUG",
    logging.INFO: "INFO",
    logging.WARNING: "WARNING",
    logging.ERROR: "ERROR",
    logging.CRITICAL: "CRITICAL",
}
# Attributes every LogRecord has; anything else on a record came from ``extra``.
_RESERVED: Final = frozenset(logging.makeLogRecord({}).__dict__) | {
    "message",
    "asctime",
    "_kibit_run_id",
    "_kibit_trace",
}

_CLOUD_TRACE: Final = re.compile(r"([0-9a-fA-F]{32})(?:/([0-9]+))?(?:;o=([01]))?")
_TRACEPARENT: Final = re.compile(r"[0-9a-f]{2}-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})")

TraceContext = tuple[str, str | None, bool]
"""``(trace_id, span_id as 16 hex digits or None, sampled)``."""

_run_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("run_id", default=None)
_trace: contextvars.ContextVar[TraceContext | None] = contextvars.ContextVar("trace", default=None)

# The run and trace are stamped on each record when it is *created* (not when a handler
# formats it), so a buffering or deferred handler still sees the right correlation.
_RUN_ATTR: Final = "_kibit_run_id"
_TRACE_ATTR: Final = "_kibit_trace"
_FACTORY_MARK: Final = "_kibit_correlating_factory"


def _install_record_factory() -> None:
    previous = logging.getLogRecordFactory()
    if getattr(previous, _FACTORY_MARK, False):
        return

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = previous(*args, **kwargs)
        record.__dict__[_RUN_ATTR] = _run_id.get()
        record.__dict__[_TRACE_ATTR] = _trace.get()
        return record

    setattr(factory, _FACTORY_MARK, True)
    logging.setLogRecordFactory(factory)


_install_record_factory()


# --- run and trace correlation --------------------------------------------------------------


def current_run_id() -> str | None:
    """The ``run_id`` of the polling cycle in progress, if any."""
    return _run_id.get()


@contextmanager
def run_scope(run_id: str | None = None) -> Iterator[str]:
    """Tag every record logged inside with one ``run_id``; nested scopes join the outer run."""
    active = _run_id.get()
    if active is not None:
        yield active
        return
    token = _run_id.set(run_id or uuid.uuid4().hex)
    try:
        yield _run_id.get() or ""
    finally:
        _run_id.reset(token)


def parse_trace_context(
    cloud_trace: str | None, traceparent: str | None = None
) -> TraceContext | None:
    """Parse ``X-Cloud-Trace-Context`` (preferred) or W3C ``traceparent``; None if absent."""
    if cloud_trace and (m := _CLOUD_TRACE.fullmatch(cloud_trace.strip())):
        trace_id, span, sampled = m.groups()
        span_hex = format(int(span), "016x") if span is not None else None
        return trace_id.lower(), span_hex, sampled == "1"
    if traceparent and (m := _TRACEPARENT.fullmatch(traceparent.strip())):
        trace_id, span_id, flags = m.groups()
        return trace_id, span_id, bool(int(flags, 16) & 1)
    return None


@contextmanager
def trace_scope(cloud_trace: str | None, traceparent: str | None = None) -> Iterator[None]:
    """Correlate records logged inside with the request's trace, when it has one."""
    token = _trace.set(parse_trace_context(cloud_trace, traceparent))
    try:
        yield
    finally:
        _trace.reset(token)


# --- redaction --------------------------------------------------------------------------------


def _is_safe(kind: FieldKind, value: object) -> bool:
    if kind == "int":
        return type(value) is int
    return isinstance(value, str) and _CODE.fullmatch(value) is not None


def safe_fields(record: logging.LogRecord) -> tuple[dict[str, object], list[str]]:
    """The record's allowlisted ``extra`` fields with safe values, and the dropped names."""
    fields: dict[str, object] = {}
    dropped: list[str] = []
    for name, value in record.__dict__.items():
        if name in _RESERVED or value is None:
            continue
        kind = ALLOWED_FIELDS.get(name)
        if kind is not None and _is_safe(kind, value):
            fields[name] = value
        else:
            dropped.append(name)
    return fields, sorted(dropped)


def _type_name(exc_type: type[BaseException]) -> str:
    module = exc_type.__module__
    if module in ("builtins", "__main__"):
        return exc_type.__qualname__
    return f"{module}.{exc_type.__qualname__}"


def _one_traceback(exc: BaseException) -> str:
    frames = traceback.StackSummary.extract(
        traceback.walk_tb(exc.__traceback__), lookup_lines=False, capture_locals=False
    )
    lines = [f'  File "{f.filename}", line {f.lineno}, in {f.name}\n' for f in frames]
    head = "Traceback (most recent call last):\n" if lines else ""
    return head + "".join(lines) + _type_name(type(exc)) + "\n"


_CAUSE: Final = "\nThe above exception was the direct cause of the following exception:\n\n"
_CONTEXT: Final = "\nDuring handling of the above exception, another exception occurred:\n\n"


def safe_stack_trace(exc: BaseException) -> str:
    """Python's traceback layout, chained exceptions included, with types and frames only."""
    chain: list[tuple[BaseException, str]] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    link = ""
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append((current, link))
        if current.__cause__ is not None:
            current, link = current.__cause__, _CAUSE
        elif current.__context__ is not None and not current.__suppress_context__:
            current, link = current.__context__, _CONTEXT
        else:
            current = None
    parts: list[str] = []
    for index in range(len(chain) - 1, -1, -1):
        error, relation = chain[index]
        parts.append(_one_traceback(error))
        if index > 0:
            parts.append(relation)
    return "".join(parts)


def _record_fields(record: logging.LogRecord, *, with_stack: bool) -> dict[str, object]:
    fields, dropped = safe_fields(record)
    run_id = record.__dict__[_RUN_ATTR] if _RUN_ATTR in record.__dict__ else _run_id.get()
    if isinstance(run_id, str) and "run_id" not in fields:
        fields["run_id"] = run_id
    exc = record.exc_info[1] if record.exc_info else None
    if exc is not None:
        fields["error_type"] = type(exc).__name__
        if with_stack:
            fields["stack_trace"] = safe_stack_trace(exc)
    if dropped:
        fields["redacted_fields"] = dropped
    return fields


def _severity(levelno: int) -> str:
    return _SEVERITY.get(levelno, "DEFAULT")


# --- formatters -------------------------------------------------------------------------------


class StructuredFormatter(logging.Formatter):
    """One Cloud Logging structured-log JSON object per record (see module docstring)."""

    def __init__(
        self,
        *,
        project_id: str | None = None,
        service: str | None = None,
        version: str | None = None,
    ) -> None:
        super().__init__()
        self._project_id = project_id or None
        self._service = service or DEFAULT_SERVICE
        self._version = version or None

    def format(self, record: logging.LogRecord) -> str:
        is_error = record.levelno >= logging.ERROR
        payload: dict[str, object] = {
            "severity": _severity(record.levelno),
            "message": record.getMessage(),
            "time": datetime.fromtimestamp(record.created, UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "logger": record.name,
        }
        fields = _record_fields(record, with_stack=is_error)
        payload.update(fields)
        if is_error and "stack_trace" in fields:
            context: dict[str, str] = {"service": self._service}
            if self._version:
                context["version"] = self._version
            payload["@type"] = ERROR_EVENT_TYPE
            payload["serviceContext"] = context
        payload["logging.googleapis.com/sourceLocation"] = {
            "file": record.pathname,
            "line": str(record.lineno),
            "function": record.funcName,
        }
        trace = record.__dict__[_TRACE_ATTR] if _TRACE_ATTR in record.__dict__ else _trace.get()
        if trace is not None and self._project_id:
            trace_id, span_id, sampled = trace
            payload["logging.googleapis.com/trace"] = (
                f"projects/{self._project_id}/traces/{trace_id}"
            )
            if span_id is not None:
                payload["logging.googleapis.com/spanId"] = span_id
            payload["logging.googleapis.com/trace_sampled"] = sampled
        return jsonlib.dumps(payload, ensure_ascii=False, default=str)


class ReadableFormatter(logging.Formatter):
    """Local, human-readable lines with the same fields and the same redaction."""

    def __init__(self) -> None:
        super().__init__(datefmt="%H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        fields = _record_fields(record, with_stack=True)
        stack = fields.pop("stack_trace", None)
        line = (
            f"{self.formatTime(record, self.datefmt)} {record.levelname} "
            f"{record.name}: {record.getMessage()}"
        )
        if fields:
            line += " [" + " ".join(f"{k}={v}" for k, v in fields.items()) + "]"
        if isinstance(stack, str):
            line += "\n" + stack.rstrip("\n")
        return line


# --- configuration ----------------------------------------------------------------------------


def is_cloud_run(env: Mapping[str, str]) -> bool:
    """Cloud Run sets ``K_SERVICE`` on every revision."""
    return bool(env.get("K_SERVICE", "").strip())


def _level(env: Mapping[str, str]) -> int:
    name = env.get("LOG_LEVEL", "").strip().upper()
    return logging.getLevelNamesMapping().get(name, logging.INFO) if name else logging.INFO


def configure_logging(
    *,
    json: bool | None = None,
    env: Mapping[str, str] | None = None,
    stream: TextIO | None = None,
) -> logging.Handler:
    """Install the one root handler: JSON on Cloud Run (or ``json=True``), readable otherwise.

    Idempotent: a handler installed by an earlier call is replaced, handlers installed by
    others (e.g. pytest's) are kept. Third-party loggers log at WARNING and above; the
    ``intake`` package at ``LOG_LEVEL`` (default INFO).
    """
    environ = os.environ if env is None else env
    use_json = is_cloud_run(environ) if json is None else json
    formatter: logging.Formatter
    if use_json:
        formatter = StructuredFormatter(
            project_id=environ.get("GCP_PROJECT_ID") or environ.get("GOOGLE_CLOUD_PROJECT"),
            service=environ.get("K_SERVICE"),
            version=environ.get("K_REVISION"),
        )
    else:
        formatter = ReadableFormatter()
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(formatter)
    setattr(handler, HANDLER_MARK, True)

    root = logging.getLogger()
    for existing in list(root.handlers):
        if getattr(existing, HANDLER_MARK, False):
            root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(logging.WARNING)
    logging.getLogger("intake").setLevel(_level(environ))
    return handler


__all__ = [
    "ALLOWED_FIELDS",
    "ERROR_EVENT_TYPE",
    "HANDLER_MARK",
    "ReadableFormatter",
    "StructuredFormatter",
    "configure_logging",
    "current_run_id",
    "is_cloud_run",
    "parse_trace_context",
    "run_scope",
    "safe_fields",
    "safe_stack_trace",
    "trace_scope",
]
