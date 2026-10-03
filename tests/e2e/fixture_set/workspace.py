"""Live Google Workspace side of the E2E fixture suite: seed the pairs, read the state back.

Kept thin on purpose: every judgement about what is read lives in ``verify.py``. The
sandbox helpers of ``scripts/setup_sandbox.py`` (spreadsheet/folder creation, message
building and insertion) are reused, not duplicated. Nothing here deletes anything.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import logging
import os
import sys
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, NoReturn

import pytest

from intake.__main__ import TOKEN_FILE
from intake.clients.auth import ENVIRONMENT_ENV, PROJECT_ID_ENV
from intake.config import Config
from intake.extraction import AI_COMPASS_API_KEY_ENV, DocumentKind, Extractor
from intake.models import InvoiceExtraction, StageResult
from intake.pipeline.orchestrator import RunSummary
from intake.pipeline.runtime import build_extractor, build_service
from tests.e2e.fixture_set.verify import (
    LABELS,
    PairExpectation,
    message_id_for,
    run_marker,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "tig_pairs"
ANSWER_KEY = FIXTURES / "answer_key.json"

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import setup_sandbox as sandbox  # noqa: E402

MAILBOX = sandbox.MAILBOX
PM_ADDRESS = sandbox.PM_ADDRESS
FOLDER_MIME = sandbox.FOLDER_MIME
UNREAD = "UNREAD"
DRAFT = "DRAFT"
# Reads only (never inserts): googleapiclient retries 429/5xx and 403 rateLimitExceeded
# with exponential backoff. The sandbox mailbox's per-minute Gmail quota is easy to hit.
READ_RETRIES = 6

# Set by the release gate: anything that would skip the suite fails it instead, so a
# gate that ran nothing can never pass.
REQUIRED_ENV = "KIBIT_E2E_REQUIRED"


def unavailable(reason: str) -> NoReturn:
    """Skip the live suite, or fail it when ``KIBIT_E2E_REQUIRED=1`` (release gate)."""
    if os.environ.get(REQUIRED_ENV) == "1":
        pytest.fail(f"E2E suite required but unavailable: {reason}")
    pytest.skip(reason)


def credential_source(env: Mapping[str, str], secrets_dir: Path) -> str | None:
    """``"token"`` (local ``token.json``), ``"secret_manager"`` (CI) or ``None``.

    In CI the sandbox OAuth credentials come from Secret Manager, the same way the
    deployed staging service reads them (``GCP_PROJECT_ID`` + ``ENVIRONMENT``).
    """
    if (secrets_dir / TOKEN_FILE).is_file():
        return "token"
    if env.get(PROJECT_ID_ENV) and env.get(ENVIRONMENT_ENV):
        return "secret_manager"
    return None


def ai_compass_key_available(env: Mapping[str, str]) -> bool:
    """A local key, or a project + environment to read it from Secret Manager."""
    if env.get(AI_COMPASS_API_KEY_ENV, "").strip():
        return True
    return bool(env.get(PROJECT_ID_ENV) and env.get(ENVIRONMENT_ENV))


@dataclass(frozen=True, slots=True)
class Services:
    gmail: Any
    drive: Any
    sheets: Any


def build_services(credentials: Any) -> Services:
    return Services(
        gmail=build_service("gmail", "v1", credentials),
        drive=build_service("drive", "v3", credentials),
        sheets=build_service("sheets", "v4", credentials),
    )


def mailbox_address(gmail: Any) -> str:
    return str(
        gmail.users().getProfile(userId="me").execute(num_retries=READ_RETRIES)["emailAddress"]
    )


# --- creating the run's ledger, folder and emails -------------------------------------------------


def create_ledger_and_folder(
    services: Services, *, spreadsheet_title: str, folder_name: str, contractors: Sequence[str]
) -> tuple[str, str]:
    """A fresh spreadsheet (Ledger header + Config) and Drive root folder for one run."""
    if sandbox.find_file(services.drive, folder_name, FOLDER_MIME) is not None:
        raise RuntimeError(f"Drive folder {folder_name!r} already exists; use a new run id")
    if sandbox.find_file(services.drive, spreadsheet_title, sandbox.SHEET_MIME) is not None:
        raise RuntimeError(f"spreadsheet {spreadsheet_title!r} already exists; use a new run id")
    folder_id = sandbox.ensure_drive_folder(services.drive, folder_name)
    spreadsheet_id = sandbox.ensure_spreadsheet(
        services.drive,
        services.sheets,
        folder_id,
        title=spreadsheet_title,
        contractor_domains=contractors,
    )
    return spreadsheet_id, folder_id


def seed_pairs(
    gmail: Any, pairs: Sequence[PairExpectation], run_id: str, base_time: datetime
) -> dict[str, dict[str, dict[str, str]]]:
    """Insert each pair as a thread: the PM's TIG mail, then the contractor's invoice reply.

    Returns ``{pair: {"tig": {"id", "thread_id", "rfc822"}, "invoice": {...}}}``.
    """
    marker = run_marker(run_id)
    seeded: dict[str, dict[str, dict[str, str]]] = {}
    for index, p in enumerate(pairs):
        folder = FIXTURES / p.pair
        sent = base_time + timedelta(minutes=2 * index)
        tig_mid = message_id_for(run_id, p.pair, "tig")
        tig_id, thread_id = sandbox.insert_message_ids(
            gmail,
            sandbox.build_message(
                sender=f"Projektmenedzser <{PM_ADDRESS}>",
                to=p.sender,
                subject=f"Teljesítésigazolás {p.tig_number} {marker}",
                body="Csatolva küldöm a teljesítésigazolást. Kérjük, ez alapján számlázzanak.",
                message_id=tig_mid,
                sent=sent,
                attachment=(p.tig_file, (folder / p.tig_file).read_bytes()),
            ),
        )
        invoice_mid = message_id_for(run_id, p.pair, "invoice")
        invoice_id, invoice_thread = sandbox.insert_message_ids(
            gmail,
            sandbox.build_message(
                sender=f"{p.supplier} <{p.sender}>",
                to=MAILBOX,
                subject=f"Re: Teljesítésigazolás {p.tig_number} {marker}",
                body="Mellékelten küldjük a számlát.",
                message_id=invoice_mid,
                sent=sent + timedelta(minutes=1),
                attachment=(p.invoice_file, (folder / p.invoice_file).read_bytes()),
                in_reply_to=tig_mid,
            ),
            thread_id,
        )
        if invoice_thread != thread_id:
            raise RuntimeError(f"{p.pair}: invoice was not threaded with its TIG")
        seeded[p.pair] = {
            "tig": {"id": tig_id, "thread_id": thread_id, "rfc822": tig_mid},
            "invoice": {"id": invoice_id, "thread_id": thread_id, "rfc822": invoice_mid},
        }
    return seeded


def find_seeded(
    gmail: Any, pairs: Sequence[PairExpectation], run_id: str
) -> dict[str, dict[str, dict[str, str]]]:
    """Look a finished run's messages up again by their deterministic Message-IDs."""
    seeded: dict[str, dict[str, dict[str, str]]] = {}
    for p in pairs:
        entry: dict[str, dict[str, str]] = {}
        for kind in ("tig", "invoice"):
            rfc822 = message_id_for(run_id, p.pair, kind)
            found = (
                gmail.users()
                .messages()
                .list(userId="me", q=f"rfc822msgid:{rfc822.strip('<>')}")
                .execute(num_retries=READ_RETRIES)
                .get("messages", [])
            )
            if found:
                entry[kind] = {
                    "id": found[0]["id"],
                    "thread_id": found[0]["threadId"],
                    "rfc822": rfc822,
                }
        if len(entry) == 2:
            seeded[p.pair] = entry
    return seeded


def leftover_e2e_invoices(gmail: Any) -> list[str]:
    """Unfinished invoice mails of earlier E2E runs: they would be processed into this run.

    Any unread inbox message tagged ``[Kibit E2E ...]`` from someone other than the PM
    that is not in a terminal Kibit state (AwaitingTIG counts as unfinished).
    """
    excluded = (
        LABELS["processed"],
        LABELS["pending"],
        LABELS["needs_review"],
        LABELS["duplicate"],
    )
    query = " ".join(
        [
            'in:inbox is:unread subject:"Kibit E2E"',
            f"-from:{PM_ADDRESS}",
            *(f"-label:{name}" for name in excluded),
        ]
    )
    response = gmail.users().messages().list(userId="me", q=query).execute(num_retries=READ_RETRIES)
    return [m["id"] for m in response.get("messages", [])]


# --- reading the state back ---------------------------------------------------------------


def label_names_by_id(gmail: Any) -> dict[str, str]:
    labels = (
        gmail.users().labels().list(userId="me").execute(num_retries=READ_RETRIES).get("labels", [])
    )
    return {lab["id"]: lab["name"] for lab in labels}


def read_ledger(sheets: Any, spreadsheet_id: str) -> list[list[Any]]:
    response = (
        sheets.spreadsheets()
        .values()
        .get(
            spreadsheetId=spreadsheet_id,
            range="'Ledger'!A1:H",
            valueRenderOption="UNFORMATTED_VALUE",
        )
        .execute(num_retries=READ_RETRIES)
    )
    rows: list[list[Any]] = response.get("values", [])
    return [list(r) for r in rows[1:] if any(str(c).strip() for c in r)]


def spreadsheet_title_of(sheets: Any, spreadsheet_id: str) -> str:
    response = (
        sheets.spreadsheets()
        .get(spreadsheetId=spreadsheet_id, fields="properties.title")
        .execute(num_retries=READ_RETRIES)
    )
    return str(response["properties"]["title"])


def _children(drive: Any, folder_id: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {
            "q": f"'{folder_id}' in parents and trashed = false",
            "fields": "nextPageToken, files(id, name, mimeType)",
            "pageSize": 1000,
        }
        if token:
            kwargs["pageToken"] = token
        response = drive.files().list(**kwargs).execute(num_retries=READ_RETRIES)
        items.extend(response.get("files", []))
        token = response.get("nextPageToken")
        if not token:
            return items


def read_drive(drive: Any, folder_id: str) -> dict[str, Any]:
    folders: dict[str, list[str]] = {}
    root_files: list[str] = []
    for item in _children(drive, folder_id):
        if item["mimeType"] == FOLDER_MIME:
            names = [c["name"] for c in _children(drive, item["id"])]
            folders[item["name"]] = sorted(names)
        else:
            root_files.append(item["name"])
    return {"folders": folders, "root_files": sorted(root_files)}


def _text_body(payload: Mapping[str, Any]) -> str:
    if payload.get("mimeType") == "text/plain" and (payload.get("body") or {}).get("data"):
        data = payload["body"]["data"]
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8")
    return "".join(_text_body(part) for part in payload.get("parts") or ())


def _header(payload: Mapping[str, Any], name: str) -> str:
    for h in payload.get("headers") or ():
        if h.get("name", "").lower() == name.lower():
            return str(h.get("value", ""))
    return ""


def read_messages(
    gmail: Any, seeded: Mapping[str, Mapping[str, Mapping[str, str]]]
) -> dict[str, Any]:
    names = label_names_by_id(gmail)
    kibit = set(LABELS.values())
    result: dict[str, Any] = {}
    for pair, entry in seeded.items():
        thread_id = entry["invoice"]["thread_id"]
        thread = (
            gmail.users()
            .threads()
            .get(userId="me", id=thread_id, format="full")
            .execute(num_retries=READ_RETRIES)
        )
        by_id = {m["id"]: m for m in thread.get("messages", [])}
        record: dict[str, Any] = {}
        for kind in ("invoice", "tig"):
            message = by_id.get(entry[kind]["id"], {})
            label_ids = message.get("labelIds", [])
            record[kind] = {
                "id": entry[kind]["id"],
                "thread_id": thread_id,
                "labels": sorted(names.get(i, i) for i in label_ids if names.get(i, i) in kibit),
                "unread": UNREAD in label_ids,
            }
        record["drafts"] = [
            {"to": _header(m["payload"], "To"), "body": _text_body(m["payload"])}
            for m in thread.get("messages", [])
            if DRAFT in m.get("labelIds", [])
        ]
        result[pair] = record
    return result


def summary_dict(summary: RunSummary) -> dict[str, Any]:
    return {
        "counts": summary.to_dict(),
        "candidates": [
            {
                "message_id": r.message_id,
                "status": r.status.value,
                "reason": r.reason,
                "registry_number": r.registry_number,
            }
            for r in summary.results
        ],
    }


def snapshot(
    services: Services,
    *,
    run_id: str,
    spreadsheet_id: str,
    folder_id: str,
    seeded: Mapping[str, Mapping[str, Mapping[str, str]]],
    summary: Mapping[str, Any] | None,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "spreadsheet_id": spreadsheet_id,
        "folder_id": folder_id,
        "ledger": read_ledger(services.sheets, spreadsheet_id),
        "drive": read_drive(services.drive, folder_id),
        "messages": read_messages(services.gmail, seeded),
        "summary": dict(summary or {}),
    }


# --- observing the extractor --------------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, tuple | list):
        return [_jsonable(v) for v in value]
    return value


@dataclass
class ExtractionLog:
    """What the model returned per document (for diagnosing a failed pair) and token use."""

    documents: dict[str, str] = field(default_factory=dict)  # sha256 -> "pair/file"
    calls: list[dict[str, Any]] = field(default_factory=list)
    tokens: dict[str, int] = field(default_factory=lambda: {"input": 0, "output": 0})
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def register_fixtures(self, pairs: Sequence[PairExpectation]) -> None:
        for p in pairs:
            for name in (p.tig_file, p.invoice_file):
                digest = hashlib.sha256((FIXTURES / p.pair / name).read_bytes()).hexdigest()
                self.documents[digest] = f"{p.pair}/{name}"

    def wrap(self, inner: Extractor, run: int) -> Extractor:
        log = self

        class _Recording:
            def extract(
                self, content: bytes, mime_type: str, *, kind: DocumentKind = DocumentKind.INVOICE
            ) -> StageResult[InvoiceExtraction]:
                digest = hashlib.sha256(content).hexdigest()
                started = datetime.now()
                entry: dict[str, Any] = {
                    "run": run,
                    "document": log.documents.get(digest, digest[:12]),
                    "kind": kind.value,
                }
                try:
                    result = inner.extract(content, mime_type, kind=kind)
                except Exception as exc:  # recorded, then re-raised unchanged
                    entry.update(status="exception", detail=type(exc).__name__, value=None)
                    raise
                else:
                    entry.update(
                        status=result.status.value,
                        detail=result.detail or result.error,
                        value=_jsonable(result.value),
                    )
                finally:
                    entry["seconds"] = round((datetime.now() - started).total_seconds(), 1)
                    with log._lock:
                        log.calls.append(entry)
                return result

        return _Recording()

    def factory(self, run: int) -> Any:
        def build(env: Mapping[str, str], config: Config) -> Extractor:
            return self.wrap(build_extractor(env, config), run)

        return build


class TokenCounter(logging.Handler):
    """Sums ``input_tokens``/``output_tokens`` from the extractor's structured log records."""

    def __init__(self, log: ExtractionLog) -> None:
        super().__init__(level=logging.DEBUG)
        self._log = log

    def emit(self, record: logging.LogRecord) -> None:
        for attr, key in (("input_tokens", "input"), ("output_tokens", "output")):
            value = getattr(record, attr, None)
            if isinstance(value, int):
                self._log.tokens[key] += value
