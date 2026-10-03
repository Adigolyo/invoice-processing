"""Task 20 / USR-005-03, USR-005-04: pending-label trigger and missing-TIG fallback rules.

Pure decision functions only: no Gmail, Drive or Sheets client is touched. The labelling
mechanism itself (``apply_label``/``mark_read``) belongs to the orchestrator (Task 21).
The data-driven test replays the 32 fixture pairs of ``answer_key.json`` through
``compare`` and then ``tig_outcome``.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from intake.config import LabelNames
from intake.models import (
    ComparisonResult,
    Discrepancy,
    DiscrepancyField,
    FlagReason,
    InvoiceExtraction,
    LineItem,
    Route,
)
from intake.reconciliation import NotTigRouteError, TigDocument, TigLookup, compare
from intake.reconciliation.outcome_rules import (
    Outcome,
    is_reevaluated,
    label_to_apply,
    missing_tig_outcome,
    needs_evaluation,
    outcome_label,
    pending_outcome,
    prior_outcome,
    should_file_and_book,
    superseded_labels,
    tig_outcome,
)

ANSWER_KEY = Path(__file__).resolve().parents[2] / "fixtures" / "tig_pairs" / "answer_key.json"
D = Decimal

LABELS = LabelNames(
    processed="Kibit/Processed",
    pending="Kibit/Pending",
    needs_review="Kibit/NeedsReview",
    awaiting_tig="Kibit/AwaitingTIG",
)


# --- builders ---------------------------------------------------------------------------------


def _tig_document() -> TigDocument:
    return TigDocument(
        message_id="m-tig",
        filename="TIG-2026-09-KEK-A.pdf",
        lines=(LineItem("Fejlesztés", D("10"), D("8000"), D("80000")),),
    )


def _found() -> TigLookup:
    return TigLookup.found_document(_tig_document())


def _missing() -> TigLookup:
    return TigLookup.missing()


def _unreadable() -> TigLookup:
    return TigLookup.unreadable("TIG line 0 has an unreadable quantity", "m-tig", "TIG.pdf")


def _ambiguous() -> TigLookup:
    return TigLookup.ambiguous("2 TIG attachments in message m-tig", "m-tig")


def _mismatch() -> ComparisonResult:
    return ComparisonResult.mismatch(
        [Discrepancy(DiscrepancyField.QUANTITY, D("12"), D("10"), 0, "Fejlesztés")]
    )


def _match() -> ComparisonResult:
    return ComparisonResult.match()


# --- Outcome ------------------------------------------------------------------------------------


def test_outcome_has_exactly_the_four_adr3_states() -> None:
    assert {o.value for o in Outcome} == {
        "processed",
        "pending",
        "needs_review",
        "awaiting_tig",
        "duplicate",
    }


def test_outcome_values_are_strings_for_the_run_summary() -> None:
    assert Outcome.AWAITING_TIG == "awaiting_tig"
    assert isinstance(Outcome.PENDING, str)


# --- pending rule (USR-005-03) ----------------------------------------------------------------


def test_pending_outcome_is_true_for_a_recorded_mismatch() -> None:
    assert pending_outcome(_mismatch()) is True


def test_pending_outcome_is_false_for_a_clean_match() -> None:
    assert pending_outcome(_match()) is False


def test_found_tig_with_mismatch_yields_pending() -> None:
    """USR-005-03 AC2: a mismatch is labelled "pending" instead of "processed"."""
    assert tig_outcome(Route.TIG, _found(), _mismatch()) is Outcome.PENDING


def test_found_tig_with_clean_match_yields_processed_not_pending() -> None:
    """USR-005-03 AC3 / QA: a clean TIG match never receives the pending label."""
    assert tig_outcome(Route.TIG, _found(), _match()) is Outcome.PROCESSED


def test_mismatch_is_still_filed_and_booked() -> None:
    """USR-005-03 AC1: a TIG mismatch never skips or blocks filing and booking."""
    outcome = tig_outcome(Route.TIG, _found(), _mismatch())

    assert should_file_and_book(outcome) is True
    assert should_file_and_book(outcome) == should_file_and_book(Outcome.PROCESSED)


def test_every_discrepancy_type_yields_pending() -> None:
    for field in DiscrepancyField:
        comparison = ComparisonResult.mismatch([Discrepancy(field, D("1"), D("2"))])
        assert tig_outcome(Route.TIG, _found(), comparison) is Outcome.PENDING


# --- missing-TIG rule (USR-005-04) ------------------------------------------------------------


def test_missing_tig_outcome_true_for_tig_route_without_any_tig() -> None:
    assert missing_tig_outcome(Route.TIG, _missing()) is True


def test_no_tig_in_thread_yields_processed() -> None:
    """A TIG is always sent out first and the invoice is the reply, so an invoice whose
    thread holds no TIG has nothing to reconcile: it is filed, booked and processed
    (project owner decision, 2026-10-03; replaces USR-005-04's AwaitingTIG)."""
    outcome = tig_outcome(Route.TIG, _missing())
    assert outcome is Outcome.PROCESSED
    assert should_file_and_book(outcome) is True


def test_awaiting_tig_is_neither_filed_nor_booked() -> None:
    """USR-005-04 AC1: nothing filed, nothing booked."""
    assert should_file_and_book(Outcome.AWAITING_TIG) is False


def test_awaiting_tig_is_reevaluated_next_run() -> None:
    assert is_reevaluated(Outcome.AWAITING_TIG) is True


@pytest.mark.parametrize("outcome", [Outcome.PROCESSED, Outcome.PENDING, Outcome.NEEDS_REVIEW])
def test_other_outcomes_are_terminal_for_automation(outcome: Outcome) -> None:
    assert is_reevaluated(outcome) is False


def test_found_but_mismatched_tig_takes_the_pending_path_not_the_fallback() -> None:
    """USR-005-04 AC2."""
    lookup = _found()

    assert missing_tig_outcome(Route.TIG, lookup) is False
    assert tig_outcome(Route.TIG, lookup, _mismatch()) is Outcome.PENDING


@pytest.mark.parametrize("lookup", [_found(), _unreadable(), _ambiguous()])
def test_missing_tig_outcome_false_whenever_a_tig_exists(lookup: TigLookup) -> None:
    assert missing_tig_outcome(Route.TIG, lookup) is False


@pytest.mark.parametrize("comparison", [_match(), _mismatch()])
def test_tig_arriving_later_lets_normal_reconciliation_proceed(
    comparison: ComparisonResult,
) -> None:
    """A thread labelled AwaitingTIG (before the rule change) is still re-evaluated, and
    a TIG found there is reconciled normally."""
    assert needs_evaluation({LABELS.awaiting_tig}, LABELS) is True

    second = tig_outcome(Route.TIG, _found(), comparison)

    expected = Outcome.PENDING if comparison.is_mismatch else Outcome.PROCESSED
    assert second is expected
    assert should_file_and_book(second) is True
    assert label_to_apply(second, {LABELS.awaiting_tig}, LABELS) == outcome_label(second, LABELS)
    assert superseded_labels(second, {LABELS.awaiting_tig}, LABELS) == {LABELS.awaiting_tig}


def test_legacy_awaiting_tig_thread_without_a_tig_becomes_processed() -> None:
    current = {LABELS.awaiting_tig, "INBOX", "UNREAD"}
    outcome = tig_outcome(Route.TIG, _missing())

    assert outcome is Outcome.PROCESSED
    assert label_to_apply(outcome, current, LABELS) == LABELS.processed
    assert superseded_labels(outcome, current, LABELS) == {LABELS.awaiting_tig}
    assert should_file_and_book(outcome) is True


@pytest.mark.parametrize("route", [Route.DIRECT, Route.AMBIGUOUS])
def test_missing_tig_fallback_never_applies_to_non_tig_routes(route: Route) -> None:
    """USR-005-04 AC6: never evaluated for direct-route invoices, regardless of thread."""
    assert missing_tig_outcome(route, None) is False
    assert missing_tig_outcome(route, _missing()) is False


@pytest.mark.parametrize("route", [Route.DIRECT, Route.AMBIGUOUS])
def test_tig_outcome_refuses_non_tig_routes(route: Route) -> None:
    with pytest.raises(NotTigRouteError):
        tig_outcome(route, _missing())
    with pytest.raises(NotTigRouteError):
        tig_outcome(route, _found(), _mismatch())


def test_missing_tig_outcome_requires_a_lookup_for_the_tig_route() -> None:
    with pytest.raises(ValueError, match="lookup"):
        missing_tig_outcome(Route.TIG, None)


# --- unusable TIG (UNREADABLE / AMBIGUOUS) ----------------------------------------------------


@pytest.mark.parametrize("lookup", [_unreadable(), _ambiguous()])
def test_unusable_tig_yields_needs_review(lookup: TigLookup) -> None:
    outcome = tig_outcome(Route.TIG, lookup)

    assert outcome is Outcome.NEEDS_REVIEW
    assert should_file_and_book(outcome) is False
    assert is_reevaluated(outcome) is False


def test_needs_review_mapping_agrees_with_the_lookup_flag_reason() -> None:
    assert _unreadable().flag_reason is FlagReason.INCOMPLETE_DATA
    assert _missing().flag_reason is FlagReason.MISSING_TIG
    assert tig_outcome(Route.TIG, _unreadable()) is Outcome.NEEDS_REVIEW
    assert tig_outcome(Route.TIG, _missing()) is Outcome.PROCESSED


# --- contract guards --------------------------------------------------------------------------


def test_found_tig_without_comparison_is_rejected() -> None:
    with pytest.raises(ValueError, match="comparison"):
        tig_outcome(Route.TIG, _found())


@pytest.mark.parametrize("lookup", [_missing(), _unreadable(), _ambiguous()])
def test_comparison_without_a_found_tig_is_rejected(lookup: TigLookup) -> None:
    with pytest.raises(ValueError, match="comparison"):
        tig_outcome(Route.TIG, lookup, _match())


def test_tig_outcome_is_deterministic() -> None:
    cases: list[tuple[TigLookup, ComparisonResult | None]] = [
        (_found(), _match()),
        (_found(), _mismatch()),
        (_missing(), None),
        (_unreadable(), None),
        (_ambiguous(), None),
    ]
    for lookup, comparison in cases:
        results = {tig_outcome(Route.TIG, lookup, comparison) for _ in range(50)}
        assert len(results) == 1


# --- filing / booking -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (Outcome.PROCESSED, True),
        (Outcome.PENDING, True),
        (Outcome.AWAITING_TIG, False),
        (Outcome.NEEDS_REVIEW, False),
    ],
)
def test_should_file_and_book(outcome: Outcome, expected: bool) -> None:
    assert should_file_and_book(outcome) is expected


# --- labels and idempotency -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("outcome", "label"),
    [
        (Outcome.PROCESSED, "Kibit/Processed"),
        (Outcome.PENDING, "Kibit/Pending"),
        (Outcome.NEEDS_REVIEW, "Kibit/NeedsReview"),
        (Outcome.AWAITING_TIG, "Kibit/AwaitingTIG"),
    ],
)
def test_outcome_label_uses_the_configured_names(outcome: Outcome, label: str) -> None:
    assert outcome_label(outcome, LABELS) == label


def test_outcome_label_is_not_hardcoded() -> None:
    custom = LabelNames(
        processed="P", pending="Q", needs_review="R", awaiting_tig="S", duplicate="D"
    )

    assert [outcome_label(o, custom) for o in Outcome] == ["P", "Q", "R", "S", "D"]


@pytest.mark.parametrize(
    ("current", "expected"),
    [
        (set(), None),
        ({"INBOX", "UNREAD"}, None),
        ({"Kibit/Processed"}, Outcome.PROCESSED),
        ({"Kibit/Pending"}, Outcome.PENDING),
        ({"Kibit/NeedsReview"}, Outcome.NEEDS_REVIEW),
        ({"Kibit/AwaitingTIG", "INBOX"}, Outcome.AWAITING_TIG),
        # A terminal label wins over a stale AwaitingTIG; Pending wins over the rest.
        ({"Kibit/AwaitingTIG", "Kibit/Pending"}, Outcome.PENDING),
        ({"Kibit/AwaitingTIG", "Kibit/Processed"}, Outcome.PROCESSED),
        ({"Kibit/Processed", "Kibit/Pending"}, Outcome.PENDING),
    ],
)
def test_prior_outcome(current: set[str], expected: Outcome | None) -> None:
    assert prior_outcome(current, LABELS) is expected


@pytest.mark.parametrize(
    ("current", "expected"),
    [
        (set(), True),
        ({"Kibit/AwaitingTIG"}, True),
        ({"Kibit/Pending"}, False),
        ({"Kibit/Processed"}, False),
        ({"Kibit/NeedsReview"}, False),
        ({"Kibit/AwaitingTIG", "Kibit/Pending"}, False),
    ],
)
def test_needs_evaluation(current: set[str], expected: bool) -> None:
    assert needs_evaluation(current, LABELS) is expected


def test_already_pending_thread_is_not_retriggered() -> None:
    """USR-005-03 AC4/AC5: a pending thread is left as is, label never re-applied."""
    current = frozenset({LABELS.pending})

    assert needs_evaluation(current, LABELS) is False
    assert prior_outcome(current, LABELS) is Outcome.PENDING
    assert label_to_apply(Outcome.PENDING, current, LABELS) is None
    assert superseded_labels(Outcome.PENDING, current, LABELS) == frozenset()


def test_pending_label_is_applied_once_for_a_fresh_mismatch() -> None:
    outcome = tig_outcome(Route.TIG, _found(), _mismatch())

    assert label_to_apply(outcome, set(), LABELS) == LABELS.pending
    assert label_to_apply(outcome, {LABELS.pending}, LABELS) is None


def test_label_to_apply_ignores_non_kibit_labels() -> None:
    assert label_to_apply(Outcome.PROCESSED, {"INBOX", "IMPORTANT"}, LABELS) == LABELS.processed


def test_superseded_labels_only_names_other_kibit_outcome_labels() -> None:
    current = {"INBOX", LABELS.awaiting_tig, LABELS.pending}

    assert superseded_labels(Outcome.PENDING, current, LABELS) == {LABELS.awaiting_tig}


# --- answer key: all 32 fixture pairs ---------------------------------------------------------


def _key_line(line: dict[str, Any]) -> LineItem:
    return LineItem(
        description=line["description"],
        quantity=D(line["quantity"]),
        unit_price=D(line["unit_price"]),
        net=D(line["net"]),
    )


def _answer_key_pairs() -> list[dict[str, Any]]:
    if not ANSWER_KEY.exists():
        return []
    pairs: list[dict[str, Any]] = json.loads(ANSWER_KEY.read_text(encoding="utf-8"))["pairs"]
    return pairs


@pytest.mark.skipif(not ANSWER_KEY.exists(), reason="answer_key.json not present")
@pytest.mark.parametrize("pair", _answer_key_pairs(), ids=lambda p: p["pair"])
def test_answer_key_pair_outcome(pair: dict[str, Any]) -> None:
    """Mismatch pairs end up PENDING (still filed/booked), matching pairs PROCESSED."""
    key_tig, key_invoice = pair["tig"], pair["invoice"]
    tig = TigDocument(
        message_id="m-tig",
        filename=key_tig["file"],
        lines=tuple(_key_line(line) for line in key_tig["lines"]),
        total_net=None if key_tig["total_net"] is None else D(key_tig["total_net"]),
    )
    invoice = InvoiceExtraction(
        net=key_invoice["net_raw"],
        line_items=tuple(_key_line(line) for line in key_invoice["lines"]),
    )
    comparison = compare(
        invoice, tig, D("0.01"), route=Route.TIG, currency_iso=key_invoice["currency"]
    )

    outcome = tig_outcome(Route.TIG, TigLookup.found_document(tig), comparison)

    expected = Outcome.PENDING if pair["expected_outcome"] == "mismatch" else Outcome.PROCESSED
    assert outcome is expected
    assert should_file_and_book(outcome) is True


def test_duplicate_is_terminal_and_never_filed_or_booked() -> None:
    assert Outcome.DUPLICATE == "duplicate"
    assert should_file_and_book(Outcome.DUPLICATE) is False
    assert is_reevaluated(Outcome.DUPLICATE) is False
    assert outcome_label(Outcome.DUPLICATE, LABELS) == LABELS.duplicate
    assert needs_evaluation({LABELS.duplicate}, LABELS) is False
