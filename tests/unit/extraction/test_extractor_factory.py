"""``extractor_from_env``: the orchestrator's single entry point for picking a backend."""

from __future__ import annotations

from typing import Any

import pytest

from intake.clients.auth import AuthConfigurationError
from intake.extraction import (
    EXTRACTOR_BACKEND_ENV,
    ClaudeExtractor,
    Extractor,
    GeminiExtractor,
    claude_extractor,
    extractor_from_env,
    gemini_extractor,
)
from tests.unit.extraction.backends import FakeClaudeClient, FakeGeminiClient


@pytest.fixture
def created(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[dict[str, Any]]]:
    calls: dict[str, list[dict[str, Any]]] = {"anthropic": [], "genai": []}

    def fake_anthropic(**kwargs: Any) -> FakeClaudeClient:
        calls["anthropic"].append(kwargs)
        return FakeClaudeClient()

    def fake_genai(**kwargs: Any) -> FakeGeminiClient:
        calls["genai"].append(kwargs)
        return FakeGeminiClient()

    monkeypatch.setattr(claude_extractor.anthropic, "Anthropic", fake_anthropic)
    monkeypatch.setattr(gemini_extractor.genai, "Client", fake_genai)
    return calls


@pytest.mark.parametrize("value", [None, "", "  ", "ai_compass", "AI_COMPASS", " ai_compass "])
def test_ai_compass_is_the_default_backend(
    created: dict[str, list[dict[str, Any]]], value: str | None
) -> None:
    env = {"AI_COMPASS_API_KEY": "k"}
    if value is not None:
        env[EXTRACTOR_BACKEND_ENV] = value

    extractor = extractor_from_env(env)

    assert isinstance(extractor, ClaudeExtractor)
    assert isinstance(extractor, Extractor)
    assert len(created["anthropic"]) == 1 and created["genai"] == []


def test_gemini_is_selectable(created: dict[str, list[dict[str, Any]]]) -> None:
    extractor = extractor_from_env({EXTRACTOR_BACKEND_ENV: "gemini", "GEMINI_API_KEY": "g"})

    assert isinstance(extractor, GeminiExtractor)
    assert created["anthropic"] == []
    assert created["genai"][0]["api_key"] == "g"


def test_gemini_key_is_not_needed_for_the_default_backend(
    created: dict[str, list[dict[str, Any]]],
) -> None:
    extractor_from_env({"AI_COMPASS_API_KEY": "k"})  # no GEMINI_API_KEY anywhere


def test_unknown_backend_is_a_clear_error(created: dict[str, list[dict[str, Any]]]) -> None:
    with pytest.raises(ValueError) as excinfo:
        extractor_from_env({EXTRACTOR_BACKEND_ENV: "openai", "AI_COMPASS_API_KEY": "k"})

    message = str(excinfo.value)
    assert "EXTRACTOR_BACKEND" in message and "openai" in message
    assert "ai_compass" in message and "gemini" in message
    assert created["anthropic"] == [] and created["genai"] == []


def test_currency_map_and_accessor_are_passed_through(
    created: dict[str, list[dict[str, Any]]],
) -> None:
    name = "projects/p/secrets/kibit-ai-compass-api-key-staging/versions/latest"
    requested: list[str] = []

    def accessor(secret: str) -> str:
        requested.append(secret)
        return "from-secret-manager"

    extractor = extractor_from_env(
        {"GCP_PROJECT_ID": "p", "ENVIRONMENT": "staging"},
        currency_map={"Ft": "HUF"},
        accessor=accessor,
    )

    assert isinstance(extractor, ClaudeExtractor)
    assert requested == [name]
    assert created["anthropic"][0]["api_key"] == "from-secret-manager"


def test_missing_key_configuration_propagates(created: dict[str, list[dict[str, Any]]]) -> None:
    with pytest.raises(AuthConfigurationError):
        extractor_from_env({})
