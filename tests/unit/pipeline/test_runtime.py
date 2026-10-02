"""Task 21: wiring real clients from the environment (no Google/model API is called)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any
from unittest.mock import MagicMock

import pytest

from intake.clients.drive_client import DriveClient
from intake.clients.gmail_client import GmailClient
from intake.clients.sheets_client import SheetsClient
from intake.config import Config, ConfigError
from intake.pipeline import runtime
from intake.pipeline.orchestrator import PipelineClients, RunSummary
from tests.unit.pipeline.fakes import FakeExtractor, make_config

ENV = {"LEDGER_SPREADSHEET_ID": "sheet-123"}


class Captured:
    sheets: Any = None
    connect: Callable[[Config], PipelineClients] | None = None


def _capture(monkeypatch: pytest.MonkeyPatch) -> Captured:
    captured = Captured()

    def fake_cycle(sheets: Any, connect: Callable[[Config], PipelineClients]) -> RunSummary:
        captured.sheets, captured.connect = sheets, connect
        return RunSummary(())

    monkeypatch.setattr(runtime, "run_cycle", fake_cycle)
    return captured


def _builder() -> tuple[list[tuple[str, str]], Callable[[str, str, Any], Any]]:
    built: list[tuple[str, str]] = []

    def build(api: str, version: str, credentials: Any) -> Any:
        built.append((api, version))
        return MagicMock(name=api)

    return built, build


def test_run_with_credentials_wires_sheets_gmail_drive_and_extractor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture(monkeypatch)
    built, build = _builder()
    extractor = FakeExtractor()
    seen: list[Mapping[str, str]] = []

    def factory(env: Mapping[str, str], config: Config) -> FakeExtractor:
        seen.append(env)
        return extractor

    summary = runtime.run_with_credentials(
        object(), ENV, service_builder=build, extractor_factory=factory
    )

    assert summary.to_dict()["processed"] == 0
    assert isinstance(captured.sheets, SheetsClient)
    assert captured.connect is not None
    clients = captured.connect(make_config())
    assert isinstance(clients.gmail, GmailClient)
    assert isinstance(clients.drive, DriveClient)
    assert clients.extractor is extractor
    assert seen == [ENV]
    assert built == [("sheets", "v4"), ("gmail", "v1"), ("drive", "v3")]


def test_missing_spreadsheet_id_fails_before_any_api_call() -> None:
    built, build = _builder()

    with pytest.raises(runtime.PipelineConfigurationError, match="LEDGER_SPREADSHEET_ID"):
        runtime.run_with_credentials(object(), {}, service_builder=build)
    assert built == []


def test_drive_root_env_must_agree_with_the_config_tab(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture(monkeypatch)
    _, build = _builder()
    env = {**ENV, "DRIVE_ROOT_FOLDER_ID": "some-other-folder"}

    runtime.run_with_credentials(
        object(), env, service_builder=build, extractor_factory=lambda e, c: FakeExtractor()
    )

    assert captured.connect is not None
    with pytest.raises(ConfigError, match="DRIVE_ROOT_FOLDER_ID"):
        captured.connect(make_config())
    # Agreeing values are fine.
    captured.connect(make_config(drive_root_folder_id="some-other-folder"))


def test_build_extractor_uses_the_backend_selector(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    sentinel = FakeExtractor()

    def selector(env: Mapping[str, str], **kwargs: Any) -> FakeExtractor:
        calls.append({"env": env, **kwargs})
        return sentinel

    monkeypatch.setattr(runtime, "extractor_from_env", selector)

    assert runtime.build_extractor(ENV, make_config()) is sentinel
    assert calls[0]["currency_map"] == make_config().currency_map


def test_run_from_env_reads_credentials_from_secret_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}
    credentials = object()
    monkeypatch.setattr(runtime, "credentials_from_env", lambda env: credentials)

    def fake_run(creds: Any, env: Mapping[str, str]) -> RunSummary:
        seen.update(creds=creds, env=env)
        return RunSummary(())

    monkeypatch.setattr(runtime, "run_with_credentials", fake_run)

    runtime.run_from_env(ENV)

    assert seen == {"creds": credentials, "env": ENV}


def test_default_service_builder_disables_the_discovery_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def fake_build(api: str, version: str, **kwargs: Any) -> str:
        seen.update(api=api, version=version, **kwargs)
        return "service"

    monkeypatch.setattr(runtime.discovery, "build", fake_build)
    creds = object()

    assert runtime.build_service("gmail", "v1", creds) == "service"
    assert seen == {
        "api": "gmail",
        "version": "v1",
        "credentials": creds,
        "cache_discovery": False,
    }
