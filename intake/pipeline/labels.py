"""The Gmail label taxonomy: which label and read state each outcome leaves behind.

ADR 3 implements the three business-facing states with four technical labels, whose
names come from the ``Config`` tab (``Config.labels``), never from code:

==============  ====================  ===========  ===================================
Outcome         label                 read state   re-evaluated next run?
==============  ====================  ===========  ===================================
PROCESSED       ``labels.processed``  read         no (excluded from the poll query)
PENDING         ``labels.pending``    read         no (excluded from the poll query)
NEEDS_REVIEW    ``labels.needs_review``  **unread**  no (excluded from the poll query)
AWAITING_TIG    ``labels.awaiting_tig``  **unread**  yes, every run (USR-005-04)
==============  ====================  ===========  ===================================

USR-001-04 says a flagged email "remains unread"; the NeedsReview label is the ADR 3
technical marker that keeps it from being re-evaluated (AC3/AC4) and is not one of the
business outcome labels "processed"/"pending" (AC1/AC2).

``plan_labels`` is pure: it says what to add, which stale Kibit outcome label to remove
(e.g. AwaitingTIG once the TIG arrived; the outcome labels are mutually exclusive,
USR-001-03 AC4) and whether to mark the message read. Labels that are not one of the
four Kibit outcome labels (a bookkeeper's own labels, ``STARRED``...) are never touched.
The orchestrator applies a plan in a fixed order: add the label, remove superseded
labels, and mark read last, so a crash in between can never leave an email read
without an outcome label (USR-001-03 constraint).
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import Final

from intake.config import ConfigError, LabelNames
from intake.reconciliation.outcome_rules import (
    Outcome,
    label_to_apply,
    outcome_label,
    superseded_labels,
)

MARKS_READ: Final = frozenset({Outcome.PROCESSED, Outcome.PENDING})
"""Outcomes whose email is marked read; NeedsReview and AwaitingTIG stay unread."""

_SYSTEM_LABELS: Final = frozenset(
    {"INBOX", "UNREAD", "SENT", "DRAFT", "STARRED", "IMPORTANT", "SPAM", "TRASH"}
)


@dataclass(frozen=True, slots=True)
class LabelPlan:
    """Gmail changes for one outcome; applied as add -> remove -> mark read."""

    add: str | None
    remove: tuple[str, ...]
    mark_read: bool

    @property
    def is_noop(self) -> bool:
        return self.add is None and not self.remove and not self.mark_read


def marks_read(outcome: Outcome) -> bool:
    """True when the outcome's email is marked read (processed / pending only)."""
    return outcome in MARKS_READ


def outcome_label_names(labels: LabelNames) -> tuple[str, str, str, str]:
    """The four configured Kibit outcome label names (processed, pending, review, TIG)."""
    return (labels.processed, labels.pending, labels.needs_review, labels.awaiting_tig)


def validate_label_names(labels: LabelNames) -> None:
    """Fail loudly unless the four outcome labels are non-blank, distinct, user labels.

    Two outcomes sharing a label (or a Gmail system label such as ``UNREAD``) would make
    the state machine ambiguous, e.g. a pending email look processed.
    """
    names = outcome_label_names(labels)
    if any(not name.strip() for name in names):
        raise ConfigError("Config label names must not be blank")
    if len(set(names)) != len(names):
        raise ConfigError(f"Config label names must be distinct, got {list(names)}")
    reserved = sorted(name for name in names if name.upper() in _SYSTEM_LABELS)
    if reserved:
        raise ConfigError(f"Config label names must not be Gmail system labels: {reserved}")


def plan_labels(outcome: Outcome, current_labels: Collection[str], labels: LabelNames) -> LabelPlan:
    """What to change on a message reaching ``outcome``, given its current labels."""
    return LabelPlan(
        add=label_to_apply(outcome, current_labels, labels),
        remove=tuple(sorted(superseded_labels(outcome, current_labels, labels))),
        mark_read=marks_read(outcome),
    )


__all__ = [
    "MARKS_READ",
    "LabelPlan",
    "Outcome",
    "marks_read",
    "outcome_label",
    "outcome_label_names",
    "plan_labels",
    "validate_label_names",
]
