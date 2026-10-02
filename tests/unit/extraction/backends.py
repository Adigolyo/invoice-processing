"""Hermetic fakes for every extraction backend, behind one test harness.

``Backend`` lets the shared contract tests (``test_extractor_contract.py``) hold the
Gemini and the Claude (AI Compass) extractors to exactly the same bar: each backend
builds an extractor around a recording fake client that replays queued response texts
or exceptions, and knows how to read the document, instructions and schema back out of
a recorded request. No network, no API key.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest
from anthropic.types import Message
from google.genai import types

from intake.extraction import Extractor

Outcome = str | None | Exception | Message


# --- Gemini -----------------------------------------------------------------------------


class FakeGeminiModels:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    def generate_content(self, *, model: str, contents: Any, config: Any = None) -> Any:
        self.calls.append({"model": model, "contents": contents, "config": config})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeGeminiClient:
    def __init__(self, *outcomes: Outcome) -> None:
        self.models = FakeGeminiModels([_gemini_response(o) for o in outcomes])

    @property
    def calls(self) -> list[dict[str, Any]]:
        return self.models.calls


def _gemini_response(outcome: Outcome) -> Any:
    if isinstance(outcome, Exception):
        return outcome
    if outcome is None:
        return types.GenerateContentResponse(candidates=[])
    assert isinstance(outcome, str)
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(content=types.Content(role="model", parts=[types.Part(text=outcome)]))
        ]
    )


def _gemini_document(call: dict[str, Any]) -> tuple[str, bytes]:
    (part,) = [p for p in call["contents"] if isinstance(p, types.Part) and p.inline_data]
    assert part.inline_data is not None
    assert part.inline_data.mime_type is not None and part.inline_data.data is not None
    return part.inline_data.mime_type, part.inline_data.data


def _gemini_instructions(call: dict[str, Any]) -> str:
    config: types.GenerateContentConfig = call["config"]
    texts = [str(config.system_instruction)]
    texts.extend(p.text for p in call["contents"] if isinstance(p, types.Part) and p.text)
    return "\n".join(texts)


def _gemini_schema(call: dict[str, Any]) -> Any:
    return call["config"].response_json_schema


# --- Claude via AI Compass --------------------------------------------------------------


def claude_message(
    text: str | None,
    *,
    stop_reason: str = "end_turn",
    stop_details: dict[str, Any] | None = None,
    input_tokens: int = 1234,
    output_tokens: int = 321,
) -> Message:
    """An ``anthropic.types.Message`` as the gateway returns it (one text block)."""
    content = [] if text is None else [{"type": "text", "text": text}]
    return Message.model_validate(
        {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-5-5",
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "stop_details": stop_details,
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        }
    )


class FakeClaudeMessages:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeClaudeClient:
    def __init__(self, *outcomes: Outcome) -> None:
        self.messages = FakeClaudeMessages([_claude_response(o) for o in outcomes])

    @property
    def calls(self) -> list[dict[str, Any]]:
        return self.messages.calls


def _claude_response(outcome: Outcome) -> Any:
    if isinstance(outcome, Exception | Message):
        return outcome
    return claude_message(outcome)


def claude_content(call: dict[str, Any]) -> list[dict[str, Any]]:
    (message,) = call["messages"]
    assert message["role"] == "user"
    content: list[dict[str, Any]] = message["content"]
    return content


def _claude_document(call: dict[str, Any]) -> tuple[str, bytes]:
    (block,) = [b for b in claude_content(call) if b["type"] in ("document", "image")]
    source = block["source"]
    assert source["type"] == "base64"
    return source["media_type"], base64.b64decode(source["data"], validate=True)


def _claude_instructions(call: dict[str, Any]) -> str:
    texts = [call["system"]]
    texts.extend(b["text"] for b in claude_content(call) if b["type"] == "text")
    return "\n".join(texts)


def _claude_schema(call: dict[str, Any]) -> Any:
    return call["output_config"]["format"]["schema"]


# --- harness ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Backend:
    name: str
    make_client: Callable[..., Any]
    make_extractor: Callable[[Any, dict[str, str]], Extractor]
    document: Callable[[dict[str, Any]], tuple[str, bytes]]
    instructions: Callable[[dict[str, Any]], str]
    schema: Callable[[dict[str, Any]], Any]
    # Patches the SDK client constructor so ``from_env`` builds a fake; returns the
    # env that selects this backend with ``api_key``.
    patch_from_env: Callable[[pytest.MonkeyPatch, Any, str], dict[str, str]]
    image_mime_types: tuple[str, ...]

    def extractor(
        self, *outcomes: Outcome, currency_map: dict[str, str] | None = None
    ) -> tuple[Extractor, Any]:
        client = self.make_client(*outcomes)
        return self.make_extractor(client, {} if currency_map is None else currency_map), client


def _make_gemini(client: Any, currency_map: dict[str, str]) -> Extractor:
    from intake.extraction import GeminiExtractor

    return GeminiExtractor(client, currency_map=currency_map)


def _make_claude(client: Any, currency_map: dict[str, str]) -> Extractor:
    from intake.extraction.claude_extractor import ClaudeExtractor

    return ClaudeExtractor(client, currency_map=currency_map)


def _patch_gemini(monkeypatch: pytest.MonkeyPatch, client: Any, api_key: str) -> dict[str, str]:
    from intake.extraction import gemini_extractor

    monkeypatch.setattr(gemini_extractor.genai, "Client", lambda **_: client)
    return {"EXTRACTOR_BACKEND": "gemini", "GEMINI_API_KEY": api_key}


def _patch_claude(monkeypatch: pytest.MonkeyPatch, client: Any, api_key: str) -> dict[str, str]:
    from intake.extraction import claude_extractor

    monkeypatch.setattr(claude_extractor.anthropic, "Anthropic", lambda **_: client)
    return {"EXTRACTOR_BACKEND": "ai_compass", "AI_COMPASS_API_KEY": api_key}


GEMINI = Backend(
    name="gemini",
    make_client=FakeGeminiClient,
    make_extractor=_make_gemini,
    document=_gemini_document,
    instructions=_gemini_instructions,
    schema=_gemini_schema,
    patch_from_env=_patch_gemini,
    image_mime_types=("image/jpeg", "image/png", "image/webp"),
)
CLAUDE = Backend(
    name="ai_compass",
    make_client=FakeClaudeClient,
    make_extractor=_make_claude,
    document=_claude_document,
    instructions=_claude_instructions,
    schema=_claude_schema,
    patch_from_env=_patch_claude,
    image_mime_types=("image/jpeg", "image/png", "image/webp"),
)
BACKENDS = (GEMINI, CLAUDE)
