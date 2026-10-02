"""Task 21: the local one-cycle entrypoint (``python -m intake``); no API is called."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from intake import __main__ as cli
from intake.pipeline.orchestrator import CandidateResult, CandidateStatus, RunSummary

TOKEN = {
    "token": "access-xyz",
    "refresh_token": "refresh-SECRET",
    "client_id": "client-id.apps.googleusercontent.com",
    "client_secret": "client-SECRET",
    "token_uri": "https://oauth2.googleapis.com/token",
}


def _secrets(tmp_path: Path, token: bool = True) -> Path:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    if token:
        (secrets / "token.json").write_text(json.dumps(TOKEN), encoding="utf-8")
    return secrets


def test_load_env_file_parses_simple_dotenv(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "# comment\n\nLEDGER_SPREADSHEET_ID=abc\nexport GEMINI_MODEL = 'm'\n"
        'AI_COMPASS_BASE_URL="https://compass.example/v1"  # trailing\nBROKEN\n',
        encoding="utf-8",
    )

    assert cli.load_env_file(path) == {
        "LEDGER_SPREADSHEET_ID": "abc",
        "GEMINI_MODEL": "m",
        "AI_COMPASS_BASE_URL": "https://compass.example/v1",
    }


def test_load_env_file_missing_is_empty(tmp_path: Path) -> None:
    assert cli.load_env_file(tmp_path / "nope.env") == {}


def test_local_credentials_come_from_token_json(tmp_path: Path) -> None:
    creds = cli.local_credentials(_secrets(tmp_path))

    assert creds.refresh_token == "refresh-SECRET"


def test_main_runs_one_cycle_and_prints_only_the_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    secrets = _secrets(tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text("LEDGER_SPREADSHEET_ID=sheet-1\nGEMINI_API_KEY=key-SECRET\n")
    seen: dict[str, Any] = {}

    def runner(credentials: Any, env: Mapping[str, str]) -> RunSummary:
        seen.update(credentials=credentials, env=dict(env))
        return RunSummary((CandidateResult("m1", CandidateStatus.PROCESSED, "ok", "2610001AKFT"),))

    code = cli.main(
        ["--env-file", str(env_file), "--secrets-dir", str(secrets)],
        runner=runner,
        environ={"LEDGER_SPREADSHEET_ID": "from-process-env"},
    )

    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["summary"]["processed"] == 1
    assert out["candidates"] == [
        {
            "message_id": "m1",
            "status": "processed",
            "reason": "ok",
            "registry_number": "2610001AKFT",
        }
    ]
    # Real environment wins over .env; secrets are never printed.
    assert seen["env"]["LEDGER_SPREADSHEET_ID"] == "from-process-env"
    assert seen["env"]["GEMINI_API_KEY"] == "key-SECRET"
    assert seen["credentials"].refresh_token == "refresh-SECRET"


def test_main_never_prints_secrets(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    secrets = _secrets(tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text("GEMINI_API_KEY=key-SECRET\nAI_COMPASS_API_KEY=compass-SECRET\n")

    cli.main(
        ["--env-file", str(env_file), "--secrets-dir", str(secrets)],
        runner=lambda c, e: RunSummary(()),
        environ={},
    )

    captured = capsys.readouterr()
    assert "SECRET" not in captured.out + captured.err


def test_main_without_token_explains_how_to_authorize(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    secrets = _secrets(tmp_path, token=False)

    code = cli.main(
        ["--secrets-dir", str(secrets), "--env-file", str(tmp_path / "none")],
        runner=lambda c, e: RunSummary(()),
        environ={},
    )

    assert code == 2
    assert "--authorize" in capsys.readouterr().err


def test_main_reports_a_failed_run_without_details(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    secrets = _secrets(tmp_path)

    def runner(credentials: Any, env: Mapping[str, str]) -> RunSummary:
        raise RuntimeError("ledger header mismatch with value 12345")

    code = cli.main(
        ["--secrets-dir", str(secrets), "--env-file", str(tmp_path / "none")],
        runner=runner,
        environ={},
    )

    assert code == 1
    err = capsys.readouterr().err
    assert "RuntimeError" in err
    assert "12345" not in err


def test_authorize_runs_the_consent_flow_and_saves_the_token(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    secrets = _secrets(tmp_path, token=False)
    (secrets / "client_secret.json").write_text("{}")
    seen: dict[str, Any] = {}

    class FakeCreds:
        def to_json(self) -> str:
            return json.dumps(TOKEN)

    class FakeFlow:
        @classmethod
        def from_client_secrets_file(cls, path: str, scopes: list[str]) -> FakeFlow:
            seen.update(path=path, scopes=scopes)
            return cls()

        def run_local_server(self, **kwargs: Any) -> FakeCreds:
            seen.update(kwargs)
            return FakeCreds()

    code = cli.main(["--authorize", "--secrets-dir", str(secrets)], flow_class=FakeFlow, environ={})

    assert code == 0
    token = secrets / "token.json"
    assert json.loads(token.read_text())["refresh_token"] == "refresh-SECRET"
    assert token.stat().st_mode & 0o077 == 0
    assert seen["path"].endswith("client_secret.json")
    assert seen["access_type"] == "offline"
    assert "refresh-SECRET" not in capsys.readouterr().out


def test_authorize_without_client_secret_fails(tmp_path: Path) -> None:
    secrets = _secrets(tmp_path, token=False)

    assert cli.main(["--authorize", "--secrets-dir", str(secrets)], environ={}) == 2
