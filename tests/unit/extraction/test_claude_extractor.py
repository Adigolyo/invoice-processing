"""Claude Sonnet 5.5 extraction through the AI Compass gateway (user-directed ADR 4 change).

Claude-specific behaviour only: the Messages API request shape, stop-reason handling,
SDK error propagation, configuration and key resolution. The shared extraction contract
(AC1-AC6, TIG, malformed output, logging) is enforced for this backend by
``test_extractor_contract.py``. The ``anthropic.Anthropic`` client is a recording fake:
no network, no key.
"""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import anthropic
import httpx2
import pytest

from intake.clients.auth import AuthConfigurationError, SecretAccessError
from intake.extraction import DocumentKind, UnsupportedDocumentError
from intake.extraction import claude_extractor as module
from intake.extraction.claude_extractor import (
    AI_COMPASS_API_KEY_ENV,
    AI_COMPASS_BASE_URL_ENV,
    AI_COMPASS_EFFORT_ENV,
    AI_COMPASS_MODEL_ENV,
    CLAUDE_SUPPORTED_MIME_TYPES,
    DEFAULT_AI_COMPASS_BASE_URL,
    DEFAULT_AI_COMPASS_MODEL,
    DEFAULT_EFFORT,
    DEFAULT_MAX_TOKENS,
    EFFORT_LEVELS,
    ClaudeExtractor,
    ai_compass_api_key_secret_id,
    resolve_ai_compass_api_key,
)
from intake.extraction.common import RESPONSE_JSON_SCHEMA, SYSTEM_PROMPTS, USER_TEXT
from intake.models import FlagReason, StageStatus
from tests.unit.extraction.backends import FakeClaudeClient, claude_content, claude_message

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "tig_pairs"
INVOICE_A = FIXTURES / "TIG-2026-09-KEK-A__INV_A-2026-01" / "INV_A-2026-01.pdf"
API_KEY = "sk-SENTINEL-ai-compass-key"

PAYLOAD: dict[str, Any] = {
    "supplier": "A Kft.",
    "invoice_number": "INV_A-2026-01",
    "supplier_country": "HU",
    "currency": "Ft",
    "net": "72 000 Ft",
    "gross": "72 000 Ft",
    "issue_date": "2026.10.05.",
    "due_date": "2026.11.04.",
    "supply_date": None,
    "performance_date": "2026.10.05.",
    "line_items": [
        {
            "description": "Nyomdai",
            "quantity": "1 200 db",
            "unit_price": "60 Ft",
            "net": "72 000 Ft",
        }
    ],
}
OK_TEXT = json.dumps(PAYLOAD, ensure_ascii=False)
_REQUEST = httpx2.Request("POST", "https://ai-compass.kibit.cloud/v1/messages")


def _extractor(*outcomes: Any, **kwargs: Any) -> tuple[ClaudeExtractor, FakeClaudeClient]:
    client = FakeClaudeClient(*outcomes)
    return ClaudeExtractor(client, **kwargs), client  # type: ignore[arg-type]


# --- defaults ---------------------------------------------------------------------------


def test_defaults_are_sonnet_5_5_at_medium_effort() -> None:
    assert DEFAULT_AI_COMPASS_MODEL == "claude-sonnet-5-5"
    assert DEFAULT_EFFORT == "medium"
    assert DEFAULT_MAX_TOKENS == 16000
    assert DEFAULT_AI_COMPASS_BASE_URL == "https://ai-compass.kibit.cloud"
    assert {"low", "medium", "high", "xhigh", "max"} == set(EFFORT_LEVELS)


# --- request shape ----------------------------------------------------------------------


def test_pdf_request_has_the_document_block_before_the_text_and_structured_output() -> None:
    pdf = INVOICE_A.read_bytes()
    extractor, client = _extractor(OK_TEXT)

    result = extractor.extract(pdf, "application/pdf")

    assert result.status is StageStatus.OK
    (call,) = client.calls
    assert call["model"] == "claude-sonnet-5-5"
    assert call["max_tokens"] == 16000
    assert call["system"] == SYSTEM_PROMPTS[DocumentKind.INVOICE]
    document, text = claude_content(call)
    assert document == {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": "application/pdf",
            "data": base64.standard_b64encode(pdf).decode("ascii"),
        },
    }
    assert "\n" not in document["source"]["data"]
    assert text == {"type": "text", "text": USER_TEXT[DocumentKind.INVOICE]}
    assert call["output_config"] == {
        "effort": "medium",
        "format": {"type": "json_schema", "schema": RESPONSE_JSON_SCHEMA},
    }


def test_request_omits_parameters_sonnet_5_5_or_the_proxy_rejects() -> None:
    extractor, client = _extractor(OK_TEXT)

    extractor.extract(b"%PDF", "application/pdf")

    (call,) = client.calls
    forbidden = {
        "temperature",
        "top_p",
        "top_k",
        "thinking",
        "fallbacks",
        "betas",
        "extra_headers",
        "extra_body",
        "output_format",
    }
    assert forbidden.isdisjoint(call)
    # No assistant prefill: the only message is the user's.
    assert [m["role"] for m in call["messages"]] == ["user"]


@pytest.mark.parametrize("mime_type", ["image/jpeg", "image/png", "image/webp", "image/jpg"])
def test_image_is_sent_as_a_base64_image_block_before_the_text(mime_type: str) -> None:
    image = b"\x89PNG\r\n fake image"
    extractor, client = _extractor(OK_TEXT)

    extractor.extract(image, mime_type)

    image_block, text = claude_content(client.calls[0])
    assert image_block["type"] == "image"
    assert image_block["source"]["type"] == "base64"
    assert image_block["source"]["media_type"] == mime_type.replace("jpg", "jpeg")
    assert base64.b64decode(image_block["source"]["data"]) == image
    assert text["type"] == "text"


def test_tig_kind_uses_the_tig_prompt_and_user_text() -> None:
    tig = dict(PAYLOAD, net=None, gross=None, due_date=None)
    extractor, client = _extractor(json.dumps(tig))

    result = extractor.extract(b"%PDF", "application/pdf", kind=DocumentKind.TIG)

    assert result.status is StageStatus.OK
    call = client.calls[0]
    assert call["system"] == SYSTEM_PROMPTS[DocumentKind.TIG]
    assert "Megbízott" in call["system"]
    assert claude_content(call)[1]["text"] == USER_TEXT[DocumentKind.TIG]


@pytest.mark.parametrize("mime_type", ["image/heic", "image/heif"])
def test_formats_claude_cannot_read_are_rejected_before_any_call(mime_type: str) -> None:
    extractor, client = _extractor()

    with pytest.raises(UnsupportedDocumentError):
        extractor.extract(b"heic bytes", mime_type)

    assert client.calls == []
    assert mime_type not in CLAUDE_SUPPORTED_MIME_TYPES


def test_model_effort_and_max_tokens_are_configurable() -> None:
    extractor, client = _extractor(
        OK_TEXT, model=" claude-opus-5-5 ", effort="low", max_tokens=8000
    )

    extractor.extract(b"%PDF", "application/pdf")

    call = client.calls[0]
    assert (call["model"], call["max_tokens"]) == ("claude-opus-5-5", 8000)
    assert call["output_config"]["effort"] == "low"
    assert extractor.model == "claude-opus-5-5"


@pytest.mark.parametrize(
    "kwargs", [{"model": " "}, {"effort": "extreme"}, {"effort": ""}, {"max_tokens": 0}]
)
def test_invalid_configuration_is_rejected(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        ClaudeExtractor(FakeClaudeClient(), **kwargs)  # type: ignore[arg-type]


# --- stop reasons: checked before reading content ---------------------------------------


@pytest.mark.parametrize(
    ("stop_reason", "stop_details"),
    [
        ("refusal", {"type": "refusal", "category": "general_harms", "explanation": "x"}),
        ("refusal", None),
        ("max_tokens", None),
        ("pause_turn", None),
        ("model_context_window_exceeded", None),
    ],
)
def test_non_end_turn_stop_is_an_extraction_failure_even_with_valid_json(
    stop_reason: str, stop_details: dict[str, Any] | None, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    message = claude_message(OK_TEXT, stop_reason=stop_reason, stop_details=stop_details)
    extractor, _ = _extractor(message)

    result = extractor.extract(INVOICE_A.read_bytes(), "application/pdf")

    assert result.status is StageStatus.INCOMPLETE
    assert result.flag_reason is FlagReason.INCOMPLETE_DATA
    assert result.value is None
    assert result.detail is not None and stop_reason in result.detail
    assert "A Kft." not in result.detail
    assert any(getattr(r, "stop_reason", None) == stop_reason for r in caplog.records)


def test_refusal_category_is_named_in_the_detail() -> None:
    message = claude_message(
        None,
        stop_reason="refusal",
        stop_details={"type": "refusal", "category": "cyber", "explanation": "declined"},
    )
    extractor, _ = _extractor(message)

    result = extractor.extract(b"%PDF", "application/pdf")

    assert result.detail is not None and "cyber" in result.detail


def test_split_text_blocks_are_joined_before_validation() -> None:
    message = claude_message(OK_TEXT)
    half = len(OK_TEXT) // 2
    message.content = [
        anthropic.types.TextBlock(type="text", text=OK_TEXT[:half]),
        anthropic.types.TextBlock(type="text", text=OK_TEXT[half:]),
    ]
    extractor, _ = _extractor(message)

    result = extractor.extract(b"%PDF", "application/pdf")

    assert result.status is StageStatus.OK


def test_token_usage_is_logged_without_content(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    extractor, _ = _extractor(claude_message(OK_TEXT, input_tokens=4321, output_tokens=987))

    extractor.extract(INVOICE_A.read_bytes(), "application/pdf")

    (record,) = [r for r in caplog.records if getattr(r, "outcome", None) == "ok"]
    assert record.__dict__["input_tokens"] == 4321
    assert record.__dict__["output_tokens"] == 987
    assert record.__dict__["model"] == "claude-sonnet-5-5"
    assert record.__dict__["stop_reason"] == "end_turn"


# --- API errors propagate (orchestrator retries next run; SDK retries first) ------------


@pytest.mark.parametrize(
    "error",
    [
        anthropic.InternalServerError(
            "unavailable", response=httpx2.Response(503, request=_REQUEST), body=None
        ),
        anthropic.RateLimitError(
            "rate limited", response=httpx2.Response(429, request=_REQUEST), body=None
        ),
        anthropic.BadRequestError(
            "bad request", response=httpx2.Response(400, request=_REQUEST), body=None
        ),
        anthropic.APIConnectionError(request=_REQUEST),
        anthropic.APITimeoutError(request=_REQUEST),
    ],
    ids=lambda e: type(e).__name__,
)
def test_api_errors_propagate_unchanged(error: Exception) -> None:
    extractor, _ = _extractor(error)

    with pytest.raises(type(error)):
        extractor.extract(INVOICE_A.read_bytes(), "application/pdf")


# --- API key resolution -----------------------------------------------------------------


class FakeAccessor:
    def __init__(self, store: dict[str, str]) -> None:
        self.store = store
        self.requested: list[str] = []

    def __call__(self, name: str) -> str:
        self.requested.append(name)
        if name not in self.store:
            raise LookupError("404")
        return self.store[name]


def test_secret_id_follows_the_per_environment_convention() -> None:
    assert ai_compass_api_key_secret_id("staging") == "kibit-ai-compass-api-key-staging"


def test_invalid_environment_name_is_rejected() -> None:
    with pytest.raises(AuthConfigurationError):
        ai_compass_api_key_secret_id("../prod")


def test_api_key_from_env_wins_for_local_dev() -> None:
    accessor = FakeAccessor({})

    key = resolve_ai_compass_api_key({AI_COMPASS_API_KEY_ENV: f" {API_KEY}\n"}, accessor)

    assert key == API_KEY
    assert accessor.requested == []


def test_api_key_is_read_from_secret_manager_otherwise() -> None:
    name = "projects/kibit-p/secrets/kibit-ai-compass-api-key-production/versions/latest"
    accessor = FakeAccessor({name: API_KEY + "\n"})

    key = resolve_ai_compass_api_key(
        {"GCP_PROJECT_ID": "kibit-p", "ENVIRONMENT": "production"}, accessor
    )

    assert key == API_KEY
    assert accessor.requested == [name]


def test_missing_project_or_environment_is_a_configuration_error() -> None:
    with pytest.raises(AuthConfigurationError) as excinfo:
        resolve_ai_compass_api_key({"ENVIRONMENT": "staging"}, FakeAccessor({}))
    assert "GCP_PROJECT_ID" in str(excinfo.value)
    assert AI_COMPASS_API_KEY_ENV in str(excinfo.value)


def test_unreadable_secret_raises_without_leaking_anything() -> None:
    with pytest.raises(SecretAccessError):
        resolve_ai_compass_api_key(
            {"GCP_PROJECT_ID": "kibit-p", "ENVIRONMENT": "staging"}, FakeAccessor({})
        )


# --- from_env ---------------------------------------------------------------------------


def _capture_client(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    created: list[dict[str, Any]] = []

    def fake_anthropic(**kwargs: Any) -> FakeClaudeClient:
        created.append(kwargs)
        return FakeClaudeClient(OK_TEXT)

    monkeypatch.setattr(module.anthropic, "Anthropic", fake_anthropic)
    return created


def test_from_env_builds_a_gateway_client_with_the_resolved_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = _capture_client(monkeypatch)

    extractor = ClaudeExtractor.from_env(
        {
            AI_COMPASS_API_KEY_ENV: API_KEY,
            AI_COMPASS_BASE_URL_ENV: " https://gateway.example.test ",
            AI_COMPASS_MODEL_ENV: "claude-opus-5-5",
            AI_COMPASS_EFFORT_ENV: "HIGH",
        },
        currency_map={"Ft": "HUF"},
    )

    (kwargs,) = created
    assert kwargs["api_key"] == API_KEY
    assert kwargs["base_url"] == "https://gateway.example.test"
    assert "max_retries" not in kwargs  # keep the SDK's default retries
    assert "default_headers" not in kwargs  # no beta headers through the proxy
    assert extractor.model == "claude-opus-5-5"
    assert extractor.effort == "high"
    assert API_KEY not in repr(extractor)
    assert extractor.extract(INVOICE_A.read_bytes(), "application/pdf").status is StageStatus.OK


def test_from_env_defaults_to_the_kibit_gateway_sonnet_and_medium_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = _capture_client(monkeypatch)

    extractor = ClaudeExtractor.from_env(
        {AI_COMPASS_API_KEY_ENV: API_KEY, AI_COMPASS_MODEL_ENV: " ", AI_COMPASS_EFFORT_ENV: ""}
    )

    assert created[0]["base_url"] == DEFAULT_AI_COMPASS_BASE_URL
    assert (extractor.model, extractor.effort) == ("claude-sonnet-5-5", "medium")


def test_from_env_reads_the_key_from_secret_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    created = _capture_client(monkeypatch)
    name = "projects/kibit-p/secrets/kibit-ai-compass-api-key-staging/versions/latest"

    ClaudeExtractor.from_env(
        {"GCP_PROJECT_ID": "kibit-p", "ENVIRONMENT": "staging"},
        accessor=FakeAccessor({name: API_KEY}),
    )

    assert created[0]["api_key"] == API_KEY


def test_from_env_rejects_an_unknown_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    _capture_client(monkeypatch)

    with pytest.raises(ValueError, match="AI_COMPASS_EFFORT|effort"):
        ClaudeExtractor.from_env({AI_COMPASS_API_KEY_ENV: API_KEY, AI_COMPASS_EFFORT_ENV: "turbo"})


def test_close_releases_the_http_client() -> None:
    extractor, client = _extractor()
    client.close = MagicMock()  # type: ignore[attr-defined]

    extractor.close()

    client.close.assert_called_once_with()  # type: ignore[attr-defined]
