"""Task 24 release gate: the 32 TIG/invoice fixture pairs through the live pipeline, twice.

Covers the technical design's E2E 2 (clean contractor invoice + matching TIG ->
Processed, no draft), E2E 3 (TIG mismatch -> filed, booked, one draft, Pending) and
E2E 6 (second back-to-back run: no new rows, files or drafts, labels unchanged), with the
acceptance bar of Task 24: zero wrong ledger rows, correct filenames/folders, correct
final label per invoice, gap-free sequences. See ``tests/e2e/README.md``.

Each test reports every failure it finds as a full per-pair table, never only the first.
Run: ``pytest -m e2e tests/e2e -s``
"""

from __future__ import annotations

import pytest

from tests.e2e.fixture_set import verify
from tests.e2e.fixture_set.conftest import E2ERun

pytestmark = [pytest.mark.live, pytest.mark.e2e]


def test_every_pair_is_filed_booked_labelled_and_drafted_per_answer_key(e2e_run: E2ERun) -> None:
    """E2E 2 + E2E 3, per pair: label + read, ledger row, registry number, Drive file, draft."""
    verdicts = verify.evaluate_pairs(e2e_run.pairs, e2e_run.first)
    if not all(v.passed for v in verdicts):
        pytest.fail(
            f"run {e2e_run.run_id}: pairs failing the answer key\n"
            f"{verify.format_table(verdicts)}\nresults: {e2e_run.results_path}",
            pytrace=False,
        )


def test_run_wide_invariants_hold(e2e_run: E2ERun) -> None:
    """Gap-free sequences per month, no stray rows/files, 7 drafts, TIG mails untouched,
    and no error / needs_review / awaiting_tig result for this run's messages."""
    problems = verify.global_problems(e2e_run.pairs, e2e_run.first)
    if problems:
        pytest.fail(
            f"run {e2e_run.run_id}: run-wide problems\n" + "\n".join(problems), pytrace=False
        )


def test_second_run_changes_nothing(e2e_run: E2ERun) -> None:
    """E2E 6: re-running over the same inbox adds no rows, files or drafts, keeps labels."""
    problems = verify.idempotency_problems(e2e_run.first, e2e_run.second)
    if problems:
        pytest.fail(
            f"run {e2e_run.run_id}: second run was not idempotent\n" + "\n".join(problems),
            pytrace=False,
        )
