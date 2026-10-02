"""Multimodal extraction of invoice and TIG documents (EPIC-002, USR-002-01)."""

from intake.extraction.gemini_extractor import (
    DEFAULT_GEMINI_MODEL,
    GEMINI_API_KEY_ENV,
    GEMINI_MODEL_ENV,
    INVOICE_REQUIRED_FIELDS,
    SUPPORTED_MIME_TYPES,
    DocumentKind,
    Extractor,
    GeminiExtractor,
    UnsupportedDocumentError,
    gemini_api_key_secret_id,
    model_from_env,
    resolve_api_key,
)
from intake.extraction.quantities import parse_quantity

__all__ = [
    "DEFAULT_GEMINI_MODEL",
    "GEMINI_API_KEY_ENV",
    "GEMINI_MODEL_ENV",
    "INVOICE_REQUIRED_FIELDS",
    "SUPPORTED_MIME_TYPES",
    "DocumentKind",
    "Extractor",
    "GeminiExtractor",
    "UnsupportedDocumentError",
    "gemini_api_key_secret_id",
    "model_from_env",
    "parse_quantity",
    "resolve_api_key",
]
