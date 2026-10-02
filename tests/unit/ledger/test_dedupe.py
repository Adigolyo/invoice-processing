"""Task 17 (USR-004-03): duplicate invoice entry prevention.

Scenarios are derived from USR-004-03 AC1-AC6 and the QA strategy's
"Duplicate Invoice Entry Prevention" feature, plus negative/edge paths:

- AC1 no match -> proceed to append
- AC2 exact (ext_id, provider) match -> already booked
- AC3 same ext_id, different provider -> not a duplicate
- AC4 holds across separate runs (fresh gate, live ledger read)
- AC5 holds within one run for repeated candidates
- AC6 a failed check blocks booking (fail closed), never "no duplicate"
- constraint: the ledger is read live at every check, never cached
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import httplib2
import pytest
from googleapiclient.errors import HttpError

from intake.clients.sheets_client import (
    LEDGER_HEADER,
    LedgerHeaderError,
    SheetsClient,
    SheetsResponseError,
)
from intake.ledger import DuplicateCheckError, DuplicateGate, dedupe_key, is_duplicate

# --- fakes --------------------------------------------------------------------


class FakeLedger:
    """Stands in for ``SheetsClient``: a live, mutable set of ledger keys.

    ``load_existing_keys`` returns a *copy* each call (like a real read) and counts
    reads so tests can assert the ledger is consulted at every check.
    """

    def __init__(self, keys: set[tuple[str, str]] | None = None) -> None:
        self.keys: set[tuple[str, str]] = set(keys or ())
        self.reads = 0
        self.error: Exception | None = None
        self.result_override: Any = None

    def load_existing_keys(self) -> set[tuple[str, str]]:
        self.reads += 1
        if self.error is not None:
            raise self.error
        if self.result_override is not None:
            result: set[tuple[str, str]] = self.result_override
            return result
        return set(self.keys)

    def append(self, ext_id: str, provider: str) -> None:
        """Simulates the orchestrator's ``append_row`` landing in the ledger."""
        self.keys.add((ext_id, provider))


def _http_error(status: int = 503) -> HttpError:
    return HttpError(httplib2.Response({"status": status}), b'{"error": "boom"}')


def _sheets_client(rows: list[list[Any]]) -> tuple[SheetsClient, MagicMock]:
    """A real ``SheetsClient`` over a mocked googleapiclient resource."""
    service = MagicMock(name="sheets")
    request = MagicMock(name="request")
    request.execute.return_value = {"values": rows}
    service.spreadsheets.return_value.values.return_value.get.return_value = request
    return SheetsClient(service, "spreadsheet-123"), request


HEADER = list(LEDGER_HEADER)


def _ledger_row(provider: str, ext_id: str) -> list[Any]:
    return ["2405001ACMECORP", provider, ext_id, "direct", "HUF", 1000, 1270, "2024.05.31"]


# --- AC1: no matching entry -> proceeds to append -----------------------------


def test_ac1_empty_ledger_is_not_duplicate() -> None:
    assert is_duplicate(FakeLedger(), ext_id="INV-001", provider="ACME Kft") is False


def test_ac1_no_matching_pair_is_not_duplicate() -> None:
    ledger = FakeLedger({("INV-001", "ACME Kft"), ("INV-002", "Beta Zrt")})
    assert is_duplicate(ledger, ext_id="INV-003", provider="ACME Kft") is False


def test_ac1_gate_no_match_is_not_duplicate() -> None:
    gate = DuplicateGate(FakeLedger({("INV-001", "ACME Kft")}))
    assert gate.is_duplicate(ext_id="INV-999", provider="ACME Kft") is False


# --- AC2: exact match -> already booked ----------------------------------------


def test_ac2_exact_match_is_duplicate() -> None:
    ledger = FakeLedger({("INV-001", "ACME Kft")})
    assert is_duplicate(ledger, ext_id="INV-001", provider="ACME Kft") is True


def test_ac2_gate_exact_match_is_duplicate() -> None:
    gate = DuplicateGate(FakeLedger({("INV-001", "ACME Kft")}))
    assert gate.is_duplicate(ext_id="INV-001", provider="ACME Kft") is True


def test_ac2_key_order_is_ext_id_then_provider() -> None:
    # load_existing_keys() yields (INV_ID_ext, Provider); a swapped comparison would
    # miss this duplicate (and falsely flag the swapped pair).
    ledger = FakeLedger({("INV-001", "ACME Kft")})
    assert is_duplicate(ledger, ext_id="INV-001", provider="ACME Kft") is True
    assert is_duplicate(ledger, ext_id="ACME Kft", provider="INV-001") is False


# --- AC3: both values must match together ---------------------------------------


def test_ac3_same_ext_id_different_provider_is_not_duplicate() -> None:
    ledger = FakeLedger({("2024/001", "ACME Kft")})
    assert is_duplicate(ledger, ext_id="2024/001", provider="Beta Zrt") is False


def test_ac3_same_provider_different_ext_id_is_not_duplicate() -> None:
    ledger = FakeLedger({("2024/001", "ACME Kft")})
    assert is_duplicate(ledger, ext_id="2024/002", provider="ACME Kft") is False


def test_ac3_second_provider_with_same_ext_id_is_appended_then_both_detected() -> None:
    ledger = FakeLedger({("2024/001", "ACME Kft")})
    gate = DuplicateGate(ledger)
    assert gate.is_duplicate(ext_id="2024/001", provider="Beta Zrt") is False
    ledger.append("2024/001", "Beta Zrt")
    gate.record_booked(ext_id="2024/001", provider="Beta Zrt")
    assert gate.is_duplicate(ext_id="2024/001", provider="Beta Zrt") is True
    assert gate.is_duplicate(ext_id="2024/001", provider="ACME Kft") is True
    assert gate.is_duplicate(ext_id="2024/001", provider="Gamma Bt") is False


def test_ac3_record_booked_does_not_block_other_providers() -> None:
    gate = DuplicateGate(FakeLedger())
    gate.record_booked(ext_id="2024/001", provider="ACME Kft")
    assert gate.is_duplicate(ext_id="2024/001", provider="Beta Zrt") is False


# --- AC4: across separate runs ---------------------------------------------------


def test_ac4_booked_in_previous_run_detected_by_fresh_gate() -> None:
    ledger = FakeLedger()
    run1 = DuplicateGate(ledger)
    assert run1.is_duplicate(ext_id="INV-001", provider="ACME Kft") is False
    ledger.append("INV-001", "ACME Kft")

    run2 = DuplicateGate(ledger)  # new run: no in-memory state carried over
    assert run2.is_duplicate(ext_id="INV-001", provider="ACME Kft") is True


def test_ac4_repeated_run_over_fixture_like_batch_books_each_once() -> None:
    ledger = FakeLedger()
    batch = [("INV-1", "ACME Kft"), ("INV-2", "ACME Kft"), ("INV-1", "Beta Zrt")]

    for _run in range(2):
        gate = DuplicateGate(ledger)
        for ext_id, provider in batch:
            if not gate.is_duplicate(ext_id=ext_id, provider=provider):
                ledger.append(ext_id, provider)
                gate.record_booked(ext_id=ext_id, provider=provider)

    assert ledger.keys == set(batch)


def test_ac4_detects_rows_through_real_sheets_client() -> None:
    client, _ = _sheets_client([HEADER, _ledger_row("ACME Kft", "INV-001")])
    assert is_duplicate(client, ext_id="INV-001", provider="ACME Kft") is True
    assert is_duplicate(client, ext_id="INV-001", provider="Beta Zrt") is False


def test_ac4_numeric_ext_id_cell_matches_string_ext_id() -> None:
    # UNFORMATTED_VALUE returns a number for a numeric-looking cell.
    client, _ = _sheets_client([HEADER, _ledger_row("ACME Kft", 12345)])  # type: ignore[arg-type]
    assert is_duplicate(client, ext_id="12345", provider="ACME Kft") is True


# --- AC5: within the same run ------------------------------------------------------


def test_ac5_second_occurrence_after_append_is_duplicate_via_live_read() -> None:
    ledger = FakeLedger()
    gate = DuplicateGate(ledger)
    assert gate.is_duplicate(ext_id="INV-001", provider="ACME Kft") is False
    ledger.append("INV-001", "ACME Kft")
    gate.record_booked(ext_id="INV-001", provider="ACME Kft")
    assert gate.is_duplicate(ext_id="INV-001", provider="ACME Kft") is True


def test_ac5_in_run_guard_covers_a_lagging_ledger_read() -> None:
    # The append succeeded but the following read does not show the row yet.
    ledger = FakeLedger()
    gate = DuplicateGate(ledger)
    assert gate.is_duplicate(ext_id="INV-001", provider="ACME Kft") is False
    gate.record_booked(ext_id="INV-001", provider="ACME Kft")  # ledger.keys unchanged
    assert gate.is_duplicate(ext_id="INV-001", provider="ACME Kft") is True


def test_ac5_stateless_function_relies_on_live_read_after_append() -> None:
    ledger = FakeLedger()
    assert is_duplicate(ledger, ext_id="INV-001", provider="ACME Kft") is False
    ledger.append("INV-001", "ACME Kft")
    assert is_duplicate(ledger, ext_id="INV-001", provider="ACME Kft") is True


def test_ac5_in_run_guard_still_reads_ledger_live() -> None:
    # A recorded key short-circuits nothing: every check performs a fresh read, so a
    # failing read still blocks booking even for a key already recorded this run.
    ledger = FakeLedger()
    gate = DuplicateGate(ledger)
    gate.record_booked(ext_id="INV-001", provider="ACME Kft")
    ledger.error = _http_error(503)
    with pytest.raises(DuplicateCheckError):
        gate.is_duplicate(ext_id="INV-001", provider="ACME Kft")


# --- constraint: live read at every check, never cached --------------------------


def test_every_check_reads_the_ledger_fresh() -> None:
    ledger = FakeLedger()
    gate = DuplicateGate(ledger)
    for _ in range(3):
        gate.is_duplicate(ext_id="INV-001", provider="ACME Kft")
    assert ledger.reads == 3


def test_row_added_externally_between_checks_is_seen() -> None:
    ledger = FakeLedger()
    gate = DuplicateGate(ledger)
    assert gate.is_duplicate(ext_id="INV-001", provider="ACME Kft") is False
    ledger.keys.add(("INV-001", "ACME Kft"))  # e.g. booked manually by the bookkeeper
    assert gate.is_duplicate(ext_id="INV-001", provider="ACME Kft") is True


def test_stateless_function_reads_once_per_call() -> None:
    ledger = FakeLedger()
    is_duplicate(ledger, ext_id="A", provider="B")
    is_duplicate(ledger, ext_id="A", provider="B")
    assert ledger.reads == 2


def test_real_sheets_client_is_queried_at_every_check() -> None:
    client, request = _sheets_client([HEADER])
    gate = DuplicateGate(client)
    gate.is_duplicate(ext_id="INV-001", provider="ACME Kft")
    gate.is_duplicate(ext_id="INV-001", provider="ACME Kft")
    assert request.execute.call_count == 2


# --- AC6: a failed check blocks booking (fail closed) ----------------------------


@pytest.mark.parametrize("status", [429, 500, 503])
def test_ac6_sheets_api_error_raises_not_false(status: int) -> None:
    ledger = FakeLedger()
    ledger.error = _http_error(status)
    with pytest.raises(DuplicateCheckError) as excinfo:
        is_duplicate(ledger, ext_id="INV-001", provider="ACME Kft")
    assert isinstance(excinfo.value.__cause__, HttpError)


def test_ac6_gate_api_error_raises() -> None:
    ledger = FakeLedger()
    ledger.error = _http_error(503)
    with pytest.raises(DuplicateCheckError):
        DuplicateGate(ledger).is_duplicate(ext_id="INV-001", provider="ACME Kft")


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("read timed out"),
        ConnectionError("reset"),
        LedgerHeaderError("reordered header"),
        SheetsResponseError("malformed"),
    ],
)
def test_ac6_any_read_failure_blocks_booking(error: Exception) -> None:
    ledger = FakeLedger()
    ledger.error = error
    with pytest.raises(DuplicateCheckError) as excinfo:
        is_duplicate(ledger, ext_id="INV-001", provider="ACME Kft")
    assert excinfo.value.__cause__ is error


def test_ac6_real_sheets_client_api_failure_blocks_booking() -> None:
    client, request = _sheets_client([HEADER])
    request.execute.side_effect = _http_error(503)
    with pytest.raises(DuplicateCheckError):
        is_duplicate(client, ext_id="INV-001", provider="ACME Kft")


def test_ac6_real_sheets_client_reordered_header_blocks_booking() -> None:
    client, _ = _sheets_client([list(reversed(HEADER)), _ledger_row("ACME Kft", "INV-1")])
    with pytest.raises(DuplicateCheckError):
        is_duplicate(client, ext_id="INV-001", provider="ACME Kft")


@pytest.mark.parametrize(
    "bad_result",
    [
        None,
        "INV-001",
        [("INV-001",)],
        [("INV-001", "ACME Kft", "extra")],
        [("INV-001", 7)],
        [None],
    ],
)
def test_ac6_malformed_key_set_blocks_booking(bad_result: Any) -> None:
    ledger = FakeLedger()
    ledger.result_override = bad_result
    with pytest.raises(DuplicateCheckError):
        is_duplicate(ledger, ext_id="INV-001", provider="ACME Kft")


def test_ac6_keyboard_interrupt_is_not_swallowed() -> None:
    ledger = FakeLedger()
    ledger.error = KeyboardInterrupt()  # type: ignore[assignment]
    with pytest.raises(KeyboardInterrupt):
        is_duplicate(ledger, ext_id="INV-001", provider="ACME Kft")


def test_duplicate_check_error_is_a_runtime_error() -> None:
    assert issubclass(DuplicateCheckError, RuntimeError)


# --- key normalisation: lenient against double-booking ------------------------------


@pytest.mark.parametrize(
    ("ext_id", "provider"),
    [
        ("INV-001", "ACME Kft"),
        ("  INV-001 ", "ACME Kft  "),
        ("inv-001", "acme kft"),
        ("INV-001", "ACME  Kft"),
        ("INV-001", "ACME Kft"),  # non-breaking space
        ("ＩＮＶ-001", "ACME Kft"),  # full-width letters (NFKC)
        ("INV-001", "ACME\tKft"),
    ],
)
def test_cosmetic_variants_of_a_booked_key_are_duplicates(ext_id: str, provider: str) -> None:
    ledger = FakeLedger({("INV-001", "ACME Kft")})
    assert is_duplicate(ledger, ext_id=ext_id, provider=provider) is True


def test_cosmetic_variant_stored_in_ledger_is_detected() -> None:
    ledger = FakeLedger({(" inv-001", "acme  KFT ")})
    assert is_duplicate(ledger, ext_id="INV-001", provider="ACME Kft") is True


def test_case_insensitive_includes_hungarian_accents() -> None:
    ledger = FakeLedger({("SZ-1", "KŐVÁRI KFT")})
    assert is_duplicate(ledger, ext_id="sz-1", provider="Kővári Kft") is True


@pytest.mark.parametrize(
    ("ext_id", "provider"),
    [
        ("INV-0011", "ACME Kft"),
        ("INV001", "ACME Kft"),  # punctuation is significant
        ("INV-001", "ACME Kft."),
        ("INV-001", "Kovari Kft"),  # accents are significant (different provider string)
    ],
)
def test_substantive_differences_are_not_duplicates(ext_id: str, provider: str) -> None:
    ledger = FakeLedger({("INV-001", "ACME Kft"), ("INV-002", "Kővári Kft")})
    assert is_duplicate(ledger, ext_id=ext_id, provider=provider) is False


def test_dedupe_key_normalises_both_parts() -> None:
    assert dedupe_key(ext_id="  Inv-001 ", provider="ACME   kft") == ("inv-001", "acme kft")


def test_dedupe_key_order_is_ext_id_then_provider() -> None:
    assert dedupe_key(ext_id="X", provider="Y") == ("x", "y")


@pytest.mark.parametrize(
    ("ext_id", "provider"),
    [("", "ACME Kft"), ("INV-001", ""), ("   ", "ACME Kft"), ("INV-001", "\t\n")],
)
def test_blank_values_are_rejected_before_reading(ext_id: str, provider: str) -> None:
    ledger = FakeLedger()
    with pytest.raises(ValueError):
        is_duplicate(ledger, ext_id=ext_id, provider=provider)
    with pytest.raises(ValueError):
        DuplicateGate(ledger).is_duplicate(ext_id=ext_id, provider=provider)
    assert ledger.reads == 0


def test_blank_values_rejected_by_record_booked() -> None:
    with pytest.raises(ValueError):
        DuplicateGate(FakeLedger()).record_booked(ext_id=" ", provider="ACME Kft")


def test_blank_keys_in_ledger_are_ignored() -> None:
    ledger = FakeLedger()
    ledger.result_override = {("  ", "ACME Kft"), ("INV-001", "")}
    assert is_duplicate(ledger, ext_id="INV-002", provider="ACME Kft") is False


def test_frozenset_and_list_results_are_accepted() -> None:
    ledger = FakeLedger()
    ledger.result_override = frozenset({("INV-001", "ACME Kft")})
    assert is_duplicate(ledger, ext_id="INV-001", provider="ACME Kft") is True
    ledger.result_override = [("INV-001", "ACME Kft")]
    assert is_duplicate(ledger, ext_id="INV-001", provider="ACME Kft") is True


def test_check_does_not_mutate_the_returned_key_set() -> None:
    keys = {("INV-001", "ACME Kft")}
    ledger = FakeLedger()
    ledger.result_override = keys
    is_duplicate(ledger, ext_id="inv-001", provider="acme kft")
    assert keys == {("INV-001", "ACME Kft")}
