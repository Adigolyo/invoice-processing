"""Task 3: Cloud Run HTTP skeleton — /healthz and the OIDC-protected /run stub.

OIDC verification is mocked via the injectable ``verifier``; no network calls.
"""

from collections.abc import Mapping
from typing import Any

import pytest
from flask.testing import FlaskClient
from google.auth.exceptions import TransportError

from intake import app as app_module

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


def _client(verifier: FakeVerifier, env: Mapping[str, str] = ENV) -> FlaskClient:
    return app_module.create_app(verifier=verifier, env=env).test_client()


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


def test_run_with_valid_scheduler_token_returns_stub_summary() -> None:
    verifier = FakeVerifier()
    response = _client(verifier).post("/run", headers={"Authorization": f"Bearer {VALID_TOKEN}"})

    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "stub"
    assert body["summary"] == {
        "processed": 0,
        "pending": 0,
        "needs_review": 0,
        "awaiting_tig": 0,
        "errors": 0,
    }
    # The token is verified against the configured audience.
    assert verifier.calls == [(VALID_TOKEN, AUDIENCE)]


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
    client = app_module.create_app().test_client()
    response = client.post("/run", headers={"Authorization": f"Bearer {VALID_TOKEN}"})
    assert response.status_code == 200


def test_module_exposes_wsgi_app_for_gunicorn() -> None:
    assert app_module.app.name == "intake.app"
