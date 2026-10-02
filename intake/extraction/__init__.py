"""Multimodal extraction of invoice and TIG documents (EPIC-002, USR-002-01).

Two interchangeable backends implement ``Extractor`` (ADR 4): Claude through Kibit's AI
Compass gateway (the default) and Gemini. ``extractor_from_env`` picks one by
``EXTRACTOR_BACKEND``.
"""

from collections.abc import Mapping
from typing import Final

from intake.clients.auth import SecretAccessor
from intake.extraction.claude_extractor import (
    AI_COMPASS_API_KEY_ENV,
    AI_COMPASS_BASE_URL_ENV,
    AI_COMPASS_EFFORT_ENV,
    AI_COMPASS_MODEL_ENV,
    DEFAULT_AI_COMPASS_MODEL,
    ClaudeExtractor,
    ai_compass_api_key_secret_id,
    resolve_ai_compass_api_key,
)
from intake.extraction.common import (
    INVOICE_REQUIRED_FIELDS,
    SUPPORTED_MIME_TYPES,
    DocumentKind,
    Extractor,
    UnsupportedDocumentError,
)
from intake.extraction.gemini_extractor import (
    DEFAULT_GEMINI_MODEL,
    GEMINI_API_KEY_ENV,
    GEMINI_MODEL_ENV,
    GeminiExtractor,
    gemini_api_key_secret_id,
    model_from_env,
    resolve_api_key,
)
from intake.extraction.quantities import parse_quantity

EXTRACTOR_BACKEND_ENV: Final = "EXTRACTOR_BACKEND"
AI_COMPASS_BACKEND: Final = "ai_compass"
GEMINI_BACKEND: Final = "gemini"
DEFAULT_EXTRACTOR_BACKEND: Final = AI_COMPASS_BACKEND


def extractor_from_env(
    env: Mapping[str, str],
    *,
    currency_map: Mapping[str, str] | None = None,
    accessor: SecretAccessor | None = None,
) -> Extractor:
    """The ``Extractor`` selected by ``EXTRACTOR_BACKEND`` (``ai_compass`` by default).

    ``ai_compass`` builds a ``ClaudeExtractor`` (``AI_COMPASS_*``), ``gemini`` a
    ``GeminiExtractor`` (``GEMINI_*``); only the selected backend's key is resolved.
    Any other value raises ``ValueError``.
    """
    backend = env.get(EXTRACTOR_BACKEND_ENV, "").strip().lower() or DEFAULT_EXTRACTOR_BACKEND
    if backend == AI_COMPASS_BACKEND:
        return ClaudeExtractor.from_env(env, currency_map=currency_map, accessor=accessor)
    if backend == GEMINI_BACKEND:
        return GeminiExtractor.from_env(env, currency_map=currency_map, accessor=accessor)
    raise ValueError(
        f"unknown {EXTRACTOR_BACKEND_ENV} {backend!r}; "
        f"expected {AI_COMPASS_BACKEND!r} (default) or {GEMINI_BACKEND!r}"
    )


__all__ = [
    "AI_COMPASS_API_KEY_ENV",
    "AI_COMPASS_BACKEND",
    "AI_COMPASS_BASE_URL_ENV",
    "AI_COMPASS_EFFORT_ENV",
    "AI_COMPASS_MODEL_ENV",
    "DEFAULT_AI_COMPASS_MODEL",
    "DEFAULT_EXTRACTOR_BACKEND",
    "DEFAULT_GEMINI_MODEL",
    "EXTRACTOR_BACKEND_ENV",
    "GEMINI_API_KEY_ENV",
    "GEMINI_BACKEND",
    "GEMINI_MODEL_ENV",
    "INVOICE_REQUIRED_FIELDS",
    "SUPPORTED_MIME_TYPES",
    "ClaudeExtractor",
    "DocumentKind",
    "Extractor",
    "GeminiExtractor",
    "UnsupportedDocumentError",
    "ai_compass_api_key_secret_id",
    "extractor_from_env",
    "gemini_api_key_secret_id",
    "model_from_env",
    "parse_quantity",
    "resolve_ai_compass_api_key",
    "resolve_api_key",
]
