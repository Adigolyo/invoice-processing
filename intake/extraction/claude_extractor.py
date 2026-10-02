"""Claude extraction of invoice and TIG documents via the AI Compass gateway (ADR 4).

AI Compass is Kibit's AI gateway, a LiteLLM proxy exposing the Anthropic Messages API;
the default model is Claude Sonnet 5.5. One ``messages.create`` call per document: the
whole PDF (``document`` block) or image (``image`` block) goes base64-encoded before the
instruction text, and the answer is constrained by the shared JSON schema through
structured outputs (``output_config.format``). The schema, prompts, validation,
conversion and result contract are shared with the Gemini backend
(``intake.extraction.common``; see ``interpret_response`` there).

Claude Sonnet 5.5 request rules respected here: no ``temperature``/``top_p``/``top_k``
(non-default values are rejected), no ``thinking`` parameter (``disabled`` is rejected;
omitted means adaptive), no assistant prefill, ``output_config.effort`` always set
explicitly. No ``fallbacks`` parameter and no beta headers: those are Claude-API-only and
the request goes through a third-party proxy.

``stop_reason`` is checked before the content is read: anything but ``end_turn``
(``refusal``, ``max_tokens``, ...) is an extraction failure -- ``incomplete`` with
``FlagReason.INCOMPLETE_DATA`` and no value -- never parsed.

SDK errors (``anthropic.APIStatusError``, ``RateLimitError``, ``APIConnectionError``,
``APITimeoutError``) are **raised** unchanged after the SDK's own default retries: the
orchestrator aborts the candidate before any label change and it is retried next run.

Neither document content nor the API key is ever logged; log records carry only the
kind, MIME type, size, model, effort, stop reason, token usage, outcome and a SHA-256 of
the response text.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Mapping
from typing import Final, Literal, cast

import anthropic
from anthropic.types import (
    DocumentBlockParam,
    ImageBlockParam,
    Message,
    MessageParam,
    OutputConfigParam,
)

from intake.clients.auth import SecretAccessor
from intake.extraction.common import (
    RESPONSE_JSON_SCHEMA,
    SUPPORTED_MIME_TYPES,
    SYSTEM_PROMPTS,
    USER_TEXT,
    DocumentKind,
    check_document,
    interpret_response,
    rejected,
    resolve_key,
    response_sha256,
    secret_id,
)
from intake.models import InvoiceExtraction, StageResult

logger = logging.getLogger(__name__)

DEFAULT_AI_COMPASS_BASE_URL: Final = "https://ai-compass.kibit.cloud"
DEFAULT_AI_COMPASS_MODEL: Final = "claude-sonnet-5-5"
DEFAULT_EFFORT: Final = "medium"
DEFAULT_MAX_TOKENS: Final = 16000
EFFORT_LEVELS: Final = ("low", "medium", "high", "xhigh", "max")
# Per attempt; the SDK retries timeouts with its default policy (2 retries).
REQUEST_TIMEOUT_S: Final = 300.0

AI_COMPASS_BASE_URL_ENV: Final = "AI_COMPASS_BASE_URL"
AI_COMPASS_MODEL_ENV: Final = "AI_COMPASS_MODEL"
AI_COMPASS_EFFORT_ENV: Final = "AI_COMPASS_EFFORT"
AI_COMPASS_API_KEY_ENV: Final = "AI_COMPASS_API_KEY"
AI_COMPASS_API_KEY_SECRET_PREFIX: Final = "kibit-ai-compass-api-key"

# Claude reads PDFs and JPEG/PNG/GIF/WebP images; HEIC/HEIF are not accepted.
CLAUDE_SUPPORTED_MIME_TYPES: Final = SUPPORTED_MIME_TYPES - {"image/heic", "image/heif"}

_ACCEPTED_STOP_REASON: Final = "end_turn"

_Effort = Literal["low", "medium", "high", "xhigh", "max"]
_ImageMediaType = Literal["image/jpeg", "image/png", "image/gif", "image/webp"]


# --- configuration ----------------------------------------------------------------------


def ai_compass_api_key_secret_id(environment: str) -> str:
    """Secret Manager ID of the AI Compass key, e.g. ``kibit-ai-compass-api-key-staging``."""
    return secret_id(AI_COMPASS_API_KEY_SECRET_PREFIX, environment)


def resolve_ai_compass_api_key(
    env: Mapping[str, str], accessor: SecretAccessor | None = None
) -> str:
    """The AI Compass key: ``AI_COMPASS_API_KEY`` (local dev) or Secret Manager.

    Without ``AI_COMPASS_API_KEY``, the key is read from
    ``kibit-ai-compass-api-key-<ENVIRONMENT>`` in project ``GCP_PROJECT_ID``. Errors
    never include the key.
    """
    return resolve_key(
        env,
        local_env=AI_COMPASS_API_KEY_ENV,
        secret_prefix=AI_COMPASS_API_KEY_SECRET_PREFIX,
        label="AI Compass API key",
        accessor=accessor,
    )


def _document_block(content: bytes, mime: str) -> DocumentBlockParam | ImageBlockParam:
    """The base64 ``document`` (PDF) or ``image`` block; ``mime`` is already validated."""
    data = base64.standard_b64encode(content).decode("ascii")
    if mime == "application/pdf":
        return {
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": data},
        }
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": cast(_ImageMediaType, mime), "data": data},
    }


def _response_text(message: Message) -> str | None:
    texts = [block.text for block in message.content if block.type == "text"]
    return "".join(texts) if texts else None


class ClaudeExtractor:
    """``Extractor`` backed by Claude through the AI Compass gateway (``anthropic`` SDK)."""

    def __init__(
        self,
        client: anthropic.Anthropic,
        *,
        model: str = DEFAULT_AI_COMPASS_MODEL,
        effort: str = DEFAULT_EFFORT,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        currency_map: Mapping[str, str] | None = None,
    ) -> None:
        if not model.strip():
            raise ValueError("a Claude model ID is required")
        normalised_effort = effort.strip().lower()
        if normalised_effort not in EFFORT_LEVELS:
            raise ValueError(
                f"invalid effort {effort!r} ({AI_COMPASS_EFFORT_ENV}); "
                f"expected one of {', '.join(EFFORT_LEVELS)}"
            )
        if max_tokens < 1:
            raise ValueError("max_tokens must be positive")
        self._client = client
        self.model = model.strip()
        self._effort = cast(_Effort, normalised_effort)
        self.max_tokens = max_tokens
        # Config's symbol -> ISO map, used only to read line-item amounts (HUF rule).
        self._currency_map: Mapping[str, str] = dict(currency_map or {})

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str],
        *,
        currency_map: Mapping[str, str] | None = None,
        accessor: SecretAccessor | None = None,
    ) -> ClaudeExtractor:
        """Build from ``AI_COMPASS_*`` and the key ``resolve_ai_compass_api_key`` finds."""
        client = anthropic.Anthropic(
            api_key=resolve_ai_compass_api_key(env, accessor),
            base_url=env.get(AI_COMPASS_BASE_URL_ENV, "").strip() or DEFAULT_AI_COMPASS_BASE_URL,
            timeout=REQUEST_TIMEOUT_S,
        )
        return cls(
            client,
            model=env.get(AI_COMPASS_MODEL_ENV, "").strip() or DEFAULT_AI_COMPASS_MODEL,
            effort=env.get(AI_COMPASS_EFFORT_ENV, "").strip() or DEFAULT_EFFORT,
            currency_map=currency_map,
        )

    @property
    def effort(self) -> str:
        """The ``output_config.effort`` sent with every request."""
        return self._effort

    def __repr__(self) -> str:
        return f"ClaudeExtractor(model={self.model!r}, effort={self.effort!r})"

    def extract(
        self, content: bytes, mime_type: str, *, kind: DocumentKind = DocumentKind.INVOICE
    ) -> StageResult[InvoiceExtraction]:
        """Extract one document; see ``common.interpret_response`` for the contract."""
        mime = check_document(content, mime_type, CLAUDE_SUPPORTED_MIME_TYPES)

        user: MessageParam = {
            "role": "user",
            "content": [
                _document_block(content, mime),
                {"type": "text", "text": USER_TEXT[kind]},
            ],
        }
        output_config: OutputConfigParam = {
            "effort": self._effort,
            "format": {"type": "json_schema", "schema": RESPONSE_JSON_SCHEMA},
        }
        message = self._client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=SYSTEM_PROMPTS[kind],
            messages=[user],
            output_config=output_config,
        )
        text = _response_text(message)
        log_fields: dict[str, object] = {
            "document_kind": kind.value,
            "mime_type": mime,
            "size_bytes": len(content),
            "model": self.model,
            "effort": self.effort,
            "stop_reason": message.stop_reason,
            "input_tokens": message.usage.input_tokens,
            "output_tokens": message.usage.output_tokens,
            "response_sha256": response_sha256(text),
        }

        if message.stop_reason != _ACCEPTED_STOP_REASON:
            detail = f"Claude stopped with stop_reason={message.stop_reason}"
            details = message.stop_details
            if details is not None and details.category:
                detail += f" (category={details.category})"
            return rejected(detail, logger=logger, log_fields=log_fields, reason="stop_reason")

        return interpret_response(
            text,
            kind=kind,
            currency_map=self._currency_map,
            provider="Claude",
            logger=logger,
            log_fields=log_fields,
        )


__all__ = [
    "AI_COMPASS_API_KEY_ENV",
    "AI_COMPASS_BASE_URL_ENV",
    "AI_COMPASS_EFFORT_ENV",
    "AI_COMPASS_MODEL_ENV",
    "CLAUDE_SUPPORTED_MIME_TYPES",
    "DEFAULT_AI_COMPASS_BASE_URL",
    "DEFAULT_AI_COMPASS_MODEL",
    "DEFAULT_EFFORT",
    "DEFAULT_MAX_TOKENS",
    "EFFORT_LEVELS",
    "ClaudeExtractor",
    "ai_compass_api_key_secret_id",
    "resolve_ai_compass_api_key",
]
