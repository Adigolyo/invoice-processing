"""Pending-label trigger and missing-TIG fallback rules (USR-005-03, USR-005-04).

Pure, deterministic decision functions the orchestrator (Task 21) consumes. They decide
*which* outcome a candidate reaches and what that outcome implies; they never call Gmail,
Drive or Sheets. Applying labels and marking read stays in ``intake/pipeline/orchestrator.py``.

Outcomes (ADR 3: four technical labels behind three business-facing states):

=====================================  ==============  =============  ===============
TIG-route input                        ``Outcome``     file & book?   re-evaluated?
=====================================  ==============  =============  ===============
TIG ``FOUND`` + comparison MATCH       PROCESSED       yes            no
TIG ``FOUND`` + comparison MISMATCH    PENDING         yes            no
TIG ``MISSING``                        PROCESSED       yes            no
TIG ``UNREADABLE`` / ``AMBIGUOUS``     NEEDS_REVIEW    no             no
=====================================  ==============  =============  ===============

- A mismatch never skips filing or booking (USR-005-03 AC1); it only changes the final
  label from processed to pending (AC2).
- A *missing* TIG means the invoice did not come in as a reply to a TIG. A TIG is
  always sent out first and the invoice is the reply, so there is nothing to reconcile:
  the invoice is filed, booked and processed like a direct one (project owner decision,
  2026-10-03; replaces USR-005-04's AwaitingTIG). A TIG that exists but cannot be used
  (unreadable, or several candidates) is not "missing": it needs a human, consistent
  with ``TigLookup.flag_reason`` (``INCOMPLETE_DATA``).
- ``AWAITING_TIG`` is no longer produced. Threads labelled AwaitingTIG by the old rule
  are still re-evaluated, and their label is replaced once they are processed.
- Only the TIG route is reconciled. ``tig_outcome`` refuses any other route with
  ``NotTigRouteError``; ``missing_tig_outcome`` answers ``False`` for them without looking
  at the lookup (USR-005-04 AC6).

Idempotency (USR-005-03 AC4/AC5, USR-005-04 AC3/AC4) is read from the thread's current
Kibit labels: a thread already labelled processed, pending or needs-review is not
evaluated again (``needs_evaluation``), an awaiting-TIG thread is; ``label_to_apply``
returns ``None`` when the outcome's label is already present, and ``superseded_labels``
names the stale outcome label (e.g. awaiting-TIG once a TIG has arrived) to remove.
"""

from __future__ import annotations

from collections.abc import Collection
from enum import StrEnum

from intake.config import LabelNames
from intake.models import ComparisonResult, Route
from intake.reconciliation.tig_matcher import NotTigRouteError, TigLookup, TigLookupStatus


class Outcome(StrEnum):
    """The final per-candidate outcome; each maps to one configured Kibit label."""

    PROCESSED = "processed"
    PENDING = "pending"
    NEEDS_REVIEW = "needs_review"
    AWAITING_TIG = "awaiting_tig"
    # Not a TIG outcome: an invoice already filed or booked from another email
    # (same PDF, or same external ID + provider). Nothing is filed or booked again.
    DUPLICATE = "duplicate"


_FILED_AND_BOOKED = frozenset({Outcome.PROCESSED, Outcome.PENDING})
# When a thread somehow carries several Kibit labels, the earliest here is reported.
_PRECEDENCE = (
    Outcome.PENDING,
    Outcome.PROCESSED,
    Outcome.DUPLICATE,
    Outcome.NEEDS_REVIEW,
    Outcome.AWAITING_TIG,
)


def pending_outcome(comparison: ComparisonResult) -> bool:
    """True when a reconciliation comparison recorded a mismatch (USR-005-03)."""
    return comparison.is_mismatch


def missing_tig_outcome(route: Route, lookup: TigLookup | None) -> bool:
    """True when the missing-TIG fallback applies (USR-005-04).

    Only a TIG-route invoice whose thread holds no TIG at all qualifies. For any other
    route the fallback is never evaluated and ``lookup`` is ignored (it may be ``None``).
    """
    if route is not Route.TIG:
        return False
    if lookup is None:
        raise ValueError("a TIG-route invoice needs a TIG lookup before the fallback check")
    return lookup.is_missing


def tig_outcome(
    route: Route, lookup: TigLookup, comparison: ComparisonResult | None = None
) -> Outcome:
    """The outcome of a TIG-route invoice; see the module docstring's table.

    ``comparison`` is required exactly when ``lookup`` found a TIG.
    """
    if route is not Route.TIG:
        raise NotTigRouteError(
            f"TIG outcome rules only apply to TIG-route invoices, not route {route.value!r}"
        )
    if lookup.found != (comparison is not None):
        raise ValueError("a comparison is given exactly when the TIG lookup found a TIG")
    if comparison is not None:
        return Outcome.PENDING if pending_outcome(comparison) else Outcome.PROCESSED
    if lookup.status is TigLookupStatus.MISSING:
        return Outcome.PROCESSED
    return Outcome.NEEDS_REVIEW


def should_file_and_book(outcome: Outcome) -> bool:
    """True for outcomes whose invoice is filed, booked and then marked read."""
    return outcome in _FILED_AND_BOOKED


def is_reevaluated(outcome: Outcome) -> bool:
    """True when the candidate is re-evaluated on the next run (only AWAITING_TIG)."""
    return outcome is Outcome.AWAITING_TIG


def outcome_label(outcome: Outcome, labels: LabelNames) -> str:
    """The configured Gmail label name for ``outcome``."""
    return {
        Outcome.PROCESSED: labels.processed,
        Outcome.PENDING: labels.pending,
        Outcome.NEEDS_REVIEW: labels.needs_review,
        Outcome.AWAITING_TIG: labels.awaiting_tig,
        Outcome.DUPLICATE: labels.duplicate,
    }[outcome]


def prior_outcome(current_labels: Collection[str], labels: LabelNames) -> Outcome | None:
    """The outcome a thread's existing Kibit labels record, or ``None`` if it has none."""
    for outcome in _PRECEDENCE:
        if outcome_label(outcome, labels) in current_labels:
            return outcome
    return None


def needs_evaluation(current_labels: Collection[str], labels: LabelNames) -> bool:
    """False when an earlier run already reached a terminal outcome for this thread."""
    prior = prior_outcome(current_labels, labels)
    return prior is None or is_reevaluated(prior)


def label_to_apply(
    outcome: Outcome, current_labels: Collection[str], labels: LabelNames
) -> str | None:
    """The label to add for ``outcome``, or ``None`` if it is already present."""
    label = outcome_label(outcome, labels)
    return None if label in current_labels else label


def superseded_labels(
    outcome: Outcome, current_labels: Collection[str], labels: LabelNames
) -> frozenset[str]:
    """Other Kibit outcome labels on the thread that ``outcome`` replaces."""
    own = outcome_label(outcome, labels)
    return frozenset(
        label
        for label in (outcome_label(o, labels) for o in Outcome)
        if label != own and label in current_labels
    )


__all__ = [
    "Outcome",
    "is_reevaluated",
    "label_to_apply",
    "missing_tig_outcome",
    "needs_evaluation",
    "outcome_label",
    "pending_outcome",
    "prior_outcome",
    "should_file_and_book",
    "superseded_labels",
    "tig_outcome",
]
