"""OAuth2 user credentials for the shared Workspace mailbox, sourced from Secret Manager.

Per the technical design ("Security & Auth"), the refresh token for the shared mailbox is
stored only in Secret Manager and never in code, env files or logs. The OAuth client ID and
client secret that minted it are read from Secret Manager as well (the factory documents
are silent on where they live; keeping them next to the refresh token is the assumption).

Secret names follow the factory's per-environment convention
(``kibit-oauth-refresh-token-{env}``); see ``infra/secrets.tf``.
"""

import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from google.cloud import secretmanager
from google.oauth2.credentials import Credentials

logger = logging.getLogger(__name__)

GMAIL_MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
SCOPES: tuple[str, ...] = (GMAIL_MODIFY_SCOPE, DRIVE_SCOPE, SHEETS_SCOPE)

TOKEN_URI = "https://oauth2.googleapis.com/token"

PROJECT_ID_ENV = "GCP_PROJECT_ID"
ENVIRONMENT_ENV = "ENVIRONMENT"

_ENVIRONMENT_PATTERN = re.compile(r"^[a-z][a-z0-9-]*$")

SecretAccessor = Callable[[str], str]
"""Returns the payload of a Secret Manager secret version, given its full resource name."""


class AuthConfigurationError(RuntimeError):
    """The service is missing configuration needed to locate its credentials."""


class SecretAccessError(RuntimeError):
    """A secret could not be read from Secret Manager (missing, denied or empty)."""


@dataclass(frozen=True)
class SecretIds:
    """Secret Manager secret IDs holding the OAuth credential for one environment."""

    refresh_token: str
    client_id: str
    client_secret: str


def secret_ids_for(environment: str) -> SecretIds:
    """Return the per-environment secret IDs, e.g. ``kibit-oauth-refresh-token-staging``."""
    if not _ENVIRONMENT_PATTERN.fullmatch(environment):
        raise AuthConfigurationError(f"invalid environment name: {environment!r}")
    return SecretIds(
        refresh_token=f"kibit-oauth-refresh-token-{environment}",
        client_id=f"kibit-oauth-client-id-{environment}",
        client_secret=f"kibit-oauth-client-secret-{environment}",
    )


def secret_version_name(project_id: str, secret_id: str, version: str = "latest") -> str:
    """Full Secret Manager resource name of a secret version."""
    return f"projects/{project_id}/secrets/{secret_id}/versions/{version}"


_secret_manager_client: secretmanager.SecretManagerServiceClient | None = None


def _shared_secret_manager_client() -> secretmanager.SecretManagerServiceClient:
    """One gRPC client per process: a new one per secret per run, never closed, grew the
    Cloud Run instance's memory by tens of MiB a minute until it was killed."""
    global _secret_manager_client
    if _secret_manager_client is None:
        _secret_manager_client = secretmanager.SecretManagerServiceClient()
    return _secret_manager_client


def reset_secret_manager_client() -> None:
    """Forget the shared client (tests)."""
    global _secret_manager_client
    _secret_manager_client = None


def _secret_manager_accessor(name: str) -> str:
    response = _shared_secret_manager_client().access_secret_version(name=name)
    return response.payload.data.decode("utf-8")


def read_secret(project_id: str, secret_id: str, accessor: SecretAccessor | None = None) -> str:
    """Read the latest version of a secret, stripped of surrounding whitespace.

    ``gcloud secrets versions add --data-file=- <<< "$TOKEN"`` (the runbook's command)
    stores a trailing newline, so the value is stripped. Failures name the secret ID but
    never include the value.
    """
    access = accessor or _secret_manager_accessor
    name = secret_version_name(project_id, secret_id)
    try:
        value = access(name).strip()
    except Exception as exc:
        raise SecretAccessError(f"could not read secret {secret_id!r} from Secret Manager") from exc
    if not value:
        raise SecretAccessError(f"secret {secret_id!r} is empty")
    return value


def build_credentials(
    project_id: str, environment: str, accessor: SecretAccessor | None = None
) -> Credentials:
    """Build OAuth2 user credentials for the shared mailbox from Secret Manager.

    No access token is minted here: the Google client libraries refresh it on first use,
    so an expired or revoked refresh token fails loudly at the first API call.
    """
    if not project_id:
        raise AuthConfigurationError("project_id is required to read secrets")
    ids = secret_ids_for(environment)
    refresh_token = read_secret(project_id, ids.refresh_token, accessor)
    client_id = read_secret(project_id, ids.client_id, accessor)
    client_secret = read_secret(project_id, ids.client_secret, accessor)
    logger.info("built Workspace OAuth credentials", extra={"environment": environment})
    return Credentials(  # type: ignore[no-untyped-call]
        token=None,
        refresh_token=refresh_token,
        token_uri=TOKEN_URI,
        client_id=client_id,
        client_secret=client_secret,
        scopes=list(SCOPES),
    )


def credentials_from_env(
    env: Mapping[str, str], accessor: SecretAccessor | None = None
) -> Credentials:
    """Build credentials using ``GCP_PROJECT_ID`` and ``ENVIRONMENT`` from ``env``.

    Only the project and environment come from the environment; the credential values
    themselves are always read from Secret Manager.
    """
    missing = [key for key in (PROJECT_ID_ENV, ENVIRONMENT_ENV) if not env.get(key)]
    if missing:
        raise AuthConfigurationError(f"missing environment variable(s): {', '.join(missing)}")
    return build_credentials(env[PROJECT_ID_ENV], env[ENVIRONMENT_ENV], accessor)
