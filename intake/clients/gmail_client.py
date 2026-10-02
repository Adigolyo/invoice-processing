"""Thin wrapper over the Gmail API for the shared invoice mailbox.

The ``googleapiclient`` Gmail resource (``build("gmail", "v1", credentials=...)``) is
injected through the constructor, so unit tests pass a fake and never touch the network.

Error semantics: every Gmail API failure propagates unchanged as
``googleapiclient.errors.HttpError`` (never caught, retried or swallowed here), so the
orchestrator can abort a candidate before any label or read-state change. Responses that
do not have the expected shape raise ``GmailResponseError``.

Label handling: ``apply_label`` takes a label *name* (from ``Config.labels``) and resolves
it to Gmail's label ID. The specification does not say the service should create labels,
so a name that does not exist in the mailbox raises ``GmailLabelNotFoundError``; the four
Kibit labels must be created once during mailbox setup.

Only ``intake/pipeline/orchestrator.py`` may call ``apply_label`` and ``mark_read``.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Any

from intake.config import LabelNames
from intake.models import Attachment, Candidate

GmailResource = Any
"""The untyped ``googleapiclient`` Gmail v1 resource."""

UNREAD_LABEL_ID = "UNREAD"
SENT_LABEL_ID = "SENT"


class GmailResponseError(RuntimeError):
    """The Gmail API answered, but not with the shape this client relies on."""


class GmailLabelNotFoundError(LookupError):
    """A label name from Config does not exist in the mailbox."""


class GmailReplyError(RuntimeError):
    """A threaded draft reply cannot be built from the thread's messages."""


@dataclass(frozen=True, slots=True)
class ThreadMessage:
    """One message of a Gmail thread, with the headers needed for reconciliation/replies."""

    message_id: str
    thread_id: str
    sender: str
    subject: str
    rfc822_message_id: str | None
    references: str | None
    internal_date: int
    attachments: tuple[Attachment, ...] = ()
    label_names: frozenset[str] = field(default_factory=frozenset)

    def to_candidate(self) -> Candidate:
        return Candidate(
            message_id=self.message_id,
            thread_id=self.thread_id,
            sender=self.sender,
            subject=self.subject,
            attachments=self.attachments,
            label_names=self.label_names,
        )


@dataclass(frozen=True, slots=True)
class Thread:
    """A Gmail thread; ``messages`` are in Gmail's order (oldest first)."""

    thread_id: str
    messages: tuple[ThreadMessage, ...]


@dataclass(frozen=True, slots=True)
class AttachmentContent:
    """An attachment's metadata together with its decoded bytes."""

    attachment: Attachment
    content: bytes


def _quote_label(name: str) -> str:
    if any(ch.isspace() or ch == '"' for ch in name):
        escaped = name.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return name


def build_candidate_query(labels: LabelNames) -> str:
    """Gmail search for unread inbox mail not yet in a terminal Kibit state.

    ``labels.awaiting_tig`` is deliberately *not* excluded: those threads are re-evaluated
    every run (ADR 3, USR-005-04).
    """
    excluded = (labels.processed, labels.pending, labels.needs_review)
    return " ".join(["in:inbox", "is:unread", *(f"-label:{_quote_label(n)}" for n in excluded)])


def _execute(request: Any) -> dict[str, Any]:
    result = request.execute()
    if not isinstance(result, dict):
        raise GmailResponseError(f"unexpected Gmail API response type: {type(result).__name__}")
    return result


def _require_str(data: Mapping[str, Any], key: str, what: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise GmailResponseError(f"{what} is missing {key!r}")
    return value


def _decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _headers(payload: Mapping[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for header in payload.get("headers") or ():
        name = header.get("name")
        if isinstance(name, str) and name.lower() not in result:
            result[name.lower()] = str(header.get("value", ""))
    return result


def _walk_parts(payload: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    for part in payload.get("parts") or ():
        yield part
        yield from _walk_parts(part)


def _attachment_parts(payload: Mapping[str, Any]) -> Iterator[tuple[Attachment, Mapping[str, Any]]]:
    """Every part carrying a filename, i.e. a (non-body) attachment, depth-first."""
    for part in _walk_parts(payload):
        filename = part.get("filename")
        if not filename:
            continue
        body = part.get("body") or {}
        attachment_id = body.get("attachmentId")
        yield (
            Attachment(
                filename=str(filename),
                mime_type=str(part.get("mimeType", "")),
                attachment_id=str(attachment_id) if attachment_id else None,
            ),
            body,
        )


class GmailClient:
    """Gmail operations used by the pipeline, for one mailbox (``user_id``)."""

    def __init__(self, service: GmailResource, labels: LabelNames, user_id: str = "me") -> None:
        self._service = service
        self._labels = labels
        self._user_id = user_id
        self._label_ids_by_name: dict[str, str] | None = None

    # --- reads ---------------------------------------------------------------------------

    def list_candidates(self) -> list[Candidate]:
        """All messages matching the candidate query, fully fetched, in Gmail's order.

        Read-only: no label or read-state is changed. Any API error raises before a
        partial list is returned.
        """
        query = build_candidate_query(self._labels)
        names = self._label_names_by_id()
        candidates: list[Candidate] = []
        for message_id in self._list_message_ids(query):
            message = self._get_message(message_id)
            candidates.append(self._to_thread_message(message, names).to_candidate())
        return candidates

    def get_thread(self, thread_id: str) -> Thread:
        """Every message in the thread (oldest first), with attachment metadata."""
        response = _execute(
            self._service.users().threads().get(userId=self._user_id, id=thread_id, format="full")
        )
        names = self._label_names_by_id()
        messages = tuple(self._to_thread_message(m, names) for m in response.get("messages") or ())
        return Thread(thread_id=str(response.get("id", thread_id)), messages=messages)

    def download_attachments(self, message_id: str) -> list[AttachmentContent]:
        """Decoded bytes of every attachment of a message (no MIME filtering here)."""
        payload = self._get_message(message_id).get("payload") or {}
        result: list[AttachmentContent] = []
        for attachment, body in _attachment_parts(payload):
            if attachment.attachment_id is not None:
                fetched = _execute(
                    self._service.users()
                    .messages()
                    .attachments()
                    .get(userId=self._user_id, messageId=message_id, id=attachment.attachment_id)
                )
                data = fetched.get("data")
            else:
                data = body.get("data")
            if not isinstance(data, str):
                raise GmailResponseError(
                    f"attachment {attachment.filename!r} of message {message_id} has no data"
                )
            result.append(AttachmentContent(attachment, _decode(data)))
        return result

    # --- writes --------------------------------------------------------------------------

    def apply_label(self, message_id: str, label: str) -> None:
        """Add the label called ``label`` to a message (additive, idempotent in Gmail)."""
        label_id = self._resolve_label_id(label)
        self._modify(message_id, {"addLabelIds": [label_id]})

    def mark_read(self, message_id: str) -> None:
        self._modify(message_id, {"removeLabelIds": [UNREAD_LABEL_ID]})

    def create_draft_reply(self, thread_id: str, body: str) -> str:
        """Create (never send) a plain-text draft replying to the thread; returns the draft ID.

        The reply goes to the ``From`` of the latest message in the thread that the mailbox
        did not send itself, with ``Re:`` subject and ``In-Reply-To``/``References`` set so
        Gmail threads it.
        """
        thread = self.get_thread(thread_id)
        inbound = [m for m in thread.messages if SENT_LABEL_ID not in m.label_names]
        if not inbound:
            raise GmailReplyError(f"thread {thread_id} has no inbound message to reply to")
        target = inbound[-1]
        if not target.sender.strip():
            raise GmailReplyError(f"message {target.message_id} has no sender to reply to")
        if not target.rfc822_message_id:
            raise GmailReplyError(
                f"message {target.message_id} has no Message-ID header; reply cannot be threaded"
            )

        message = EmailMessage()
        message["To"] = target.sender
        subject = target.subject.strip()
        message["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        message["In-Reply-To"] = target.rfc822_message_id
        references = " ".join(filter(None, [target.references, target.rfc822_message_id]))
        message["References"] = references
        message.set_content(body)
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")

        response = _execute(
            self._service.users()
            .drafts()
            .create(userId=self._user_id, body={"message": {"raw": raw, "threadId": thread_id}})
        )
        return _require_str(response, "id", "drafts.create response")

    # --- helpers -------------------------------------------------------------------------

    def _list_message_ids(self, query: str) -> list[str]:
        ids: list[str] = []
        page_token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"userId": self._user_id, "q": query}
            if page_token:
                kwargs["pageToken"] = page_token
            response = _execute(self._service.users().messages().list(**kwargs))
            for ref in response.get("messages") or ():
                ids.append(_require_str(ref, "id", "messages.list entry"))
            page_token = response.get("nextPageToken")
            if not page_token:
                return ids

    def _get_message(self, message_id: str) -> dict[str, Any]:
        return _execute(
            self._service.users().messages().get(userId=self._user_id, id=message_id, format="full")
        )

    def _modify(self, message_id: str, body: dict[str, list[str]]) -> None:
        _execute(
            self._service.users().messages().modify(userId=self._user_id, id=message_id, body=body)
        )

    def _fetch_labels(self) -> dict[str, str]:
        response = _execute(self._service.users().labels().list(userId=self._user_id))
        labels: dict[str, str] = {}
        for label in response.get("labels") or ():
            labels[_require_str(label, "name", "label")] = _require_str(label, "id", "label")
        self._label_ids_by_name = labels
        return labels

    def _label_names_by_id(self) -> dict[str, str]:
        by_name = self._label_ids_by_name or self._fetch_labels()
        return {label_id: name for name, label_id in by_name.items()}

    def _resolve_label_id(self, name: str) -> str:
        by_name = self._label_ids_by_name or self._fetch_labels()
        if name not in by_name:
            # The cache may predate a label created since; re-read once before failing.
            by_name = self._fetch_labels()
        if name not in by_name:
            raise GmailLabelNotFoundError(f"Gmail label {name!r} does not exist in the mailbox")
        return by_name[name]

    @staticmethod
    def _to_thread_message(message: Mapping[str, Any], names: Mapping[str, str]) -> ThreadMessage:
        payload = message.get("payload") or {}
        headers = _headers(payload)
        return ThreadMessage(
            message_id=_require_str(message, "id", "message"),
            thread_id=_require_str(message, "threadId", "message"),
            sender=headers.get("from", ""),
            subject=headers.get("subject", ""),
            rfc822_message_id=headers.get("message-id") or None,
            references=headers.get("references") or None,
            internal_date=int(message.get("internalDate") or 0),
            attachments=tuple(a for a, _ in _attachment_parts(payload)),
            label_names=frozenset(names.get(i, i) for i in message.get("labelIds") or ()),
        )


__all__ = [
    "AttachmentContent",
    "GmailClient",
    "GmailLabelNotFoundError",
    "GmailReplyError",
    "GmailResponseError",
    "Thread",
    "ThreadMessage",
    "build_candidate_query",
]
