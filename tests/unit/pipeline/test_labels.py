"""Task 21: the outcome -> Gmail label / read-state taxonomy (ADR 3, USR-001-03/04)."""

from __future__ import annotations

import pytest

from intake.config import ConfigError, LabelNames
from intake.pipeline.labels import (
    LabelPlan,
    marks_read,
    outcome_label_names,
    plan_labels,
    validate_label_names,
)
from intake.reconciliation.outcome_rules import Outcome
from tests.unit.pipeline.fakes import LABELS

UNREAD = frozenset({"INBOX", "UNREAD"})


@pytest.mark.parametrize(
    ("outcome", "label", "read"),
    [
        (Outcome.PROCESSED, LABELS.processed, True),
        (Outcome.PENDING, LABELS.pending, True),
        (Outcome.NEEDS_REVIEW, LABELS.needs_review, False),
        (Outcome.AWAITING_TIG, LABELS.awaiting_tig, False),
    ],
)
def test_each_outcome_maps_to_its_label_and_read_state(
    outcome: Outcome, label: str, read: bool
) -> None:
    plan = plan_labels(outcome, UNREAD, LABELS)

    assert plan == LabelPlan(add=label, remove=(), mark_read=read)
    assert marks_read(outcome) is read


def test_processed_supersedes_awaiting_tig() -> None:
    plan = plan_labels(Outcome.PROCESSED, UNREAD | {LABELS.awaiting_tig}, LABELS)

    assert plan == LabelPlan(add=LABELS.processed, remove=(LABELS.awaiting_tig,), mark_read=True)


def test_needs_review_supersedes_awaiting_tig_and_stays_unread() -> None:
    plan = plan_labels(Outcome.NEEDS_REVIEW, UNREAD | {LABELS.awaiting_tig}, LABELS)

    assert plan == LabelPlan(
        add=LABELS.needs_review, remove=(LABELS.awaiting_tig,), mark_read=False
    )


def test_awaiting_tig_already_present_is_a_noop() -> None:
    plan = plan_labels(Outcome.AWAITING_TIG, UNREAD | {LABELS.awaiting_tig}, LABELS)

    assert plan.is_noop
    assert plan == LabelPlan(add=None, remove=(), mark_read=False)


def test_processed_and_pending_are_mutually_exclusive() -> None:
    plan = plan_labels(Outcome.PENDING, UNREAD | {LABELS.processed}, LABELS)

    assert plan.add == LABELS.pending
    assert plan.remove == (LABELS.processed,)


def test_non_kibit_labels_are_never_removed() -> None:
    plan = plan_labels(Outcome.PROCESSED, UNREAD | {"Bookkeeping/Urgent", "STARRED"}, LABELS)

    assert plan.remove == ()


def test_outcome_label_names_come_from_config() -> None:
    custom = LabelNames("A/Done", "A/Wait", "A/Look", "A/Tig", "A/Dup")

    assert outcome_label_names(custom) == ("A/Done", "A/Wait", "A/Look", "A/Tig", "A/Dup")
    assert plan_labels(Outcome.PROCESSED, UNREAD, custom).add == "A/Done"


def test_validate_label_names_accepts_distinct_names() -> None:
    validate_label_names(LABELS)


@pytest.mark.parametrize(
    "labels",
    [
        LabelNames("Kibit/Done", "Kibit/Done", "Kibit/Review", "Kibit/Tig"),
        LabelNames("Kibit/Done", "Kibit/Pending", "", "Kibit/Tig"),
        LabelNames("Kibit/Done", "Kibit/Pending", "UNREAD", "Kibit/Tig"),
    ],
)
def test_validate_label_names_rejects_duplicate_blank_or_system_names(
    labels: LabelNames,
) -> None:
    with pytest.raises(ConfigError):
        validate_label_names(labels)


def test_duplicate_is_labelled_and_marked_read() -> None:
    plan = plan_labels(Outcome.DUPLICATE, UNREAD, LabelNames("P", "Q", "R", "S", "D"))
    assert plan.add == "D"
    assert plan.mark_read is True


def test_duplicate_label_must_be_distinct() -> None:
    with pytest.raises(ConfigError):
        validate_label_names(LabelNames("P", "Q", "R", "S", "P"))
