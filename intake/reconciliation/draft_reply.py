"""Gmail draft reply on a TIG mismatch (USR-005-02).

``compose_discrepancy_body(discrepancies, ...) -> str``
    A plain-text, factual Hungarian message (the contractors, the fixtures and the
    business are Hungarian) that lists every discrepancy found by ``compare``
    (USR-005-01): the 1-based line item position and description (or *Nettó végösszeg*
    for the total), the field (*mennyiség* / *egységár* / *nettó végösszeg*), and both
    values, the invoice's first, then the TIG's. It references the invoice and TIG
    numbers when known. No persuasive or negotiation language (story assumption).

    Amounts use ``format_hungarian_amount`` with their source precision plus the currency
    code; quantities drop trailing zeros (``12.00`` -> ``12``). A ``None`` value is spelled
    out: *nem szerepel (hiányzó tétel)* when the line does not exist on that side
    (``line_index`` beyond that side's line count), otherwise *nem olvasható*.

``draft_reply_on_mismatch(gmail, thread_id, comparison, ...) -> DraftReplyResult``
    Creates the draft through ``GmailClient.create_draft_reply`` only for a MISMATCH
    (AC1, AC4). It never sends (AC3) and never labels or marks read: those belong to the
    orchestrator.

    **Exactly one draft (AC5):** before creating, the thread is re-read and any message
    carrying Gmail's system ``DRAFT`` label counts as the existing reply, so a
    re-evaluation (e.g. after an interrupted run) creates nothing. Gmail returns drafts as
    thread messages with that label, so this needs no extra state or client method. A
    draft someone wrote by hand in the thread also suppresses a new one: never a
    duplicate, and the project manager already has a draft to finish.

    **Failure (AC6):** a Gmail failure while reading the thread or creating the draft
    (``HttpError``, ``GmailReplyError``, ``GmailResponseError``, ``OSError`` including
    timeouts) is *returned* as ``FAILED``, not raised, so the orchestrator still files,
    books and labels the invoice ``Kibit/Pending``: the mismatch stays visible as an open
    item and the draft can be created manually. ``mismatch_outstanding`` is true for every
    mismatch result (CREATED, ALREADY_DRAFTED, FAILED); ``draft_missing`` singles out
    FAILED for logging. Other exceptions (programming errors) propagate.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final, Protocol

from googleapiclient.errors import HttpError

from intake.clients.gmail_client import (
    GmailReplyError,
    GmailResponseError,
    Thread,
    ThreadMessage,
)
from intake.models import ComparisonResult, Discrepancy, DiscrepancyField, InvoiceExtraction
from intake.normalization.amounts import format_hungarian_amount
from intake.reconciliation.tig_matcher import TigDocument

DRAFT_LABEL: Final = "DRAFT"
"""Gmail's system label on draft messages (its ID and name are both ``DRAFT``)."""

_FIELD_NAMES: Final = {
    DiscrepancyField.QUANTITY: "mennyiség",
    DiscrepancyField.UNIT_PRICE: "egységár",
    DiscrepancyField.TOTAL_NET: "nettó végösszeg",
}
_UNREADABLE: Final = "nem olvasható"
_ABSENT_LINE: Final = "nem szerepel (hiányzó tétel)"

_DRAFT_ERRORS: Final = (HttpError, GmailReplyError, GmailResponseError, OSError)


class DraftGmail(Protocol):
    """The part of ``GmailClient`` this step needs."""

    def get_thread(self, thread_id: str) -> Thread: ...

    def create_draft_reply(self, thread_id: str, body: str) -> str: ...


# --- composing ----------------------------------------------------------------------------------


def _clean(text: str | None) -> str:
    return " ".join(text.split()) if text else ""


def _format_value(
    discrepancy: Discrepancy,
    value: Decimal | None,
    line_count: int | None,
    currency: str | None,
) -> str:
    if value is None:
        index = discrepancy.line_index
        if index is not None and line_count is not None and index >= line_count:
            return _ABSENT_LINE
        return _UNREADABLE
    if discrepancy.field is DiscrepancyField.QUANTITY:
        # normalize() drops trailing zeros; format_hungarian_amount writes it out in full.
        return format_hungarian_amount(value.normalize())
    amount = format_hungarian_amount(value)
    return f"{amount} {currency}" if currency else amount


def _subject_of(discrepancy: Discrepancy) -> str:
    if discrepancy.line_index is None:
        return "Nettó végösszeg"
    label = f"{discrepancy.line_index + 1}. tétel"
    description = _clean(discrepancy.line_description)
    label = f"{label} ({description})" if description else label
    return f"{label}, {_FIELD_NAMES[discrepancy.field]}"


def _discrepancy_line(
    discrepancy: Discrepancy,
    currency: str | None,
    invoice_line_count: int | None,
    tig_line_count: int | None,
) -> str:
    invoice = _format_value(discrepancy, discrepancy.invoice_value, invoice_line_count, currency)
    tig = _format_value(discrepancy, discrepancy.tig_value, tig_line_count, currency)
    return f"- {_subject_of(discrepancy)}: a számlán {invoice}, a teljesítésigazoláson {tig}"


def compose_discrepancy_body(
    discrepancies: Sequence[Discrepancy],
    *,
    currency: str | None = None,
    invoice_number: str | None = None,
    tig_number: str | None = None,
    invoice_line_count: int | None = None,
    tig_line_count: int | None = None,
) -> str:
    """The draft's plain-text body; see the module docstring for the wording rules.

    ``currency`` labels unit prices and totals (the invoice's ISO code); the line counts
    tell a missing line (*nem szerepel*) from an unreadable value (*nem olvasható*).
    """
    if not discrepancies:
        raise ValueError("a draft reply needs at least one discrepancy")
    invoice_number, tig_number = _clean(invoice_number), _clean(tig_number)
    invoice_ref = f"A(z) {invoice_number} számú számlát" if invoice_number else "A számlát"
    tig_ref = (
        f"a(z) {tig_number} számú teljesítésigazolással"
        if tig_number
        else "a teljesítésigazolással"
    )
    items = [
        _discrepancy_line(d, _clean(currency) or None, invoice_line_count, tig_line_count)
        for d in discrepancies
    ]
    return "\n".join(
        [
            "Tisztelt Partnerünk!",
            "",
            f"{invoice_ref} összevetettük {tig_ref}, és az alábbi eltéréseket találtuk:",
            "",
            *items,
            "",
            "Kérjük, ellenőrizzék a fenti tételeket.",
            "",
            "Üdvözlettel:",
            "",
        ]
    )


# --- drafting -----------------------------------------------------------------------------------


class DraftReplyStatus(StrEnum):
    NOT_NEEDED = "not_needed"  # MATCH: no draft, nothing outstanding
    CREATED = "created"
    ALREADY_DRAFTED = "already_drafted"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class DraftReplyResult:
    """What the draft-reply step did; see the module docstring for the orchestrator contract."""

    status: DraftReplyStatus
    draft_id: str | None = None
    existing_draft_message_id: str | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        expected = {
            DraftReplyStatus.NOT_NEEDED: (False, False, False),
            DraftReplyStatus.CREATED: (True, False, False),
            DraftReplyStatus.ALREADY_DRAFTED: (False, True, False),
            DraftReplyStatus.FAILED: (False, False, True),
        }[self.status]
        actual = (
            bool(self.draft_id),
            bool(self.existing_draft_message_id),
            bool(self.error),
        )
        if actual != expected:
            raise ValueError(f"inconsistent fields for a {self.status.value} DraftReplyResult")

    @property
    def mismatch_outstanding(self) -> bool:
        """The comparison was a mismatch: the invoice's outcome must be pending."""
        return self.status is not DraftReplyStatus.NOT_NEEDED

    @property
    def draft_missing(self) -> bool:
        """A mismatch whose draft could not be created (to be logged / done by hand)."""
        return self.status is DraftReplyStatus.FAILED


def find_existing_draft(thread: Thread) -> ThreadMessage | None:
    """The latest draft message in the thread, if any."""
    drafts = [m for m in thread.messages if DRAFT_LABEL in m.label_names]
    return drafts[-1] if drafts else None


def draft_reply_on_mismatch(
    gmail: DraftGmail,
    thread_id: str,
    comparison: ComparisonResult,
    *,
    invoice: InvoiceExtraction,
    tig: TigDocument,
    currency_iso: str | None,
) -> DraftReplyResult:
    """Create the mismatch draft reply on ``thread_id`` once; see the module docstring.

    ``currency_iso`` is the invoice's resolved currency; when unresolved, the TIG's printed
    currency labels the amounts instead.
    """
    if not comparison.is_mismatch:
        return DraftReplyResult(DraftReplyStatus.NOT_NEEDED)

    body = compose_discrepancy_body(
        comparison.discrepancies,
        currency=currency_iso or tig.currency,
        invoice_number=invoice.invoice_number,
        tig_number=tig.number,
        invoice_line_count=len(invoice.line_items),
        tig_line_count=len(tig.lines),
    )
    try:
        existing = find_existing_draft(gmail.get_thread(thread_id))
        if existing is not None:
            return DraftReplyResult(
                DraftReplyStatus.ALREADY_DRAFTED, existing_draft_message_id=existing.message_id
            )
        draft_id = gmail.create_draft_reply(thread_id, body)
    except _DRAFT_ERRORS as exc:
        return DraftReplyResult(DraftReplyStatus.FAILED, error=f"{type(exc).__name__}: {exc}")
    return DraftReplyResult(DraftReplyStatus.CREATED, draft_id=draft_id)


__all__ = [
    "DRAFT_LABEL",
    "DraftGmail",
    "DraftReplyResult",
    "DraftReplyStatus",
    "compose_discrepancy_body",
    "draft_reply_on_mismatch",
    "find_existing_draft",
]
