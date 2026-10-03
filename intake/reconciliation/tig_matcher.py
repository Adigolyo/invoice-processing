"""Contractor invoice vs. TIG reconciliation comparison (USR-005-01).

Two steps, both refusing anything but a TIG-route invoice (AC6, ``NotTigRouteError``):

``find_tig(thread, invoice_message_id, ...) -> TigLookup``
    Locates the performance certificate (TIG, *teljesítésigazolás*) in messages strictly
    **earlier** than the invoice's message in the same Gmail thread and extracts it with
    the shared ``Extractor`` (``kind=DocumentKind.TIG``). The result distinguishes:

    - ``FOUND``: ``document`` holds the TIG's lines and printed total.
    - ``MISSING``: no TIG in any earlier message. The orchestrator applies the
      missing-TIG fallback (USR-005-04, ``Kibit/AwaitingTIG``, re-evaluated next run).
    - ``UNREADABLE``: a TIG is there but cannot be used (extraction flagged it, a line's
      quantity or unit price is unreadable, its printed total is unparseable, or its file
      type cannot be extracted). This is **not** "no TIG": waiting for another TIG would
      never resolve it, so it needs a human (``Kibit/NeedsReview``).
    - ``AMBIGUOUS``: the latest TIG-bearing message carries several TIG attachments, so
      which one certifies this invoice cannot be decided without guessing (NeedsReview).

    Gmail and Gemini API errors propagate unchanged, so the candidate is retried.

``compare(invoice, tig, tolerance, ...) -> ComparisonResult``
    Compares quantity and unit price line by line, and the total net within
    ``tolerance`` (``Config.rounding_tolerance``).

How a TIG is recognised (USR-005-01 assumption: "distinguishable from the invoice
itself"):

1. Only messages before the invoice's message are scanned: earlier in Gmail's thread
   order and not with a later ``internal_date``. The invoice's own message is never a
   TIG source, even if it also carries a TIG-named file.
2. An attachment whose filename names a TIG (the whole word ``TIG``, as in
   ``TIG-2026-09-KEK-A.pdf``, or *teljesítésigazolás*, accent- and case-insensitive).
3. Only when no earlier message has such a file: an attachment of an earlier message
   whose subject carries a configured ``Config.tig_subject_indicators`` term, skipping
   files with the same name as one of the invoice's own attachments (a forwarded copy of
   the invoice is not its TIG) and non-document types.

When several earlier messages carry a TIG, the latest one before the invoice wins (a
re-sent or corrected certificate supersedes the older one); an unreadable latest TIG is
reported as such, never silently replaced by an older one.

Comparison rules:

- **Lines are matched by position** (invoice line *i* against TIG line *i*), not by
  description: contractors extend the certified description (the fixtures append
  "– Kék projekt, 2026. szeptember"), so exact-description matching would fail and
  fuzzy matching would be a guess.
- Quantities and unit prices must be numerically equal (``Decimal`` comparison, so
  ``0.4 == 0.40``); the rounding tolerance applies to the total net only (AC5 wording).
- A line present on one side only yields a ``QUANTITY`` discrepancy whose missing side is
  ``None`` (the TIG certifies none of an extra invoice line). An unreadable (``None``)
  quantity or unit price on either side is a discrepancy, never a silent match.
- Total net: against the TIG's printed total (multi-line TIGs print *Összesen (nettó)*)
  whenever there is one. A single-line TIG prints no total; then the total is derived
  (sum of line nets, or of quantity x unit price when nets are missing) and checked only
  when every line agreed, so an extra fee hidden in the invoice total is still caught
  (AC4) without restating a line discrepancy. An unreadable invoice total is a
  discrepancy with ``invoice_value=None``.
- ``line_description`` is the invoice's description (what the contractor wrote), or the
  TIG's for a line the invoice lacks.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Final, Protocol

from intake.clients.gmail_client import (
    DRAFT_LABEL_ID,
    AttachmentContent,
    Thread,
    ThreadMessage,
)
from intake.config import Config
from intake.extraction import SUPPORTED_MIME_TYPES, DocumentKind, Extractor
from intake.extraction.gemini_extractor import UnsupportedDocumentError
from intake.models import (
    Attachment,
    ComparisonResult,
    Discrepancy,
    DiscrepancyField,
    FlagReason,
    InvoiceExtraction,
    LineItem,
    Route,
)
from intake.normalization.amounts import normalize_amount
from intake.normalization.country_of_origin import determine_origin
from intake.normalization.currency import to_iso_code

_TIG_FILENAME: Final = re.compile(r"(?<![a-z])(?:tig|teljesitesigazolas)(?![a-z])")


class NotTigRouteError(ValueError):
    """Reconciliation was requested for an invoice that is not on the TIG route (AC6)."""


def _require_tig_route(route: Route) -> None:
    if route is not Route.TIG:
        raise NotTigRouteError(
            f"TIG reconciliation only runs for TIG-route invoices, not route {route.value!r}"
        )


class AttachmentDownloader(Protocol):
    """The part of ``GmailClient`` that ``find_tig`` needs."""

    def download_attachments(self, message_id: str) -> list[AttachmentContent]: ...


@dataclass(frozen=True, slots=True)
class TigDocument:
    """An extracted TIG: its line items and printed total net (``None`` if not printed)."""

    message_id: str
    filename: str
    lines: tuple[LineItem, ...]
    total_net: Decimal | None = None
    currency: str | None = None
    number: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.lines, tuple):
            object.__setattr__(self, "lines", tuple(self.lines))
        if self.total_net is not None and not isinstance(self.total_net, Decimal):
            raise TypeError(
                f"TigDocument.total_net must be a Decimal or None, "
                f"got {type(self.total_net).__name__}"
            )


class TigLookupStatus(StrEnum):
    FOUND = "found"
    MISSING = "missing"
    UNREADABLE = "unreadable"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class TigLookup:
    """What ``find_tig`` found; see the module docstring for each status' meaning.

    ``message_id``/``filename`` name the TIG source (also when unreadable/ambiguous);
    ``detail`` explains an unreadable or ambiguous result (never document content).
    """

    status: TigLookupStatus
    document: TigDocument | None = None
    detail: str | None = None
    message_id: str | None = None
    filename: str | None = None

    def __post_init__(self) -> None:
        if (self.status is TigLookupStatus.FOUND) != (self.document is not None):
            raise ValueError("a TigLookup has a document exactly when its status is FOUND")
        if self.needs_review and not self.detail:
            raise ValueError(f"a {self.status.value} TigLookup needs a detail")

    @classmethod
    def found_document(cls, document: TigDocument) -> TigLookup:
        return cls(
            TigLookupStatus.FOUND,
            document=document,
            message_id=document.message_id,
            filename=document.filename,
        )

    @classmethod
    def missing(cls) -> TigLookup:
        return cls(TigLookupStatus.MISSING)

    @classmethod
    def unreadable(cls, detail: str, message_id: str, filename: str | None = None) -> TigLookup:
        return cls(
            TigLookupStatus.UNREADABLE, detail=detail, message_id=message_id, filename=filename
        )

    @classmethod
    def ambiguous(cls, detail: str, message_id: str) -> TigLookup:
        return cls(TigLookupStatus.AMBIGUOUS, detail=detail, message_id=message_id)

    @property
    def found(self) -> bool:
        return self.status is TigLookupStatus.FOUND

    @property
    def is_missing(self) -> bool:
        """No TIG at all: the USR-005-04 missing-TIG fallback applies."""
        return self.status is TigLookupStatus.MISSING

    @property
    def needs_review(self) -> bool:
        """A TIG exists but cannot be used without a human."""
        return self.status in (TigLookupStatus.UNREADABLE, TigLookupStatus.AMBIGUOUS)

    @property
    def flag_reason(self) -> FlagReason | None:
        if self.is_missing:
            return FlagReason.MISSING_TIG
        if self.needs_review:
            return FlagReason.INCOMPLETE_DATA
        return None


# --- locating the TIG -----------------------------------------------------------------------


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()


def is_tig_filename(filename: str) -> bool:
    """True when an attachment's filename names a TIG (``TIG-…``, ``…teljesítésigazolás…``)."""
    stem = PurePosixPath(filename).stem if "." in filename else filename
    return bool(_TIG_FILENAME.search(_fold(stem)))


def _subject_has_indicator(subject: str, indicators: Iterable[str]) -> bool:
    folded = _fold(subject)
    for indicator in indicators:
        term = _fold(indicator.strip())
        if not term:
            continue
        prefix = r"(?<!\w)" if re.match(r"\w", term[0]) else ""
        suffix = r"(?!\w)" if re.match(r"\w", term[-1]) else ""
        if re.search(prefix + re.escape(term) + suffix, folded):
            return True
    return False


def _earlier_messages(thread: Thread, invoice_message_id: str) -> list[ThreadMessage]:
    for position, message in enumerate(thread.messages):
        if message.message_id == invoice_message_id:
            return [
                m for m in thread.messages[:position] if m.internal_date <= message.internal_date
            ]
    raise ValueError(f"message {invoice_message_id} is not in thread {thread.thread_id}")


def has_earlier_tig_attachment(thread: Thread, invoice_message_id: str) -> bool:
    """True when a message before the invoice in its thread carries a TIG-named file.

    The TIG flow is: a TIG goes out first and the invoice comes back as a reply (project
    owner, 2026-10-03). This is the thread signal that puts a reply into the TIG route
    whoever sent it; drafts (this service's own replies) never count. Only the filename
    rule is used here, not the weaker subject-indicator fallback, so a supplier's invoice
    titled "TIG ..." is not pulled into the TIG flow.
    """
    return any(
        is_tig_filename(attachment.filename)
        for message in _earlier_messages(thread, invoice_message_id)
        if DRAFT_LABEL_ID not in message.label_names
        for attachment in message.attachments
    )


def _base_mime(mime_type: str) -> str:
    return mime_type.split(";", 1)[0].strip().lower()


def _is_document(attachment: Attachment, config: Config) -> bool:
    mime = _base_mime(attachment.mime_type)
    allowed = {_base_mime(m) for m in config.attachment_mime_allowlist}
    return mime in SUPPORTED_MIME_TYPES and mime in allowed


def _select_source(
    earlier: Sequence[ThreadMessage], invoice: ThreadMessage, config: Config
) -> tuple[ThreadMessage, list[Attachment]] | None:
    """The latest earlier message carrying TIG attachments, and those attachments."""
    for message in reversed(earlier):
        named = [a for a in message.attachments if is_tig_filename(a.filename)]
        if named:
            return message, named
    invoice_files = {_fold(a.filename) for a in invoice.attachments}
    for message in reversed(earlier):
        if not _subject_has_indicator(message.subject, config.tig_subject_indicators):
            continue
        documents = [
            a
            for a in message.attachments
            if _is_document(a, config) and _fold(a.filename) not in invoice_files
        ]
        if documents:
            return message, documents
    return None


def _amount(raw: str | None, extraction: InvoiceExtraction, config: Config) -> Decimal | None:
    iso: str | None = None
    if extraction.currency is not None:
        resolved = to_iso_code(
            extraction.currency, determine_origin(extraction), config.currency_map
        )
        iso = resolved if isinstance(resolved, str) else None
    value = normalize_amount(raw, currency_iso=iso)
    return value if isinstance(value, Decimal) else None


def _download(
    gmail: AttachmentDownloader, message_id: str, attachment: Attachment
) -> AttachmentContent | None:
    contents = gmail.download_attachments(message_id)
    if attachment.attachment_id is not None:
        for item in contents:
            if item.attachment.attachment_id == attachment.attachment_id:
                return item
    for item in contents:
        if item.attachment.filename == attachment.filename:
            return item
    return None


def find_tig(
    thread: Thread,
    invoice_message_id: str,
    *,
    route: Route,
    gmail: AttachmentDownloader,
    extractor: Extractor,
    config: Config,
) -> TigLookup:
    """Locate and extract the TIG preceding the invoice's message; see the module docstring."""
    _require_tig_route(route)
    earlier = _earlier_messages(thread, invoice_message_id)
    invoice = next(m for m in thread.messages if m.message_id == invoice_message_id)

    source = _select_source(earlier, invoice, config)
    if source is None:
        return TigLookup.missing()
    message, attachments = source
    if len(attachments) > 1:
        return TigLookup.ambiguous(
            f"{len(attachments)} TIG attachments in message {message.message_id}",
            message.message_id,
        )
    (attachment,) = attachments

    def unreadable(detail: str) -> TigLookup:
        return TigLookup.unreadable(detail, message.message_id, attachment.filename)

    if not _is_document(attachment, config):
        return unreadable(f"TIG attachment has an unsupported MIME type {attachment.mime_type!r}")
    content = _download(gmail, message.message_id, attachment)
    if content is None:
        return unreadable("TIG attachment is missing from the message download")

    try:
        result = extractor.extract(content.content, attachment.mime_type, kind=DocumentKind.TIG)
    except UnsupportedDocumentError as exc:
        return unreadable(f"TIG cannot be extracted: {exc}")
    extraction = result.value
    if not result.is_ok or extraction is None:
        return unreadable(f"TIG extraction incomplete: {result.detail or result.error}")

    for index, line in enumerate(extraction.line_items):
        if line.quantity is None or line.unit_price is None:
            return unreadable(f"TIG line {index} has an unreadable quantity or unit price")

    total_net: Decimal | None = None
    if extraction.net is not None:
        total_net = _amount(extraction.net, extraction, config)
        if total_net is None:
            return unreadable("TIG total net is printed but unreadable")

    return TigLookup.found_document(
        TigDocument(
            message_id=message.message_id,
            filename=attachment.filename,
            lines=extraction.line_items,
            total_net=total_net,
            currency=extraction.currency,
            number=extraction.invoice_number,
        )
    )


# --- comparing --------------------------------------------------------------------------------


def _differs(invoice_value: Decimal | None, tig_value: Decimal | None) -> bool:
    return invoice_value is None or tig_value is None or invoice_value != tig_value


def _line_discrepancies(
    index: int, invoice_line: LineItem | None, tig_line: LineItem | None
) -> list[Discrepancy]:
    if invoice_line is None or tig_line is None:
        present = invoice_line or tig_line
        assert present is not None
        return [
            Discrepancy(
                DiscrepancyField.QUANTITY,
                invoice_value=invoice_line.quantity if invoice_line else None,
                tig_value=tig_line.quantity if tig_line else None,
                line_index=index,
                line_description=present.description,
            )
        ]
    description = invoice_line.description or tig_line.description
    found: list[Discrepancy] = []
    for field, invoice_value, tig_value in (
        (DiscrepancyField.QUANTITY, invoice_line.quantity, tig_line.quantity),
        (DiscrepancyField.UNIT_PRICE, invoice_line.unit_price, tig_line.unit_price),
    ):
        if _differs(invoice_value, tig_value):
            found.append(Discrepancy(field, invoice_value, tig_value, index, description))
    return found


def _derived_total(lines: Sequence[LineItem]) -> Decimal | None:
    """Sum of line nets, else of quantity x unit price; ``None`` if neither is complete."""
    if not lines:
        return None
    nets = [line.net for line in lines]
    if all(net is not None for net in nets):
        return sum((net for net in nets if net is not None), Decimal(0))
    total = Decimal(0)
    for line in lines:
        if line.quantity is None or line.unit_price is None:
            return None
        total += line.quantity * line.unit_price
    return total


def compare(
    invoice: InvoiceExtraction,
    tig: TigDocument,
    tolerance: Decimal,
    *,
    route: Route,
    currency_iso: str | None = None,
) -> ComparisonResult:
    """Reconcile a TIG-route invoice against its TIG; see the module docstring for rules.

    ``invoice.net`` is read as printed; ``currency_iso`` (the invoice's resolved ISO code)
    lets a HUF total such as ``"72.000 Ft"`` be read as thousands.
    """
    _require_tig_route(route)
    if tolerance < 0:
        raise ValueError(f"rounding tolerance must be >= 0, got {tolerance}")

    discrepancies: list[Discrepancy] = []
    invoice_lines, tig_lines = invoice.line_items, tig.lines
    for index in range(max(len(invoice_lines), len(tig_lines))):
        discrepancies += _line_discrepancies(
            index,
            invoice_lines[index] if index < len(invoice_lines) else None,
            tig_lines[index] if index < len(tig_lines) else None,
        )

    tig_total = tig.total_net
    if tig_total is None and not discrepancies:
        tig_total = _derived_total(tig_lines)
    if tig_total is not None:
        parsed = normalize_amount(invoice.net, currency_iso=currency_iso)
        invoice_total = parsed if isinstance(parsed, Decimal) else None
        if invoice_total is None or abs(invoice_total - tig_total) > tolerance:
            discrepancies.append(Discrepancy(DiscrepancyField.TOTAL_NET, invoice_total, tig_total))

    return ComparisonResult.mismatch(discrepancies) if discrepancies else ComparisonResult.match()


__all__ = [
    "AttachmentDownloader",
    "NotTigRouteError",
    "TigDocument",
    "TigLookup",
    "TigLookupStatus",
    "compare",
    "find_tig",
    "has_earlier_tig_attachment",
    "is_tig_filename",
]
