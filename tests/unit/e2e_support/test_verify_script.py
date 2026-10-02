"""``scripts/verify_fixture_answer_key.py`` (called by the factory CI with ``--strict``)."""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

from tests.unit.e2e_support.test_verify import MATCH, perfect_snapshot

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "verify_fixture_answer_key.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("verify_fixture_answer_key", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


script = _load()


def test_strict_answer_key_only_passes(capsys: pytest.CaptureFixture[str]) -> None:
    assert script.main(["--strict"]) == 0
    out = capsys.readouterr().out
    assert "answer key: 32 pairs, OK" in out and "PASS" in out


def _results(tmp_path: Path, first: dict[str, object], second: dict[str, object]) -> Path:
    path = tmp_path / "run.json"
    path.write_text(json.dumps({"run1": first, "run2": second}), encoding="utf-8")
    return path


def test_results_file_of_a_correct_run_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    first = perfect_snapshot()
    second = copy.deepcopy(first)
    second["summary"] = {"candidates": []}
    assert script.main(["--strict", "--results", str(_results(tmp_path, first, second))]) == 0
    out = capsys.readouterr().out
    assert "32/32 PASS" in out and "second run (idempotency): OK" in out


def test_failures_exit_1_only_under_strict(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    first = perfect_snapshot()
    first["messages"][MATCH]["invoice"]["labels"] = []
    second = copy.deepcopy(first)
    second["ledger"].append(list(second["ledger"][0]))
    path = _results(tmp_path, first, second)
    assert script.main(["--strict", "--results", str(path)]) == 1
    out = capsys.readouterr().out
    assert "31/32 PASS" in out and "ledger rows 32 -> 33" in out and "FAIL" in out
    assert script.main(["--results", str(path)]) == 0


def test_results_without_a_first_run_fail(tmp_path: Path) -> None:
    path = tmp_path / "partial.json"
    path.write_text(json.dumps({"run_id": "x"}), encoding="utf-8")
    assert script.main(["--strict", "--results", str(path)]) == 1


def test_spreadsheet_needs_folder() -> None:
    with pytest.raises(SystemExit):
        script.main(["--spreadsheet", "abc"])


# --- release gate: a gate without a run must fail ---------------------------------------


def test_require_run_fails_without_a_run(capsys: pytest.CaptureFixture[str]) -> None:
    assert script.main(["--strict", "--require-run"]) == 1
    assert "no E2E run to verify" in capsys.readouterr().out


def test_require_run_fails_without_a_second_run(tmp_path: Path) -> None:
    path = tmp_path / "one-run.json"
    path.write_text(json.dumps({"run1": perfect_snapshot()}), encoding="utf-8")
    assert script.main(["--strict", "--require-run", "--results", str(path)]) == 1


def test_require_run_passes_a_complete_correct_run(tmp_path: Path) -> None:
    first = perfect_snapshot()
    second = copy.deepcopy(first)
    second["summary"] = {"candidates": []}
    path = _results(tmp_path, first, second)
    assert script.main(["--strict", "--require-run", "--results", str(path)]) == 0
