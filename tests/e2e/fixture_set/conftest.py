"""Session fixture for the Task 24 E2E fixture suite: one live run, judged by the tests.

Opt-in only (``pytest -m e2e tests/e2e``): it creates a spreadsheet and a Drive folder,
inserts 64 emails into the sandbox mailbox and sends 64 documents to AI Compass.

``e2e_run`` does, once per session:

1. checks the answer key, the sandbox mailbox and that no unfinished E2E invoice from an
   earlier run is still in the inbox (it would be booked into this run's ledger);
2. creates "Kibit E2E ledger <run-id>" (Ledger header + Config) and "Invoices E2E
   <run-id>", with the 11 fixture contractor domains as ``contractor_identifiers``;
3. seeds all 32 pairs as threads (PM's TIG mail, then the contractor's invoice reply);
4. runs the pipeline locally (``run_with_credentials``) against the fresh ledger/folder,
   snapshots Sheets/Drive/Gmail, runs it a second time and snapshots again;
5. writes ``tests/e2e/results/<run-id>.json`` (gitignored) and returns everything.

Configuration (environment):

- ``KIBIT_ENV_FILE`` (default ``<repo>/.env``) and ``KIBIT_SECRETS_DIR`` (default
  ``<repo>/secrets``): the local OAuth token and the AI Compass settings. The real
  environment overrides the file. ``LEDGER_SPREADSHEET_ID``/``DRIVE_ROOT_FOLDER_ID`` from
  it are ignored: the run always uses its own fresh spreadsheet and folder.
- In CI (no ``token.json``): ``GCP_PROJECT_ID`` + ``ENVIRONMENT=staging``. The sandbox
  OAuth credentials and the AI Compass key are then read from Secret Manager, as the
  deployed service reads them.
- ``KIBIT_E2E_REQUIRED=1`` (release gate): whatever would skip the suite fails it.
- ``KIBIT_E2E_RESULTS_DIR`` (default ``tests/e2e/results``).
- ``KIBIT_E2E_KEEP=1``: keep the run's ledger and folder even when it passed (by
  default a passed run's are moved to the Drive trash; a failed run's are kept).
- ``KIBIT_E2E_PAIRS`` (default ``all``): ``all`` 32 pairs, or ``smoke`` (one pair per
  outcome x currency x month, 7 pairs; used by the release gate).
- ``KIBIT_E2E_COOLDOWN_S`` (default 90): pause before run 1 and before run 2, so the
  sandbox's per-user Gmail quota recovers (live runs hit ``rateLimitExceeded`` while
  polling right after seeding and right after run 1). A run whose poll is still
  rate-limited is retried (``RATE_LIMIT_ATTEMPTS`` x ``RATE_LIMIT_WAIT_S``).
- ``KIBIT_E2E_RESUME=<run-id>``: continue a run that was seeded but whose first pipeline
  run failed before processing (e.g. that quota error): no new ledger, folder or emails.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from intake.__main__ import TOKEN_FILE, load_env_file, local_credentials
from intake.clients.auth import credentials_from_env
from intake.extraction import AI_COMPASS_API_KEY_ENV
from intake.pipeline.runtime import (
    DRIVE_ROOT_FOLDER_ID_ENV,
    LEDGER_SPREADSHEET_ID_ENV,
    run_with_credentials,
)
from tests.e2e.fixture_set import verify, workspace

RESULTS_DIR_DEFAULT = workspace.REPO_ROOT / "tests" / "e2e" / "results"
# A rate-limited poll changes nothing, so the run is retried instead of the suite
# waiting a long fixed cooldown up front.
RATE_LIMIT_ATTEMPTS = 4
RATE_LIMIT_WAIT_S = 30.0


@dataclass
class E2ERun:
    run_id: str
    spreadsheet_id: str
    folder_id: str
    pairs: list[verify.PairExpectation]
    first: dict[str, Any]
    second: dict[str, Any]
    results_path: Path
    timings: dict[str, float] = field(default_factory=dict)

    @property
    def ledger_url(self) -> str:
        return f"https://docs.google.com/spreadsheets/d/{self.spreadsheet_id}/edit"

    @property
    def folder_url(self) -> str:
        return f"https://drive.google.com/drive/folders/{self.folder_id}"


class LiveEnv:
    """The run's environment and OAuth credentials; its repr never shows a value.

    pytest prints fixture values in tracebacks, and the environment holds API keys.
    """

    def __init__(self, values: dict[str, str], credentials: Any, source: str = "token") -> None:
        self.values = values
        self.credentials = credentials
        self.source = source

    def __repr__(self) -> str:
        return f"LiveEnv(<{len(self.values)} variables, redacted>)"


def _paths() -> tuple[Path, Path]:
    env_file = Path(os.environ.get("KIBIT_ENV_FILE", workspace.REPO_ROOT / ".env"))
    secrets_dir = Path(os.environ.get("KIBIT_SECRETS_DIR", workspace.REPO_ROOT / "secrets"))
    return env_file, secrets_dir


@pytest.fixture(scope="session")
def e2e_env(pytestconfig: pytest.Config) -> LiveEnv:
    if "e2e" not in (pytestconfig.option.markexpr or ""):
        workspace.unavailable("live E2E suite: opt in with `pytest -m e2e tests/e2e`")
    env_file, secrets_dir = _paths()
    env = {**load_env_file(env_file), **os.environ}
    source = workspace.credential_source(env, secrets_dir)
    if source is None:
        workspace.unavailable(
            f"no OAuth token at {secrets_dir / TOKEN_FILE} and no GCP_PROJECT_ID + "
            "ENVIRONMENT to read the sandbox credentials from Secret Manager"
        )
    if not workspace.ai_compass_key_available(env):
        workspace.unavailable(f"{AI_COMPASS_API_KEY_ENV} not set (env or {env_file})")
    if not workspace.ANSWER_KEY.is_file():
        workspace.unavailable("tests/fixtures/tig_pairs/answer_key.json not present")
    credentials = local_credentials(secrets_dir) if source == "token" else credentials_from_env(env)
    return LiveEnv(env, credentials, source)


def e2e_ledger_hint(spreadsheet_id: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"


def _write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


@pytest.fixture(scope="session")
def e2e_run(e2e_env: LiveEnv, request: pytest.FixtureRequest) -> Iterator[E2ERun]:
    env, credentials = e2e_env.values, e2e_env.credentials
    started = time.monotonic()
    pairs = verify.load_answer_key(workspace.ANSWER_KEY)
    problems = verify.answer_key_problems(pairs, workspace.FIXTURES)
    if problems:
        pytest.fail("answer key unusable:\n" + "\n".join(problems))
    selection = os.environ.get("KIBIT_E2E_PAIRS", "all").strip() or "all"
    try:
        pairs = verify.select_pairs(pairs, selection)
    except ValueError as exc:
        pytest.fail(str(exc))

    services = workspace.build_services(credentials)
    address = workspace.mailbox_address(services.gmail)
    if address != workspace.MAILBOX:
        pytest.fail(f"token belongs to {address}, not the sandbox mailbox {workspace.MAILBOX}")

    results_dir = Path(os.environ.get("KIBIT_E2E_RESULTS_DIR", RESULTS_DIR_DEFAULT))
    resume = os.environ.get("KIBIT_E2E_RESUME", "").strip()
    record: dict[str, Any]
    if resume:
        # Seeded already, but run 1 never got through (e.g. a Gmail quota error).
        results_path = results_dir / f"{resume}.json"
        record = json.loads(results_path.read_text(encoding="utf-8"))
        if "seeded" not in record or "run1" in record:
            pytest.fail(f"{results_path}: can only resume a run that was seeded but not run")
        own = {s["invoice"]["id"] for s in record["seeded"].values()}
    else:
        run_id = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        results_path = results_dir / f"{run_id}.json"
        record = {"run_id": run_id, "pair_selection": selection}
        own = set()
    leftovers = [m for m in workspace.leftover_e2e_invoices(services.gmail) if m not in own]
    if leftovers:
        pytest.fail(
            f"{len(leftovers)} unfinished invoice mail(s) of an earlier E2E run are still "
            f"unread in the inbox ({leftovers[:5]}...); they would be booked into this run's "
            "ledger. Resume that run (KIBIT_E2E_RESUME=<run-id>) or mark them read by hand."
        )

    run_id = record["run_id"]
    timings: dict[str, float] = dict(record.get("timings", {}))
    if not resume:
        contractors = sorted({p.contractor_domain for p in pairs})
        spreadsheet_id, folder_id = workspace.create_ledger_and_folder(
            services,
            spreadsheet_title=verify.spreadsheet_title(run_id),
            folder_name=verify.folder_name(run_id),
            contractors=contractors,
        )
        record.update(
            spreadsheet_id=spreadsheet_id,
            folder_id=folder_id,
            contractor_identifiers=contractors,
            mode=f"local run_with_credentials, credentials from {e2e_env.source}",
            model=env.get("AI_COMPASS_MODEL") or "default",
        )
        t = time.monotonic()
        base_time = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=2)
        record["seeded"] = workspace.seed_pairs(services.gmail, pairs, run_id, base_time)
        timings["seed_s"] = round(time.monotonic() - t, 1)
        _write(results_path, record)
    else:
        record["resumed"] = True
    spreadsheet_id, folder_id = record["spreadsheet_id"], record["folder_id"]
    seeded = record["seeded"]
    run_env = {
        **env,
        LEDGER_SPREADSHEET_ID_ENV: spreadsheet_id,
        DRIVE_ROOT_FOLDER_ID_ENV: folder_id,
    }

    extraction_log = workspace.ExtractionLog()
    extraction_log.register_fixtures(pairs)
    intake_logger = logging.getLogger("intake")
    token_counter = workspace.TokenCounter(extraction_log)
    intake_logger.addHandler(token_counter)
    previous_level = intake_logger.level
    intake_logger.setLevel(logging.INFO)
    cooldown_s = float(os.environ.get("KIBIT_E2E_COOLDOWN_S", "90"))
    record["cooldown_s"] = cooldown_s
    try:
        snaps: list[dict[str, Any]] = []
        for run in (1, 2):
            # Let the per-user Gmail quota ("units per minute") recover after seeding and
            # after run 1: polling has no retry by design (a failed poll changes nothing).
            time.sleep(cooldown_s)
            t = time.monotonic()
            summary = workspace.retry_rate_limited(
                functools.partial(
                    run_with_credentials,
                    credentials,
                    run_env,
                    extractor_factory=extraction_log.factory(run),
                    candidate_scope=workspace.candidate_scope(run_id),
                ),
                attempts=RATE_LIMIT_ATTEMPTS,
                wait_s=RATE_LIMIT_WAIT_S,
                sleep=time.sleep,
            )
            timings[f"run{run}_s"] = round(time.monotonic() - t, 1)
            snaps.append(
                workspace.snapshot(
                    services,
                    run_id=run_id,
                    spreadsheet_id=spreadsheet_id,
                    folder_id=folder_id,
                    seeded=seeded,
                    summary=workspace.summary_dict(summary),
                )
            )
            record[f"run{run}"] = snaps[-1]
            _write(results_path, record)
    finally:
        intake_logger.removeHandler(token_counter)
        intake_logger.setLevel(previous_level)
        timings["total_s"] = round(time.monotonic() - started, 1)
        record["timings"] = timings
        record["tokens"] = extraction_log.tokens
        record["extractions"] = extraction_log.calls
        _write(results_path, record)

    first, second = snaps
    verdicts = verify.evaluate_pairs(pairs, first)
    record["verdicts"] = [v.to_dict() for v in verdicts]
    record["global_problems"] = verify.global_problems(pairs, first)
    record["idempotency_problems"] = verify.idempotency_problems(first, second)
    record["summary_scope"] = {
        "run1": verify.run_scoped_counts(first),
        "run2": verify.run_scoped_counts(second),
    }
    record["table"] = verify.format_table(verdicts)
    _write(results_path, record)
    print(f"\nE2E run {run_id}: results in {results_path}")
    print(record["table"])
    yield E2ERun(
        run_id=run_id,
        spreadsheet_id=spreadsheet_id,
        folder_id=folder_id,
        pairs=pairs,
        first=first,
        second=second,
        results_path=results_path,
        timings=timings,
    )
    # Teardown, after every test of the session: a fully passed run's ledger and folder
    # are trashed (the results JSON keeps the snapshots); a failed run's are kept.
    if workspace.should_clean_up(tests_failed=request.session.testsfailed):
        workspace.trash_run_artifacts(services, spreadsheet_id=spreadsheet_id, folder_id=folder_id)
        print(f"\nE2E run {run_id} passed: ledger and folder moved to the Drive trash")
    else:
        print(f"\nE2E run {run_id}: ledger and folder kept ({e2e_ledger_hint(spreadsheet_id)})")
