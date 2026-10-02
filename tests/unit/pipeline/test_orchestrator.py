"""Task 21: the per-candidate state machine (USR-001-03, USR-001-04, ADR 3).

Every client is an in-memory fake (``tests/unit/pipeline/fakes.py``); no Google or model
API is ever called.
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

import httplib2
import pytest
from googleapiclient.errors import HttpError

from intake.extraction import DocumentKind, UnsupportedDocumentError
from intake.models import Candidate, StageResult
from intake.pipeline.orchestrator import (
    CandidateStatus,
    Orchestrator,
    PipelineClients,
    RunSummary,
    run_cycle,
)
from tests.unit.pipeline.fakes import (
    LABELS,
    FakeDrive,
    FakeExtractor,
    FakeGmail,
    FakeSheets,
    incomplete,
    invoice_extraction,
    make_config,
    ok,
    tig_extraction,
)

INVOICE_PDF = b"%PDF-1.7 invoice one"
INVOICE_PDF_2 = b"%PDF-1.7 invoice two"
TIG_PDF = b"%PDF-1.7 tig one"
PDF = "application/pdf"
CONTRACTOR = "Kővári Kft <billing@contractor.example>"
PM = "Project Manager <pm@kibit.example>"
NUMBER = "2610001KOVARIKF"


def _http_error(status: int = 503) -> HttpError:
    return HttpError(httplib2.Response({"status": status}), b'{"error": "boom"}')


class Harness:
    def __init__(self, **config_overrides: str) -> None:
        self.config = make_config(**config_overrides)
        self.gmail = FakeGmail()
        self.drive = FakeDrive()
        self.sheets = FakeSheets(self.config)
        self.extractor = FakeExtractor()

    def run(self) -> RunSummary:
        return run_cycle(
            self.sheets,
            lambda config: PipelineClients(
                gmail=self.gmail, drive=self.drive, extractor=self.extractor
            ),
        )

    def direct_invoice(
        self,
        message_id: str = "m1",
        *,
        content: bytes = INVOICE_PDF,
        answer: StageResult | BaseException | None = None,  # type: ignore[type-arg]
        filename: str = "szamla.pdf",
        **kwargs: object,
    ) -> None:
        self.gmail.add_message(message_id, files=[(filename, PDF, content)], **kwargs)  # type: ignore[arg-type]
        self.extractor.answers[content] = answer if answer is not None else ok(invoice_extraction())

    def tig_thread(
        self,
        *,
        tig: StageResult | None = None,  # type: ignore[type-arg]
        with_tig: bool = True,
        invoice: StageResult | None = None,  # type: ignore[type-arg]
    ) -> None:
        if with_tig:
            self.add_tig()
        self.gmail.add_message(
            "inv",
            thread_id="t1",
            sender=CONTRACTOR,
            subject="Számla INV-1",
            files=[("INV-1.pdf", PDF, INVOICE_PDF)],
        )
        self.extractor.answers[INVOICE_PDF] = invoice or ok(invoice_extraction())
        self.extractor.answers[TIG_PDF] = tig or ok(tig_extraction())

    def add_tig(self, labels: tuple[str, ...] = ("INBOX",)) -> None:
        self.gmail.add_message(
            "tig",
            thread_id="t1",
            sender=PM,
            subject="TIG 2026/10",
            files=[("TIG-2026-10-KEK.pdf", PDF, TIG_PDF)],
            labels=labels,
        )
        self.extractor.answers.setdefault(TIG_PDF, ok(tig_extraction()))


def _counts(summary: RunSummary) -> dict[str, int]:
    return {k: v for k, v in summary.to_dict().items() if v}


def _untouched(h: Harness, message_id: str) -> None:
    assert h.gmail.writes_for(message_id) == []
    assert h.gmail.is_unread(message_id)
    assert h.gmail.kibit_labels_of(message_id) == set()


def _nothing_filed_or_booked(h: Harness) -> None:
    assert h.drive.uploads == 0
    assert h.sheets.rows == []


# --- happy paths ----------------------------------------------------------------------------


def test_direct_invoice_is_filed_booked_marked_read_and_labelled_processed() -> None:
    h = Harness()
    h.direct_invoice()
    h.gmail.fail["get_thread"] = AssertionError("direct route must not look for a TIG")

    summary = h.run()

    assert _counts(summary) == {"processed": 1}
    assert summary.results[0].registry_number == NUMBER
    # USR-001-03 AC1, AC4: read + Processed only; label written before the read change.
    assert h.gmail.kibit_labels_of("m1") == {LABELS.processed}
    assert not h.gmail.is_unread("m1")
    assert h.gmail.writes_for("m1") == [
        ("apply_label", "m1", LABELS.processed),
        ("mark_read", "m1", None),
    ]
    # USR-003-04: original bytes, renamed, in the YYMM folder, linked to its source.
    assert h.drive.filenames("2610") == [f"{NUMBER}.pdf"]
    (filed,) = h.drive.files.values()
    assert filed.content == INVOICE_PDF
    assert filed.app_properties["kibitSourceMessageId"] == "m1"
    # USR-004-01/02: one row, normalised and rounded.
    (row,) = h.sheets.rows
    assert (row.inv_id_int, row.provider, row.inv_id_ext, row.inv_type, row.currency) == (
        NUMBER,
        "Kővári Kft",
        "INV-1",
        "direct",
        "HUF",
    )
    assert (row.net, row.gross, row.due_date) == (
        Decimal(100000),
        Decimal(127000),
        date(2026, 11, 4),
    )
    # USR-005-01 AC6 / USR-005-04 AC6: no TIG extraction for a direct invoice.
    assert h.extractor.kinds() == [DocumentKind.INVOICE]


def test_two_invoices_for_the_same_month_in_one_run_get_consecutive_numbers() -> None:
    h = Harness()
    h.direct_invoice("m1")
    h.direct_invoice(
        "m2",
        content=INVOICE_PDF_2,
        answer=ok(invoice_extraction(invoice_number="INV-2", supplier="ACME Zrt")),
    )

    assert _counts(h.run()) == {"processed": 2}
    assert h.drive.filenames("2610") == [f"{NUMBER}.pdf", "2610002ACMEZRT.pdf"]


def test_tig_match_is_filed_booked_and_processed_without_a_draft() -> None:
    h = Harness()
    h.tig_thread()

    assert _counts(h.run()) == {"processed": 1}
    assert h.gmail.kibit_labels_of("inv") == {LABELS.processed}
    assert not h.gmail.is_unread("inv")
    assert h.gmail.drafts == []
    assert [r.inv_type for r in h.sheets.rows] == ["tig"]
    assert h.extractor.kinds() == [DocumentKind.INVOICE, DocumentKind.TIG]
    # The TIG message itself is never touched.
    assert h.gmail.writes_for("tig") == []


def test_tig_mismatch_creates_one_draft_files_books_and_labels_pending() -> None:
    h = Harness()
    h.tig_thread(tig=ok(tig_extraction(quantity="8", total="80 000 Ft")))

    assert _counts(h.run()) == {"pending": 1}
    assert h.gmail.kibit_labels_of("inv") == {LABELS.pending}
    assert not h.gmail.is_unread("inv")
    ((thread_id, body),) = h.gmail.drafts
    assert thread_id == "t1"
    assert "mennyiség" in body
    assert h.drive.filenames("2610") == [f"{NUMBER}.pdf"]
    assert len(h.sheets.rows) == 1


def test_tig_mismatch_already_drafted_creates_no_second_draft() -> None:
    h = Harness()
    h.tig_thread(tig=ok(tig_extraction(quantity="8", total="80 000 Ft")))
    h.gmail.add_message("old-draft", thread_id="t1", sender="ap@kibit.example", labels=("DRAFT",))

    assert _counts(h.run()) == {"pending": 1}
    assert h.gmail.drafts == []


def test_tig_mismatch_whose_draft_fails_is_still_filed_booked_and_pending(
    caplog: pytest.LogCaptureFixture,
) -> None:
    h = Harness()
    h.tig_thread(tig=ok(tig_extraction(quantity="8", total="80 000 Ft")))
    h.gmail.fail["create_draft_reply"] = _http_error(500)
    caplog.set_level(logging.WARNING)

    assert _counts(h.run()) == {"pending": 1}
    assert h.gmail.kibit_labels_of("inv") == {LABELS.pending}
    assert len(h.sheets.rows) == 1
    assert "draft" in caplog.text.lower()


# --- missing TIG / AwaitingTIG --------------------------------------------------------------


def test_missing_tig_awaits_then_a_later_tig_lets_the_invoice_proceed() -> None:
    h = Harness()
    h.tig_thread(with_tig=False)

    # USR-005-04 AC1: AwaitingTIG, unread, nothing filed or booked.
    assert _counts(h.run()) == {"awaiting_tig": 1}
    assert h.gmail.kibit_labels_of("inv") == {LABELS.awaiting_tig}
    assert h.gmail.is_unread("inv")
    _nothing_filed_or_booked(h)

    # AC3: a later run without a TIG changes nothing.
    writes_before = list(h.gmail.writes)
    assert _counts(h.run()) == {"awaiting_tig": 1}
    assert h.gmail.writes == writes_before

    # AC4: a TIG sent into the thread later is picked up; AwaitingTIG is replaced.
    h.add_tig()
    assert _counts(h.run()) == {"processed": 1}
    assert h.gmail.kibit_labels_of("inv") == {LABELS.processed}
    assert not h.gmail.is_unread("inv")
    assert ("remove_label", "inv", LABELS.awaiting_tig) in h.gmail.writes
    assert len(h.sheets.rows) == 1


@pytest.mark.parametrize(
    "tig",
    [incomplete(), ok(tig_extraction(total="garbage"))],
    ids=["extraction-incomplete", "total-unreadable"],
)
def test_unreadable_tig_needs_review_and_nothing_is_filed(
    tig: StageResult,  # type: ignore[type-arg]
) -> None:
    h = Harness()
    h.tig_thread(tig=tig)

    assert _counts(h.run()) == {"needs_review": 1}
    assert h.gmail.kibit_labels_of("inv") == {LABELS.needs_review}
    assert h.gmail.is_unread("inv")
    _nothing_filed_or_booked(h)


# --- NeedsReview branches (USR-001-04) ------------------------------------------------------


def test_ambiguous_route_needs_review_without_extraction() -> None:
    h = Harness()
    h.direct_invoice(sender="", subject="")

    assert _counts(h.run()) == {"needs_review": 1}
    assert h.gmail.kibit_labels_of("m1") == {LABELS.needs_review}
    assert h.gmail.is_unread("m1")
    assert h.extractor.calls == []
    _nothing_filed_or_booked(h)


def test_incomplete_extraction_needs_review() -> None:
    h = Harness()
    h.direct_invoice(answer=incomplete(invoice_extraction(due_date=None)))

    assert _counts(h.run()) == {"needs_review": 1}
    assert h.gmail.is_unread("m1")
    assert h.gmail.kibit_labels_of("m1") == {LABELS.needs_review}
    _nothing_filed_or_booked(h)


def test_unsupported_document_needs_review() -> None:
    h = Harness()
    h.direct_invoice(answer=UnsupportedDocumentError("unsupported document MIME type"))

    assert _counts(h.run()) == {"needs_review": 1}
    _nothing_filed_or_booked(h)


@pytest.mark.parametrize(
    "override",
    [
        {"currency": "XYZ"},
        {"net": "1.2.3,4,5"},
        {"gross": "abc"},
        {"due_date": "2026.13.45."},
        {"performance_date": None, "issue_date": None},
        {"supplier_country": None},
    ],
    ids=["currency", "net", "gross", "due-date", "no-performance-date", "unknown-origin"],
)
def test_normalisation_failure_needs_review(override: dict[str, object]) -> None:
    h = Harness()
    h.direct_invoice(answer=ok(invoice_extraction(**override)))

    assert _counts(h.run()) == {"needs_review": 1}
    assert h.gmail.is_unread("m1")
    _nothing_filed_or_booked(h)


def test_supplier_normalisation_failure_needs_review_before_a_number_is_taken() -> None:
    h = Harness()
    h.direct_invoice(answer=ok(invoice_extraction(supplier="---")))

    assert _counts(h.run()) == {"needs_review": 1}
    _nothing_filed_or_booked(h)
    assert h.drive.folders == {}


def test_exhausted_sequence_needs_review() -> None:
    h = Harness(sequence_width="1", sequence_start="1")
    h.drive.add_file("2610", "26109OTHER.pdf")
    h.direct_invoice()

    assert _counts(h.run()) == {"needs_review": 1}
    assert h.sheets.rows == []


def test_invoice_without_any_invoice_attachment_needs_review() -> None:
    h = Harness()
    h.gmail.add_message("m1", subject="Számla INV-1", files=[])

    assert _counts(h.run()) == {"needs_review": 1}
    assert h.gmail.kibit_labels_of("m1") == {LABELS.needs_review}


def test_several_invoice_attachments_need_review() -> None:
    h = Harness()
    h.gmail.add_message("m1", files=[("a.pdf", PDF, INVOICE_PDF), ("b.pdf", PDF, INVOICE_PDF_2)])

    assert _counts(h.run()) == {"needs_review": 1}
    assert h.extractor.calls == []


def test_byte_identical_attachments_count_as_one_invoice() -> None:
    h = Harness()
    h.gmail.add_message("m1", files=[("a.pdf", PDF, INVOICE_PDF), ("a copy.pdf", PDF, INVOICE_PDF)])
    h.extractor.answers[INVOICE_PDF] = ok(invoice_extraction())

    assert _counts(h.run()) == {"processed": 1}
    assert h.drive.uploads == 1


def test_non_allowlisted_and_tig_attachments_are_not_invoices() -> None:
    h = Harness()
    h.gmail.add_message(
        "m1",
        files=[
            ("notes.txt", "text/plain", b"hello"),
            ("TIG-2026-10.pdf", PDF, TIG_PDF),
            ("szamla.pdf", PDF, INVOICE_PDF),
        ],
    )
    h.extractor.answers[INVOICE_PDF] = ok(invoice_extraction())

    assert _counts(h.run()) == {"processed": 1}
    assert len(h.extractor.calls) == 1


def test_message_carrying_only_a_tig_is_left_untouched() -> None:
    h = Harness()
    h.add_tig(labels=("INBOX", "UNREAD"))

    summary = h.run()

    assert _counts(summary) == {"skipped": 1}
    _untouched(h, "tig")
    assert h.extractor.calls == []


def test_needs_review_is_never_re_evaluated() -> None:
    h = Harness()
    h.direct_invoice(sender="", subject="")
    h.run()
    writes = list(h.gmail.writes)

    assert _counts(h.run()) == {}
    assert h.gmail.writes == writes


def test_candidate_already_carrying_a_terminal_label_is_skipped() -> None:
    h = Harness()
    orchestrator = Orchestrator(
        config=h.config,
        gmail=h.gmail,
        drive=h.drive,
        sheets=h.sheets,
        extractor=h.extractor,
    )
    for label in (LABELS.processed, LABELS.pending, LABELS.needs_review):
        candidate = Candidate("m9", "t9", "a@b.example", "Számla", (), frozenset({label}))
        result = orchestrator.process(candidate)
        assert result.status is CandidateStatus.SKIPPED
    assert h.gmail.writes == []
    assert h.extractor.calls == []


def test_manual_human_changes_are_never_reverted() -> None:
    h = Harness()
    h.direct_invoice("review", sender="", subject="")
    h.tig_thread(with_tig=False)
    h.run()
    # A bookkeeper reads the flagged emails, clears/changes labels by hand.
    for message_id in ("review", "inv"):
        h.gmail.messages[message_id].labels -= {"UNREAD", LABELS.needs_review}
        h.gmail.messages[message_id].labels.add("Bookkeeping/Done")
    writes = list(h.gmail.writes)

    assert _counts(h.run()) == {}
    assert h.gmail.writes == writes
    assert "Bookkeeping/Done" in h.gmail.labels_of("review")


def test_custom_labels_on_a_processed_email_are_kept() -> None:
    h = Harness()
    h.direct_invoice(labels=("INBOX", "UNREAD", "STARRED", "Bookkeeping/Urgent"))

    h.run()

    assert {"STARRED", "Bookkeeping/Urgent", LABELS.processed} <= h.gmail.labels_of("m1")


# --- failures: no label, no read change, retried next run ------------------------------


def test_filing_failure_releases_the_number_and_leaves_the_email_untouched() -> None:
    h = Harness()
    h.direct_invoice("m1")
    h.direct_invoice(
        "m2",
        content=INVOICE_PDF_2,
        answer=ok(invoice_extraction(invoice_number="INV-2")),
    )
    h.drive.fail_once["save_file"] = _http_error(500)

    summary = h.run()

    assert _counts(summary) == {"errors": 1, "processed": 1}
    _untouched(h, "m1")
    assert h.sheets.rows[0].inv_id_ext == "INV-2"
    # m1's released number went to m2: no gap.
    assert h.drive.filenames("2610") == [f"{NUMBER}.pdf"]
    assert h.drive.files["file-1"].app_properties["kibitSourceMessageId"] == "m2"


def test_rerun_after_filed_but_not_booked_reuses_the_number_and_does_not_refile() -> None:
    h = Harness()
    h.direct_invoice()
    h.sheets.fail_once["append_row"] = _http_error(429)

    assert _counts(h.run()) == {"errors": 1}
    _untouched(h, "m1")
    assert h.drive.filenames("2610") == [f"{NUMBER}.pdf"]
    assert h.sheets.rows == []

    assert _counts(h.run()) == {"processed": 1}
    assert h.drive.uploads == 1
    assert h.drive.filenames("2610") == [f"{NUMBER}.pdf"]
    assert [r.inv_id_int for r in h.sheets.rows] == [NUMBER]
    assert h.gmail.kibit_labels_of("m1") == {LABELS.processed}


def test_filed_number_disagreeing_with_the_extraction_needs_review() -> None:
    h = Harness()
    h.direct_invoice()
    from intake.registry.filing import attachment_source_key

    h.drive.add_file(
        "2609",
        "2609001KOVARIKF.pdf",
        kibitSourceMessageId="m1",
        kibitSourceAttachment=attachment_source_key(INVOICE_PDF),
    )

    assert _counts(h.run()) == {"needs_review": 1}
    assert h.sheets.rows == []


def test_ambiguous_source_link_needs_review() -> None:
    h = Harness()
    h.direct_invoice()
    from intake.registry.filing import attachment_source_key

    key = attachment_source_key(INVOICE_PDF)
    for name in ("2610001KOVARIKF.pdf", "2610002KOVARIKF.pdf"):
        h.drive.add_file("2610", name, kibitSourceMessageId="m1", kibitSourceAttachment=key)

    assert _counts(h.run()) == {"needs_review": 1}
    assert h.sheets.rows == []


def test_invoice_already_booked_is_neither_refiled_nor_booked_again() -> None:
    h = Harness()
    h.direct_invoice("m1")
    h.run()
    # The same invoice arrives again in a new email (USR-004-03 AC4).
    h.direct_invoice("m2", content=INVOICE_PDF_2)

    assert _counts(h.run()) == {"processed": 1}
    assert h.sheets.appends == 1
    assert h.drive.uploads == 1
    assert h.gmail.kibit_labels_of("m2") == {LABELS.processed}


def test_same_invoice_twice_in_one_run_is_booked_once() -> None:
    h = Harness()
    h.direct_invoice("m1")
    h.direct_invoice("m2", content=INVOICE_PDF_2)

    assert _counts(h.run()) == {"processed": 2}
    assert h.sheets.appends == 1
    assert h.drive.uploads == 1


def test_duplicate_check_failure_leaves_the_email_untouched_and_unbooked() -> None:
    h = Harness()
    h.direct_invoice()
    h.sheets.fail["load_existing_keys"] = _http_error(503)

    assert _counts(h.run()) == {"errors": 1}
    _untouched(h, "m1")
    _nothing_filed_or_booked(h)


def test_sheets_append_failure_leaves_the_email_untouched() -> None:
    h = Harness()
    h.direct_invoice()
    h.sheets.fail["append_row"] = _http_error(429)

    assert _counts(h.run()) == {"errors": 1}
    _untouched(h, "m1")


@pytest.mark.parametrize(
    "inject",
    [
        lambda h: h.gmail.fail.__setitem__("download_attachments", _http_error()),
        lambda h: h.extractor.answers.__setitem__(INVOICE_PDF, RuntimeError("Gemini 503")),
        lambda h: h.extractor.answers.__setitem__(INVOICE_PDF, StageResult.failed("boom")),
        lambda h: h.drive.fail.__setitem__("find_filed_by_source", _http_error()),
        lambda h: h.drive.fail.__setitem__("list_month_folder", _http_error()),
        lambda h: h.gmail.fail.__setitem__("apply_label", _http_error()),
    ],
    ids=["download", "extract-raises", "extract-error", "source-lookup", "sequence", "label"],
)
def test_any_stage_failure_records_an_error_without_marking_read(inject: object) -> None:
    h = Harness()
    h.direct_invoice()
    inject(h)  # type: ignore[operator]

    assert _counts(h.run()) == {"errors": 1}
    assert h.gmail.is_unread("m1")
    assert h.gmail.kibit_labels_of("m1") == set()


def test_tig_lookup_failure_is_an_error() -> None:
    h = Harness()
    h.tig_thread()
    h.gmail.fail["get_thread"] = _http_error()

    assert _counts(h.run()) == {"errors": 1}
    _untouched(h, "inv")
    _nothing_filed_or_booked(h)


def test_an_exception_in_one_candidate_does_not_stop_the_next() -> None:
    h = Harness()
    h.direct_invoice("m1", answer=RuntimeError("unexpected"))
    h.direct_invoice("m2", content=INVOICE_PDF_2)

    summary = h.run()

    assert _counts(summary) == {"errors": 1, "processed": 1}
    assert [r.status for r in summary.results] == [
        CandidateStatus.ERROR,
        CandidateStatus.PROCESSED,
    ]
    assert summary.results[0].reason == "RuntimeError"
    _untouched(h, "m1")


# --- run level ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("client", "method"),
    [
        ("sheets", "load_config"),
        ("sheets", "verify_ledger_header"),
        ("gmail", "list_candidates"),
    ],
)
def test_run_level_failure_aborts_before_touching_any_email(client: str, method: str) -> None:
    h = Harness()
    h.direct_invoice()
    getattr(h, client).fail[method] = _http_error()

    with pytest.raises(HttpError):
        h.run()
    _untouched(h, "m1")


def test_run_summary_reports_every_outcome_count() -> None:
    assert RunSummary(()).to_dict() == {
        "processed": 0,
        "pending": 0,
        "needs_review": 0,
        "awaiting_tig": 0,
        "errors": 0,
        "skipped": 0,
    }


def test_stage_logs_carry_no_financial_content(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    h = Harness()
    h.tig_thread(tig=ok(tig_extraction(quantity="8", total="80 000 Ft")))

    h.run()

    stage_records = [r for r in caplog.records if hasattr(r, "stage")]
    assert {r.stage for r in stage_records} >= {  # type: ignore[attr-defined]
        "route",
        "attachments",
        "extract",
        "normalise",
        "reconcile",
        "file",
        "book",
        "finalise",
    }
    for record in stage_records:
        assert record.message_id == "inv"  # type: ignore[attr-defined]
        assert isinstance(record.duration_ms, int)  # type: ignore[attr-defined]
        assert record.outcome  # type: ignore[attr-defined]
    text = caplog.text + " ".join(str(r.__dict__) for r in stage_records)
    for secret in ("100 000", "100000", "80 000", "127 000", "INV-1", "Kővári", "Fejlesztés"):
        assert secret not in text


def test_receipt_without_due_or_performance_date_is_booked_with_the_issue_date() -> None:
    h = Harness()
    h.direct_invoice(
        answer=ok(
            invoice_extraction(
                issue_date="2026.09.23 08:03:15", due_date=None, performance_date=None
            )
        )
    )

    assert _counts(h.run()) == {"processed": 1}
    (row,) = h.sheets.rows
    assert row.due_date == date(2026, 9, 23)
