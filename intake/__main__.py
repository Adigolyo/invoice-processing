"""Local one-cycle run: ``python -m intake`` (e.g. a live demo against the sandbox account).

Uses the local OAuth files instead of Secret Manager:

- ``secrets/client_secret.json``: the Desktop OAuth client (``docs/google-workspace-setup.md``
  Path A2), needed only once, by ``python -m intake --authorize``, which opens the browser
  consent flow (sign in as the *sandbox mailbox*) and writes ``secrets/token.json``
  (mode 0600; needs ``google-auth-oauthlib``).
- ``secrets/token.json``: the authorised-user token (refresh token + client) used by
  every run.

Configuration is read from ``.env`` (``KEY=value`` lines) with the real environment
taking precedence: ``LEDGER_SPREADSHEET_ID`` (required), ``DRIVE_ROOT_FOLDER_ID``
(optional cross-check of the Config tab), and the extractor settings
(``EXTRACTOR_BACKEND``, ``AI_COMPASS_BASE_URL``, ``AI_COMPASS_API_KEY``,
``AI_COMPASS_MODEL``; or ``GEMINI_API_KEY``/``GEMINI_MODEL`` for the Gemini backend).

The run summary is printed as JSON to stdout: counts per outcome plus, per candidate, the
Gmail message ID, outcome, a fixed reason code and the registry number. Secrets,
document content and amounts are never printed; a failed run prints only the error type.
Logs go to stderr, human-readable (``intake.observability.logging``); a failed run also
logs ``event=run_failed`` with a message-free stack trace.
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from google.oauth2.credentials import Credentials

from intake.clients.auth import SCOPES
from intake.observability.logging import configure_logging, run_scope
from intake.pipeline.orchestrator import RunSummary
from intake.pipeline.runtime import run_with_credentials

logger = logging.getLogger("intake.cli")

TOKEN_FILE = "token.json"
CLIENT_SECRET_FILE = "client_secret.json"

LocalRunner = Callable[[Credentials, Mapping[str, str]], RunSummary]


def load_env_file(path: Path) -> dict[str, str]:
    """Parse simple ``KEY=value`` lines (``export``, quotes, ``#`` comments); ``{}`` if absent."""
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key:
            continue
        value = value.strip()
        if value[:1] in {'"', "'"} and value[:1] in value[1:]:
            value = value[1 : value.index(value[0], 1)]
        else:
            value = value.split(" #", 1)[0].strip()
        values[key] = value
    return values


def local_credentials(secrets_dir: Path) -> Credentials:
    """Authorised-user credentials from ``<secrets_dir>/token.json``."""
    creds: Credentials = Credentials.from_authorized_user_file(  # type: ignore[no-untyped-call]
        str(secrets_dir / TOKEN_FILE), scopes=list(SCOPES)
    )
    return creds


def authorize(secrets_dir: Path, flow_class: Any = None) -> Path:
    """Run the browser consent flow and save ``token.json`` (owner-only permissions)."""
    if flow_class is None:
        flow_class = importlib.import_module("google_auth_oauthlib.flow").InstalledAppFlow
    flow = flow_class.from_client_secrets_file(str(secrets_dir / CLIENT_SECRET_FILE), list(SCOPES))
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    token_path = secrets_dir / TOKEN_FILE
    fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(creds.to_json())
    token_path.chmod(0o600)
    return token_path


def _summary_json(summary: RunSummary) -> str:
    return json.dumps(
        {
            "summary": summary.to_dict(),
            "candidates": [
                {
                    "message_id": r.message_id,
                    "status": r.status.value,
                    "reason": r.reason,
                    "registry_number": r.registry_number,
                }
                for r in summary.results
            ],
        },
        indent=2,
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: LocalRunner = run_with_credentials,
    environ: Mapping[str, str] | None = None,
    flow_class: Any = None,
) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m intake", description="Run one invoice-intake cycle locally."
    )
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--secrets-dir", type=Path, default=Path("secrets"))
    parser.add_argument(
        "--authorize",
        action="store_true",
        help="run the one-time OAuth consent flow and save secrets/token.json",
    )
    args = parser.parse_args(argv)
    secrets_dir: Path = args.secrets_dir

    if args.authorize:
        if not (secrets_dir / CLIENT_SECRET_FILE).is_file():
            print(f"missing {secrets_dir / CLIENT_SECRET_FILE}", file=sys.stderr)
            return 2
        try:
            token_path = authorize(secrets_dir, flow_class)
        except ImportError:
            print("--authorize needs google-auth-oauthlib (pip install it)", file=sys.stderr)
            return 2
        print(f"Saved {token_path}")
        return 0

    if not (secrets_dir / TOKEN_FILE).is_file():
        print(
            f"missing {secrets_dir / TOKEN_FILE}; run `python -m intake --authorize` first "
            "(sign in as the sandbox mailbox)",
            file=sys.stderr,
        )
        return 2

    env = {**load_env_file(args.env_file), **(os.environ if environ is None else environ)}
    with run_scope():
        try:
            summary = runner(local_credentials(secrets_dir), env)
        except Exception as exc:
            logger.error(
                "run failed (%s)",
                type(exc).__name__,
                exc_info=True,
                extra={"event": "run_failed", "error_type": type(exc).__name__},
            )
            print(f"run failed: {type(exc).__name__}", file=sys.stderr)
            return 1
    print(_summary_json(summary))
    return 0


def run() -> int:
    """Console entry: configure logging (readable locally, JSON on Cloud Run), then run."""
    configure_logging()
    return main()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run())
