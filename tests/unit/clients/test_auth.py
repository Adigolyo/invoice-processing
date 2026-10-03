"""Task 3: OAuth2 user credentials built from Secret Manager only.

Secret Manager is mocked through the injectable ``accessor`` callable, so these tests are
hermetic (no GCP calls, no network).
"""

import logging

import pytest
from google.oauth2.credentials import Credentials

from intake.clients import auth

PROJECT = "kibit-invoice-intake"


@pytest.fixture(autouse=True)
def _fresh_secret_manager_client() -> None:
    """Each test sees its own (faked) Secret Manager client, not one cached by another."""
    auth.reset_secret_manager_client()


REFRESH_TOKEN = "1//refresh-token-SENTINEL-value"
CLIENT_ID = "1234-abc.apps.googleusercontent.com"
CLIENT_SECRET = "GOCSPX-client-secret-SENTINEL"


def _fake_store(environment: str = "staging") -> dict[str, str]:
    base = f"projects/{PROJECT}/secrets"
    return {
        f"{base}/kibit-oauth-refresh-token-{environment}/versions/latest": REFRESH_TOKEN + "\n",
        f"{base}/kibit-oauth-client-id-{environment}/versions/latest": CLIENT_ID,
        f"{base}/kibit-oauth-client-secret-{environment}/versions/latest": CLIENT_SECRET,
    }


class FakeAccessor:
    """Stands in for Secret Manager: records every resource name it is asked for."""

    def __init__(self, store: dict[str, str]) -> None:
        self.store = store
        self.requested: list[str] = []

    def __call__(self, name: str) -> str:
        self.requested.append(name)
        if name not in self.store:
            raise LookupError(f"404 secret not found: {name}")
        return self.store[name]


# --- scopes -----------------------------------------------------------------------------


def test_scopes_are_exactly_the_three_minimal_workspace_scopes() -> None:
    assert set(auth.SCOPES) == {
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/spreadsheets",
    }


# --- secret naming ----------------------------------------------------------------------


def test_secret_ids_follow_the_factory_naming_per_environment() -> None:
    ids = auth.secret_ids_for("production")
    assert ids.refresh_token == "kibit-oauth-refresh-token-production"
    assert ids.client_id == "kibit-oauth-client-id-production"
    assert ids.client_secret == "kibit-oauth-client-secret-production"


@pytest.mark.parametrize("environment", ["", "Staging", "prod/../x", "staging production"])
def test_secret_ids_reject_invalid_environment_names(environment: str) -> None:
    with pytest.raises(auth.AuthConfigurationError):
        auth.secret_ids_for(environment)


def test_secret_version_name_defaults_to_latest() -> None:
    assert auth.secret_version_name("p", "s") == "projects/p/secrets/s/versions/latest"


# --- read_secret ------------------------------------------------------------------------


def test_read_secret_strips_trailing_whitespace_from_value() -> None:
    accessor = FakeAccessor(_fake_store())
    value = auth.read_secret(PROJECT, "kibit-oauth-refresh-token-staging", accessor=accessor)
    assert value == REFRESH_TOKEN


def test_read_secret_wraps_access_failures() -> None:
    accessor = FakeAccessor({})
    with pytest.raises(auth.SecretAccessError, match="kibit-oauth-refresh-token-staging"):
        auth.read_secret(PROJECT, "kibit-oauth-refresh-token-staging", accessor=accessor)


def test_read_secret_rejects_empty_secret_value() -> None:
    name = f"projects/{PROJECT}/secrets/empty/versions/latest"
    accessor = FakeAccessor({name: "  \n"})
    with pytest.raises(auth.SecretAccessError):
        auth.read_secret(PROJECT, "empty", accessor=accessor)


# --- build_credentials ------------------------------------------------------------------


def test_build_credentials_reads_refresh_token_and_client_from_secret_manager() -> None:
    accessor = FakeAccessor(_fake_store("staging"))

    creds = auth.build_credentials(PROJECT, "staging", accessor=accessor)

    assert isinstance(creds, Credentials)
    assert creds.refresh_token == REFRESH_TOKEN
    assert creds.client_id == CLIENT_ID
    assert creds.client_secret == CLIENT_SECRET
    assert creds.token_uri == "https://oauth2.googleapis.com/token"
    assert set(creds.scopes or []) == set(auth.SCOPES)
    # No access token is minted at build time (no network); it is refreshed on first use.
    assert creds.token is None
    assert sorted(accessor.requested) == sorted(_fake_store("staging"))


def test_build_credentials_uses_the_environment_specific_secrets() -> None:
    accessor = FakeAccessor(_fake_store("production"))
    creds = auth.build_credentials(PROJECT, "production", accessor=accessor)
    assert creds.refresh_token == REFRESH_TOKEN
    assert all("-production/" in name for name in accessor.requested)


def test_build_credentials_fails_loudly_when_refresh_token_missing() -> None:
    store = _fake_store()
    del store[f"projects/{PROJECT}/secrets/kibit-oauth-refresh-token-staging/versions/latest"]
    with pytest.raises(auth.SecretAccessError):
        auth.build_credentials(PROJECT, "staging", accessor=FakeAccessor(store))


def test_build_credentials_requires_project_id() -> None:
    with pytest.raises(auth.AuthConfigurationError):
        auth.build_credentials("", "staging", accessor=FakeAccessor(_fake_store()))


def test_build_credentials_never_logs_secret_values(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    auth.build_credentials(PROJECT, "staging", accessor=FakeAccessor(_fake_store()))

    store_missing = _fake_store()
    store_missing.pop(next(iter(store_missing)))
    with pytest.raises(auth.SecretAccessError) as excinfo:
        auth.build_credentials(PROJECT, "staging", accessor=FakeAccessor(store_missing))

    logged = caplog.text + str(excinfo.value)
    for secret in (REFRESH_TOKEN, CLIENT_SECRET):
        assert secret not in logged


# --- credentials_from_env ---------------------------------------------------------------


def test_credentials_from_env_reads_project_and_environment_only() -> None:
    env = {
        "GCP_PROJECT_ID": PROJECT,
        "ENVIRONMENT": "staging",
        # A token in the environment must be ignored: Secret Manager is the only source.
        "OAUTH_REFRESH_TOKEN": "should-not-be-used",
    }
    creds = auth.credentials_from_env(env, accessor=FakeAccessor(_fake_store()))
    assert creds.refresh_token == REFRESH_TOKEN


@pytest.mark.parametrize("missing", ["GCP_PROJECT_ID", "ENVIRONMENT"])
def test_credentials_from_env_requires_both_variables(missing: str) -> None:
    env = {"GCP_PROJECT_ID": PROJECT, "ENVIRONMENT": "staging"}
    del env[missing]
    with pytest.raises(auth.AuthConfigurationError, match=missing):
        auth.credentials_from_env(env, accessor=FakeAccessor(_fake_store()))


# --- default Secret Manager accessor ----------------------------------------------------


def test_default_accessor_calls_secret_manager_client(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class FakePayload:
        data = b"value-from-sm"

    class FakeResponse:
        payload = FakePayload()

    class FakeClient:
        def access_secret_version(self, *, name: str) -> FakeResponse:
            calls.append(name)
            return FakeResponse()

    monkeypatch.setattr(auth.secretmanager, "SecretManagerServiceClient", FakeClient)

    value = auth.read_secret(PROJECT, "some-secret")

    assert value == "value-from-sm"
    assert calls == [f"projects/{PROJECT}/secrets/some-secret/versions/latest"]


def test_default_accessor_reuses_one_secret_manager_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A new gRPC client per secret per run grew staging's memory until Cloud Run killed it.
    created: list[object] = []

    class FakePayload:
        data = b"v"

    class FakeResponse:
        payload = FakePayload()

    class FakeClient:
        def __init__(self) -> None:
            created.append(self)

        def access_secret_version(self, *, name: str) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr(auth.secretmanager, "SecretManagerServiceClient", FakeClient)
    auth.reset_secret_manager_client()

    for secret in ("a", "b", "c", "d"):
        auth.read_secret(PROJECT, secret)

    assert len(created) == 1
