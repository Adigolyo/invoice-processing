"""Cloud Run HTTP entrypoint.

- ``GET /healthz``: unauthenticated liveness check, no data and no side effects.
- ``POST /run``: accepts only a Google-signed OIDC token minted for the Cloud Scheduler
  service account, with the configured audience, then runs one full polling cycle
  (``intake.pipeline.runtime.run_from_env``) and returns its summary:
  ``{"status": "ok", "summary": {"processed", "pending", "needs_review",
  "awaiting_tig", "errors", "skipped"}}``. A run that fails before processing candidates
  (Config, Ledger header, credentials, polling) answers 500 with only the error type.

Logging (``intake.observability.logging``): importing this module configures logging,
JSON for Cloud Logging on Cloud Run. Each authorised ``/run`` is one ``run_scope`` (all
its records share a ``run_id``) inside the request's trace (``X-Cloud-Trace-Context``),
and ends with ``event=run_completed`` (INFO) or ``event=run_failed`` (ERROR, with the
error type and a message-free stack trace for Error Reporting).

Served by gunicorn (see ``Dockerfile``): ``gunicorn intake.app:app``.
"""

import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from flask import Flask, Response, jsonify, request
from google.auth.exceptions import GoogleAuthError
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

from intake.observability.logging import configure_logging, run_scope, trace_scope
from intake.pipeline.orchestrator import RunSummary
from intake.pipeline.runtime import run_from_env

logger = logging.getLogger(__name__)

AUDIENCE_ENV = "OIDC_AUDIENCE"
SCHEDULER_EMAIL_ENV = "SCHEDULER_SERVICE_ACCOUNT_EMAIL"

TokenVerifier = Callable[[str, str], Mapping[str, Any]]
"""Verifies an OIDC token for an audience and returns its claims; raises if invalid."""

PipelineRunner = Callable[[], RunSummary]
"""Runs one polling cycle and returns its summary."""


@dataclass(frozen=True)
class OidcSettings:
    audience: str
    allowed_email: str


def oidc_settings_from_env(env: Mapping[str, str]) -> OidcSettings | None:
    """Read the expected audience and Scheduler SA email; ``None`` if either is unset."""
    audience = env.get(AUDIENCE_ENV, "").strip()
    email = env.get(SCHEDULER_EMAIL_ENV, "").strip()
    if not audience or not email:
        return None
    return OidcSettings(audience=audience, allowed_email=email)


def verify_google_oidc_token(token: str, audience: str) -> Mapping[str, Any]:
    """Verify a Google-signed ID token's signature, expiry, issuer and audience."""
    claims: Mapping[str, Any] = id_token.verify_oauth2_token(  # type: ignore[no-untyped-call]
        token, google_requests.Request(), audience=audience
    )
    return claims


def _bearer_token(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def is_authorized(
    authorization: str | None, settings: OidcSettings | None, verifier: TokenVerifier
) -> bool:
    """True only for a valid token issued to the allowed service account. Fails closed."""
    if settings is None:
        logger.error(
            "POST /run rejected: %s and %s must both be set", AUDIENCE_ENV, SCHEDULER_EMAIL_ENV
        )
        return False
    token = _bearer_token(authorization)
    if token is None:
        logger.warning("POST /run rejected: missing or malformed bearer token")
        return False
    try:
        claims = verifier(token, settings.audience)
    except (ValueError, GoogleAuthError) as exc:
        logger.warning("POST /run rejected: OIDC verification failed (%s)", type(exc).__name__)
        return False
    email = claims.get("email")
    if claims.get("email_verified") is not True or not isinstance(email, str):
        logger.warning("POST /run rejected: token has no verified email claim")
        return False
    if email.lower() != settings.allowed_email.lower():
        logger.warning("POST /run rejected: caller is not the scheduler service account")
        return False
    return True


def _unauthorized() -> tuple[Response, int]:
    response = jsonify({"error": "unauthorized"})
    response.headers["WWW-Authenticate"] = "Bearer"
    return response, 401


def create_app(
    verifier: TokenVerifier | None = None,
    env: Mapping[str, str] | None = None,
    runner: PipelineRunner | None = None,
    *,
    configure: bool = False,
) -> Flask:
    """Build the WSGI app. ``verifier``, ``env`` and ``runner`` are injectable for tests.

    The default ``runner`` builds the real clients from ``env`` on every request.
    ``configure`` installs the process-wide logging handler (the served app does).
    """
    environment = os.environ if env is None else env
    if configure:
        configure_logging(env=environment)
    settings = oidc_settings_from_env(environment)
    verify = verifier or verify_google_oidc_token

    def default_runner() -> RunSummary:
        return run_from_env(environment)

    run_pipeline = runner or default_runner
    app = Flask(__name__)

    @app.get("/healthz")
    def healthz() -> tuple[Response, int]:
        return jsonify({"status": "ok"}), 200

    @app.post("/run")
    def run() -> tuple[Response, int]:
        if not is_authorized(request.headers.get("Authorization"), settings, verify):
            return _unauthorized()
        trace = request.headers.get("X-Cloud-Trace-Context")
        with trace_scope(trace, request.headers.get("traceparent")), run_scope():
            try:
                summary = run_pipeline().to_dict()
            except Exception as exc:
                # Only the type: messages may carry sheet/document values.
                logger.error(
                    "POST /run failed (%s)",
                    type(exc).__name__,
                    exc_info=True,
                    extra={"event": "run_failed", "error_type": type(exc).__name__},
                )
                return jsonify({"status": "error", "error": type(exc).__name__}), 500
            logger.info("POST /run completed", extra={"event": "run_completed"})
            return jsonify({"status": "ok", "summary": summary}), 200

    return app


app = create_app(configure=True)
