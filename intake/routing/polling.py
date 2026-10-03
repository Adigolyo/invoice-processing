"""Gmail inbox polling and invoice candidate matching (USR-001-01).

``poll_candidates`` turns the mailbox's unread inbox into the run's list of candidate
invoice emails, which the orchestrator hands to route classification (USR-001-02).

Which messages are looked at (AC4) is decided by ``GmailClient.list_candidates()``, which
runs the design's query (``in:inbox is:unread`` minus the ``Processed``, ``Pending`` and
``NeedsReview`` labels from ``Config.labels``). ``AwaitingTIG`` is deliberately *not*
excluded: those threads are re-evaluated every run (ADR 3, USR-005-04). As a defensive
second line, a message that nonetheless carries one of the three terminal labels is
dropped here too.

A listed message is kept when (AC1, AC2):

- its subject contains a configured invoice keyword (``Config.invoice_keywords``), or
- it has at least one attachment whose MIME type is in ``Config.attachment_mime_allowlist``.

Everything else is dropped and left exactly as it was (AC3). A message already labelled
``AwaitingTIG`` is kept even if it would no longer match (e.g. after a Config edit): it
was admitted to the pipeline in an earlier run and must stay re-evaluable.

Matching rules (the specification fixes only "contains a keyword" and "PDF or image
MIME type"; the details below are this module's interpretation):

- Keywords are matched as substrings, case-insensitively, independent of Unicode
  normalisation form, ignoring accents and treating any whitespace run as one space. So
  ``számla`` matches ``SZÁMLA``, ``Szamla``, ``számlája`` and ``E-számla``. Substring
  rather than whole-word matching is chosen because Hungarian inflects nouns
  (``számlát``, ``számlája``) and a missed invoice is never processed (story section 2),
  whereas over-matching only costs a later classification/extraction step.
- MIME types are compared case-insensitively on ``type/subtype`` only (parameters such
  as ``; name=...`` are ignored). An allowlist entry ``type/*`` admits every subtype of
  that type. The attachment's filename is never consulted, so an executable renamed to
  ``.pdf`` does not qualify (security guardrail).

Read-only (story constraint): nothing here applies labels or changes read state. Any
Gmail API failure propagates unchanged before a single candidate is returned (AC6), so
the same unread messages stay eligible for the next run. Within one run each message is
returned at most once, in Gmail's order (AC5); across runs, de-duplication comes from the
labels the orchestrator applies, since the service keeps no other state.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from typing import Protocol

from intake.config import Config
from intake.models import Candidate

_WHITESPACE = re.compile(r"\s+")


class CandidateSource(Protocol):
    """Anything that lists unread inbox candidates, e.g. ``GmailClient``."""

    def list_candidates(self) -> list[Candidate]: ...


def poll_candidates(gmail: CandidateSource, config: Config) -> list[Candidate]:
    """The run's candidate invoice emails, de-duplicated by message ID, in Gmail's order.

    Read-only. Exceptions from ``gmail.list_candidates()`` propagate unchanged.
    """
    listed = gmail.list_candidates()
    terminal = {
        config.labels.processed,
        config.labels.pending,
        config.labels.needs_review,
        config.labels.duplicate,
    }
    awaiting_tig = config.labels.awaiting_tig

    seen: set[str] = set()
    result: list[Candidate] = []
    for candidate in listed:
        if candidate.message_id in seen:
            continue
        seen.add(candidate.message_id)
        if not terminal.isdisjoint(candidate.label_names):
            continue
        if awaiting_tig in candidate.label_names or is_invoice_candidate(candidate, config):
            result.append(candidate)
    return result


def is_invoice_candidate(candidate: Candidate, config: Config) -> bool:
    """True when the subject has an invoice keyword or an attachment is allowlisted."""
    return _has_keyword(candidate.subject, config.invoice_keywords) or _has_allowed_attachment(
        (attachment.mime_type for attachment in candidate.attachments),
        config.attachment_mime_allowlist,
    )


def _fold(text: str) -> str:
    """Case-, accent- and normalisation-form-insensitive form of ``text``."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _WHITESPACE.sub(" ", stripped).casefold().strip()


def _has_keyword(subject: str, keywords: Iterable[str]) -> bool:
    folded_subject = _fold(subject)
    return any((term := _fold(keyword)) and term in folded_subject for keyword in keywords)


def _mime_essence(mime_type: str) -> str:
    return mime_type.split(";", 1)[0].strip().lower()


def _has_allowed_attachment(mime_types: Iterable[str], allowlist: Iterable[str]) -> bool:
    allowed = {essence for entry in allowlist if (essence := _mime_essence(entry))}
    for mime_type in mime_types:
        essence = _mime_essence(mime_type)
        if not essence:
            continue
        main_type = essence.partition("/")[0]
        if essence in allowed or f"{main_type}/*" in allowed:
            return True
    return False


__all__ = ["CandidateSource", "is_invoice_candidate", "poll_candidates"]
