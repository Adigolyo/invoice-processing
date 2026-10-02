"""The pipeline orchestrator: one polling cycle and the per-candidate state machine.

This is the only module that changes inbox state or the ledger: it alone calls
``apply_label``, ``remove_label``, ``mark_read`` and ``append_row`` (technical design,
"Core Logic"). Every other stage is consulted for a decision and its result turned into
one of four outcomes (``intake.pipeline.labels``), or into an ``error`` that changes
nothing.

Per run (``run_cycle``): load ``Config``, check the label taxonomy and the Ledger header,
then build the Gmail/Drive clients and the extractor (``connect``), create one
``SequenceAllocator`` and one ``DuplicateGate``, poll, and process candidates strictly
one after another (ADR 5). A failure before the first candidate (Config, header,
polling) raises and touches nothing.

Per candidate (``Orchestrator.process``), in order:

1. **Idempotency.** A message already carrying a terminal Kibit label is skipped
   (``needs_evaluation``); AwaitingTIG is re-evaluated. A message whose only invoice-type
   attachments are TIG documents (e.g. the project manager's certificate email) is not
   an invoice and is skipped without any change.
2. **Route** (``classify``). AMBIGUOUS -> NeedsReview.
3. **Attachments.** Downloaded; kept when the MIME type is on
   ``Config.attachment_mime_allowlist`` and the filename does not name a TIG.
   Byte-identical copies count once. None left, or more than one distinct invoice ->
   NeedsReview (the specification speaks of "the invoice" of an email; splitting one
   email into several invoices would need a per-invoice outcome the labels cannot hold).
4. **Extract** (``Extractor``, injected). Incomplete or unsupported -> NeedsReview;
   an ``error`` result or any exception -> error (retried next run).
5. **Normalise**: origin -> currency ISO -> amounts (HUF thousands rule needs the
   currency) -> dates -> performance date -> ledger rounding. Any failure in a value the
   ledger or the registry number needs -> NeedsReview.
6. **Reconcile** (TIG route only): ``find_tig`` on the thread; when it finds nothing
   before the invoice, the whole thread is searched again so a TIG sent *after* the
   invoice is picked up (USR-005-04 AC4). Missing -> AwaitingTIG; unreadable/ambiguous
   -> NeedsReview; mismatch -> one draft reply (never sent) and outcome Pending, even if
   the draft could not be created (USR-005-02 AC6).
7. **Registry number + filing**, idempotent across runs through the Drive source link:
   an earlier filing of this message's attachment is reused (never refiled); otherwise
   an invoice already in the ledger (same external ID + provider, e.g. a re-sent email)
   is neither filed nor booked again; otherwise a sequence number is allocated, the
   registry number assembled, the ledger row validated, and the original file uploaded.
   The number is released whenever filing does not complete.
8. **Book**: the duplicate gate is consulted immediately before ``append_row``; an
   already-booked invoice is not appended again (USR-004-03).
9. **Finalise**: add the outcome label, remove superseded Kibit labels, then mark read
   (processed/pending only). NeedsReview and AwaitingTIG stay unread.

Any exception in steps 1-9 aborts that candidate before any label or read-state change
(beyond what step 9 itself already applied), is recorded as ``error`` and the run
continues with the next candidate: one bad email never stops the run, and the failed
one is retried next run because it is still unread and unlabelled.

Logging: one structured record per stage with ``stage``, ``outcome``, ``message_id`` and
``duration_ms`` (plus a fixed ``reason`` code when flagged), never document content,
amounts, names or exception messages.
"""

from __future__ import annotations

import dataclasses
import logging
import time
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Final, Protocol

from intake.clients.drive_client import FiledFileRef, SavedFile
from intake.clients.gmail_client import AttachmentContent, Thread
from intake.clients.sheets_client import LedgerRow
from intake.config import Config
from intake.extraction import DocumentKind, Extractor, UnsupportedDocumentError
from intake.ledger.booking import BookingError, build_ledger_row
from intake.ledger.dedupe import DuplicateGate
from intake.models import Candidate, InvoiceExtraction, Route, StageStatus
from intake.normalization import (
    NormalizationFailure,
    determine_origin,
    normalize_amounts,
    normalize_dates,
    round_amounts_for_ledger,
    select_performance_date,
    to_iso_code,
)
from intake.pipeline.labels import plan_labels, validate_label_names
from intake.reconciliation.draft_reply import draft_reply_on_mismatch
from intake.reconciliation.outcome_rules import Outcome, needs_evaluation, tig_outcome
from intake.reconciliation.tig_matcher import TigLookup, compare, find_tig, is_tig_filename
from intake.registry.filing import (
    FilingConflictError,
    FilingError,
    FilingSourceConflictError,
    attachment_source_key,
    file_attachment,
    find_existing_filing,
)
from intake.registry.folder_naming import yymm_folder_name
from intake.registry.registry_number import RegistryNumberError, assemble_for_date
from intake.registry.sequence import SequenceAllocator, SequenceExhaustedError
from intake.registry.supplier_id import normalize_supplier
from intake.routing import classify, poll_candidates

logger = logging.getLogger(__name__)

DRAFT_LABEL: Final = "DRAFT"


# --- client ports -----------------------------------------------------------------------------


class GmailPort(Protocol):
    """The ``GmailClient`` surface the pipeline uses."""

    def list_candidates(self) -> list[Candidate]: ...

    def get_thread(self, thread_id: str) -> Thread: ...

    def download_attachments(self, message_id: str) -> list[AttachmentContent]: ...

    def apply_label(self, message_id: str, label: str) -> None: ...

    def remove_label(self, message_id: str, label: str) -> None: ...

    def mark_read(self, message_id: str) -> None: ...

    def create_draft_reply(self, thread_id: str, body: str) -> str: ...


class DrivePort(Protocol):
    """The ``DriveClient`` surface the pipeline uses."""

    def find_month_folder(self, yymm: str) -> str | None: ...

    def create_month_folder(self, yymm: str) -> str: ...

    def list_month_folder(self, yymm: str) -> list[str]: ...

    def save_file(
        self,
        folder_id: str,
        filename: str,
        content: bytes,
        mime_type: str | None = None,
        *,
        source_message_id: str | None = None,
        source_attachment_key: str | None = None,
    ) -> SavedFile: ...

    def find_filed_by_source(
        self, message_id: str, attachment_key: str | None = None
    ) -> list[FiledFileRef]: ...


class SheetsPort(Protocol):
    """The ``SheetsClient`` surface the pipeline uses."""

    def load_config(self) -> Config: ...

    def verify_ledger_header(self) -> None: ...

    def load_existing_keys(self) -> set[tuple[str, str]]: ...

    def append_row(self, row: LedgerRow) -> None: ...


@dataclass(frozen=True, slots=True)
class PipelineClients:
    """The clients built once ``Config`` is known (labels, Drive root, currency map)."""

    gmail: GmailPort
    drive: DrivePort
    extractor: Extractor


# --- results ------------------------------------------------------------------------------------


class CandidateStatus(StrEnum):
    PROCESSED = "processed"
    PENDING = "pending"
    NEEDS_REVIEW = "needs_review"
    AWAITING_TIG = "awaiting_tig"
    ERROR = "error"
    SKIPPED = "skipped"


_STATUS_BY_OUTCOME: Final = {
    Outcome.PROCESSED: CandidateStatus.PROCESSED,
    Outcome.PENDING: CandidateStatus.PENDING,
    Outcome.NEEDS_REVIEW: CandidateStatus.NEEDS_REVIEW,
    Outcome.AWAITING_TIG: CandidateStatus.AWAITING_TIG,
}

_SUMMARY_KEYS: Final = (
    ("processed", CandidateStatus.PROCESSED),
    ("pending", CandidateStatus.PENDING),
    ("needs_review", CandidateStatus.NEEDS_REVIEW),
    ("awaiting_tig", CandidateStatus.AWAITING_TIG),
    ("errors", CandidateStatus.ERROR),
    ("skipped", CandidateStatus.SKIPPED),
)


@dataclass(frozen=True, slots=True)
class CandidateResult:
    """What happened to one candidate. ``reason`` is a fixed code, never content."""

    message_id: str
    status: CandidateStatus
    reason: str
    registry_number: str | None = None


@dataclass(frozen=True, slots=True)
class RunSummary:
    """The outcome of one polling cycle, in candidate order."""

    results: tuple[CandidateResult, ...]

    def count(self, status: CandidateStatus) -> int:
        return sum(1 for r in self.results if r.status is status)

    def to_dict(self) -> dict[str, int]:
        """Counts per outcome, as returned by ``POST /run``."""
        counts = Counter(r.status for r in self.results)
        return {key: counts[status] for key, status in _SUMMARY_KEYS}


# --- internal control flow ------------------------------------------------------------------------


class _Stop(Exception):  # noqa: N818 - control flow, not an error
    """Ends a candidate's evaluation with a definite outcome (or a no-change skip)."""

    def __init__(self, outcome: Outcome | None, reason: str) -> None:
        super().__init__(reason)
        self.outcome = outcome
        self.reason = reason


class ExtractionFailedError(RuntimeError):
    """The extractor answered with an ``error`` StageResult: retry next run."""


@dataclass(frozen=True, slots=True)
class _Normalised:
    currency_iso: str
    net: Decimal
    gross: Decimal
    due_date: date
    performance_date: date


def _mime_essence(mime_type: str) -> str:
    return mime_type.split(";", 1)[0].strip().lower()


def mime_type_allowed(mime_type: str, allowlist: Sequence[str]) -> bool:
    """Same rule as polling: ``type/subtype`` match, or a ``type/*`` allowlist entry."""
    essence = _mime_essence(mime_type)
    if not essence:
        return False
    allowed = {e for entry in allowlist if (e := _mime_essence(entry))}
    return essence in allowed or f"{essence.partition('/')[0]}/*" in allowed


# --- the orchestrator -----------------------------------------------------------------------------


class Orchestrator:
    """Runs one cycle. Create one per run: the allocator and the dedupe gate are run-scoped."""

    def __init__(
        self,
        *,
        config: Config,
        gmail: GmailPort,
        drive: DrivePort,
        sheets: SheetsPort,
        extractor: Extractor,
    ) -> None:
        self._config = config
        self._gmail = gmail
        self._drive = drive
        self._sheets = sheets
        self._extractor = extractor
        self._allocator = SequenceAllocator.from_config(drive, config)
        self._gate = DuplicateGate(sheets)

    def run(self) -> RunSummary:
        """Poll and process every candidate in Gmail's order. Polling errors propagate."""
        candidates = poll_candidates(self._gmail, self._config)
        logger.info("pipeline run started", extra={"candidates": len(candidates)})
        summary = RunSummary(tuple(self.process(c) for c in candidates))
        logger.info("pipeline run finished", extra={"summary": summary.to_dict()})
        return summary

    def process(self, candidate: Candidate) -> CandidateResult:
        """Evaluate one candidate and apply its outcome; never raises."""
        message_id = candidate.message_id
        try:
            if not needs_evaluation(candidate.label_names, self._config.labels):
                return CandidateResult(message_id, CandidateStatus.SKIPPED, "terminal_label")
            state = _CandidateState()
            try:
                outcome = self._evaluate(candidate, state)
                reason = "ok"
            except _Stop as stop:
                if stop.outcome is None:
                    self._log(message_id, "evaluate", "skipped", 0, stop.reason)
                    return CandidateResult(message_id, CandidateStatus.SKIPPED, stop.reason)
                outcome, reason = stop.outcome, stop.reason
            with self._stage("finalise", message_id):
                self._finalise(candidate, outcome)
            return CandidateResult(
                message_id, _STATUS_BY_OUTCOME[outcome], reason, state.registry_number
            )
        except Exception as exc:
            logger.error(
                "candidate aborted (%s): message_id=%s left unchanged for retry next run",
                type(exc).__name__,
                message_id,
                extra={"message_id": message_id, "error_type": type(exc).__name__},
            )
            return CandidateResult(message_id, CandidateStatus.ERROR, type(exc).__name__)

    # --- stages ---

    def _evaluate(self, candidate: Candidate, state: _CandidateState) -> Outcome:
        message_id = candidate.message_id
        if self._is_tig_carrier(candidate):
            raise _Stop(None, "tig_document_only")

        with self._stage("route", message_id):
            decision = classify(candidate.sender, candidate.subject, self._config)
            if decision.is_ambiguous:
                raise _Stop(Outcome.NEEDS_REVIEW, "ambiguous_route")
        route = decision.route

        with self._stage("attachments", message_id):
            invoice = self._invoice_attachment(message_id)
        with self._stage("extract", message_id):
            extraction = self._extract(invoice)
        with self._stage("normalise", message_id):
            normalised = self._normalise(extraction)

        outcome = Outcome.PROCESSED
        if route is Route.TIG:
            with self._stage("reconcile", message_id):
                outcome = self._reconcile(candidate, route, extraction, normalised)

        with self._stage("file", message_id):
            row = self._file(candidate, route, invoice, extraction, normalised, state)
        if row is not None:
            with self._stage("book", message_id):
                self._book(row)
        return outcome

    def _is_tig_carrier(self, candidate: Candidate) -> bool:
        documents = [
            a
            for a in candidate.attachments
            if mime_type_allowed(a.mime_type, self._config.attachment_mime_allowlist)
        ]
        return bool(documents) and all(is_tig_filename(a.filename) for a in documents)

    def _invoice_attachment(self, message_id: str) -> AttachmentContent:
        allowlist = self._config.attachment_mime_allowlist
        distinct: dict[str, AttachmentContent] = {}
        for item in self._gmail.download_attachments(message_id):
            if not mime_type_allowed(item.attachment.mime_type, allowlist):
                continue
            if is_tig_filename(item.attachment.filename):
                continue
            distinct.setdefault(attachment_source_key(item.content), item)
        if not distinct:
            raise _Stop(Outcome.NEEDS_REVIEW, "no_invoice_attachment")
        if len(distinct) > 1:
            raise _Stop(Outcome.NEEDS_REVIEW, "multiple_invoice_attachments")
        (invoice,) = distinct.values()
        return invoice

    def _extract(self, invoice: AttachmentContent) -> InvoiceExtraction:
        try:
            result = self._extractor.extract(
                invoice.content, invoice.attachment.mime_type, kind=DocumentKind.INVOICE
            )
        except UnsupportedDocumentError:
            raise _Stop(Outcome.NEEDS_REVIEW, "unsupported_document") from None
        if result.status is StageStatus.ERROR:
            raise ExtractionFailedError("extractor returned an error result")
        if not result.is_ok or result.value is None:
            raise _Stop(Outcome.NEEDS_REVIEW, "incomplete_extraction")
        return result.value

    def _normalise(self, extraction: InvoiceExtraction) -> _Normalised:
        origin = determine_origin(extraction)
        currency = to_iso_code(extraction.currency, origin, self._config.currency_map)
        currency_iso = currency if isinstance(currency, str) else None
        amounts = round_amounts_for_ledger(
            normalize_amounts(extraction, currency_iso=currency_iso), currency_iso
        )
        dates = normalize_dates(extraction, origin)
        performance = select_performance_date(dates, origin)

        missing = [
            name
            for name, value, kind in (
                ("currency", currency_iso, str),
                ("net", amounts.net, Decimal),
                ("gross", amounts.gross, Decimal),
                ("due_date", dates.due_date, date),
                ("performance_date", performance, date),
            )
            if not isinstance(value, kind) or isinstance(value, NormalizationFailure)
        ]
        if missing:
            raise _Stop(Outcome.NEEDS_REVIEW, "normalisation_failed:" + ",".join(missing))
        assert isinstance(currency_iso, str)
        assert isinstance(amounts.net, Decimal) and isinstance(amounts.gross, Decimal)
        assert isinstance(dates.due_date, date) and isinstance(performance, date)
        return _Normalised(currency_iso, amounts.net, amounts.gross, dates.due_date, performance)

    def _reconcile(
        self,
        candidate: Candidate,
        route: Route,
        extraction: InvoiceExtraction,
        normalised: _Normalised,
    ) -> Outcome:
        thread = self._gmail.get_thread(candidate.thread_id)
        lookup = self._find_tig(thread, candidate.message_id, route)
        comparison = None
        if lookup.found:
            assert lookup.document is not None
            comparison = compare(
                extraction,
                lookup.document,
                self._config.rounding_tolerance,
                route=route,
                currency_iso=normalised.currency_iso,
            )
        outcome = tig_outcome(route, lookup, comparison)
        if outcome is Outcome.AWAITING_TIG:
            raise _Stop(outcome, "missing_tig")
        if outcome is Outcome.NEEDS_REVIEW:
            raise _Stop(outcome, f"tig_{lookup.status.value}")
        if comparison is not None and comparison.is_mismatch:
            assert lookup.document is not None
            result = draft_reply_on_mismatch(
                self._gmail,
                candidate.thread_id,
                comparison,
                invoice=extraction,
                tig=lookup.document,
                currency_iso=normalised.currency_iso,
            )
            if result.draft_missing:
                logger.warning(
                    "TIG mismatch draft reply could not be created; create it by hand",
                    extra={"message_id": candidate.message_id, "draft_status": result.status},
                )
        return outcome

    def _find_tig(self, thread: Thread, message_id: str, route: Route) -> TigLookup:
        def lookup(view: Thread) -> TigLookup:
            return find_tig(
                view,
                message_id,
                route=route,
                gmail=self._gmail,
                extractor=self._extractor,
                config=self._config,
            )

        found = lookup(thread)
        if not found.is_missing:
            return found
        # USR-005-04 AC4: a TIG sent into the thread after the invoice. ``find_tig`` only
        # scans messages before the invoice, so search a view with the invoice moved last
        # (drafts excluded: they are this service's own replies, never a TIG).
        invoice = next(m for m in thread.messages if m.message_id == message_id)
        others = [
            m
            for m in thread.messages
            if m.message_id != message_id and DRAFT_LABEL not in m.label_names
        ]
        if not others:
            return found
        latest = max(m.internal_date for m in thread.messages)
        view = Thread(
            thread_id=thread.thread_id,
            messages=(*others, dataclasses.replace(invoice, internal_date=latest)),
        )
        return lookup(view)

    def _file(
        self,
        candidate: Candidate,
        route: Route,
        invoice: AttachmentContent,
        extraction: InvoiceExtraction,
        normalised: _Normalised,
        state: _CandidateState,
    ) -> LedgerRow | None:
        """File the invoice (or find its earlier filing); the row to book, or None."""
        width = self._config.sequence_width
        message_id = candidate.message_id
        key = attachment_source_key(invoice.content)
        try:
            existing = find_existing_filing(self._drive, message_id, key, sequence_width=width)
        except FilingSourceConflictError:
            raise _Stop(Outcome.NEEDS_REVIEW, "filing_source_conflict") from None
        try:
            supplier8 = normalize_supplier(extraction.supplier or "")
        except ValueError:
            raise _Stop(Outcome.NEEDS_REVIEW, "supplier_unusable") from None
        yymm = yymm_folder_name(normalised.performance_date)

        if existing is not None:
            number = existing.registry_number
            if number[:4] != yymm or number[4 + width :] != supplier8:
                raise _Stop(Outcome.NEEDS_REVIEW, "filed_number_disagrees")
            state.registry_number = number
            return self._row(number, route, extraction, normalised)

        # A re-sent email of an invoice that is already booked: do not file a second copy.
        if self._is_booked(extraction):
            return None

        try:
            seq = self._allocator.allocate(yymm)
        except SequenceExhaustedError:
            raise _Stop(Outcome.NEEDS_REVIEW, "sequence_exhausted") from None
        try:
            number = assemble_for_date(normalised.performance_date, seq, supplier8, width)
            row = self._row(number, route, extraction, normalised)
            file_attachment(
                self._drive,
                number,
                invoice,
                sequence_width=width,
                performance_date=normalised.performance_date,
                source_message_id=message_id,
                require_new=True,
            )
        except (RegistryNumberError, FilingError, FilingConflictError) as exc:
            self._allocator.release(yymm, seq)
            raise _Stop(Outcome.NEEDS_REVIEW, f"filing_refused:{type(exc).__name__}") from None
        except BaseException:
            self._allocator.release(yymm, seq)
            raise
        state.registry_number = number
        return row

    def _row(
        self,
        number: str,
        route: Route,
        extraction: InvoiceExtraction,
        normalised: _Normalised,
    ) -> LedgerRow:
        try:
            return build_ledger_row(
                registry_number=number,
                provider=extraction.supplier,
                external_invoice_id=extraction.invoice_number,
                route=route,
                currency=normalised.currency_iso,
                net=normalised.net,
                gross=normalised.gross,
                due_date=normalised.due_date,
                sequence_width=self._config.sequence_width,
            )
        except BookingError:
            raise _Stop(Outcome.NEEDS_REVIEW, "ledger_row_invalid") from None

    def _is_booked(self, extraction: InvoiceExtraction) -> bool:
        try:
            return self._gate.is_duplicate(
                ext_id=extraction.invoice_number or "", provider=extraction.supplier or ""
            )
        except ValueError:
            raise _Stop(Outcome.NEEDS_REVIEW, "ledger_row_invalid") from None

    def _book(self, row: LedgerRow) -> None:
        if self._gate.is_duplicate(ext_id=row.inv_id_ext, provider=row.provider):
            return
        self._sheets.append_row(row)
        self._gate.record_booked(ext_id=row.inv_id_ext, provider=row.provider)

    def _finalise(self, candidate: Candidate, outcome: Outcome) -> None:
        plan = plan_labels(outcome, candidate.label_names, self._config.labels)
        if plan.add is not None:
            self._gmail.apply_label(candidate.message_id, plan.add)
        for label in plan.remove:
            self._gmail.remove_label(candidate.message_id, label)
        if plan.mark_read:
            self._gmail.mark_read(candidate.message_id)

    # --- logging ---

    @contextmanager
    def _stage(self, stage: str, message_id: str) -> Iterator[None]:
        start = time.perf_counter()
        outcome, reason = "ok", None
        try:
            yield
        except _Stop as stop:
            outcome, reason = "flagged", stop.reason
            raise
        except Exception as exc:
            outcome, reason = "error", type(exc).__name__
            raise
        finally:
            self._log(message_id, stage, outcome, _ms(start), reason)

    @staticmethod
    def _log(
        message_id: str, stage: str, outcome: str, duration_ms: int, reason: str | None
    ) -> None:
        logger.info(
            "stage=%s outcome=%s message_id=%s duration_ms=%d",
            stage,
            outcome,
            message_id,
            duration_ms,
            extra={
                "stage": stage,
                "outcome": outcome,
                "message_id": message_id,
                "duration_ms": duration_ms,
                "reason": reason,
            },
        )


@dataclass
class _CandidateState:
    registry_number: str | None = None


def _ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


def run_cycle(sheets: SheetsPort, connect: Callable[[Config], PipelineClients]) -> RunSummary:
    """One full polling cycle: Config, header check, clients, then every candidate.

    ``connect`` builds the Gmail/Drive clients and the extractor from the loaded Config,
    so tests inject fakes and production builds real clients (``intake.pipeline.runtime``).
    """
    config = sheets.load_config()
    validate_label_names(config.labels)
    sheets.verify_ledger_header()
    clients = connect(config)
    return Orchestrator(
        config=config,
        gmail=clients.gmail,
        drive=clients.drive,
        sheets=sheets,
        extractor=clients.extractor,
    ).run()


__all__ = [
    "CandidateResult",
    "CandidateStatus",
    "DrivePort",
    "ExtractionFailedError",
    "GmailPort",
    "Orchestrator",
    "PipelineClients",
    "RunSummary",
    "SheetsPort",
    "mime_type_allowed",
    "run_cycle",
]
