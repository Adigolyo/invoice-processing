"""Gemini multimodal extraction of invoice and TIG documents (USR-002-01, ADR 4).

One ``generate_content`` call per document: the whole PDF or image goes inline as a
single part (Gemini reads every page of a multi-page PDF natively) and the answer is
constrained by a JSON schema (structured output). The schema, prompts, validation,
conversion and result contract are shared with the other backends and live in
``intake.extraction.common``; see ``interpret_response`` there for the result contract.

Gemini/API/network errors are **raised** unchanged: the orchestrator aborts the
candidate before any label change and it is retried on the next run.

Neither document content nor the API key is ever logged; log records carry only the
kind, MIME type, size, model, outcome and a SHA-256 of the response text.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Final

from google import genai
from google.genai import types

from intake.clients.auth import SecretAccessor
from intake.extraction.common import (
    HEADER_FIELDS,
    INVOICE_REQUIRED_FIELDS,
    LINE_ITEM_FIELDS,
    RESPONSE_JSON_SCHEMA,
    SUPPORTED_MIME_TYPES,
    SYSTEM_PROMPTS,
    USER_TEXT,
    DocumentKind,
    Extractor,
    UnsupportedDocumentError,
    check_document,
    interpret_response,
    resolve_key,
    response_sha256,
    secret_id,
)
from intake.models import InvoiceExtraction, StageResult

logger = logging.getLogger(__name__)

# Current recommended Gemini model for new projects; the Gemini API document
# understanding guide uses it for PDF input (ai.google.dev/gemini-api/docs/models,
# .../document-processing, checked 2026-10-02). Override with GEMINI_MODEL.
DEFAULT_GEMINI_MODEL: Final = "gemini-3.8-flash"
GEMINI_MODEL_ENV: Final = "GEMINI_MODEL"
GEMINI_API_KEY_ENV: Final = "GEMINI_API_KEY"
GEMINI_API_KEY_SECRET_PREFIX: Final = "kibit-gemini-api-key"
REQUEST_TIMEOUT_MS: Final = 120_000


# --- configuration ----------------------------------------------------------------------


def model_from_env(env: Mapping[str, str]) -> str:
    """The Gemini model ID from ``GEMINI_MODEL``, or ``DEFAULT_GEMINI_MODEL``."""
    return env.get(GEMINI_MODEL_ENV, "").strip() or DEFAULT_GEMINI_MODEL


def gemini_api_key_secret_id(environment: str) -> str:
    """Secret Manager ID of the Gemini API key, e.g. ``kibit-gemini-api-key-staging``."""
    return secret_id(GEMINI_API_KEY_SECRET_PREFIX, environment)


def resolve_api_key(env: Mapping[str, str], accessor: SecretAccessor | None = None) -> str:
    """The Gemini API key: ``GEMINI_API_KEY`` (local dev) or Secret Manager.

    Without ``GEMINI_API_KEY``, the key is read from ``kibit-gemini-api-key-<ENVIRONMENT>``
    in project ``GCP_PROJECT_ID``. Errors never include the key.
    """
    return resolve_key(
        env,
        local_env=GEMINI_API_KEY_ENV,
        secret_prefix=GEMINI_API_KEY_SECRET_PREFIX,
        label="Gemini API key",
        accessor=accessor,
    )


class GeminiExtractor:
    """``Extractor`` backed by the Gemini API (``google-genai``)."""

    def __init__(
        self,
        client: genai.Client,
        *,
        model: str = DEFAULT_GEMINI_MODEL,
        currency_map: Mapping[str, str] | None = None,
    ) -> None:
        if not model.strip():
            raise ValueError("a Gemini model ID is required")
        self._client = client
        self.model = model.strip()
        # Config's symbol -> ISO map, used only to read line-item amounts (HUF rule).
        self._currency_map: Mapping[str, str] = dict(currency_map or {})

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str],
        *,
        currency_map: Mapping[str, str] | None = None,
        accessor: SecretAccessor | None = None,
    ) -> GeminiExtractor:
        """Build from ``GEMINI_MODEL`` and the key ``resolve_api_key`` finds."""
        client = genai.Client(
            api_key=resolve_api_key(env, accessor),
            http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_MS),
        )
        return cls(client, model=model_from_env(env), currency_map=currency_map)

    def __repr__(self) -> str:
        return f"GeminiExtractor(model={self.model!r})"

    def extract(
        self, content: bytes, mime_type: str, *, kind: DocumentKind = DocumentKind.INVOICE
    ) -> StageResult[InvoiceExtraction]:
        """Extract one document; see ``common.interpret_response`` for the contract."""
        mime = check_document(content, mime_type)

        response = self._client.models.generate_content(
            model=self.model,
            contents=[
                types.Part.from_bytes(data=content, mime_type=mime),
                types.Part(text=USER_TEXT[kind]),
            ],
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPTS[kind],
                response_mime_type="application/json",
                response_json_schema=RESPONSE_JSON_SCHEMA,
                temperature=0,
            ),
        )
        text = response.text
        return interpret_response(
            text,
            kind=kind,
            currency_map=self._currency_map,
            provider="Gemini",
            logger=logger,
            log_fields={
                "document_kind": kind.value,
                "mime_type": mime,
                "size_bytes": len(content),
                "model": self.model,
                "response_sha256": response_sha256(text),
            },
        )


__all__ = [
    "DEFAULT_GEMINI_MODEL",
    "GEMINI_API_KEY_ENV",
    "GEMINI_MODEL_ENV",
    "HEADER_FIELDS",
    "INVOICE_REQUIRED_FIELDS",
    "LINE_ITEM_FIELDS",
    "RESPONSE_JSON_SCHEMA",
    "SUPPORTED_MIME_TYPES",
    "DocumentKind",
    "Extractor",
    "GeminiExtractor",
    "UnsupportedDocumentError",
    "gemini_api_key_secret_id",
    "model_from_env",
    "resolve_api_key",
]
