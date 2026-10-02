"""The E2E suite's live-environment checks: local token or Secret Manager, fail-closed gate."""

from __future__ import annotations

from pathlib import Path

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
