"""Cloud Run HTTP entrypoint — /healthz and the OIDC-protected /run (Tasks 3, 21).

OIDC verification is mocked via the injectable ``verifier`` and the pipeline run via the
injectable ``runner``; no network calls.
"""

from collections.abc import Mapping
from typing import Any

import pytest
from flask.testing import FlaskClient
from google.auth.exceptions import TransportError

from intake import app as app_module
from intake.pipeline.orchestrator import CandidateResult, CandidateStatus, RunSummary

AUDIENCE = "https://kibit-intake-staging-abc123-ew.a.run.app"
SCHEDULER_SA = "kibit-scheduler-staging@kibit-invoice-intake.iam.gserviceaccount.com"
ENV = {"OIDC_AUDIENCE": AUDIENCE, "SCHEDULER_SERVICE_ACCOUNT_EMAIL": SCHEDULER_SA}
VALID_TOKEN = "valid.jwt.token"


class FakeVerifier:
    def __init__(self, claims: Mapping[str, Any] | None = None, error: Exception | None = None):
        self.claims = (
            claims if claims is not None else {"email": SCHEDULER_SA, "email_verified": True}
        )
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def __call__(self, token: str, audience: str) -> Mapping[str, Any]:
        self.calls.append((token, audience))
        if self.error is not None:
            raise self.error
        return self.claims


class FakeRunner:
    def __init__(self, summary: RunSummary | None = None, error: Exception | None = None):
        self.summary = summary or RunSummary(())
        self.error = error
        self.calls = 0

    def __call__(self) -> RunSummary:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.summary


def _client(
    verifier: FakeVerifier, env: Mapping[str, str] = ENV, runner: FakeRunner | None = None
) -> FlaskClient:
    return app_module.create_app(
        verifier=verifier, env=env, runner=runner or FakeRunner()
    ).test_client()


# --- /healthz ---------------------------------------------------------------------------


def test_healthz_returns_200_without_authentication() -> None:
    verifier = FakeVerifier()
    response = _client(verifier).get("/healthz")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}
    assert verifier.calls == []


def test_healthz_works_even_when_oidc_is_not_configured() -> None:
    response = _client(FakeVerifier(), env={}).get("/healthz")
    assert response.status_code == 200


# --- /run: rejection paths --------------------------------------------------------------


def test_run_without_authorization_header_is_401() -> None:
    verifier = FakeVerifier()
    response = _client(verifier).post("/run")
    assert response.status_code == 401
    assert verifier.calls == []


@pytest.mark.parametrize("header", ["Basic abc", "Bearer", "Bearer ", "bearer-token-only"])
def test_run_with_malformed_authorization_header_is_401(header: str) -> None:
    response = _client(FakeVerifier()).post("/run", headers={"Authorization": header})
    assert response.status_code == 401


@pytest.mark.parametrize(
    "error", [ValueError("Token expired"), TransportError("could not fetch certs")]
)
def test_run_with_token_failing_verification_is_401(error: Exception) -> None:
    response = _client(FakeVerifier(error=error)).post(
        "/run", headers={"Authorization": f"Bearer {VALID_TOKEN}"}
    )
    assert response.status_code == 401


def test_run_with_token_from_another_service_account_is_401() -> None:
    verifier = FakeVerifier(
        claims={"email": "attacker@evil.iam.gserviceaccount.com", "email_verified": True}
    )
    response = _client(verifier).post("/run", headers={"Authorization": f"Bearer {VALID_TOKEN}"})
    assert response.status_code == 401


@pytest.mark.parametrize("claims", [{"email": SCHEDULER_SA}, {"email_verified": True}, {}])
def test_run_with_unverified_or_missing_email_claim_is_401(claims: dict[str, Any]) -> None:
    response = _client(FakeVerifier(claims=claims)).post(
        "/run", headers={"Authorization": f"Bearer {VALID_TOKEN}"}
    )
    assert response.status_code == 401


@pytest.mark.parametrize(
    "env",
    [{}, {"OIDC_AUDIENCE": AUDIENCE}, {"SCHEDULER_SERVICE_ACCOUNT_EMAIL": SCHEDULER_SA}],
)
def test_run_fails_closed_when_oidc_settings_missing(env: dict[str, str]) -> None:
    verifier = FakeVerifier()
    response = _client(verifier, env=env).post(
        "/run", headers={"Authorization": f"Bearer {VALID_TOKEN}"}
    )
    assert response.status_code == 401
    assert verifier.calls == []


def test_run_rejects_get() -> None:
    response = _client(FakeVerifier()).get("/run")
    assert response.status_code == 405


# --- /run: accepted path ----------------------------------------------------------------


def test_run_with_valid_scheduler_token_runs_one_cycle_and_returns_its_summary() -> None:
    verifier = FakeVerifier()
    runner = FakeRunner(
        RunSummary(
            (
                CandidateResult("m1", CandidateStatus.PROCESSED, "ok"),
                CandidateResult("m2", CandidateStatus.PENDING, "ok"),
                CandidateResult("m3", CandidateStatus.ERROR, "HttpError"),
            )
        )
    )
    response = _client(verifier, runner=runner).post(
        "/run", headers={"Authorization": f"Bearer {VALID_TOKEN}"}
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "ok"
    assert body["summary"] == {
        "processed": 1,
        "pending": 1,
        "needs_review": 0,
        "awaiting_tig": 0,
        "errors": 1,
        "skipped": 0,
    }
    assert runner.calls == 1
    # The token is verified against the configured audience.
    assert verifier.calls == [(VALID_TOKEN, AUDIENCE)]


def test_unauthorized_run_never_starts_the_pipeline() -> None:
    runner = FakeRunner()
    response = _client(FakeVerifier(error=ValueError("bad")), runner=runner).post(
        "/run", headers={"Authorization": f"Bearer {VALID_TOKEN}"}
    )
    assert response.status_code == 401
    assert runner.calls == 0


def test_run_failure_returns_500_with_only_the_error_type() -> None:
    runner = FakeRunner(error=RuntimeError("Ledger header is wrong: 12345 Ft"))
    response = _client(FakeVerifier(), runner=runner).post(
        "/run", headers={"Authorization": f"Bearer {VALID_TOKEN}"}
    )
    assert response.status_code == 500
    assert response.get_json() == {"status": "error", "error": "RuntimeError"}


def test_default_runner_runs_the_pipeline_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[Mapping[str, str]] = []

    def fake_run_from_env(env: Mapping[str, str]) -> RunSummary:
        seen.append(env)
        return RunSummary(())

    monkeypatch.setattr(app_module, "run_from_env", fake_run_from_env)
    client = app_module.create_app(verifier=FakeVerifier(), env=ENV).test_client()

    response = client.post("/run", headers={"Authorization": f"Bearer {VALID_TOKEN}"})

    assert response.status_code == 200
    assert seen == [ENV]


def test_run_matches_service_account_email_case_insensitively() -> None:
    verifier = FakeVerifier(claims={"email": SCHEDULER_SA.upper(), "email_verified": True})
    response = _client(verifier).post("/run", headers={"Authorization": f"Bearer {VALID_TOKEN}"})
    assert response.status_code == 200


def test_run_never_logs_the_bearer_token(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("DEBUG")
    _client(FakeVerifier(error=ValueError("bad"))).post(
        "/run", headers={"Authorization": f"Bearer {VALID_TOKEN}"}
    )
    _client(FakeVerifier()).post("/run", headers={"Authorization": f"Bearer {VALID_TOKEN}"})
    assert VALID_TOKEN not in caplog.text


# --- default verifier and module-level app ----------------------------------------------


def test_default_verifier_uses_google_id_token_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def fake_verify(token: str, request: object, audience: str | None = None) -> dict[str, Any]:
        seen.update(token=token, audience=audience, request=request)
        return {"email": SCHEDULER_SA, "email_verified": True}

    monkeypatch.setattr(app_module.id_token, "verify_oauth2_token", fake_verify)

    claims = app_module.verify_google_oidc_token(VALID_TOKEN, AUDIENCE)

    assert claims["email"] == SCHEDULER_SA
    assert seen["token"] == VALID_TOKEN
    assert seen["audience"] == AUDIENCE
    assert seen["request"] is not None


def test_create_app_defaults_to_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OIDC_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("SCHEDULER_SERVICE_ACCOUNT_EMAIL", SCHEDULER_SA)
    monkeypatch.setattr(
        app_module.id_token,
        "verify_oauth2_token",
        lambda token, request, audience=None: {"email": SCHEDULER_SA, "email_verified": True},
    )
    monkeypatch.setattr(app_module, "run_from_env", lambda env: RunSummary(()))
    client = app_module.create_app().test_client()
    response = client.post("/run", headers={"Authorization": f"Bearer {VALID_TOKEN}"})
    assert response.status_code == 200


def test_module_exposes_wsgi_app_for_gunicorn() -> None:
    assert app_module.app.name == "intake.app"
