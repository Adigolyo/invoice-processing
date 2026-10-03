"""Production wiring: build the real clients for one cycle from the environment.

Kept apart from the orchestrator so the state machine depends only on client ports and
the ``Extractor`` protocol, and ``POST /run`` / ``python -m intake`` share one factory.

Environment:

- ``LEDGER_SPREADSHEET_ID`` (required): the ledger spreadsheet (``Ledger`` + ``Config``).
- ``DRIVE_ROOT_FOLDER_ID`` (optional): the Drive root comes from the Config tab
  (``drive_root_folder_id``); when this variable is also set it must agree, so an
  environment can never file into another environment's folder by mistake.
- Cloud Run credentials: ``GCP_PROJECT_ID`` + ``ENVIRONMENT`` select the Secret Manager
  secrets holding the OAuth refresh token and client (``intake.clients.auth``).
- Extractor: see ``build_extractor`` (``EXTRACTOR_BACKEND``, ``AI_COMPASS_BASE_URL``,
  ``AI_COMPASS_API_KEY``, ``AI_COMPASS_MODEL``; or ``GEMINI_API_KEY`` / ``GEMINI_MODEL``
  for the Gemini backend).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Final

from googleapiclient import discovery

from intake.clients.auth import credentials_from_env
from intake.clients.drive_client import DriveClient
from intake.clients.gmail_client import GmailClient
from intake.clients.sheets_client import SheetsClient
from intake.config import Config, ConfigError
from intake.extraction import Extractor, extractor_from_env
from intake.pipeline.orchestrator import PipelineClients, RunSummary, run_cycle

LEDGER_SPREADSHEET_ID_ENV: Final = "LEDGER_SPREADSHEET_ID"
DRIVE_ROOT_FOLDER_ID_ENV: Final = "DRIVE_ROOT_FOLDER_ID"

ServiceBuilder = Callable[[str, str, Any], Any]
"""Builds a ``googleapiclient`` resource: ``(api, version, credentials)``."""
ExtractorFactory = Callable[[Mapping[str, str], Config], Extractor]


class PipelineConfigurationError(RuntimeError):
    """A required environment variable for a run is missing."""


def build_service(api: str, version: str, credentials: Any) -> Any:
    """A ``googleapiclient`` resource (no on-disk discovery cache, which Cloud Run lacks)."""
    return discovery.build(api, version, credentials=credentials, cache_discovery=False)


def build_extractor(env: Mapping[str, str], config: Config) -> Extractor:
    """The single seam choosing the extraction backend (``EXTRACTOR_BACKEND``)."""
    return extractor_from_env(env, currency_map=config.currency_map)


def run_with_credentials(
    credentials: Any,
    env: Mapping[str, str],
    *,
    service_builder: ServiceBuilder = build_service,
    extractor_factory: ExtractorFactory = build_extractor,
) -> RunSummary:
    """Run one cycle against the Workspace account the ``credentials`` belong to."""
    spreadsheet_id = env.get(LEDGER_SPREADSHEET_ID_ENV, "").strip()
    if not spreadsheet_id:
        raise PipelineConfigurationError(f"{LEDGER_SPREADSHEET_ID_ENV} is not set")
    sheets = SheetsClient(service_builder("sheets", "v4", credentials), spreadsheet_id)

    def connect(config: Config) -> PipelineClients:
        root = config.drive_root_folder_id
        expected_root = env.get(DRIVE_ROOT_FOLDER_ID_ENV, "").strip()
        if expected_root and expected_root != root:
            raise ConfigError(
                f"{DRIVE_ROOT_FOLDER_ID_ENV} does not match the Config tab's "
                "drive_root_folder_id; refusing to file into an unexpected folder"
            )
        gmail = GmailClient(service_builder("gmail", "v1", credentials), config.labels)
        drive = DriveClient(service_builder("drive", "v3", credentials), root)
        # Drive keeps a trashed file working by ID, so a ledger or root folder deleted
        # by mistake would otherwise keep being written to, out of sight, until the trash
        # is emptied. Refuse instead: the run fails before any email is touched.
        drive.ensure_not_trashed(spreadsheet_id, "ledger spreadsheet")
        drive.ensure_not_trashed(root, "Drive root folder")
        return PipelineClients(
            gmail=gmail,
            drive=drive,
            extractor=extractor_factory(env, config),
        )

    return run_cycle(sheets, connect)


def run_from_env(env: Mapping[str, str]) -> RunSummary:
    """Cloud Run: credentials from Secret Manager (``GCP_PROJECT_ID``/``ENVIRONMENT``)."""
    return run_with_credentials(credentials_from_env(env), env)


__all__ = [
    "DRIVE_ROOT_FOLDER_ID_ENV",
    "LEDGER_SPREADSHEET_ID_ENV",
    "PipelineConfigurationError",
    "build_extractor",
    "build_service",
    "run_from_env",
    "run_with_credentials",
]
