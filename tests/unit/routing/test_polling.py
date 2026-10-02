"""Task 9 / USR-001-01: Gmail inbox polling and invoice candidate matching."""

from __future__ import annotations

import unicodedata
from typing import Any
from unittest.mock import MagicMock

import httplib2
import pytest
from googleapiclient.errors import HttpError

from intake.clients.gmail_client import GmailClient
from intake.config import Config
from intake.models import Attachment, Candidate
from intake.routing import poll_candidates as poll_candidates_from_package
from intake.routing.polling import is_invoice_candidate, poll_candidates

PDF = Attachment(filename="invoice.pdf", mime_type="application/pdf", attachment_id="a1")
JPEG = Attachment(filename="scan.jpg", mime_type="image/jpeg", attachment_id="a2")
DOCX = Attachment(
    filename="notes.docx",
    mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    attachment_id="a3",
)
EXE = Attachment(filename="invoice.pdf.exe", mime_type="application/x-msdownload")


def _config(**overrides: Any) -> Config:
    raw: dict[str, Any] = {
        "invoice_keywords": ["számla", "invoice", "díjbekérő"],
        "attachment_mime_allowlist": ["application/pdf", "image/jpeg", "image/png"],
        "contractor_identifiers": ["dev@contractor.example"],
        "tig_subject_indicators": ["TIG"],
        "currency_map": {"Ft": "HUF"},
        "label_processed": "Kibit/Processed",
        "label_pending": "Kibit/Pending",
        "label_needs_review": "Kibit/NeedsReview",
        "label_awaiting_tig": "Kibit/AwaitingTIG",
        "sequence_start": 1,
        "sequence_width": 3,
        "rounding_tolerance": "0.01",
        "drive_root_folder_id": "folder-123",
    }
    raw.update(overrides)
    return Config.from_mapping(raw)


@pytest.fixture
def config() -> Config:
    return _config()


def _candidate(
    message_id: str = "m1",
    *,
    subject: str = "Hello",
    attachments: tuple[Attachment, ...] = (),
    labels: frozenset[str] = frozenset({"INBOX", "UNREAD"}),
) -> Candidate:
    return Candidate(
        message_id=message_id,
        thread_id=f"t-{message_id}",
        sender="Supplier Kft <billing@supplier.example>",
        subject=subject,
        attachments=attachments,
        label_names=labels,
    )


def _http_error(status: int = 503) -> HttpError:
    return HttpError(httplib2.Response({"status": status}), b'{"error": "boom"}')


def _gmail(*candidates: Candidate) -> MagicMock:
    """A spec'd GmailClient mock; any write call would be recorded and asserted against."""
    gmail = MagicMock(spec=GmailClient)
    gmail.list_candidates.return_value = list(candidates)
    return gmail


def _assert_no_writes(gmail: MagicMock) -> None:
    gmail.apply_label.assert_not_called()
    gmail.mark_read.assert_not_called()
    gmail.create_draft_reply.assert_not_called()


def test_poll_candidates_is_exported_from_routing_package() -> None:
    assert poll_candidates_from_package is poll_candidates


# --- AC1: invoice keyword in the subject -------------------------------------------


@pytest.mark.parametrize(
    "subject",
    [
        "Számla 2024/05",
        "számla",
        "SZÁMLA - Kővári Kft",
        "Re: Fwd: invoice #123",
        "Your INVOICE is ready",
        "Díjbekérő májusra",
        "Számlázás: 2024/05 számlája",  # inflected/compound Hungarian forms
        "E-számla értesítő",
    ],
)
def test_keyword_in_subject_is_a_candidate(config: Config, subject: str) -> None:
    candidate = _candidate(subject=subject)
    gmail = _gmail(candidate)

    assert poll_candidates(gmail, config) == [candidate]
    _assert_no_writes(gmail)


def test_keyword_matching_is_independent_of_unicode_normalisation_form(config: Config) -> None:
    decomposed_subject = unicodedata.normalize("NFD", "Számla 2024/05")
    assert decomposed_subject != "Számla 2024/05"

    assert is_invoice_candidate(_candidate(subject=decomposed_subject), config)


def test_decomposed_keyword_in_config_matches_composed_subject() -> None:
    config = _config(invoice_keywords=[unicodedata.normalize("NFD", "számla")])

    assert is_invoice_candidate(_candidate(subject="Számla 2024/05"), config)


@pytest.mark.parametrize("subject", ["Szamla 2024/05", "SZAMLA", "dijbekero"])
def test_keyword_matching_ignores_accents(config: Config, subject: str) -> None:
    # Hungarian senders often drop accents; a missed invoice is never processed (story §2).
    assert is_invoice_candidate(_candidate(subject=subject), config)


def test_keyword_matching_tolerates_irregular_whitespace() -> None:
    config = _config(invoice_keywords=["pro forma"])

    assert is_invoice_candidate(_candidate(subject="PRO   FORMA 12"), config)


def test_keywords_come_from_config_not_hardcoded() -> None:
    config = _config(invoice_keywords=["rechnung"])

    assert is_invoice_candidate(_candidate(subject="Rechnung 42"), config)
    assert not is_invoice_candidate(_candidate(subject="Számla 2024/05"), config)


def test_blank_configured_keyword_never_matches_everything() -> None:
    config = _config(invoice_keywords=["invoice", "  "])

    assert not is_invoice_candidate(_candidate(subject="Lunch on Friday?"), config)


# --- AC2: qualifying attachment without a keyword ----------------------------------


@pytest.mark.parametrize("attachment", [PDF, JPEG])
def test_allowlisted_attachment_without_keyword_is_a_candidate(
    config: Config, attachment: Attachment
) -> None:
    candidate = _candidate(subject="Documents for May", attachments=(attachment,))
    gmail = _gmail(candidate)

    assert poll_candidates(gmail, config) == [candidate]
    _assert_no_writes(gmail)


def test_one_qualifying_attachment_among_others_is_enough(config: Config) -> None:
    candidate = _candidate(subject="Files", attachments=(DOCX, PDF))

    assert is_invoice_candidate(candidate, config)


@pytest.mark.parametrize(
    "mime_type", ["APPLICATION/PDF", "Application/Pdf", " application/pdf ", "image/PNG"]
)
def test_mime_matching_is_case_and_whitespace_insensitive(config: Config, mime_type: str) -> None:
    candidate = _candidate(attachments=(Attachment("x.pdf", mime_type),))

    assert is_invoice_candidate(candidate, config)


def test_mime_parameters_are_ignored(config: Config) -> None:
    candidate = _candidate(attachments=(Attachment("x.pdf", 'application/pdf; name="x.pdf"'),))

    assert is_invoice_candidate(candidate, config)


def test_mime_allowlist_comes_from_config_not_hardcoded() -> None:
    config = _config(attachment_mime_allowlist=["image/tiff"])

    assert is_invoice_candidate(
        _candidate(attachments=(Attachment("s.tif", "image/tiff"),)), config
    )
    assert not is_invoice_candidate(_candidate(attachments=(PDF,)), config)


def test_wildcard_subtype_in_allowlist_matches_any_subtype_of_that_type() -> None:
    config = _config(attachment_mime_allowlist=["application/pdf", "image/*"])

    assert is_invoice_candidate(
        _candidate(attachments=(Attachment("a.heic", "image/heic"),)), config
    )
    assert not is_invoice_candidate(_candidate(attachments=(DOCX,)), config)


# --- AC3: neither keyword nor qualifying attachment --------------------------------


@pytest.mark.parametrize(
    "candidate",
    [
        _candidate(subject="Lunch on Friday?"),
        _candidate(subject="Meeting notes", attachments=(DOCX,)),
        _candidate(subject="Your parcel", attachments=(EXE,)),  # disguised executable
        _candidate(subject="", attachments=()),
        _candidate(subject="Scan", attachments=(Attachment("scan.pdf", ""),)),  # no MIME type
    ],
)
def test_non_matching_email_is_dropped_and_left_untouched(
    config: Config, candidate: Candidate
) -> None:
    gmail = _gmail(candidate)

    assert poll_candidates(gmail, config) == []
    _assert_no_writes(gmail)


def test_attachment_filename_alone_does_not_qualify(config: Config) -> None:
    # Only the MIME type is checked against the allowlist (security guardrail).
    candidate = _candidate(attachments=(Attachment("invoice.pdf", "application/octet-stream"),))

    assert not is_invoice_candidate(candidate, config)


def test_mixed_inbox_keeps_only_matches_in_gmail_order(config: Config) -> None:
    keyword = _candidate("m1", subject="Számla 2024/05")
    noise = _candidate("m2", subject="Newsletter")
    attachment_only = _candidate("m3", subject="Docs", attachments=(PDF,))
    gmail = _gmail(keyword, noise, attachment_only)

    assert poll_candidates(gmail, config) == [keyword, attachment_only]
    _assert_no_writes(gmail)


# --- AC4: already-read / already-labelled emails -----------------------------------


@pytest.mark.parametrize("label", ["Kibit/Processed", "Kibit/Pending", "Kibit/NeedsReview"])
def test_terminal_kibit_label_is_never_re_emitted(config: Config, label: str) -> None:
    # The Gmail query already excludes these; this is the defensive second line.
    candidate = _candidate(subject="Számla", attachments=(PDF,), labels=frozenset({"INBOX", label}))

    assert poll_candidates(_gmail(candidate), config) == []


def test_terminal_label_names_come_from_config() -> None:
    config = _config(label_processed="Invoices/Done")
    done = _candidate(subject="Számla", labels=frozenset({"INBOX", "UNREAD", "Invoices/Done"}))
    old_default = _candidate(
        "m2", subject="Számla", labels=frozenset({"UNREAD", "Kibit/Processed"})
    )

    assert poll_candidates(_gmail(done, old_default), config) == [old_default]


def test_awaiting_tig_candidate_stays_a_candidate_for_re_evaluation(config: Config) -> None:
    candidate = _candidate(
        subject="Számla 2024/05", labels=frozenset({"INBOX", "UNREAD", "Kibit/AwaitingTIG"})
    )

    assert poll_candidates(_gmail(candidate), config) == [candidate]


def test_awaiting_tig_candidate_is_kept_even_if_it_no_longer_matches(config: Config) -> None:
    # Already admitted to the pipeline; a Config edit must not strand it unread forever.
    candidate = _candidate(subject="Re: May", labels=frozenset({"UNREAD", "Kibit/AwaitingTIG"}))

    assert poll_candidates(_gmail(candidate), config) == [candidate]


def test_polling_uses_the_unread_inbox_query_and_never_writes(config: Config) -> None:
    service = MagicMock(name="gmail-service")
    users = service.users.return_value
    users.labels.return_value.list.return_value.execute.return_value = {
        "labels": [{"id": "INBOX", "name": "INBOX"}, {"id": "UNREAD", "name": "UNREAD"}]
    }
    users.messages.return_value.list.return_value.execute.return_value = {
        "messages": [{"id": "m1"}]
    }
    users.messages.return_value.get.return_value.execute.return_value = {
        "id": "m1",
        "threadId": "t1",
        "labelIds": ["INBOX", "UNREAD"],
        "payload": {
            "headers": [
                {"name": "From", "value": "billing@supplier.example"},
                {"name": "Subject", "value": "Számla 2024/05"},
            ],
            "parts": [],
        },
    }
    gmail = GmailClient(service, config.labels)

    (candidate,) = poll_candidates(gmail, config)

    assert candidate.message_id == "m1"
    query = users.messages.return_value.list.call_args.kwargs["q"]
    assert "is:unread" in query
    assert "in:inbox" in query
    assert "-label:Kibit/Processed" in query
    assert "-label:Kibit/Pending" in query
    assert "-label:Kibit/NeedsReview" in query
    assert "Kibit/AwaitingTIG" not in query
    users.messages.return_value.modify.assert_not_called()
    users.messages.return_value.batchModify.assert_not_called()
    users.threads.return_value.modify.assert_not_called()
    users.drafts.return_value.create.assert_not_called()


# --- AC5: no duplicate candidates -------------------------------------------------


def test_same_message_listed_twice_is_emitted_once(config: Config) -> None:
    first = _candidate("m1", subject="Számla 2024/05")
    again = _candidate("m1", subject="Számla 2024/05")
    other = _candidate("m2", subject="Invoice 7")

    assert poll_candidates(_gmail(first, other, again), config) == [first, other]


def test_messages_in_the_same_thread_are_distinct_candidates(config: Config) -> None:
    a = Candidate("m1", "t1", "x@y.example", "Számla 1")
    b = Candidate("m2", "t1", "x@y.example", "Számla 2")

    assert poll_candidates(_gmail(a, b), config) == [a, b]


def test_repeated_runs_before_state_changes_return_the_same_set(config: Config) -> None:
    # No hidden state between runs: re-polling an unchanged inbox yields the same candidates
    # once each, never an accumulating list.
    candidate = _candidate(subject="Számla 2024/05")
    gmail = _gmail(candidate)

    first = poll_candidates(gmail, config)
    second = poll_candidates(gmail, config)

    assert first == second == [candidate]
    _assert_no_writes(gmail)


# --- AC6: Gmail API failure -------------------------------------------------------


def test_gmail_api_failure_propagates_without_any_write(config: Config) -> None:
    gmail = _gmail()
    gmail.list_candidates.side_effect = _http_error(503)

    with pytest.raises(HttpError):
        poll_candidates(gmail, config)

    _assert_no_writes(gmail)


def test_failed_run_leaves_the_same_emails_eligible_next_run(config: Config) -> None:
    candidate = _candidate(subject="Számla 2024/05")
    gmail = _gmail()
    gmail.list_candidates.side_effect = [_http_error(503), [candidate]]

    with pytest.raises(HttpError):
        poll_candidates(gmail, config)

    assert poll_candidates(gmail, config) == [candidate]
    _assert_no_writes(gmail)


def test_unexpected_error_is_not_swallowed(config: Config) -> None:
    gmail = _gmail()
    gmail.list_candidates.side_effect = TimeoutError("socket timeout")

    with pytest.raises(TimeoutError):
        poll_candidates(gmail, config)


def test_message_fetch_failure_inside_real_client_propagates(config: Config) -> None:
    service = MagicMock(name="gmail-service")
    users = service.users.return_value
    users.labels.return_value.list.return_value.execute.return_value = {"labels": []}
    users.messages.return_value.list.return_value.execute.return_value = {
        "messages": [{"id": "m1"}, {"id": "m2"}]
    }
    users.messages.return_value.get.return_value.execute.side_effect = _http_error(500)

    with pytest.raises(HttpError):
        poll_candidates(GmailClient(service, config.labels), config)

    users.messages.return_value.modify.assert_not_called()
