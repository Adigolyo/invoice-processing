"""Task 14 (USR-003-04): renamed invoice filing into the monthly ``YYMM`` Drive folder.

Scenarios are derived from USR-003-04 AC1-AC6, the execution plan's Task 14 row and the
QA strategy's "Drive Filing" feature, plus negative/edge paths: unsafe or missing
extensions, malformed registry numbers, a registry number whose ``YYMM`` prefix disagrees
with the performance date, Drive failures at each call, and a filename that is already
taken (a re-run of the same invoice vs. a collision, which filing cannot tell apart and
therefore reports explicitly).
"""

from __future__ import annotations

import hashlib
from datetime import date
from typing import Any

import httplib2
import pytest
from googleapiclient.errors import HttpError

from intake.clients.drive_client import DriveConflictError, FiledFileRef, SavedFile
from intake.clients.gmail_client import AttachmentContent
from intake.models import Attachment, FlagReason, StageStatus
from intake.registry import filing
from intake.registry.filing import (
    ExistingFiling,
    FiledInvoice,
    FilingConflictError,
    FilingError,
    FilingSourceConflictError,
    FilingStatus,
    attachment_source_key,
    file_attachment,
    file_invoice,
    filed_filename,
    find_existing_filing,
)
from intake.registry.folder_naming import yymm_folder_name
from intake.registry.registry_number import assemble
from intake.registry.sequence import SequenceAllocator

WIDTH = 3
NUMBER = "2405007ACMECORP"
PDF_BYTES = b"%PDF-1.7\n\x00\x01\x02 binary \xff\xfe invoice body\n%%EOF"


def _http_error(status: int = 503) -> HttpError:
    return HttpError(httplib2.Response({"status": status}), b'{"error": "boom"}')


class FakeDrive:
    """In-memory stand-in for the ``DriveClient`` calls filing (and sequence) use."""

    def __init__(self, folders: dict[str, dict[str, bytes]] | None = None) -> None:
        # yymm -> {filename: content}
        self.folders: dict[str, dict[str, bytes]] = folders or {}
        self.calls: list[tuple[Any, ...]] = []
        self.mime_types: dict[str, str | None] = {}
        # filename -> (source message id, source attachment key)
        self.sources: dict[str, tuple[str | None, str | None]] = {}
        self.fail: dict[str, Exception] = {}

    def _maybe_fail(self, name: str) -> None:
        if name in self.fail:
            raise self.fail[name]

    def find_month_folder(self, yymm: str) -> str | None:
        self.calls.append(("find_month_folder", yymm))
        self._maybe_fail("find_month_folder")
        return f"folder-{yymm}" if yymm in self.folders else None

    def create_month_folder(self, yymm: str) -> str:
        self.calls.append(("create_month_folder", yymm))
        self._maybe_fail("create_month_folder")
        self.folders.setdefault(yymm, {})
        return f"folder-{yymm}"

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
        self.calls.append(("save_file", folder_id, filename))
        self._maybe_fail("save_file")
        yymm = folder_id.removeprefix("folder-")
        folder = self.folders[yymm]
        if filename in folder:
            return SavedFile(file_id=f"id-{filename}", filename=filename, created=False)
        folder[filename] = content
        self.mime_types[filename] = mime_type
        self.sources[filename] = (source_message_id, source_attachment_key)
        return SavedFile(file_id=f"id-{filename}", filename=filename, created=True)

    def list_month_folder(self, yymm: str) -> list[str]:
        return list(self.folders.get(yymm, {}))

    def find_filed_by_source(
        self, message_id: str, attachment_key: str | None = None
    ) -> list[FiledFileRef]:
        self.calls.append(("find_filed_by_source", message_id, attachment_key))
        self._maybe_fail("find_filed_by_source")
        refs = []
        for yymm, folder in self.folders.items():
            for name in folder:
                msg, key = self.sources.get(name, (None, None))
                if msg == message_id and (attachment_key is None or key == attachment_key):
                    refs.append(
                        FiledFileRef(file_id=f"id-{name}", name=name, folder_id=f"folder-{yymm}")
                    )
        return refs


def _file(drive: FakeDrive, **overrides: Any) -> FiledInvoice:
    kwargs: dict[str, Any] = {
        "registry_number": NUMBER,
        "original_filename": "szamla_2024_05.pdf",
        "content": PDF_BYTES,
        "sequence_width": WIDTH,
    }
    kwargs.update(overrides)
    return file_invoice(drive, **kwargs)


# --- AC1: existing month folder ------------------------------------------------------


def test_qa_happy_path_saved_into_existing_folder_with_content_unchanged() -> None:
    drive = FakeDrive({"2405": {"2405001OTHER.pdf": b"x"}})

    result = _file(drive)

    assert result == FiledInvoice(
        file_id="id-2405007ACMECORP.pdf",
        filename="2405007ACMECORP.pdf",
        folder_id="folder-2405",
        yymm="2405",
        status=FilingStatus.CREATED,
        folder_created=False,
    )
    assert result.created is True
    assert drive.folders["2405"]["2405007ACMECORP.pdf"] == PDF_BYTES
    assert ("create_month_folder", "2405") not in drive.calls
    assert drive.folders["2405"]["2405001OTHER.pdf"] == b"x"  # neighbours untouched


# --- AC2: auto-created month folder --------------------------------------------------


def test_qa_edge_missing_month_folder_is_auto_created_and_found_by_sequence_scan() -> None:
    drive = FakeDrive({"2405": {}})

    result = _file(drive, registry_number="2406001KOVARIKF")

    assert result.folder_created is True
    assert result.yymm == "2406"
    assert ("create_month_folder", "2406") in drive.calls
    assert drive.folders["2406"] == {"2406001KOVARIKF.pdf": PDF_BYTES}
    # The folder name is exactly the convention sequence derivation scans (USR-003-02).
    assert result.yymm == yymm_folder_name(date(2024, 6, 15))
    allocator = SequenceAllocator(drive, start=1, width=WIDTH)
    assert allocator.allocate("2406") == 2


def test_folder_name_is_the_registry_number_prefix() -> None:
    drive = FakeDrive()
    result = _file(drive, registry_number="2501042TELEKOM")
    assert result.yymm == "2501"
    assert result.folder_id == "folder-2501"
    assert list(drive.folders) == ["2501"]


def test_performance_date_matching_the_prefix_is_accepted() -> None:
    drive = FakeDrive()
    result = _file(drive, performance_date=date(2024, 5, 31))
    assert result.yymm == "2405"


def test_performance_date_disagreeing_with_prefix_is_refused_before_any_drive_call() -> None:
    drive = FakeDrive()
    with pytest.raises(FilingError) as excinfo:
        _file(drive, performance_date=date(2024, 6, 1))
    assert excinfo.value.component == "registry_number"
    assert drive.calls == []


# --- AC3: renamed, extension and content preserved -----------------------------------


@pytest.mark.parametrize(
    ("original", "expected"),
    [
        ("szamla.pdf", "2405007ACMECORP.pdf"),
        ("scan.jpeg", "2405007ACMECORP.jpeg"),
        ("photo.PNG", "2405007ACMECORP.PNG"),  # original extension kept verbatim
        ("Invoice No. 12.2024.pdf", "2405007ACMECORP.pdf"),  # last segment only
        ("  szamla.pdf  ", "2405007ACMECORP.pdf"),  # surrounding whitespace ignored
        ("archive.tar.gz", "2405007ACMECORP.gz"),
        ("Számla – május.pdf", "2405007ACMECORP.pdf"),  # accented stem is irrelevant
    ],
)
def test_filename_is_registry_number_plus_original_extension(original: str, expected: str) -> None:
    assert filed_filename(NUMBER, original, sequence_width=WIDTH) == expected


def test_saved_content_is_byte_identical_and_name_is_renamed() -> None:
    drive = FakeDrive({"2405": {}})
    content = bytes(range(256)) * 10

    result = _file(drive, original_filename="kép.JPG", content=content)

    assert result.filename == "2405007ACMECORP.JPG"
    assert drive.folders["2405"] == {"2405007ACMECORP.JPG": content}


def test_mime_type_is_passed_through_to_drive() -> None:
    drive = FakeDrive({"2405": {}})
    _file(drive, mime_type="application/pdf")
    assert drive.mime_types["2405007ACMECORP.pdf"] == "application/pdf"


def test_mime_type_defaults_to_none_so_drive_guesses_from_extension() -> None:
    drive = FakeDrive({"2405": {}})
    _file(drive)
    assert drive.mime_types["2405007ACMECORP.pdf"] is None


def test_file_attachment_uses_gmail_attachment_name_mime_and_bytes() -> None:
    drive = FakeDrive({"2405": {}})
    attachment = AttachmentContent(
        attachment=Attachment(filename="Invoice-77.pdf", mime_type="application/pdf"),
        content=PDF_BYTES,
    )

    result = file_attachment(drive, NUMBER, attachment, sequence_width=WIDTH)

    assert result.filename == "2405007ACMECORP.pdf"
    assert drive.folders["2405"]["2405007ACMECORP.pdf"] == PDF_BYTES
    assert drive.mime_types["2405007ACMECORP.pdf"] == "application/pdf"


def test_file_attachment_with_blank_mime_type_lets_drive_guess() -> None:
    drive = FakeDrive({"2405": {}})
    attachment = AttachmentContent(
        attachment=Attachment(filename="a.pdf", mime_type=""), content=PDF_BYTES
    )
    file_attachment(drive, NUMBER, attachment, sequence_width=WIDTH)
    assert drive.mime_types["2405007ACMECORP.pdf"] is None


def test_file_attachment_propagates_performance_date_check() -> None:
    drive = FakeDrive()
    attachment = AttachmentContent(
        attachment=Attachment(filename="a.pdf", mime_type="application/pdf"), content=b"x"
    )
    with pytest.raises(FilingError):
        file_attachment(
            drive, NUMBER, attachment, sequence_width=WIDTH, performance_date=date(2023, 5, 1)
        )
    assert drive.calls == []


@pytest.mark.parametrize(
    "original",
    [
        "szamla",  # no extension
        "szamla.",  # empty extension
        "",
        "   ",
        "a.p df",  # whitespace inside extension
        "a.pdf/..",  # path tricks in the extension
        "a.pd\\f",
        "a.pdf\x00",  # control character
        "a.pdf\n",
        "a.ébc",  # non-ASCII extension
        "a." + "x" * 11,  # implausibly long extension
    ],
)
def test_missing_or_unsafe_extension_is_refused_before_any_drive_call(original: str) -> None:
    drive = FakeDrive({"2405": {}})
    with pytest.raises(FilingError) as excinfo:
        _file(drive, original_filename=original)
    assert excinfo.value.component == "extension"
    assert drive.calls == []


def test_non_bytes_content_is_refused() -> None:
    drive = FakeDrive({"2405": {}})
    with pytest.raises(TypeError):
        _file(drive, content="not bytes")
    assert drive.calls == []


def test_bytearray_content_is_accepted_unchanged() -> None:
    drive = FakeDrive({"2405": {}})
    _file(drive, content=bytearray(PDF_BYTES))
    assert drive.folders["2405"]["2405007ACMECORP.pdf"] == PDF_BYTES


def test_empty_content_is_refused() -> None:
    drive = FakeDrive({"2405": {}})
    with pytest.raises(FilingError) as excinfo:
        _file(drive, content=b"")
    assert excinfo.value.component == "content"
    assert drive.calls == []


# --- AC4: no registry number, no filing -----------------------------------------------


@pytest.mark.parametrize("missing", [None, "", "   "])
def test_qa_no_registry_number_blocks_filing_without_any_drive_call(missing: Any) -> None:
    drive = FakeDrive({"2405": {}})

    with pytest.raises(FilingError) as excinfo:
        _file(drive, registry_number=missing)

    assert excinfo.value.component == "registry_number"
    assert drive.calls == []
    assert drive.folders == {"2405": {}}


def test_filing_error_maps_to_incomplete_stage_result_for_manual_review() -> None:
    drive = FakeDrive()
    with pytest.raises(FilingError) as excinfo:
        _file(drive, registry_number=None)

    result = excinfo.value.to_stage_result()
    assert result.status is StageStatus.INCOMPLETE
    assert result.flag_reason is FlagReason.INCOMPLETE_DATA
    assert excinfo.value.reason is FlagReason.INCOMPLETE_DATA
    assert isinstance(excinfo.value, ValueError)


@pytest.mark.parametrize(
    "number",
    [
        "PLACEHOLDER",
        "2413007ACMECORP",  # month 13
        "2400007ACMECORP",  # month 00
        "2405ACMECORP",  # no sequence
        "240507ACMECORP",  # sequence shorter than the width
        "2405007",  # no supplier
        "2405007acmecorp",  # lower-case supplier
        "2405007ACMECORPX",  # supplier longer than 8
        "2405007ACME CORP",
        "2405007ACME/../X",
        "2405007ACMECORP.pdf",  # already has an extension
        "２４０５007ACMECORP",  # non-ASCII digits
        12345,
    ],
)
def test_malformed_registry_number_is_refused_before_any_drive_call(number: Any) -> None:
    drive = FakeDrive({"2405": {}})
    with pytest.raises(FilingError) as excinfo:
        _file(drive, registry_number=number)
    assert excinfo.value.component == "registry_number"
    assert drive.calls == []


def test_registry_number_must_match_the_configured_sequence_width() -> None:
    drive = FakeDrive()
    result = _file(drive, registry_number="24050007ACMECORP", sequence_width=4)
    assert result.filename == "24050007ACMECORP.pdf"
    # The same number is not a width-3 registry number would be read as seq 000, supplier
    # 7ACMECORP (9 chars) -> refused rather than filed under an unscannable name.
    with pytest.raises(FilingError):
        _file(FakeDrive(), registry_number="24050007ACMECORP", sequence_width=3)


def test_registry_number_with_digit_leading_supplier_is_accepted() -> None:
    assert filed_filename("24050033M", "a.pdf", sequence_width=WIDTH) == "24050033M.pdf"


def test_surrounding_whitespace_in_registry_number_is_refused_not_trimmed() -> None:
    drive = FakeDrive()
    with pytest.raises(FilingError):
        _file(drive, registry_number=f" {NUMBER}")
    assert drive.calls == []


@pytest.mark.parametrize("width", [0, -1, True, "3", None])
def test_invalid_sequence_width_is_refused(width: Any) -> None:
    drive = FakeDrive()
    with pytest.raises(FilingError) as excinfo:
        _file(drive, sequence_width=width)
    assert excinfo.value.component == "sequence_width"
    assert drive.calls == []


def test_non_date_performance_date_is_refused() -> None:
    drive = FakeDrive()
    with pytest.raises(FilingError) as excinfo:
        _file(drive, performance_date="2024-05-01")
    assert excinfo.value.component == "performance_date"
    assert drive.calls == []


# --- AC5: Drive failure propagates (no processed/booked outcome) ----------------------


@pytest.mark.parametrize("failing_call", ["find_month_folder", "create_month_folder", "save_file"])
def test_qa_drive_failure_propagates_unchanged(failing_call: str) -> None:
    drive = FakeDrive()  # folder missing, so all three calls are reached
    error = _http_error(503)
    drive.fail[failing_call] = error

    with pytest.raises(HttpError) as excinfo:
        _file(drive)

    assert excinfo.value is error
    assert all(not files for files in drive.folders.values())


def test_timeout_during_save_propagates() -> None:
    drive = FakeDrive({"2405": {}})
    drive.fail["save_file"] = TimeoutError("5000ms")
    with pytest.raises(TimeoutError):
        _file(drive)
    assert drive.folders["2405"] == {}


def test_duplicate_month_folders_conflict_propagates() -> None:
    drive = FakeDrive()
    drive.fail["find_month_folder"] = DriveConflictError("2 folders named '2405'")
    with pytest.raises(DriveConflictError):
        _file(drive)
    assert not any(call[0] == "save_file" for call in drive.calls)


# --- AC6: idempotent re-run, explicit already-present status -------------------------


def test_qa_rerun_after_successful_filing_creates_no_duplicate() -> None:
    drive = FakeDrive({"2405": {}})

    first = _file(drive)
    second = _file(drive)

    assert first.status is FilingStatus.CREATED
    assert second.status is FilingStatus.ALREADY_PRESENT
    assert second.created is False
    assert second.file_id == first.file_id
    assert second.filename == first.filename
    assert list(drive.folders["2405"]) == ["2405007ACMECORP.pdf"]
    assert drive.calls.count(("create_month_folder", "2405")) == 0


def test_rerun_after_folder_auto_creation_does_not_create_it_again() -> None:
    drive = FakeDrive()

    first = _file(drive)
    second = _file(drive)

    assert first.folder_created is True
    assert second.folder_created is False
    assert drive.calls.count(("create_month_folder", "2405")) == 1
    assert list(drive.folders["2405"]) == ["2405007ACMECORP.pdf"]


def test_existing_name_never_overwrites_the_filed_content() -> None:
    drive = FakeDrive({"2405": {"2405007ACMECORP.pdf": b"original"}})

    result = _file(drive, content=b"something else")

    assert result.status is FilingStatus.ALREADY_PRESENT
    assert drive.folders["2405"]["2405007ACMECORP.pdf"] == b"original"


def test_require_new_raises_on_existing_name_so_a_collision_is_not_swallowed() -> None:
    drive = FakeDrive({"2405": {"2405007ACMECORP.pdf": b"another invoice"}})

    with pytest.raises(FilingConflictError) as excinfo:
        _file(drive, require_new=True)

    assert excinfo.value.filename == "2405007ACMECORP.pdf"
    assert excinfo.value.file_id == "id-2405007ACMECORP.pdf"
    assert excinfo.value.folder_id == "folder-2405"
    assert drive.folders["2405"]["2405007ACMECORP.pdf"] == b"another invoice"


def test_require_new_succeeds_when_the_name_is_free() -> None:
    drive = FakeDrive({"2405": {}})
    assert _file(drive, require_new=True).status is FilingStatus.CREATED


def test_same_number_with_other_extension_is_a_separate_name() -> None:
    # Documented limitation: Drive names differ, so filing does not see this as a re-run.
    drive = FakeDrive({"2405": {"2405007ACMECORP.jpg": b"x"}})
    assert _file(drive).status is FilingStatus.CREATED


# --- AC6 across runs: source link (Drive file <-> Gmail message + attachment) --------

MSG = "18f2c3a4b5d6e7f8"


def _attachment(name: str = "Invoice-77.pdf", content: bytes = PDF_BYTES) -> AttachmentContent:
    return AttachmentContent(
        attachment=Attachment(filename=name, mime_type="application/pdf", attachment_id="a1"),
        content=content,
    )


class LegacyDrive(FakeDrive):
    """A drive whose ``save_file`` predates the source kwargs (backward compatibility)."""

    def save_file(  # type: ignore[override]
        self, folder_id: str, filename: str, content: bytes, mime_type: str | None = None
    ) -> SavedFile:
        return super().save_file(folder_id, filename, content, mime_type)


def test_attachment_source_key_is_a_content_sha256() -> None:
    key = attachment_source_key(PDF_BYTES)

    assert key == "sha256:" + hashlib.sha256(PDF_BYTES).hexdigest()
    assert attachment_source_key(bytearray(PDF_BYTES)) == key
    assert attachment_source_key(b"other") != key
    # Drive appProperties: key + value must fit 124 bytes.
    assert len("kibitSourceAttachment") + len(key.encode()) <= 124


def test_attachment_source_key_rejects_non_bytes() -> None:
    with pytest.raises(TypeError):
        attachment_source_key("text")  # type: ignore[arg-type]


def test_file_invoice_passes_the_source_through_to_drive() -> None:
    drive = FakeDrive({"2405": {}})

    _file(drive, source_message_id=MSG, source_attachment_key="sha256:abc")

    assert drive.sources["2405007ACMECORP.pdf"] == (MSG, "sha256:abc")


def test_file_invoice_without_source_works_with_a_drive_lacking_the_new_kwargs() -> None:
    drive = LegacyDrive({"2405": {}})

    assert _file(drive).status is FilingStatus.CREATED


def test_file_attachment_links_message_id_and_content_key() -> None:
    drive = FakeDrive({"2405": {}})
    attachment = _attachment()

    file_attachment(drive, NUMBER, attachment, sequence_width=WIDTH, source_message_id=MSG)

    assert drive.sources["2405007ACMECORP.pdf"] == (MSG, attachment_source_key(PDF_BYTES))


def test_file_attachment_without_message_id_sets_no_source() -> None:
    drive = FakeDrive({"2405": {}})

    file_attachment(drive, NUMBER, _attachment(), sequence_width=WIDTH)

    assert drive.sources["2405007ACMECORP.pdf"] == (None, None)


def test_find_existing_filing_returns_none_when_nothing_is_linked() -> None:
    drive = FakeDrive({"2405": {"2405001OTHER.pdf": b"x"}})

    assert find_existing_filing(drive, MSG, "sha256:abc", sequence_width=WIDTH) is None
    assert ("find_filed_by_source", MSG, "sha256:abc") in drive.calls


def test_find_existing_filing_returns_the_registry_number_of_the_one_match() -> None:
    drive = FakeDrive({"2405": {}})
    attachment = _attachment()
    file_attachment(drive, NUMBER, attachment, sequence_width=WIDTH, source_message_id=MSG)

    existing = find_existing_filing(
        drive, MSG, attachment_source_key(attachment.content), sequence_width=WIDTH
    )

    assert existing == ExistingFiling(
        registry_number=NUMBER,
        file_id="id-2405007ACMECORP.pdf",
        filename="2405007ACMECORP.pdf",
        folder_id="folder-2405",
        yymm="2405",
    )


def test_find_existing_filing_raises_on_more_than_one_match_never_guesses() -> None:
    drive = FakeDrive({"2405": {"2405001ACME.pdf": b"a", "2405002ACME.pdf": b"a"}})
    drive.sources = {"2405001ACME.pdf": (MSG, "k"), "2405002ACME.pdf": (MSG, "k")}

    with pytest.raises(FilingSourceConflictError) as excinfo:
        find_existing_filing(drive, MSG, "k", sequence_width=WIDTH)

    assert excinfo.value.message_id == MSG
    assert excinfo.value.attachment_key == "k"
    assert sorted(excinfo.value.file_ids) == ["id-2405001ACME.pdf", "id-2405002ACME.pdf"]


@pytest.mark.parametrize(
    "name", ["Copy of 2405001ACME.pdf", "2405001acme.pdf", "2405001ACME (1).pdf", "2413001ACME.pdf"]
)
def test_find_existing_filing_raises_when_the_linked_name_is_not_a_registry_filename(
    name: str,
) -> None:
    drive = FakeDrive({"2405": {name: b"a"}})
    drive.sources = {name: (MSG, "k")}

    with pytest.raises(FilingSourceConflictError, match="registry"):
        find_existing_filing(drive, MSG, "k", sequence_width=WIDTH)


def test_find_existing_filing_propagates_drive_errors_never_reports_not_found() -> None:
    drive = FakeDrive({"2405": {}})
    drive.fail["find_filed_by_source"] = _http_error(503)

    with pytest.raises(HttpError):
        find_existing_filing(drive, MSG, "k", sequence_width=WIDTH)


def test_find_existing_filing_validates_sequence_width_before_any_drive_call() -> None:
    drive = FakeDrive()

    with pytest.raises(FilingError):
        find_existing_filing(drive, MSG, "k", sequence_width=0)

    assert drive.calls == []


def test_two_attachments_of_one_email_map_to_their_own_filings() -> None:
    drive = FakeDrive({"2405": {}})
    first, second = _attachment("a.pdf", b"%PDF first"), _attachment("b.pdf", b"%PDF second")
    file_attachment(drive, "2405001ACME", first, sequence_width=WIDTH, source_message_id=MSG)
    file_attachment(drive, "2405002ACME", second, sequence_width=WIDTH, source_message_id=MSG)

    found_first = find_existing_filing(
        drive, MSG, attachment_source_key(first.content), sequence_width=WIDTH
    )
    found_second = find_existing_filing(
        drive, MSG, attachment_source_key(second.content), sequence_width=WIDTH
    )

    assert found_first is not None and found_first.registry_number == "2405001ACME"
    assert found_second is not None and found_second.registry_number == "2405002ACME"


def _run_once(drive: FakeDrive, attachment: AttachmentContent, *, book_fails: bool) -> str:
    """The orchestrator contract (Task 21) for one invoice attachment, minus extraction."""
    key = attachment_source_key(attachment.content)
    existing = find_existing_filing(drive, MSG, key, sequence_width=WIDTH)
    if existing is not None:
        number = existing.registry_number
    else:
        allocator = SequenceAllocator(drive, start=1, width=WIDTH)
        number = assemble("2405", allocator.allocate("2405"), "ACMECORP", WIDTH)
        file_attachment(
            drive, number, attachment, sequence_width=WIDTH, source_message_id=MSG, require_new=True
        )
    if book_fails:
        raise RuntimeError("Sheets append failed")
    return number


def test_qa_rerun_after_filed_but_booking_failed_reuses_the_registry_number() -> None:
    drive = FakeDrive({"2405": {"2405006OTHER.pdf": b"x"}})
    attachment = _attachment()

    with pytest.raises(RuntimeError, match="Sheets"):
        _run_once(drive, attachment, book_fails=True)
    assert list(drive.folders["2405"]) == ["2405006OTHER.pdf", NUMBER + ".pdf"]

    # Without the lookup a fresh allocation would now hand out 008 and file a second copy.
    assert SequenceAllocator(drive, start=1, width=WIDTH).allocate("2405") == 8

    number = _run_once(drive, attachment, book_fails=False)

    assert number == NUMBER
    assert list(drive.folders["2405"]) == ["2405006OTHER.pdf", NUMBER + ".pdf"]
    assert sum(1 for call in drive.calls if call[0] == "save_file") == 1


def test_module_exports_source_link_api() -> None:
    for name in (
        "ExistingFiling",
        "FilingSourceConflictError",
        "SourceLookupDrive",
        "attachment_source_key",
        "find_existing_filing",
    ):
        assert name in filing.__all__
