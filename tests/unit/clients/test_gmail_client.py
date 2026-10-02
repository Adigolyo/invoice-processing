"""Task 4: Gmail client wrapper, against a MagicMock ``googleapiclient`` service (no network)."""

from __future__ import annotations

import base64
import email
from email import policy
from typing import Any
from unittest.mock import MagicMock

import httplib2
import pytest
from googleapiclient.errors import HttpError

from intake.clients import gmail_client
from intake.clients.gmail_client import (
    AttachmentContent,
    GmailClient,
    GmailLabelNotFoundError,
    GmailReplyError,
    GmailResponseError,
    build_candidate_query,
)
from intake.config import LabelNames
from intake.models import Attachment, Candidate

LABELS = LabelNames(
    processed="Kibit/Processed",
    pending="Kibit/Pending",
    needs_review="Kibit/NeedsReview",
    awaiting_tig="Kibit/AwaitingTIG",
)

LABEL_LIST = {
    "labels": [
        {"id": "INBOX", "name": "INBOX"},
        {"id": "UNREAD", "name": "UNREAD"},
        {"id": "SENT", "name": "SENT"},
        {"id": "Label_1", "name": "Kibit/Processed"},
        {"id": "Label_2", "name": "Kibit/Pending"},
        {"id": "Label_3", "name": "Kibit/NeedsReview"},
        {"id": "Label_4", "name": "Kibit/AwaitingTIG"},
    ]
}


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _http_error(status: int = 503) -> HttpError:
    return HttpError(httplib2.Response({"status": status}), b'{"error": "boom"}')


def _request(result: Any = None, error: Exception | None = None) -> MagicMock:
    req = MagicMock(name="request")
    if error is not None:
        req.execute.side_effect = error
    else:
        req.execute.return_value = result
    return req


def _message(
    message_id: str,
    *,
    thread_id: str = "t1",
    sender: str = "Supplier Kft <billing@supplier.example>",
    subject: str = "Számla 2024/05",
    label_ids: tuple[str, ...] = ("INBOX", "UNREAD"),
    message_id_header: str | None = "<orig-1@supplier.example>",
    references: str | None = None,
    parts: list[dict[str, Any]] | None = None,
    internal_date: str = "1717000000000",
) -> dict[str, Any]:
    headers = [{"name": "From", "value": sender}, {"name": "Subject", "value": subject}]
    if message_id_header is not None:
        headers.append({"name": "Message-ID", "value": message_id_header})
    if references is not None:
        headers.append({"name": "References", "value": references})
    return {
        "id": message_id,
        "threadId": thread_id,
        "labelIds": list(label_ids),
        "internalDate": internal_date,
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": headers,
            "parts": parts if parts is not None else [],
        },
    }


PDF_PART = {
    "partId": "1",
    "mimeType": "application/pdf",
    "filename": "invoice.pdf",
    "body": {"attachmentId": "att-1", "size": 1234},
}
INLINE_IMAGE_PART = {
    "partId": "2",
    "mimeType": "image/png",
    "filename": "scan.png",
    "body": {"data": _b64(b"\x89PNG-inline"), "size": 11},
}
BODY_PART = {
    "partId": "0",
    "mimeType": "multipart/alternative",
    "filename": "",
    "body": {"size": 0},
    "parts": [
        {"partId": "0.0", "mimeType": "text/plain", "filename": "", "body": {"data": _b64(b"hi")}},
    ],
}


class FakeGmail:
    """Wires a MagicMock that looks like ``build("gmail", "v1")`` for the calls we make."""

    def __init__(self) -> None:
        self.service = MagicMock(name="gmail")
        self.users = self.service.users.return_value
        self.messages = self.users.messages.return_value
        self.labels = self.users.labels.return_value
        self.threads = self.users.threads.return_value
        self.attachments = self.messages.attachments.return_value
        self.drafts = self.users.drafts.return_value
        self.labels.list.return_value = _request(LABEL_LIST)
        self.message_store: dict[str, dict[str, Any]] = {}
        self.messages.get.side_effect = self._get_message
        self.list_pages: list[dict[str, Any]] = [{"resultSizeEstimate": 0}]
        self.messages.list.side_effect = self._list
        self.modify_calls: list[dict[str, Any]] = []
        self.messages.modify.side_effect = self._modify

    def _get_message(self, **kwargs: Any) -> MagicMock:
        msg_id = kwargs["id"]
        if msg_id not in self.message_store:
            return _request(error=_http_error(404))
        return _request(self.message_store[msg_id])

    def _list(self, **kwargs: Any) -> MagicMock:
        token = kwargs.get("pageToken")
        index = 0 if token is None else int(token)
        return _request(self.list_pages[index])

    def _modify(self, **kwargs: Any) -> MagicMock:
        self.modify_calls.append(kwargs)
        return _request({"id": kwargs["id"]})

    def client(self) -> GmailClient:
        return GmailClient(self.service, LABELS)


# --- candidate query ---------------------------------------------------------------------


def test_candidate_query_matches_the_design_for_default_label_names() -> None:
    assert build_candidate_query(LABELS) == (
        "in:inbox is:unread -label:Kibit/Processed -label:Kibit/Pending -label:Kibit/NeedsReview"
    )


def test_candidate_query_is_built_from_config_labels_not_hardcoded() -> None:
    labels = LabelNames(
        processed="Acme/Done", pending="Acme/Wait", needs_review="Acme/Check", awaiting_tig="X"
    )
    query = build_candidate_query(labels)
    assert query == "in:inbox is:unread -label:Acme/Done -label:Acme/Wait -label:Acme/Check"
    assert "Kibit" not in query


def test_candidate_query_does_not_exclude_awaiting_tig() -> None:
    assert "AwaitingTIG" not in build_candidate_query(LABELS)


def test_candidate_query_quotes_label_names_with_spaces() -> None:
    labels = LabelNames(
        processed="Kibit/Needs Review", pending="P", needs_review='Odd "q"', awaiting_tig="A"
    )
    query = build_candidate_query(labels)
    assert '-label:"Kibit/Needs Review"' in query
    assert "-label:P" in query
    assert '-label:"Odd \\"q\\""' in query


# --- list_candidates ---------------------------------------------------------------------


def test_list_candidates_uses_query_and_maps_messages_to_candidates() -> None:
    fake = FakeGmail()
    fake.list_pages = [{"messages": [{"id": "m1", "threadId": "t1"}]}]
    fake.message_store["m1"] = _message(
        "m1", label_ids=("INBOX", "UNREAD", "Label_4"), parts=[BODY_PART, PDF_PART]
    )

    candidates = fake.client().list_candidates()

    assert candidates == [
        Candidate(
            message_id="m1",
            thread_id="t1",
            sender="Supplier Kft <billing@supplier.example>",
            subject="Számla 2024/05",
            attachments=(Attachment("invoice.pdf", "application/pdf", "att-1"),),
            label_names=frozenset({"INBOX", "UNREAD", "Kibit/AwaitingTIG"}),
        )
    ]
    list_kwargs = fake.messages.list.call_args.kwargs
    assert list_kwargs["userId"] == "me"
    assert list_kwargs["q"] == build_candidate_query(LABELS)
    assert fake.messages.get.call_args.kwargs == {"userId": "me", "id": "m1", "format": "full"}


def test_list_candidates_follows_pagination() -> None:
    fake = FakeGmail()
    fake.list_pages = [
        {"messages": [{"id": "m1"}], "nextPageToken": "1"},
        {"messages": [{"id": "m2"}]},
    ]
    fake.message_store["m1"] = _message("m1")
    fake.message_store["m2"] = _message("m2", thread_id="t2")

    candidates = fake.client().list_candidates()

    assert [c.message_id for c in candidates] == ["m1", "m2"]
    assert fake.messages.list.call_count == 2
    assert fake.messages.list.call_args_list[1].kwargs["pageToken"] == "1"


def test_list_candidates_returns_empty_list_when_no_messages() -> None:
    fake = FakeGmail()
    assert fake.client().list_candidates() == []


def test_list_candidates_includes_nested_and_inline_attachments() -> None:
    fake = FakeGmail()
    fake.list_pages = [{"messages": [{"id": "m1"}]}]
    nested = {
        "partId": "1",
        "mimeType": "multipart/mixed",
        "filename": "",
        "body": {"size": 0},
        "parts": [PDF_PART, INLINE_IMAGE_PART],
    }
    fake.message_store["m1"] = _message("m1", parts=[BODY_PART, nested])

    (candidate,) = fake.client().list_candidates()

    assert candidate.attachments == (
        Attachment("invoice.pdf", "application/pdf", "att-1"),
        Attachment("scan.png", "image/png", None),
    )


def test_list_candidates_handles_single_part_message_with_missing_headers() -> None:
    fake = FakeGmail()
    fake.list_pages = [{"messages": [{"id": "m1"}]}]
    fake.message_store["m1"] = {
        "id": "m1",
        "threadId": "t1",
        "payload": {"mimeType": "text/plain", "body": {"data": _b64(b"x")}},
    }

    (candidate,) = fake.client().list_candidates()

    assert candidate.sender == ""
    assert candidate.subject == ""
    assert candidate.attachments == ()
    assert candidate.label_names == frozenset()


def test_list_candidates_reads_headers_case_insensitively() -> None:
    fake = FakeGmail()
    fake.list_pages = [{"messages": [{"id": "m1"}]}]
    msg = _message("m1")
    msg["payload"]["headers"] = [
        {"name": "from", "value": "a@b.example"},
        {"name": "SUBJECT", "value": "Invoice"},
    ]
    fake.message_store["m1"] = msg

    (candidate,) = fake.client().list_candidates()

    assert (candidate.sender, candidate.subject) == ("a@b.example", "Invoice")


def test_list_candidates_raises_on_list_api_error_and_never_modifies() -> None:
    fake = FakeGmail()
    fake.messages.list.side_effect = None
    fake.messages.list.return_value = _request(error=_http_error(503))

    with pytest.raises(HttpError):
        fake.client().list_candidates()

    fake.messages.modify.assert_not_called()


def test_list_candidates_raises_when_a_message_fetch_fails() -> None:
    fake = FakeGmail()
    fake.list_pages = [{"messages": [{"id": "missing"}]}]

    with pytest.raises(HttpError):
        fake.client().list_candidates()

    fake.messages.modify.assert_not_called()


def test_list_candidates_raises_on_malformed_response() -> None:
    fake = FakeGmail()
    fake.messages.list.side_effect = None
    fake.messages.list.return_value = _request(["not", "a", "dict"])

    with pytest.raises(GmailResponseError):
        fake.client().list_candidates()


# --- get_thread --------------------------------------------------------------------------


def test_get_thread_returns_all_messages_in_order() -> None:
    fake = FakeGmail()
    fake.threads.get.return_value = _request(
        {
            "id": "t1",
            "messages": [
                _message("m1", parts=[PDF_PART], internal_date="100"),
                _message(
                    "m2",
                    sender="Kibit <ap@kibit.example>",
                    subject="Re: Számla",
                    label_ids=("SENT",),
                    message_id_header="<reply@kibit.example>",
                    references="<orig-1@supplier.example>",
                    internal_date="200",
                ),
            ],
        }
    )

    thread = fake.client().get_thread("t1")

    assert thread.thread_id == "t1"
    assert [m.message_id for m in thread.messages] == ["m1", "m2"]
    first, second = thread.messages
    assert first.attachments == (Attachment("invoice.pdf", "application/pdf", "att-1"),)
    assert first.rfc822_message_id == "<orig-1@supplier.example>"
    assert first.internal_date == 100
    assert second.label_names == frozenset({"SENT"})
    assert second.references == "<orig-1@supplier.example>"
    assert fake.threads.get.call_args.kwargs == {"userId": "me", "id": "t1", "format": "full"}


def test_get_thread_raises_on_api_error() -> None:
    fake = FakeGmail()
    fake.threads.get.return_value = _request(error=_http_error(500))

    with pytest.raises(HttpError):
        fake.client().get_thread("t1")


def test_thread_message_to_candidate_round_trips() -> None:
    fake = FakeGmail()
    fake.threads.get.return_value = _request({"id": "t1", "messages": [_message("m1")]})

    (msg,) = fake.client().get_thread("t1").messages

    assert msg.to_candidate() == Candidate(
        message_id="m1",
        thread_id="t1",
        sender=msg.sender,
        subject=msg.subject,
        attachments=(),
        label_names=frozenset({"INBOX", "UNREAD"}),
    )


# --- download_attachments ----------------------------------------------------------------


def test_download_attachments_fetches_remote_and_decodes_inline_parts() -> None:
    fake = FakeGmail()
    fake.message_store["m1"] = _message("m1", parts=[BODY_PART, PDF_PART, INLINE_IMAGE_PART])
    fake.attachments.get.return_value = _request({"data": _b64(b"%PDF-1.7 bytes"), "size": 14})

    result = fake.client().download_attachments("m1")

    assert result == [
        AttachmentContent(Attachment("invoice.pdf", "application/pdf", "att-1"), b"%PDF-1.7 bytes"),
        AttachmentContent(Attachment("scan.png", "image/png", None), b"\x89PNG-inline"),
    ]
    assert fake.attachments.get.call_args.kwargs == {
        "userId": "me",
        "messageId": "m1",
        "id": "att-1",
    }


def test_download_attachments_returns_empty_for_message_without_attachments() -> None:
    fake = FakeGmail()
    fake.message_store["m1"] = _message("m1", parts=[BODY_PART])

    assert fake.client().download_attachments("m1") == []
    fake.attachments.get.assert_not_called()


def test_download_attachments_raises_on_attachment_api_error() -> None:
    fake = FakeGmail()
    fake.message_store["m1"] = _message("m1", parts=[PDF_PART])
    fake.attachments.get.return_value = _request(error=_http_error(500))

    with pytest.raises(HttpError):
        fake.client().download_attachments("m1")


def test_download_attachments_raises_when_attachment_has_no_data() -> None:
    fake = FakeGmail()
    fake.message_store["m1"] = _message("m1", parts=[PDF_PART])
    fake.attachments.get.return_value = _request({"size": 0})

    with pytest.raises(GmailResponseError, match="invoice.pdf"):
        fake.client().download_attachments("m1")


def test_download_attachments_raises_when_inline_part_has_no_data() -> None:
    fake = FakeGmail()
    broken = {"partId": "1", "mimeType": "image/png", "filename": "x.png", "body": {}}
    fake.message_store["m1"] = _message("m1", parts=[broken])

    with pytest.raises(GmailResponseError, match=r"x\.png"):
        fake.client().download_attachments("m1")


# --- apply_label / mark_read -------------------------------------------------------------


def test_apply_label_resolves_name_to_id() -> None:
    fake = FakeGmail()

    fake.client().apply_label("m1", "Kibit/Processed")

    assert fake.modify_calls == [{"userId": "me", "id": "m1", "body": {"addLabelIds": ["Label_1"]}}]


def test_apply_label_caches_label_lookup() -> None:
    fake = FakeGmail()
    client = fake.client()

    client.apply_label("m1", "Kibit/Processed")
    client.apply_label("m2", "Kibit/Pending")

    assert fake.labels.list.call_count == 1


def test_apply_label_refreshes_cache_before_giving_up() -> None:
    fake = FakeGmail()
    client = fake.client()
    client.apply_label("m1", "Kibit/Processed")
    fake.labels.list.return_value = _request(
        {"labels": [*LABEL_LIST["labels"], {"id": "Label_9", "name": "Kibit/New"}]}
    )

    client.apply_label("m2", "Kibit/New")

    assert fake.modify_calls[-1]["body"] == {"addLabelIds": ["Label_9"]}


def test_apply_label_raises_for_unknown_label_and_does_not_modify() -> None:
    fake = FakeGmail()

    with pytest.raises(GmailLabelNotFoundError, match="Kibit/Missing"):
        fake.client().apply_label("m1", "Kibit/Missing")

    fake.messages.modify.assert_not_called()


def test_apply_label_raises_on_modify_api_error() -> None:
    fake = FakeGmail()
    fake.messages.modify.side_effect = None
    fake.messages.modify.return_value = _request(error=_http_error(500))

    with pytest.raises(HttpError):
        fake.client().apply_label("m1", "Kibit/Processed")


def test_apply_label_raises_on_label_list_api_error() -> None:
    fake = FakeGmail()
    fake.labels.list.return_value = _request(error=_http_error(503))

    with pytest.raises(HttpError):
        fake.client().apply_label("m1", "Kibit/Processed")


def test_mark_read_removes_unread_label() -> None:
    fake = FakeGmail()

    fake.client().mark_read("m1")

    assert fake.modify_calls == [
        {"userId": "me", "id": "m1", "body": {"removeLabelIds": ["UNREAD"]}}
    ]


def test_mark_read_raises_on_api_error() -> None:
    fake = FakeGmail()
    fake.messages.modify.side_effect = None
    fake.messages.modify.return_value = _request(error=_http_error(500))

    with pytest.raises(HttpError):
        fake.client().mark_read("m1")


# --- create_draft_reply ------------------------------------------------------------------


def _thread_for_reply(*messages: dict[str, Any]) -> MagicMock:
    return _request({"id": "t1", "messages": list(messages)})


def _decode_draft(fake: FakeGmail) -> tuple[dict[str, Any], email.message.EmailMessage]:
    kwargs = fake.drafts.create.call_args.kwargs
    raw = kwargs["body"]["message"]["raw"]
    parsed = email.message_from_bytes(
        base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)), policy=policy.default
    )
    assert isinstance(parsed, email.message.EmailMessage)
    return kwargs, parsed


def test_create_draft_reply_threads_to_latest_inbound_message() -> None:
    fake = FakeGmail()
    fake.threads.get.return_value = _thread_for_reply(
        _message("m1", message_id_header="<a@c.example>", sender="Dev <dev@c.example>"),
        _message(
            "m2",
            sender="Dev <dev@c.example>",
            subject="Invoice May",
            message_id_header="<b@c.example>",
            references="<a@c.example>",
        ),
        _message("m3", sender="ap@kibit.example", label_ids=("SENT",), message_id_header="<s>"),
    )
    fake.drafts.create.return_value = _request({"id": "draft-1", "message": {"id": "x"}})

    draft_id = fake.client().create_draft_reply("t1", "Line 2: quantity 12 vs TIG 10.")

    assert draft_id == "draft-1"
    kwargs, msg = _decode_draft(fake)
    assert kwargs["userId"] == "me"
    assert kwargs["body"]["message"]["threadId"] == "t1"
    assert msg["To"] == "Dev <dev@c.example>"
    assert msg["Subject"] == "Re: Invoice May"
    assert msg["In-Reply-To"] == "<b@c.example>"
    assert msg["References"] == "<a@c.example> <b@c.example>"
    assert msg.get_content().strip() == "Line 2: quantity 12 vs TIG 10."
    fake.drafts.send.assert_not_called()
    fake.messages.send.assert_not_called()


def test_create_draft_reply_does_not_double_prefix_re() -> None:
    fake = FakeGmail()
    fake.threads.get.return_value = _thread_for_reply(_message("m1", subject="RE: Számla"))
    fake.drafts.create.return_value = _request({"id": "d"})

    fake.client().create_draft_reply("t1", "body")

    _, msg = _decode_draft(fake)
    assert msg["Subject"] == "RE: Számla"
    assert msg["References"] == "<orig-1@supplier.example>"


def test_create_draft_reply_keeps_non_ascii_body_and_subject() -> None:
    fake = FakeGmail()
    fake.threads.get.return_value = _thread_for_reply(_message("m1", subject="Számla"))
    fake.drafts.create.return_value = _request({"id": "d"})

    fake.client().create_draft_reply("t1", "Eltérés: egységár 1 200 Ft ≠ 1 000 Ft")

    _, msg = _decode_draft(fake)
    assert msg["Subject"] == "Re: Számla"
    assert msg.get_content().strip() == "Eltérés: egységár 1 200 Ft ≠ 1 000 Ft"


def test_create_draft_reply_raises_when_thread_has_no_inbound_message() -> None:
    fake = FakeGmail()
    fake.threads.get.return_value = _thread_for_reply(_message("m1", label_ids=("SENT",)))

    with pytest.raises(GmailReplyError):
        fake.client().create_draft_reply("t1", "body")

    fake.drafts.create.assert_not_called()


def test_create_draft_reply_raises_without_message_id_header() -> None:
    fake = FakeGmail()
    fake.threads.get.return_value = _thread_for_reply(_message("m1", message_id_header=None))

    with pytest.raises(GmailReplyError, match="Message-ID"):
        fake.client().create_draft_reply("t1", "body")

    fake.drafts.create.assert_not_called()


def test_create_draft_reply_raises_without_sender() -> None:
    fake = FakeGmail()
    fake.threads.get.return_value = _thread_for_reply(_message("m1", sender=""))

    with pytest.raises(GmailReplyError, match="sender"):
        fake.client().create_draft_reply("t1", "body")


def test_create_draft_reply_raises_on_api_error() -> None:
    fake = FakeGmail()
    fake.threads.get.return_value = _thread_for_reply(_message("m1"))
    fake.drafts.create.return_value = _request(error=_http_error(500))

    with pytest.raises(HttpError):
        fake.client().create_draft_reply("t1", "body")


def test_create_draft_reply_raises_when_response_has_no_draft_id() -> None:
    fake = FakeGmail()
    fake.threads.get.return_value = _thread_for_reply(_message("m1"))
    fake.drafts.create.return_value = _request({})

    with pytest.raises(GmailResponseError):
        fake.client().create_draft_reply("t1", "body")


def test_module_exports() -> None:
    assert "GmailClient" in gmail_client.__all__
