"""Deployment wiring (Task 22): the Terraform under ``infra/`` and the ``Dockerfile`` agree
with what the application reads.

Static checks only (regex over the HCL; no Terraform, no network). They catch the drift
that would only show up after a deploy: an env var the code no longer reads, a secret
name that differs from the one the code builds, a credential injected as a plain env var,
an OIDC audience configured differently on the Scheduler job and the service, or a
gunicorn timeout that kills a run before Cloud Run's request timeout does.
"""

import re
from pathlib import Path

import pytest

from intake import app as app_module
from intake.clients import auth
from intake.extraction import (
    AI_COMPASS_API_KEY_ENV,
    AI_COMPASS_BACKEND,
    AI_COMPASS_BASE_URL_ENV,
    AI_COMPASS_EFFORT_ENV,
    AI_COMPASS_MODEL_ENV,
    DEFAULT_AI_COMPASS_MODEL,
    EXTRACTOR_BACKEND_ENV,
    GEMINI_API_KEY_ENV,
    ai_compass_api_key_secret_id,
    gemini_api_key_secret_id,
)
from intake.extraction.claude_extractor import DEFAULT_AI_COMPASS_BASE_URL, DEFAULT_EFFORT
from intake.pipeline import runtime

ROOT = Path(__file__).resolve().parents[2]
INFRA = ROOT / "infra"
ENVIRONMENTS = ("staging", "production")

# Every variable the deployed service reads, named through the code's own constants.
APP_ENV_VARS = {
    app_module.AUDIENCE_ENV,
    app_module.SCHEDULER_EMAIL_ENV,
    auth.PROJECT_ID_ENV,
    auth.ENVIRONMENT_ENV,
    runtime.LEDGER_SPREADSHEET_ID_ENV,
    runtime.DRIVE_ROOT_FOLDER_ID_ENV,
    EXTRACTOR_BACKEND_ENV,
    AI_COMPASS_BASE_URL_ENV,
    AI_COMPASS_MODEL_ENV,
    AI_COMPASS_EFFORT_ENV,
}
# Read by the Dockerfile's gunicorn command line, not by Python.
SERVER_ENV_VARS = {"GUNICORN_TIMEOUT"}
# Credentials: read from Secret Manager at runtime, never injected as env vars.
FORBIDDEN_ENV_VARS = {AI_COMPASS_API_KEY_ENV, GEMINI_API_KEY_ENV, "OAUTH_REFRESH_TOKEN"}

# Cloud Scheduler HTTP targets accept an attempt deadline of at most 30 minutes.
SCHEDULER_MAX_ATTEMPT_DEADLINE_S = 1800


def _read(name: str) -> str:
    path = INFRA / name
    if not path.is_file():
        pytest.fail(f"infra/{name} is missing")
    return path.read_text(encoding="utf-8")


def _all_terraform() -> str:
    files = sorted(INFRA.glob("*.tf"))
    assert files, "no Terraform files under infra/"
    return "\n".join(path.read_text(encoding="utf-8") for path in files)


def _block(text: str, header: str) -> str:
    """The body of the first ``header { ... }`` block (brace-matched)."""
    start = text.find(header)
    assert start >= 0, f"block {header!r} not found"
    open_at = text.index("{", start)
    depth = 0
    for index in range(open_at, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[open_at + 1 : index]
    raise AssertionError(f"unbalanced braces in {header!r}")


def _variable_default(name: str) -> str:
    body = _block(_read("variables.tf"), f'variable "{name}"')
    match = re.search(r'^\s*default\s*=\s*"?([^"\n]*)"?\s*$', body, re.MULTILINE)
    assert match, f"variable {name!r} has no scalar default"
    return match.group(1).strip()


def _service_block() -> str:
    return _block(_read("cloudrun.tf"), 'resource "google_cloud_run_v2_service" "intake"')


def _env_entries(service: str) -> dict[str, str]:
    """``env { name = "X" value = <expr> }`` entries of the service, name -> value expr."""
    entries: dict[str, str] = {}
    for match in re.finditer(r"\benv\s*\{", service):
        body = _block(service[match.start() :], "env")
        name = re.search(r'name\s*=\s*"([A-Z0-9_]+)"', body)
        assert name, f"env block without a literal name: {body!r}"
        value = re.search(r"^\s*value\s*=\s*(.+?)\s*$", body, re.MULTILINE)
        entries[name.group(1)] = value.group(1) if value else ""
    return entries


# --- Cloud Run environment --------------------------------------------------------------


def test_service_sets_exactly_the_env_vars_the_code_reads() -> None:
    assert set(_env_entries(_service_block())) == APP_ENV_VARS | SERVER_ENV_VARS


def test_no_credential_is_injected_as_an_env_var() -> None:
    service = _service_block()
    assert "secret_key_ref" not in service
    assert not FORBIDDEN_ENV_VARS & set(_env_entries(service))


def test_extractor_env_defaults_match_the_code_defaults() -> None:
    env = _env_entries(_service_block())
    assert env[EXTRACTOR_BACKEND_ENV] == f'"{AI_COMPASS_BACKEND}"'
    assert env[AI_COMPASS_BASE_URL_ENV] == "var.ai_compass_base_url"
    assert env[AI_COMPASS_MODEL_ENV] == "var.ai_compass_model"
    assert env[AI_COMPASS_EFFORT_ENV] == "var.ai_compass_effort"
    assert _variable_default("ai_compass_base_url") == DEFAULT_AI_COMPASS_BASE_URL
    assert _variable_default("ai_compass_model") == DEFAULT_AI_COMPASS_MODEL
    assert _variable_default("ai_compass_effort") == DEFAULT_EFFORT


def test_environment_and_project_are_wired_per_environment() -> None:
    env = _env_entries(_service_block())
    assert env[auth.ENVIRONMENT_ENV] == "each.key"
    assert env[auth.PROJECT_ID_ENV] == "var.project_id"
    assert "scheduler_invoker[each.key].email" in env[app_module.SCHEDULER_EMAIL_ENV]


def test_both_environments_are_defined() -> None:
    locals_body = _block(_read("main.tf"), "locals")
    environments = _block(locals_body, "environments")
    for name in ENVIRONMENTS:
        assert re.search(rf"^\s*{name}\s*=\s*\{{", environments, re.MULTILINE), name


# --- OIDC audience ----------------------------------------------------------------------


def test_scheduler_and_service_use_the_same_fixed_oidc_audience() -> None:
    expression = "local.oidc_audiences[each.key]"
    env = _env_entries(_service_block())
    assert env[app_module.AUDIENCE_ENV] == expression
    assert expression in _block(_service_block(), "custom_audiences")
    job = _block(_read("scheduler.tf"), 'resource "google_cloud_scheduler_job" "poll"')
    oidc = _block(job, "oidc_token")
    assert re.search(rf"audience\s*=\s*{re.escape(expression)}", oidc)
    assert "scheduler_invoker[each.key].email" in oidc


def test_oidc_audience_does_not_reference_the_service_uri() -> None:
    audiences = re.search(r"oidc_audiences\s*=\s*(\{[^}]*\})", _read("main.tf"))
    assert audiences, "local.oidc_audiences not found"
    assert ".uri" not in audiences.group(1)


# --- concurrency and timeouts -----------------------------------------------------------


def test_single_instance_single_request() -> None:
    service = _service_block()
    assert re.search(r"max_instance_count\s*=\s*1\b", service)
    assert re.search(r"min_instance_count\s*=\s*0\b", service)
    assert re.search(r"max_instance_request_concurrency\s*=\s*1\b", service)


def test_request_timeout_fits_a_full_run_and_matches_gunicorn() -> None:
    timeout = int(_variable_default("request_timeout_seconds"))
    assert timeout >= 3000  # >= 100 invoices at ~8-12 s each, with headroom
    assert timeout <= 3600  # Cloud Run's maximum request timeout
    service = _service_block()
    assert re.search(r'timeout\s*=\s*"\$\{var\.request_timeout_seconds\}s"', service)
    assert _env_entries(service)["GUNICORN_TIMEOUT"] == "tostring(var.request_timeout_seconds)"
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(rf"GUNICORN_TIMEOUT={timeout}\b", dockerfile)
    assert '--timeout "${GUNICORN_TIMEOUT}"' in dockerfile


def test_scheduler_cannot_create_overlapping_runs() -> None:
    job = _block(_read("scheduler.tf"), 'resource "google_cloud_scheduler_job" "poll"')
    deadline = re.search(r'attempt_deadline\s*=\s*"(\d+)s"', job)
    assert deadline, "attempt_deadline missing"
    seconds = int(deadline.group(1))
    assert seconds <= SCHEDULER_MAX_ATTEMPT_DEADLINE_S
    assert seconds <= int(_variable_default("request_timeout_seconds"))
    assert re.search(r"retry_count\s*=\s*0\b", _block(job, "retry_config"))


def test_no_liveness_probe_that_could_kill_a_running_cycle() -> None:
    assert "liveness_probe" not in _service_block()


# --- secrets ----------------------------------------------------------------------------


def _secret_resources() -> dict[str, str]:
    """``google_secret_manager_secret`` resource name -> its secret_id prefix."""
    text = _read("secrets.tf")
    found: dict[str, str] = {}
    for match in re.finditer(r'resource\s+"google_secret_manager_secret"\s+"(\w+)"', text):
        body = _block(text[match.start() :], match.group(0))
        secret_id = re.search(r'secret_id\s*=\s*"([a-z0-9-]+)-\$\{each\.key\}"', body)
        assert secret_id, f"{match.group(1)} has no per-environment secret_id"
        found[match.group(1)] = secret_id.group(1)
    return found


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_secret_containers_match_the_names_the_code_reads(environment: str) -> None:
    ids = auth.secret_ids_for(environment)
    expected = {
        ids.refresh_token,
        ids.client_id,
        ids.client_secret,
        ai_compass_api_key_secret_id(environment),
        gemini_api_key_secret_id(environment),
    }
    declared = {f"{prefix}-{environment}" for prefix in _secret_resources().values()}
    assert declared == expected


def test_every_secret_has_an_accessor_binding_for_the_runtime_sa_only() -> None:
    text = _read("secrets.tf")
    for name in _secret_resources():
        reference = f"google_secret_manager_secret.{name}[each.key].secret_id"
        bindings = [
            _block(text[m.start() :], m.group(0))
            for m in re.finditer(
                r'resource\s+"google_secret_manager_secret_iam_member"\s+"\w+"', text
            )
        ]
        matching = [body for body in bindings if reference in body]
        assert len(matching) == 1, f"{name}: expected one accessor binding"
        assert '"roles/secretmanager.secretAccessor"' in matching[0]
        assert "google_service_account.runtime[each.key].email" in matching[0]
    assert "google_project_iam_member" not in text  # never project-wide secret access


def test_no_resource_is_defined_twice() -> None:
    addresses = re.findall(r'^resource\s+"(\w+)"\s+"(\w+)"', _all_terraform(), re.MULTILINE)
    duplicates = {address for address in addresses if addresses.count(address) > 1}
    assert not duplicates


def test_vertex_ai_role_is_off_by_default() -> None:
    assert _variable_default("enable_vertex_ai_user") == "false"
