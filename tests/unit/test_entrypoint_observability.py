"""Task 23: ``POST /run`` and ``python -m intake`` log run failures for Error Reporting.

A failed run emits one ERROR record ``event=run_failed`` with the error type and a
message-free stack trace; the run's records share one ``run_id`` and, on Cloud Run, the
request's trace.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from intake import __main__ as cli
from intake import app as app_module
from intake.observability.logging import StructuredFormatter, current_run_id
from intake.pipeline.orchestrator import RunSummary
from tests.unit.test_app import ENV, VALID_TOKEN, FakeRunner, FakeVerifier
from tests.unit.test_main import _secrets

LEAK = "Ledger header is wrong: Kővári Kft 127 000 Ft"


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def payloads(self) -> list[dict[str, Any]]:
        formatter = StructuredFormatter(project_id="kibit-proj", service="svc")
        return [json.loads(formatter.format(r)) for r in self.records]


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


def _post(runner: Any, headers: Mapping[str, str] | None = None) -> Any:
    client = app_module.create_app(verifier=FakeVerifier(), env=ENV, runner=runner).test_client()
    return client.post(
        "/run", headers={"Authorization": f"Bearer {VALID_TOKEN}", **(headers or {})}
    )


def test_failed_run_logs_run_failed_at_error_without_the_message(capture: _Capture) -> None:
    response = _post(FakeRunner(error=RuntimeError(LEAK)))

    assert response.status_code == 500
    (failed,) = [p for p in capture.payloads() if p.get("event") == "run_failed"]
    assert failed["severity"] == "ERROR"
    assert failed["error_type"] == "RuntimeError"
    assert failed["@type"].endswith("ReportedErrorEvent")
    assert failed["stack_trace"].rstrip().endswith("RuntimeError")
    assert failed["run_id"]
    text = json.dumps(capture.payloads(), ensure_ascii=False)
    for secret in ("Kővári", "127 000", "header is wrong"):
        assert secret not in text


def test_successful_run_shares_one_run_id_and_the_request_trace(capture: _Capture) -> None:
    seen: list[str | None] = []

    def runner() -> RunSummary:
        seen.append(current_run_id())
        logging.getLogger("intake.test").info("inside the run")
        return RunSummary(())

    response = _post(
        runner, headers={"X-Cloud-Trace-Context": "105445aa7843bc8bf206b12000100000/1;o=1"}
    )

    assert response.status_code == 200
    payloads = capture.payloads()
    assert seen[0] is not None
    assert {p["run_id"] for p in payloads} == {seen[0]}
    inside = next(p for p in payloads if p["message"] == "inside the run")
    assert inside["logging.googleapis.com/trace"] == (
        "projects/kibit-proj/traces/105445aa7843bc8bf206b12000100000"
    )
    completed = [p for p in payloads if p.get("event") == "run_completed"]
    assert len(completed) == 1 and completed[0]["severity"] == "INFO"
    assert current_run_id() is None


def test_unauthorized_run_logs_no_run_records(capture: _Capture) -> None:
    client = app_module.create_app(
        verifier=FakeVerifier(error=ValueError("bad")), env=ENV, runner=FakeRunner()
    ).test_client()
    client.post("/run", headers={"Authorization": f"Bearer {VALID_TOKEN}"})

    assert not [p for p in capture.payloads() if p.get("event") in {"run_failed", "run_completed"}]


def test_app_module_configures_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Mapping[str, str] | None] = []
    monkeypatch.setattr(app_module, "configure_logging", lambda **kw: calls.append(kw.get("env")))

    app_module.create_app(verifier=FakeVerifier(), env=ENV, runner=FakeRunner(), configure=True)
    app_module.create_app(verifier=FakeVerifier(), env=ENV, runner=FakeRunner())

    assert calls == [ENV]


def test_cli_failed_run_logs_run_failed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], capture: _Capture
) -> None:
    secrets = _secrets(tmp_path)

    def runner(credentials: Any, env: Mapping[str, str]) -> RunSummary:
        raise RuntimeError(LEAK)

    code = cli.main(
        ["--secrets-dir", str(secrets), "--env-file", str(tmp_path / "none")],
        runner=runner,
        environ={},
    )

    assert code == 1
    (failed,) = [p for p in capture.payloads() if p.get("event") == "run_failed"]
    assert failed["error_type"] == "RuntimeError" and failed["run_id"]
    assert "Kővári" not in json.dumps(capture.payloads(), ensure_ascii=False)
    assert "Kővári" not in capsys.readouterr().err


def test_cli_run_happens_inside_a_run_scope(tmp_path: Path) -> None:
    secrets = _secrets(tmp_path)
    seen: list[str | None] = []

    def runner(credentials: Any, env: Mapping[str, str]) -> RunSummary:
        seen.append(current_run_id())
        return RunSummary(())

    cli.main(
        ["--secrets-dir", str(secrets), "--env-file", str(tmp_path / "none")],
        runner=runner,
        environ={},
    )

    assert seen and seen[0] is not None
    assert current_run_id() is None


def test_cli_entrypoint_configures_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(cli, "configure_logging", lambda **kw: calls.append(kw))
    monkeypatch.setattr(cli, "main", lambda: 0)

    assert cli.run() == 0
    assert len(calls) == 1
