"""In-memory fakes of the Gmail, Drive and Sheets clients and the extractor.

They model just enough state for the orchestrator's state machine: message labels and
read state, thread order, drafts, Drive month folders with source-linked files, and the
ledger rows. Every client method can be made to fail via ``fail`` (method name ->
exception), so each stage's failure path can be injected.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from intake.clients.drive_client import (
    SOURCE_ATTACHMENT_PROPERTY,
    SOURCE_MESSAGE_ID_PROPERTY,
    FiledFileRef,
    SavedFile,
)
from intake.clients.gmail_client import (
    AttachmentContent,
    GmailLabelNotFoundError,
    GmailReplyError,
    Thread,
    ThreadMessage,
)
from intake.clients.sheets_client import LedgerRow
from intake.config import Config, LabelNames
from intake.extraction import DocumentKind
from intake.models import (
    Attachment,
    Candidate,
    FlagReason,
    InvoiceExtraction,
    LineItem,
    StageResult,
    StageStatus,
)

LABELS = LabelNames(
    processed="Kibit/Processed",
    pending="Kibit/Pending",
    needs_review="Kibit/NeedsReview",
    awaiting_tig="Kibit/AwaitingTIG",
)

CONTRACTOR_DOMAIN = "contractor.example"


def make_config(**overrides: Any) -> Config:
    raw: dict[str, Any] = {
        "invoice_keywords": "számla, invoice",
        "attachment_mime_allowlist": "application/pdf, image/*",
        "contractor_identifiers": CONTRACTOR_DOMAIN,
        "tig_subject_indicators": "TIG, teljesítésigazolás",
        "currency_map": "Ft=HUF, €=EUR, $=USD",
        "label_processed": LABELS.processed,
        "label_pending": LABELS.pending,
        "label_needs_review": LABELS.needs_review,
        "label_awaiting_tig": LABELS.awaiting_tig,
        "sequence_start": "1",
        "sequence_width": "3",
        "rounding_tolerance": "0.01",
        "drive_root_folder_id": "root-folder",
    }
    raw.update(overrides)
    return Config.from_mapping(raw)


class _Failing:
    """``fail``: method -> exception raised on every call; ``fail_once``: on the next call."""

    def __init__(self) -> None:
        self.fail: dict[str, BaseException] = {}
        self.fail_once: dict[str, BaseException] = {}

    def _check(self, method: str) -> None:
        error = self.fail_once.pop(method, None) or self.fail.get(method)
        if error is not None:
            raise error


# --- Gmail --------------------------------------------------------------------------------


@dataclass
class FakeMessage:
    message_id: str
    thread_id: str
    sender: str
    subject: str
    files: list[tuple[str, str, bytes]]
    labels: set[str]
    internal_date: int
    rfc822_message_id: str | None = None


class FakeGmail(_Failing):
    """The mailbox: messages in thread order, labels by name, drafts as DRAFT messages."""

    SYSTEM_LABELS = frozenset({"INBOX", "UNREAD", "SENT", "DRAFT"})

    def __init__(self, labels: LabelNames = LABELS) -> None:
        super().__init__()
        self.labels = labels
        self.known_labels = {
            labels.processed,
            labels.pending,
            labels.needs_review,
            labels.awaiting_tig,
        }
        self.messages: dict[str, FakeMessage] = {}
        self.writes: list[tuple[str, str, str | None]] = []
        self.drafts: list[tuple[str, str]] = []
        self._clock = 1_000

    # --- test setup ---

    def add_message(
        self,
        message_id: str,
        *,
        thread_id: str | None = None,
        sender: str = "Billing <billing@supplier.example>",
        subject: str = "Számla INV-1",
        files: Iterable[tuple[str, str, bytes]] = (),
        labels: Iterable[str] = ("INBOX", "UNREAD"),
    ) -> FakeMessage:
        self._clock += 1_000
        message = FakeMessage(
            message_id=message_id,
            thread_id=thread_id or f"thread-{message_id}",
            sender=sender,
            subject=subject,
            files=list(files),
            labels=set(labels),
            internal_date=self._clock,
            rfc822_message_id=f"<{message_id}@mail.example>",
        )
        self.messages[message_id] = message
        return message

    def labels_of(self, message_id: str) -> set[str]:
        return self.messages[message_id].labels

    def is_unread(self, message_id: str) -> bool:
        return "UNREAD" in self.messages[message_id].labels

    def kibit_labels_of(self, message_id: str) -> set[str]:
        return self.labels_of(message_id) & self.known_labels

    def writes_for(self, message_id: str) -> list[tuple[str, str, str | None]]:
        return [w for w in self.writes if w[1] == message_id]

    # --- GmailClient surface ---

    def list_candidates(self) -> list[Candidate]:
        self._check("list_candidates")
        terminal = {self.labels.processed, self.labels.pending, self.labels.needs_review}
        result: list[Candidate] = []
        for m in self.messages.values():
            if {"INBOX", "UNREAD"} <= m.labels and not (terminal & m.labels):
                result.append(
                    Candidate(
                        message_id=m.message_id,
                        thread_id=m.thread_id,
                        sender=m.sender,
                        subject=m.subject,
                        attachments=self._attachments(m),
                        label_names=frozenset(m.labels),
                    )
                )
        return result

    def get_thread(self, thread_id: str) -> Thread:
        self._check("get_thread")
        return Thread(
            thread_id=thread_id,
            messages=tuple(
                ThreadMessage(
                    message_id=m.message_id,
                    thread_id=m.thread_id,
                    sender=m.sender,
                    subject=m.subject,
                    rfc822_message_id=m.rfc822_message_id,
                    references=None,
                    internal_date=m.internal_date,
                    attachments=self._attachments(m),
                    label_names=frozenset(m.labels),
                )
                for m in self.messages.values()
                if m.thread_id == thread_id
            ),
        )

    def download_attachments(self, message_id: str) -> list[AttachmentContent]:
        self._check("download_attachments")
        m = self.messages[message_id]
        return [
            AttachmentContent(attachment, content)
            for attachment, (_, _, content) in zip(self._attachments(m), m.files, strict=True)
        ]

    def apply_label(self, message_id: str, label: str) -> None:
        self._check("apply_label")
        if label not in self.known_labels:
            raise GmailLabelNotFoundError(label)
        self.writes.append(("apply_label", message_id, label))
        self.messages[message_id].labels.add(label)

    def remove_label(self, message_id: str, label: str) -> None:
        self._check("remove_label")
        if label not in self.known_labels:
            raise GmailLabelNotFoundError(label)
        self.writes.append(("remove_label", message_id, label))
        self.messages[message_id].labels.discard(label)

    def mark_read(self, message_id: str) -> None:
        self._check("mark_read")
        self.writes.append(("mark_read", message_id, None))
        self.messages[message_id].labels.discard("UNREAD")

    def create_draft_reply(self, thread_id: str, body: str) -> str:
        self._check("create_draft_reply")
        inbound = [
            m
            for m in self.messages.values()
            if m.thread_id == thread_id and not ({"SENT", "DRAFT"} & m.labels)
        ]
        if not inbound:
            raise GmailReplyError("no inbound message")
        draft_id = f"draft-{len(self.drafts) + 1}"
        self.drafts.append((thread_id, body))
        self._clock += 1_000
        self.messages[draft_id] = FakeMessage(
            message_id=draft_id,
            thread_id=thread_id,
            sender="ap@kibit.example",
            subject="Re: draft",
            files=[],
            labels={"DRAFT"},
            internal_date=self._clock,
        )
        return draft_id

    @staticmethod
    def _attachments(m: FakeMessage) -> tuple[Attachment, ...]:
        return tuple(
            Attachment(filename=name, mime_type=mime, attachment_id=f"{m.message_id}-att-{i}")
            for i, (name, mime, _) in enumerate(m.files)
        )


# --- Drive --------------------------------------------------------------------------------


@dataclass
class FakeFile:
    file_id: str
    name: str
    folder_id: str
    content: bytes
    mime_type: str | None
    app_properties: dict[str, str] = field(default_factory=dict)


class FakeDrive(_Failing):
    """Month folders under one root, files with optional source-link app properties."""

    def __init__(self) -> None:
        super().__init__()
        self.folders: dict[str, str] = {}
        self.files: dict[str, FakeFile] = {}
        self.uploads = 0

    def filenames(self, yymm: str) -> list[str]:
        folder = self.folders.get(yymm)
        return sorted(f.name for f in self.files.values() if f.folder_id == folder)

    def add_file(self, yymm: str, name: str, content: bytes = b"x", **props: str) -> FakeFile:
        folder_id = self.create_month_folder(yymm)
        file_id = f"file-{len(self.files) + 1}"
        self.files[file_id] = FakeFile(file_id, name, folder_id, content, None, dict(props))
        return self.files[file_id]

    def find_month_folder(self, yymm: str) -> str | None:
        self._check("find_month_folder")
        return self.folders.get(yymm)

    def create_month_folder(self, yymm: str) -> str:
        self._check("create_month_folder")
        return self.folders.setdefault(yymm, f"folder-{yymm}")

    def list_month_folder(self, yymm: str) -> list[str]:
        self._check("list_month_folder")
        return self.filenames(yymm)

    def save_file(
        self,
        folder_id: str,
        filename: str,
        content: bytes,
        mime_type: str | None = None,
        *,
        source_message_id: str | None = None,
        source_attachment_key: str | None = None,
    ) -> SavedFile:
        self._check("save_file")
        for f in self.files.values():
            if f.folder_id == folder_id and f.name == filename:
                return SavedFile(f.file_id, filename, created=False)
        props: dict[str, str] = {}
        if source_message_id is not None:
            props[SOURCE_MESSAGE_ID_PROPERTY] = source_message_id
        if source_attachment_key is not None:
            props[SOURCE_ATTACHMENT_PROPERTY] = source_attachment_key
        file_id = f"file-{len(self.files) + 1}"
        self.files[file_id] = FakeFile(file_id, filename, folder_id, content, mime_type, props)
        self.uploads += 1
        return SavedFile(file_id, filename, created=True)

    def find_filed_by_source(
        self, message_id: str, attachment_key: str | None = None
    ) -> list[FiledFileRef]:
        self._check("find_filed_by_source")
        refs: list[FiledFileRef] = []
        for f in self.files.values():
            if f.app_properties.get(SOURCE_MESSAGE_ID_PROPERTY) != message_id:
                continue
            if (
                attachment_key is not None
                and f.app_properties.get(SOURCE_ATTACHMENT_PROPERTY) != attachment_key
            ):
                continue
            refs.append(FiledFileRef(f.file_id, f.name, f.folder_id))
        return refs


# --- Sheets -------------------------------------------------------------------------------


class FakeSheets(_Failing):
    """Config tab plus an append-only ledger."""

    def __init__(self, config: Config | None = None) -> None:
        super().__init__()
        self.config = config or make_config()
        self.rows: list[LedgerRow] = []
        self.appends = 0

    def load_config(self) -> Config:
        self._check("load_config")
        return self.config

    def verify_ledger_header(self) -> None:
        self._check("verify_ledger_header")

    def load_existing_keys(self) -> set[tuple[str, str]]:
        self._check("load_existing_keys")
        return {(row.inv_id_ext, row.provider) for row in self.rows}

    def append_row(self, row: LedgerRow) -> None:
        self._check("append_row")
        self.appends += 1
        self.rows.append(row)


# --- Extractor ----------------------------------------------------------------------------

type ExtractorAnswer = StageResult[InvoiceExtraction] | BaseException


class FakeExtractor:
    """Answers per document content (bytes); unknown documents fail the test loudly."""

    def __init__(self, answers: Mapping[bytes, ExtractorAnswer] | None = None) -> None:
        self.answers: dict[bytes, ExtractorAnswer] = dict(answers or {})
        self.calls: list[tuple[str, str, DocumentKind]] = []
        self.on_call: Callable[[bytes], None] | None = None

    def extract(
        self, content: bytes, mime_type: str, *, kind: DocumentKind = DocumentKind.INVOICE
    ) -> StageResult[InvoiceExtraction]:
        self.calls.append((hashlib.sha256(content).hexdigest()[:8], mime_type, kind))
        answer = self.answers[content]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def kinds(self) -> list[DocumentKind]:
        return [kind for _, _, kind in self.calls]


# --- documents ----------------------------------------------------------------------------


def invoice_extraction(**overrides: Any) -> InvoiceExtraction:
    """A complete domestic HUF invoice: 10 x 10 000 Ft = 100 000 Ft net."""
    values: dict[str, Any] = {
        "supplier": "Kővári Kft",
        "invoice_number": "INV-1",
        "currency": "Ft",
        "net": "100 000",
        "gross": "127 000,40",
        "due_date": "2026.11.04.",
        "issue_date": "2026.10.05.",
        "supply_date": "2026.10.05.",
        "performance_date": "2026.10.05.",
        "supplier_country": "HU",
        "line_items": (LineItem("Fejlesztés", Decimal(10), Decimal(10000), Decimal(100000)),),
    }
    values.update(overrides)
    return InvoiceExtraction(**values)


def tig_extraction(
    quantity: str = "10", unit_price: str = "10000", total: str | None = "100 000 Ft"
) -> InvoiceExtraction:
    q, p = Decimal(quantity), Decimal(unit_price)
    return InvoiceExtraction(
        supplier="Kővári Kft",
        invoice_number="TIG-1",
        currency="HUF",
        net=total,
        supplier_country="HU",
        line_items=(LineItem("Fejlesztés", q, p, q * p),),
    )


def ok(extraction: InvoiceExtraction) -> StageResult[InvoiceExtraction]:
    return StageResult.ok(extraction)


def incomplete(extraction: InvoiceExtraction | None = None) -> StageResult[InvoiceExtraction]:
    return StageResult[InvoiceExtraction](
        StageStatus.INCOMPLETE,
        value=extraction,
        flag_reason=FlagReason.INCOMPLETE_DATA,
        detail="missing required field(s): due_date",
    )
