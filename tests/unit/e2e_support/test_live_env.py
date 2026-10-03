"""The E2E suite's live-environment checks: local token or Secret Manager, fail-closed gate."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.e2e.fixture_set import workspace

DEPLOYED = {"GCP_PROJECT_ID": "kibit-invoice-intake", "ENVIRONMENT": "staging"}


def _token(tmp_path: Path) -> Path:
    (tmp_path / "token.json").write_text("{}", encoding="utf-8")
    return tmp_path


def test_a_local_token_file_wins(tmp_path: Path) -> None:
    assert workspace.credential_source(DEPLOYED, _token(tmp_path)) == "token"


def test_ci_reads_credentials_from_secret_manager(tmp_path: Path) -> None:
    assert workspace.credential_source(DEPLOYED, tmp_path) == "secret_manager"


@pytest.mark.parametrize("env", [{}, {"GCP_PROJECT_ID": "p"}, {"ENVIRONMENT": "staging"}])
def test_no_credentials_without_token_or_project_and_environment(
    tmp_path: Path, env: dict[str, str]
) -> None:
    assert workspace.credential_source(env, tmp_path) is None


@pytest.mark.parametrize(
    ("env", "available"),
    [
        ({"AI_COMPASS_API_KEY": "k"}, True),
        (DEPLOYED, True),  # read from Secret Manager by the extractor
        ({"AI_COMPASS_API_KEY": "  "}, False),
        ({}, False),
    ],
)
def test_ai_compass_key_availability(env: dict[str, str], available: bool) -> None:
    assert workspace.ai_compass_key_available(env) is available


def test_unavailable_skips_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KIBIT_E2E_REQUIRED", raising=False)
    with pytest.raises(pytest.skip.Exception, match="no credentials"):
        workspace.unavailable("no credentials")


def test_unavailable_fails_when_the_suite_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    # The release gate sets KIBIT_E2E_REQUIRED=1: a skipped suite must not pass the gate.
    monkeypatch.setenv("KIBIT_E2E_REQUIRED", "1")
    with pytest.raises(pytest.fail.Exception, match="no credentials"):
        workspace.unavailable("no credentials")


# --- Gmail rate-limit retry around a pipeline run ----------------------------------------


def _http_error(status: int, reason: str = "") -> Exception:
    import httplib2
    from googleapiclient.errors import HttpError

    body = f'{{"error": {{"errors": [{{"reason": "{reason}"}}]}}}}'.encode()
    return HttpError(httplib2.Response({"status": status}), body)


def test_a_rate_limited_run_is_retried_after_a_wait() -> None:
    attempts: list[int] = []
    waits: list[float] = []

    def run() -> str:
        attempts.append(1)
        if len(attempts) < 3:
            raise _http_error(429)
        return "summary"

    result = workspace.retry_rate_limited(run, attempts=4, wait_s=30, sleep=waits.append)

    assert result == "summary"
    assert len(attempts) == 3
    assert waits == [30, 30]


def test_403_rate_limit_exceeded_is_retried_too() -> None:
    calls: list[int] = []

    def run() -> str:
        calls.append(1)
        if len(calls) == 1:
            raise _http_error(403, "rateLimitExceeded")
        return "ok"

    assert workspace.retry_rate_limited(run, attempts=2, wait_s=1, sleep=lambda s: None) == "ok"


def test_rate_limit_retries_are_bounded() -> None:
    def run() -> str:
        raise _http_error(429)

    with pytest.raises(Exception, match="429"):
        workspace.retry_rate_limited(run, attempts=2, wait_s=1, sleep=lambda s: None)


def test_other_errors_are_not_retried() -> None:
    calls: list[int] = []

    def run() -> str:
        calls.append(1)
        raise _http_error(403, "forbidden")

    with pytest.raises(Exception, match="403"):
        workspace.retry_rate_limited(run, attempts=3, wait_s=1, sleep=lambda s: None)
    assert len(calls) == 1


# --- cleanup of a passed run's ledger and folder ----------------------------------------


def _services() -> tuple[Any, Any]:
    from unittest.mock import MagicMock

    drive = MagicMock()
    return workspace.Services(gmail=MagicMock(), drive=drive, sheets=MagicMock()), drive


def test_trash_run_artifacts_moves_ledger_and_folder_to_the_trash() -> None:
    services, drive = _services()

    workspace.trash_run_artifacts(services, spreadsheet_id="sheet-1", folder_id="folder-1")

    calls = [c.kwargs for c in drive.files.return_value.update.call_args_list]
    assert calls == [
        {"fileId": "sheet-1", "body": {"trashed": True}, "supportsAllDrives": True},
        {"fileId": "folder-1", "body": {"trashed": True}, "supportsAllDrives": True},
    ]


@pytest.mark.parametrize(
    ("failed", "keep", "expected"),
    [(0, None, True), (1, None, False), (0, "1", False), (2, "1", False)],
)
def test_cleanup_only_after_a_fully_passed_run(
    monkeypatch: pytest.MonkeyPatch, failed: int, keep: str | None, expected: bool
) -> None:
    if keep is None:
        monkeypatch.delenv("KIBIT_E2E_KEEP", raising=False)
    else:
        monkeypatch.setenv("KIBIT_E2E_KEEP", keep)
    assert workspace.should_clean_up(tests_failed=failed) is expected
