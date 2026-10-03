"""Task 12 (USR-003-02): sequence number derivation from the monthly Drive folder.

Scenarios are derived from USR-003-02 AC1-AC6, the execution plan's Task 12 row and the
QA strategy's "Sequence Number Derivation" feature, plus negative/edge paths around the
filename pattern ([YYMM][seq][SUPPLIER] where SUPPLIER is 1-8 chars of [A-Z0-9]).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import httplib2
import pytest
from googleapiclient.errors import HttpError

from intake.clients.drive_client import DriveClient
from intake.config import Config
from intake.registry.sequence import (
    SequenceAllocator,
    SequenceExhaustedError,
    existing_sequences,
    next_sequence,
    registry_filename_pattern,
)

YYMM = "2405"


class FakeMonthFolders:
    """Stands in for ``DriveClient.list_month_folder``: month -> filenames, read live."""

    def __init__(self, folders: dict[str, list[str]] | None = None) -> None:
        self.folders: dict[str, list[str]] = folders or {}
        self.calls: list[str] = []
        self.fail_with: Exception | None = None

    def list_month_folder(self, yymm: str) -> list[str]:
        self.calls.append(yymm)
        if self.fail_with is not None:
            raise self.fail_with
        return list(self.folders.get(yymm, []))

    def file(self, yymm: str, filename: str) -> None:
        self.folders.setdefault(yymm, []).append(filename)


def _http_error(status: int = 503) -> HttpError:
    return HttpError(httplib2.Response({"status": status}), b'{"error": "boom"}')


def _config(start: int = 1, width: int = 3) -> Config:
    return Config.from_mapping(
        {
            "invoice_keywords": "szamla",
            "attachment_mime_allowlist": "application/pdf",
            "tig_subject_indicators": "TIG",
            "currency_map": "Ft=HUF",
            "label_processed": "Kibit/Processed",
            "label_pending": "Kibit/Pending",
            "label_needs_review": "Kibit/NeedsReview",
            "label_awaiting_tig": "Kibit/AwaitingTIG",
            "sequence_start": start,
            "sequence_width": width,
            "rounding_tolerance": "0.01",
            "drive_root_folder_id": "root",
        }
    )


# --- filename pattern ---------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "seq"),
    [
        ("2405001ACMECORP.pdf", 1),
        ("2405007KOVARIKF.pdf", 7),
        ("2405123A.pdf", 123),  # 1-char supplier ID
        ("2405042ACME.jpeg", 42),  # short supplier ID, other extension
        ("2405010ACME.PDF", 10),  # extension case is irrelevant
        ("2405999ZZZZZZZZ.png", 999),
    ],
)
def test_pattern_parses_seq_from_registry_filenames(filename: str, seq: int) -> None:
    assert existing_sequences([filename], yymm=YYMM, width=3) == {seq}


@pytest.mark.parametrize(
    ("filename", "seq"),
    [
        # Supplier IDs that start with (or are entirely) digits must not leak into [seq]:
        # the seq is exactly `width` digits right after the 4-digit YYMM.
        ("24050013M.pdf", 1),
        ("240500712345678.pdf", 7),
        ("2405002123.pdf", 2),
        ("24050099.pdf", 9),  # seq 009, supplier "9"
        ("24051007ELEVEN.pdf", 100),
    ],
)
def test_digit_leading_supplier_id_is_not_confused_with_seq(filename: str, seq: int) -> None:
    assert existing_sequences([filename], yymm=YYMM, width=3) == {seq}


def test_seq_width_is_taken_from_config_width() -> None:
    # Same name, different widths -> different (but always width-exact) split.
    assert existing_sequences(["240500123ACME.pdf"], yymm=YYMM, width=3) == {1}
    assert existing_sequences(["240500123ACME.pdf"], yymm=YYMM, width=5) == {123}
    assert existing_sequences(["24051ACME.pdf"], yymm=YYMM, width=1) == {1}


@pytest.mark.parametrize(
    "filename",
    [
        "receipt-scan.pdf",  # unrelated document
        "2405-notes.txt",
        "Copy of 2405001ACME.pdf",
        "2405001acme.pdf",  # lower-case supplier is not a registry number
        "2405001.pdf",  # no supplier ID
        "240501ACME.pdf",  # seq too short for width 3 (the 'A' cannot be a digit)
        "2405001ACMECORPX.pdf",  # supplier ID longer than 8 characters
        "2405001ACME CORP.pdf",
        "2405001ÁCME.pdf",  # non-ASCII supplier character
        "2405001ACME.pdf.bak",
        "2405001ACME.",  # empty extension
        "x2405001ACME.pdf",
        "2405001ACME (1).pdf",
        "２４０５００１ACME.pdf",  # full-width digits must not count as digits
        "2405١٢٣ACME.pdf",  # Arabic-Indic digits must not count as digits
        "",
    ],
)
def test_non_matching_filenames_are_ignored(filename: str) -> None:
    assert existing_sequences([filename], yymm=YYMM, width=3) == set()


def test_files_from_another_month_are_ignored() -> None:
    # A registry number from another month cannot collide with this month's numbers.
    assert existing_sequences(["2404050ACME.pdf", "2505050ACME.pdf"], yymm=YYMM, width=3) == set()


def test_extensionless_registry_name_still_counts() -> None:
    # Errs on the side of never reusing a number that is visibly taken.
    assert existing_sequences(["2405004ACME"], yymm=YYMM, width=3) == {4}


def test_pattern_is_anchored_and_exposes_groups() -> None:
    pattern = registry_filename_pattern(YYMM, 3)
    match = pattern.fullmatch("24050013M.pdf")
    assert match is not None
    assert match.group("seq") == "001"
    assert match.group("supplier") == "3M"
    assert pattern.fullmatch("2405001ACME.pdf\n") is None


@pytest.mark.parametrize(("yymm", "width"), [("2413", 3), ("24", 3), ("2405", 0), ("2405", -1)])
def test_pattern_rejects_invalid_arguments(yymm: str, width: int) -> None:
    with pytest.raises(ValueError):
        registry_filename_pattern(yymm, width)


# --- next_sequence: AC1, AC2, AC4, AC5 ----------------------------------------


def test_ac1_empty_folder_starts_at_configured_start() -> None:
    assert next_sequence([], yymm=YYMM, width=3, start=1) == 1


def test_ac1_configured_start_value_is_honoured() -> None:
    assert next_sequence([], yymm=YYMM, width=3, start=0) == 0
    assert next_sequence([], yymm=YYMM, width=3, start=50) == 50


def test_ac1_folder_with_only_non_matching_files_starts_at_start() -> None:
    assert next_sequence(["notes.txt", "scan.pdf"], yymm=YYMM, width=3, start=1) == 1


def test_ac2_continues_from_highest_existing_sequence() -> None:
    names = ["2405001ACME.pdf", "2405003KOVARIKF.pdf", "2405002TELEKOM.pdf"]
    assert next_sequence(names, yymm=YYMM, width=3, start=1) == 4


def test_ac2_external_gaps_are_not_back_filled() -> None:
    # A gap made outside the pipeline is not repaired (USR-003-02 assumption 2).
    names = ["2405001ACME.pdf", "2405009ACME.pdf"]
    assert next_sequence(names, yymm=YYMM, width=3, start=1) == 10


def test_ac2_ordering_and_duplicates_in_listing_do_not_matter() -> None:
    names = ["2405005B.pdf", "2405002A.pdf", "2405005B.pdf"]
    assert next_sequence(names, yymm=YYMM, width=3, start=1) == 6


def test_ac4_result_is_strictly_greater_than_every_existing_sequence() -> None:
    existing = (1, 2, 3, 7, 4)
    names = [f"2405{n:03d}SUP{n}.pdf" for n in existing]
    result = next_sequence(names, yymm=YYMM, width=3, start=1)
    assert all(result > n for n in existing)
    assert result == 8


def test_ac5_unrelated_files_do_not_influence_result() -> None:
    names = ["2405002ACME.pdf", "2405999 notes.pdf", "999.pdf", "2405500acme.pdf"]
    assert next_sequence(names, yymm=YYMM, width=3, start=1) == 3


def test_digit_leading_supplier_does_not_inflate_the_next_number() -> None:
    # Greedy/variable-width parsing would read 0071234 here and jump far ahead.
    assert next_sequence(["240500712345678.pdf"], yymm=YYMM, width=3, start=1) == 8


def test_existing_seq_below_start_still_continues_from_highest() -> None:
    # AC2 is literal: the next number follows the highest found.
    assert next_sequence(["2405000ACME.pdf"], yymm=YYMM, width=3, start=1) == 1


def test_allocated_numbers_are_treated_as_taken() -> None:
    assert next_sequence([], yymm=YYMM, width=3, start=1, allocated=[1, 2]) == 3
    assert next_sequence(["2405005A.pdf"], yymm=YYMM, width=3, start=1, allocated=[2]) == 6
    assert next_sequence(["2405002A.pdf"], yymm=YYMM, width=3, start=1, allocated=[7]) == 8


def test_exhausted_width_raises_instead_of_overflowing() -> None:
    with pytest.raises(SequenceExhaustedError):
        next_sequence(["2405999ACME.pdf"], yymm=YYMM, width=3, start=1)
    with pytest.raises(SequenceExhaustedError):
        next_sequence(["24059A.pdf"], yymm=YYMM, width=1, start=1)


@pytest.mark.parametrize("start", [-1, 1000])
def test_start_must_fit_the_width(start: int) -> None:
    with pytest.raises(ValueError):
        next_sequence([], yymm=YYMM, width=3, start=start)


def test_next_sequence_accepts_any_iterable() -> None:
    assert next_sequence(iter(["2405001A.pdf"]), yymm=YYMM, width=3, start=1) == 2


# --- SequenceAllocator: AC1-AC6 against a (fake) live Drive folder ------------


def test_allocator_ac1_month_folder_missing_starts_at_start() -> None:
    drive = FakeMonthFolders()
    allocator = SequenceAllocator(drive, start=1, width=3)
    assert allocator.allocate(YYMM) == 1
    assert drive.calls == [YYMM]


def test_allocator_ac2_continues_after_existing_files() -> None:
    drive = FakeMonthFolders({YYMM: ["2405001ACME.pdf", "2405002B.pdf", "readme.txt"]})
    assert SequenceAllocator(drive, start=1, width=3).allocate(YYMM) == 3


def test_allocator_ac3_two_invoices_same_month_same_run_never_collide() -> None:
    # QA: "Two invoices for the same month in the same run receive non-colliding
    # sequential numbers" -- even when the first one has not been filed yet.
    drive = FakeMonthFolders({YYMM: ["2405004ACME.pdf"]})
    allocator = SequenceAllocator(drive, start=1, width=3)
    first = allocator.allocate(YYMM)
    second = allocator.allocate(YYMM)
    third = allocator.allocate(YYMM)
    assert (first, second, third) == (5, 6, 7)


def test_allocator_ac3_when_earlier_invoice_was_filed_in_between() -> None:
    drive = FakeMonthFolders()
    allocator = SequenceAllocator(drive, start=1, width=3)
    first = allocator.allocate(YYMM)
    drive.file(YYMM, f"2405{first:03d}ACME.pdf")
    second = allocator.allocate(YYMM)
    assert (first, second) == (1, 2)


def test_allocator_each_derivation_rescans_live_folder() -> None:
    # Constraint: live state at derivation time, no cached counter.
    drive = FakeMonthFolders()
    allocator = SequenceAllocator(drive, start=1, width=3)
    assert allocator.allocate(YYMM) == 1
    drive.file(YYMM, "2405010OTHER.pdf")  # filed by someone else meanwhile
    assert allocator.allocate(YYMM) == 11
    assert drive.calls == [YYMM, YYMM]


def test_allocator_months_are_tracked_independently() -> None:
    drive = FakeMonthFolders({"2406": ["2406003X.pdf"]})
    allocator = SequenceAllocator(drive, start=1, width=3)
    assert allocator.allocate(YYMM) == 1
    assert allocator.allocate("2406") == 4
    assert allocator.allocate(YYMM) == 2
    assert allocator.allocate("2406") == 5
    assert allocator.allocated(YYMM) == frozenset({1, 2})
    assert allocator.allocated("2406") == frozenset({4, 5})
    assert allocator.allocated("2407") == frozenset()


def test_allocator_ac4_repeated_runs_never_reuse_a_filed_number() -> None:
    # Run 1 files three invoices; run 2 (a fresh allocator, e.g. after a restart)
    # must continue strictly above them -- the repeated-run regression check.
    drive = FakeMonthFolders()
    run1 = SequenceAllocator(drive, start=1, width=3)
    for supplier in ("ACME", "KOVARIKF", "TELEKOM"):
        seq = run1.allocate(YYMM)
        drive.file(YYMM, f"2405{seq:03d}{supplier}.pdf")

    run2 = SequenceAllocator(drive, start=1, width=3)
    assert run2.allocate(YYMM) == 4
    assert sorted(existing_sequences(drive.folders[YYMM], yymm=YYMM, width=3)) == [1, 2, 3]


def test_allocator_ac4_ten_invoices_run_twice_yield_no_duplicates_or_gaps() -> None:
    drive = FakeMonthFolders()
    assigned: list[int] = []
    for _run in range(2):
        allocator = SequenceAllocator(drive, start=1, width=3)
        for i in range(10):
            seq = allocator.allocate(YYMM)
            assigned.append(seq)
            drive.file(YYMM, f"2405{seq:03d}SUP{i}.pdf")
    assert assigned == list(range(1, 21))


def test_allocator_ac5_non_matching_files_ignored() -> None:
    drive = FakeMonthFolders({YYMM: ["scanned receipt.pdf", "2405999 draft.pdf", "2405003A.pdf"]})
    assert SequenceAllocator(drive, start=1, width=3).allocate(YYMM) == 4


def test_allocator_ac6_scan_failure_propagates_and_assigns_nothing() -> None:
    drive = FakeMonthFolders({YYMM: ["2405001A.pdf"]})
    drive.fail_with = _http_error(503)
    allocator = SequenceAllocator(drive, start=1, width=3)
    with pytest.raises(HttpError):
        allocator.allocate(YYMM)
    assert allocator.allocated(YYMM) == frozenset()

    # Once Drive recovers, derivation proceeds from the real folder state, not a default.
    drive.fail_with = None
    assert allocator.allocate(YYMM) == 2


def test_allocator_ac6_failure_after_in_run_allocations_keeps_them() -> None:
    drive = FakeMonthFolders()
    allocator = SequenceAllocator(drive, start=1, width=3)
    assert allocator.allocate(YYMM) == 1
    drive.fail_with = TimeoutError("drive timed out")
    with pytest.raises(TimeoutError):
        allocator.allocate(YYMM)
    assert allocator.allocated(YYMM) == frozenset({1})
    drive.fail_with = None
    assert allocator.allocate(YYMM) == 2


def test_allocator_ac6_with_real_drive_client_http_error_propagates() -> None:
    service = MagicMock(name="drive-service")
    request: Any = MagicMock(name="request")
    request.execute.side_effect = _http_error(500)
    service.files.return_value.list.return_value = request
    allocator = SequenceAllocator(DriveClient(service, "root"), start=1, width=3)
    with pytest.raises(HttpError):
        allocator.allocate(YYMM)
    assert allocator.allocated(YYMM) == frozenset()


def test_allocator_exhaustion_raises_and_records_nothing() -> None:
    drive = FakeMonthFolders({YYMM: ["2405999ACME.pdf"]})
    allocator = SequenceAllocator(drive, start=1, width=3)
    with pytest.raises(SequenceExhaustedError):
        allocator.allocate(YYMM)
    assert allocator.allocated(YYMM) == frozenset()


def test_allocator_release_returns_unused_number_for_reuse() -> None:
    # An invoice that fails before filing hands its number back, so the next invoice
    # in the run reuses it instead of leaving a gap.
    drive = FakeMonthFolders()
    allocator = SequenceAllocator(drive, start=1, width=3)
    failed = allocator.allocate(YYMM)
    allocator.release(YYMM, failed)
    assert allocator.allocate(YYMM) == failed


def test_allocator_release_of_a_filed_number_cannot_cause_reuse() -> None:
    # The live folder remains authoritative: releasing a number that was filed is harmless.
    drive = FakeMonthFolders()
    allocator = SequenceAllocator(drive, start=1, width=3)
    seq = allocator.allocate(YYMM)
    drive.file(YYMM, f"2405{seq:03d}ACME.pdf")
    allocator.release(YYMM, seq)
    assert allocator.allocate(YYMM) == seq + 1


def test_allocator_release_unknown_number_is_a_noop() -> None:
    allocator = SequenceAllocator(FakeMonthFolders(), start=1, width=3)
    allocator.release(YYMM, 42)
    assert allocator.allocated(YYMM) == frozenset()


def test_allocator_rejects_invalid_yymm_before_scanning() -> None:
    drive = FakeMonthFolders()
    allocator = SequenceAllocator(drive, start=1, width=3)
    with pytest.raises(ValueError):
        allocator.allocate("2413")
    assert drive.calls == []


def test_allocator_from_config_uses_sequence_start_and_width() -> None:
    drive = FakeMonthFolders({YYMM: ["240500042ACME.pdf"]})
    allocator = SequenceAllocator.from_config(drive, _config(start=1, width=5))
    assert allocator.width == 5
    assert allocator.start == 1
    assert allocator.allocate(YYMM) == 43


def test_allocator_from_config_start_value_for_new_month() -> None:
    allocator = SequenceAllocator.from_config(FakeMonthFolders(), _config(start=7, width=3))
    assert allocator.allocate(YYMM) == 7


@pytest.mark.parametrize(("start", "width"), [(-1, 3), (1000, 3), (1, 0)])
def test_allocator_rejects_invalid_settings(start: int, width: int) -> None:
    with pytest.raises(ValueError):
        SequenceAllocator(FakeMonthFolders(), start=start, width=width)
