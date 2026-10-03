"""Task 4: Sheets client wrapper (Config tab, Ledger header check, dedupe keys, append)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import httplib2
import pytest
from googleapiclient.errors import HttpError

from intake.clients import sheets_client
from intake.clients.sheets_client import (
    LEDGER_HEADER,
    LedgerHeaderError,
    LedgerRow,
    SheetsClient,
    SheetsResponseError,
)
from intake.config import REQUIRED_KEYS, Config, ConfigError

SHEET_ID = "spreadsheet-123"
HEADER = list(LEDGER_HEADER)

CONFIG_ROWS: list[list[Any]] = [
    ["key", "value"],
    ["invoice_keywords", "számla, invoice, Rechnung"],
    ["attachment_mime_allowlist", "application/pdf, image/jpeg, image/png"],
    ["tig_subject_indicators", "TIG, teljesítésigazolás"],
    ["currency_map", "Ft=HUF, €=EUR, $=USD"],
    ["label_processed", "Kibit/Processed"],
    ["label_pending", "Kibit/Pending"],
    ["label_needs_review", "Kibit/NeedsReview"],
    ["label_awaiting_tig", "Kibit/AwaitingTIG"],
    ["sequence_start", 1],
    ["sequence_width", 3],
    ["rounding_tolerance", 0.01],
    ["drive_root_folder_id", "1AbCdEfRootFolder"],
]


def _http_error(status: int = 503) -> HttpError:
    return HttpError(httplib2.Response({"status": status}), b'{"error": "boom"}')


def _request(result: Any = None, error: Exception | None = None) -> MagicMock:
    req = MagicMock(name="request")
    if error is not None:
        req.execute.side_effect = error
    else:
        req.execute.return_value = result
    return req


class FakeSheets:
    """Answers ``spreadsheets().values().get/append`` from an in-memory tab map."""

    def __init__(self, ledger: list[list[Any]] | None = None) -> None:
        self.service = MagicMock(name="sheets")
        self.values = self.service.spreadsheets.return_value.values.return_value
        self.tabs: dict[str, list[list[Any]]] = {
            "Config": [list(r) for r in CONFIG_ROWS],
            "Ledger": [list(r) for r in (ledger if ledger is not None else [HEADER])],
        }
        self.values.get.side_effect = self._get
        self.values.append.side_effect = self._append
        self.get_error: Exception | None = None
        self.append_error: Exception | None = None
        self.get_ranges: list[str] = []

    def _get(self, **kwargs: Any) -> MagicMock:
        assert kwargs["spreadsheetId"] == SHEET_ID
        if self.get_error is not None:
            return _request(error=self.get_error)
        rng: str = kwargs["range"]
        self.get_ranges.append(rng)
        tab, _, cells = rng.partition("!")
        rows = self.tabs[tab.strip("'")]
        if cells.endswith("1:H1"):
            rows = rows[:1]
        result: dict[str, Any] = {"range": rng}
        if rows:
            result["values"] = rows
        return _request(result)

    def _append(self, **kwargs: Any) -> MagicMock:
        if self.append_error is not None:
            return _request(error=self.append_error)
        new_rows = kwargs["body"]["values"]
        self.tabs["Ledger"].extend(new_rows)
        return _request({"updates": {"updatedRows": len(new_rows)}})

    def client(self) -> SheetsClient:
        return SheetsClient(self.service, SHEET_ID)


def _row(**overrides: Any) -> LedgerRow:
    values: dict[str, Any] = {
        "inv_id_int": "2405001ACMECORP",
        "provider": "Acme Corp",
        "inv_id_ext": "INV-0042",
        "inv_type": "direct",
        "currency": "HUF",
        "net": Decimal("100000"),
        "gross": Decimal("127000"),
        "due_date": date(2024, 5, 31),
    }
    values.update(overrides)
    return LedgerRow(**values)


# --- load_config -------------------------------------------------------------------------


def test_load_config_builds_config_from_key_value_tab() -> None:
    fake = FakeSheets()

    config = fake.client().load_config()

    assert isinstance(config, Config)
    assert config.invoice_keywords == ("számla", "invoice", "Rechnung")
    assert config.attachment_mime_allowlist == ("application/pdf", "image/jpeg", "image/png")
    assert config.tig_subject_indicators == ("TIG", "teljesítésigazolás")
    assert dict(config.currency_map) == {"Ft": "HUF", "€": "EUR", "$": "USD"}
    assert config.labels.processed == "Kibit/Processed"
    assert config.labels.awaiting_tig == "Kibit/AwaitingTIG"
    assert config.sequence_start == 1
    assert config.sequence_width == 3
    assert config.rounding_tolerance == Decimal("0.01")
    assert config.drive_root_folder_id == "1AbCdEfRootFolder"
    kwargs = fake.values.get.call_args.kwargs
    assert kwargs["range"] == "'Config'!A:B"
    assert kwargs["valueRenderOption"] == "UNFORMATTED_VALUE"


def test_load_config_sample_covers_exactly_the_required_keys() -> None:
    assert {row[0] for row in CONFIG_ROWS[1:]} == set(REQUIRED_KEYS)


def test_load_config_accepts_header_case_and_whitespace_and_skips_blank_rows() -> None:
    fake = FakeSheets()
    fake.tabs["Config"][0] = [" Key ", "VALUE"]
    fake.tabs["Config"].insert(3, [])
    fake.tabs["Config"].insert(4, ["", ""])
    fake.tabs["Config"][1] = ["  invoice_keywords ", "számla"]
    fake.tabs["Config"].append(["unknown_key", "ignored"])

    config = fake.client().load_config()

    assert config.invoice_keywords == ("számla",)


def test_load_config_reports_missing_keys() -> None:
    fake = FakeSheets()
    rows = [r for r in fake.tabs["Config"] if r[0] != "label_pending"]
    # A key with an empty value cell: the API omits trailing blank cells.
    fake.tabs["Config"] = [["sequence_width"] if r[0] == "sequence_width" else r for r in rows]

    with pytest.raises(ConfigError) as excinfo:
        fake.client().load_config()

    assert set(excinfo.value.missing_keys) == {"label_pending", "sequence_width"}


def test_load_config_rejects_duplicate_keys() -> None:
    fake = FakeSheets()
    fake.tabs["Config"].append(["invoice_keywords", "other"])

    with pytest.raises(ConfigError, match="invoice_keywords"):
        fake.client().load_config()


@pytest.mark.parametrize("header", [["name", "value"], ["key"], []])
def test_load_config_rejects_missing_or_wrong_header(header: list[str]) -> None:
    fake = FakeSheets()
    fake.tabs["Config"][0] = header

    with pytest.raises(ConfigError, match="header"):
        fake.client().load_config()


def test_load_config_rejects_empty_tab() -> None:
    fake = FakeSheets()
    fake.tabs["Config"] = []

    with pytest.raises(ConfigError):
        fake.client().load_config()


def test_load_config_raises_on_api_error() -> None:
    fake = FakeSheets()
    fake.get_error = _http_error(503)

    with pytest.raises(HttpError):
        fake.client().load_config()


# --- ledger header -----------------------------------------------------------------------


def test_verify_ledger_header_accepts_exact_header() -> None:
    fake = FakeSheets()
    fake.client().verify_ledger_header()
    assert fake.get_ranges == ["'Ledger'!A1:H1"]


def test_verify_ledger_header_tolerates_surrounding_whitespace() -> None:
    fake = FakeSheets(ledger=[[f" {h} " for h in HEADER]])
    fake.client().verify_ledger_header()


@pytest.mark.parametrize(
    "header",
    [
        [HEADER[1], HEADER[0], *HEADER[2:]],  # reordered
        [*HEADER[:7], "Due"],  # renamed
        HEADER[:7],  # missing column
        [h.lower() for h in HEADER],  # case changed
        [],  # empty
    ],
)
def test_verify_ledger_header_fails_loudly_on_mismatch(header: list[str]) -> None:
    fake = FakeSheets(ledger=[header] if header else [])

    with pytest.raises(LedgerHeaderError, match="INV_ID_int"):
        fake.client().verify_ledger_header()


def test_verify_ledger_header_raises_on_api_error() -> None:
    fake = FakeSheets()
    fake.get_error = _http_error(500)

    with pytest.raises(HttpError):
        fake.client().verify_ledger_header()


# --- load_existing_keys ------------------------------------------------------------------


def test_load_existing_keys_returns_ext_id_provider_pairs() -> None:
    fake = FakeSheets(
        ledger=[
            HEADER,
            ["2405001ACMECORP", "Acme Corp", "INV-0042", "direct", "HUF", 100, 127, "2024.05.31"],
            ["2405002KOVARIKF", " Kővári Kft ", 12345, "tig", "EUR", 1.5, 2, "2024.06.01"],
            ["2405003SHORT", "Short"],  # truncated row: no ext id
            [],
            ["", "", ""],
        ]
    )

    keys = fake.client().load_existing_keys()

    assert keys == {("INV-0042", "Acme Corp"), ("12345", "Kővári Kft")}
    assert fake.get_ranges == ["'Ledger'!A1:H"]
    assert fake.values.get.call_args.kwargs["valueRenderOption"] == "UNFORMATTED_VALUE"


def test_load_existing_keys_reads_fresh_each_call() -> None:
    fake = FakeSheets()
    client = fake.client()
    assert client.load_existing_keys() == set()

    fake.tabs["Ledger"].append(["X", "Prov", "EXT-1", "direct", "EUR", 1, 1, "2024.01.01"])

    assert client.load_existing_keys() == {("EXT-1", "Prov")}
    assert fake.values.get.call_count == 2


def test_load_existing_keys_checks_header_first() -> None:
    fake = FakeSheets(ledger=[[HEADER[1], HEADER[0], *HEADER[2:]], ["a", "b", "c"]])

    with pytest.raises(LedgerHeaderError):
        fake.client().load_existing_keys()


def test_load_existing_keys_raises_on_api_error_rather_than_returning_empty() -> None:
    fake = FakeSheets()
    fake.get_error = _http_error(429)

    with pytest.raises(HttpError):
        fake.client().load_existing_keys()


# --- append_row --------------------------------------------------------------------------


def test_append_row_writes_columns_a_to_h_in_order() -> None:
    fake = FakeSheets()

    fake.client().append_row(_row(net=Decimal("1234.56"), gross=Decimal("1567.89")))

    kwargs = fake.values.append.call_args.kwargs
    assert kwargs["spreadsheetId"] == SHEET_ID
    assert kwargs["range"] == "'Ledger'!A:H"
    assert kwargs["valueInputOption"] == "RAW"
    assert kwargs["insertDataOption"] == "INSERT_ROWS"
    assert kwargs["body"] == {
        "values": [
            [
                "2405001ACMECORP",
                "Acme Corp",
                "INV-0042",
                "direct",
                "HUF",
                1234.56,
                1567.89,
                "2024.05.31",
            ]
        ]
    }


def test_append_row_writes_integral_amounts_as_ints() -> None:
    fake = FakeSheets()

    fake.client().append_row(_row(net=Decimal("125000"), gross=Decimal("158750.00")))

    values = fake.values.append.call_args.kwargs["body"]["values"][0]
    assert values[5] == 125000 and isinstance(values[5], int)
    assert values[6] == 158750 and isinstance(values[6], int)


def test_append_row_verifies_header_before_writing() -> None:
    fake = FakeSheets(ledger=[[*HEADER[:7], "Due"]])

    with pytest.raises(LedgerHeaderError):
        fake.client().append_row(_row())

    fake.values.append.assert_not_called()


def test_append_row_raises_on_api_error() -> None:
    fake = FakeSheets()
    fake.append_error = _http_error(429)

    with pytest.raises(HttpError):
        fake.client().append_row(_row())


def test_append_row_raises_when_response_does_not_confirm_one_row() -> None:
    fake = FakeSheets()
    fake.values.append.side_effect = None
    fake.values.append.return_value = _request({"updates": {"updatedRows": 0}})

    with pytest.raises(SheetsResponseError):
        fake.client().append_row(_row())


def test_appended_row_is_seen_by_dedupe() -> None:
    fake = FakeSheets()
    client = fake.client()

    client.append_row(_row())

    assert client.load_existing_keys() == {("INV-0042", "Acme Corp")}


# --- LedgerRow validation ----------------------------------------------------------------


@pytest.mark.parametrize("field", ["inv_id_int", "provider", "inv_id_ext", "inv_type", "currency"])
def test_ledger_row_rejects_blank_text(field: str) -> None:
    with pytest.raises(ValueError, match=field):
        _row(**{field: "  "})


@pytest.mark.parametrize("field", ["net", "gross"])
def test_ledger_row_rejects_non_decimal_amounts(field: str) -> None:
    with pytest.raises(TypeError, match=field):
        _row(**{field: 1.5})


def test_ledger_row_rejects_non_finite_amounts() -> None:
    with pytest.raises(ValueError, match="net"):
        _row(net=Decimal("NaN"))


def test_ledger_row_rejects_non_date_due_date() -> None:
    with pytest.raises(TypeError, match="due_date"):
        _row(due_date="2024.05.31")


def test_ledger_row_rejects_non_iso_currency() -> None:
    with pytest.raises(ValueError, match="currency"):
        _row(currency="Ft")


def test_ledger_row_to_values_formats_due_date() -> None:
    assert _row(due_date=date(2024, 1, 2)).to_values()[7] == "2024.01.02"


def test_module_exports() -> None:
    assert "SheetsClient" in sheets_client.__all__
