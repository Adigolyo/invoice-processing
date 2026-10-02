"""Task 15 (USR-004-01): mapping a fully normalised invoice to ledger columns A-H.

Scenarios are derived from USR-004-01 AC1-AC5, the execution plan's Task 15 row and the
QA strategy's "Ledger Row Append - 8 Core Columns" feature, plus negative/edge paths:
every one of the 8 values missing or unusable, raw (un-normalised) values such as a
currency symbol, an amount string or a date string, HUF amounts that were not rounded per
USR-004-02, an ambiguous route, and control characters in text columns.

``build_ledger_row`` is pure: the orchestrator (the only module allowed to call
``append_row``) appends the row it returns. The append-side ACs (AC1 column order on the
wire, AC4 exactly one row, AC5 failure propagation) are therefore asserted here as
contract tests that drive the built row through a real ``SheetsClient`` over an
in-memory Sheets fake.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import httplib2
import pytest
from googleapiclient.errors import HttpError

from intake.clients.sheets_client import LEDGER_HEADER, LedgerRow, SheetsClient
from intake.ledger.booking import (
    INV_TYPE_BY_ROUTE,
    BookingError,
    build_ledger_row,
    clean_ledger_text,
)
from intake.ledger.dedupe import DuplicateGate
from intake.models import FlagReason, InvoiceExtraction, Route, StageStatus
from intake.normalization import (
    NormalizationFailure,
    determine_origin,
    normalize_amounts,
    normalize_dates,
    round_amounts_for_ledger,
    to_iso_code,
)

WIDTH = 3
SHEET_ID = "ledger-sheet"
CURRENCY_MAP = {"Ft": "HUF", "€": "EUR", "$": "USD"}


def _inputs(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "registry_number": "2405007ACMECORP",
        "provider": "Acme Corp Kft.",
        "external_invoice_id": "INV-2024/0042",
        "route": Route.DIRECT,
        "currency": "HUF",
        "net": Decimal("100000"),
        "gross": Decimal("127000"),
        "due_date": date(2024, 5, 31),
        "sequence_width": WIDTH,
    }
    values.update(overrides)
    return values


def _build(**overrides: Any) -> LedgerRow:
    return build_ledger_row(**_inputs(**overrides))


# --- AC1: all 8 columns mapped A-H -------------------------------------------------------


def test_fully_ready_invoice_maps_every_value_to_its_column() -> None:
    row = _build()

    assert row == LedgerRow(
        inv_id_int="2405007ACMECORP",
        provider="Acme Corp Kft.",
        inv_id_ext="INV-2024/0042",
        inv_type="direct",
        currency="HUF",
        net=Decimal("100000"),
        gross=Decimal("127000"),
        due_date=date(2024, 5, 31),
    )


def test_cell_values_follow_ledger_header_order_a_to_h() -> None:
    row = _build(route=Route.TIG, currency="EUR", net=Decimal("1234.56"))

    cells = dict(zip(LEDGER_HEADER, row.to_values(), strict=True))

    assert cells == {
        "INV_ID_int": "2405007ACMECORP",
        "Provider": "Acme Corp Kft.",
        "INV_ID_ext": "INV-2024/0042",
        "INV_type": "tig",
        "Currency": "EUR",
        "Net": 1234.56,
        "Gross": 127000,
        "Due date": "2024.05.31",
    }


@pytest.mark.parametrize(("route", "expected"), [(Route.DIRECT, "direct"), (Route.TIG, "tig")])
def test_inv_type_is_the_route_value(route: Route, expected: str) -> None:
    assert _build(route=route).inv_type == expected
    assert INV_TYPE_BY_ROUTE[route] == expected


def test_inv_type_mapping_covers_every_bookable_route_only() -> None:
    assert set(INV_TYPE_BY_ROUTE) == {Route.DIRECT, Route.TIG}


# Answer-key style table: each "fixture" is an invoice's final normalised values and the
# exact A-H cells the ledger must show (zero wrong rows).
ANSWER_KEY: list[tuple[dict[str, Any], list[Any]]] = [
    (
        _inputs(),
        [
            "2405007ACMECORP",
            "Acme Corp Kft.",
            "INV-2024/0042",
            "direct",
            "HUF",
            100000,
            127000,
            "2024.05.31",
        ],
    ),
    (
        _inputs(
            registry_number="2406001KOVARIKF",
            provider="Kővári Kft",
            external_invoice_id="KV-77",
            route=Route.TIG,
            currency="HUF",
            net=Decimal("125000"),
            gross=Decimal("158750"),
            due_date=date(2024, 7, 15),
        ),
        ["2406001KOVARIKF", "Kővári Kft", "KV-77", "tig", "HUF", 125000, 158750, "2024.07.15"],
    ),
    (
        _inputs(
            registry_number="2403012TELEKOM",
            provider="Magyar Telekom Nyrt.",
            external_invoice_id="1-2345678",
            currency="EUR",
            net=Decimal("1234.56"),
            gross=Decimal("1567.89"),
            due_date=date(2024, 3, 14),
        ),
        [
            "2403012TELEKOM",
            "Magyar Telekom Nyrt.",
            "1-2345678",
            "direct",
            "EUR",
            1234.56,
            1567.89,
            "2024.03.14",
        ],
    ),
    (
        _inputs(
            registry_number="2412003ACME",
            provider="ACME",
            external_invoice_id="A1",
            currency="USD",
            net=Decimal("10.5"),
            gross=Decimal("10.50"),
            due_date=date(2025, 1, 2),
            sequence_width=3,
        ),
        ["2412003ACME", "ACME", "A1", "direct", "USD", 10.5, 10.5, "2025.01.02"],
    ),
]


@pytest.mark.parametrize(("inputs", "expected"), ANSWER_KEY)
def test_answer_key_rows_match_exactly(inputs: dict[str, Any], expected: list[Any]) -> None:
    assert build_ledger_row(**inputs).to_values() == expected


# --- AC2: normalised values, never raw extraction ------------------------------------------


def test_row_built_from_pipeline_normalisers_holds_normalised_values_only() -> None:
    extraction = InvoiceExtraction(
        supplier="  Acme   Corp Kft. ",
        invoice_number=" INV-2024/0042 ",
        currency="Ft",
        net="125 000,40",
        gross="158.750,50",
        due_date="2024.05.31.",
        supplier_country="Magyarország",
    )
    origin = determine_origin(extraction)
    currency = to_iso_code(extraction.currency, origin, CURRENCY_MAP)
    assert currency == "HUF"
    amounts = round_amounts_for_ledger(normalize_amounts(extraction), currency)
    dates = normalize_dates(extraction, origin)

    row = build_ledger_row(
        registry_number="2405007ACMECORP",
        provider=extraction.supplier,
        external_invoice_id=extraction.invoice_number,
        route=Route.DIRECT,
        currency=currency,
        net=amounts.net,
        gross=amounts.gross,
        due_date=dates.due_date,
        sequence_width=WIDTH,
    )

    assert row.to_values() == [
        "2405007ACMECORP",
        "Acme Corp Kft.",
        "INV-2024/0042",
        "direct",
        "HUF",
        125000,
        158751,
        "2024.05.31",
    ]


@pytest.mark.parametrize("raw_currency", ["Ft", "huf", "€", "$", "HUF.", "XXY", ""])
def test_raw_or_unresolved_currency_is_refused(raw_currency: str) -> None:
    with pytest.raises(BookingError) as info:
        _build(currency=raw_currency)

    assert "Currency" in info.value.columns


def test_non_text_currency_is_refused() -> None:
    with pytest.raises(BookingError) as info:
        _build(currency=348)  # HUF's numeric ISO code is still not the ledger form

    assert info.value.columns == ("Currency",)


def test_unrecognised_iso_shaped_currency_is_refused() -> None:
    with pytest.raises(BookingError) as info:
        _build(currency="ABC")

    assert info.value.columns == ("Currency",)


@pytest.mark.parametrize("raw_amount", ["100000", "1.234,56", 100000, 100000.0, True])
def test_raw_or_non_decimal_net_is_refused(raw_amount: Any) -> None:
    with pytest.raises(BookingError) as info:
        _build(net=raw_amount)

    assert info.value.columns == ("Net",)


@pytest.mark.parametrize("raw_date", ["2024.05.31", "05/31/2024", 20240531])
def test_raw_due_date_is_refused(raw_date: Any) -> None:
    with pytest.raises(BookingError) as info:
        _build(due_date=raw_date)

    assert info.value.columns == ("Due date",)


def test_datetime_due_date_is_refused_as_not_a_calendar_date() -> None:
    with pytest.raises(BookingError) as info:
        _build(due_date=datetime(2024, 5, 31, 12, 0))

    assert info.value.columns == ("Due date",)


def test_text_columns_are_whitespace_tidied_not_otherwise_changed() -> None:
    row = _build(provider="\tKővári   Kft\n", external_invoice_id="  KV  77 ")

    assert row.provider == "Kővári Kft"
    assert row.inv_id_ext == "KV 77"


def test_decomposed_accents_are_composed_so_identical_names_write_identical_cells() -> None:
    decomposed = "Kővári Kft"

    assert _build(provider=decomposed).provider == "Kővári Kft"


def test_clean_ledger_text_is_the_shared_cleaning_rule() -> None:
    assert clean_ledger_text("  Acme   Corp ") == "Acme Corp"
    assert clean_ledger_text("   ") == ""


# --- USR-004-02 handshake: amounts must arrive already rounded -----------------------------


@pytest.mark.parametrize(
    ("column", "amount"),
    [("Net", Decimal("125000.40")), ("Gross", Decimal("999.50")), ("Net", Decimal("0.4"))],
)
def test_unrounded_huf_amount_is_refused_not_re_rounded(column: str, amount: Decimal) -> None:
    field = "net" if column == "Net" else "gross"

    with pytest.raises(BookingError) as info:
        _build(**{field: amount})

    assert info.value.columns == (column,)
    assert "rounded" in str(info.value)


def test_huf_amount_with_zero_fraction_is_accepted_as_rounded() -> None:
    row = _build(net=Decimal("125000.00"))

    assert row.to_values()[5] == 125000


@pytest.mark.parametrize("currency", ["EUR", "USD"])
def test_non_huf_decimals_are_written_unchanged(currency: str) -> None:
    row = _build(currency=currency, net=Decimal("1234.567"), gross=Decimal("0.01"))

    assert row.net == Decimal("1234.567")
    assert row.gross == Decimal("0.01")


@pytest.mark.parametrize("amount", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")])
def test_non_finite_amount_is_refused(amount: Decimal) -> None:
    with pytest.raises(BookingError) as info:
        _build(gross=amount)

    assert info.value.columns == ("Gross",)


def test_negative_amounts_are_written_as_given() -> None:
    # The specification is silent on credit notes; booking does not invent a rule.
    row = _build(net=Decimal("-1000"), gross=Decimal("-1270"))

    assert row.to_values()[5:7] == [-1000, -1270]


# --- AC3: a missing value blocks booking ------------------------------------------------


MISSING_CASES: list[tuple[str, str]] = [
    ("registry_number", "INV_ID_int"),
    ("provider", "Provider"),
    ("external_invoice_id", "INV_ID_ext"),
    ("route", "INV_type"),
    ("currency", "Currency"),
    ("net", "Net"),
    ("gross", "Gross"),
    ("due_date", "Due date"),
]


@pytest.mark.parametrize(("field", "column"), MISSING_CASES)
def test_each_missing_value_blocks_the_row(field: str, column: str) -> None:
    with pytest.raises(BookingError) as info:
        _build(**{field: None})

    assert info.value.columns == (column,)
    assert "missing" in str(info.value)


@pytest.mark.parametrize(
    ("field", "column"),
    [
        ("registry_number", "INV_ID_int"),
        ("provider", "Provider"),
        ("external_invoice_id", "INV_ID_ext"),
        ("currency", "Currency"),
    ],
)
@pytest.mark.parametrize("blank", ["", "   ", " \t"])
def test_blank_text_counts_as_missing(field: str, column: str, blank: str) -> None:
    with pytest.raises(BookingError) as info:
        _build(**{field: blank})

    assert info.value.columns == (column,)


@pytest.mark.parametrize(
    ("field", "column"), [("net", "Net"), ("gross", "Gross"), ("due_date", "Due date")]
)
def test_normalisation_failure_blocks_the_row_with_its_detail(field: str, column: str) -> None:
    failure = NormalizationFailure("ambiguous separator pattern", field=field)

    with pytest.raises(BookingError) as info:
        _build(**{field: failure})

    assert info.value.columns == (column,)
    assert "ambiguous separator pattern" in str(info.value)


def test_currency_normalisation_failure_blocks_the_row() -> None:
    with pytest.raises(BookingError) as info:
        _build(currency=NormalizationFailure("currency is missing", field="currency"))

    assert info.value.columns == ("Currency",)


def test_every_problem_is_reported_together_in_column_order() -> None:
    with pytest.raises(BookingError) as info:
        _build(registry_number=None, net=None, due_date="31/05/2024", currency="Ft")

    assert info.value.columns == ("INV_ID_int", "Currency", "Net", "Due date")


def test_unrounded_check_is_skipped_when_the_currency_itself_is_unusable() -> None:
    with pytest.raises(BookingError) as info:
        _build(currency=None, net=Decimal("125000.40"))

    assert info.value.columns == ("Currency",)


def test_ambiguous_route_is_never_booked() -> None:
    with pytest.raises(BookingError) as info:
        _build(route=Route.AMBIGUOUS)

    assert info.value.columns == ("INV_type",)


@pytest.mark.parametrize("route", ["direct", "supplier", 1])
def test_route_must_be_a_route_member(route: Any) -> None:
    with pytest.raises(BookingError) as info:
        _build(route=route)

    assert info.value.columns == ("INV_type",)


@pytest.mark.parametrize(
    "number",
    [
        "2405007",  # no supplier ID
        "240507ACMECORP",  # sequence narrower than the configured width
        "2413007ACMECORP",  # month 13
        "2405007acmecorp",  # not upper-case
        "2405007ACMECORPX",  # supplier ID longer than 8
        "2405007ACMECORP.pdf",  # a filename, not a registry number
        " 2405007ACMECORP",  # surrounding whitespace is not silently fixed
    ],
)
def test_malformed_registry_number_is_refused(number: str) -> None:
    with pytest.raises(BookingError) as info:
        _build(registry_number=number)

    assert info.value.columns == ("INV_ID_int",)


def test_registry_number_must_be_a_string() -> None:
    with pytest.raises(BookingError) as info:
        _build(registry_number=2405007)

    assert info.value.columns == ("INV_ID_int",)


@pytest.mark.parametrize("width", [0, -1, True, "3", None])
def test_invalid_sequence_width_is_refused(width: Any) -> None:
    with pytest.raises(BookingError) as info:
        _build(sequence_width=width)

    assert "INV_ID_int" in info.value.columns


@pytest.mark.parametrize(
    ("field", "column"), [("provider", "Provider"), ("external_invoice_id", "INV_ID_ext")]
)
@pytest.mark.parametrize("bad", ["Acme\x00Corp", "Acme\x1bCorp", 42])
def test_text_with_control_characters_or_wrong_type_is_refused(
    field: str, column: str, bad: Any
) -> None:
    with pytest.raises(BookingError) as info:
        _build(**{field: bad})

    assert info.value.columns == (column,)


@pytest.mark.parametrize("formula", ['=HYPERLINK("x")', "+1", "-SUM(A1)", "@x"])
def test_formula_like_text_is_written_verbatim_because_appends_are_raw(formula: str) -> None:
    # SheetsClient appends with valueInputOption=RAW, so a leading "=" is stored as text,
    # never evaluated; booking therefore does not alter it.
    assert _build(provider=formula).provider == formula


def test_booking_error_flags_incomplete_data_for_manual_review() -> None:
    with pytest.raises(BookingError) as info:
        _build(net=None)

    error = info.value
    assert isinstance(error, ValueError)
    assert error.reason is FlagReason.INCOMPLETE_DATA
    result = error.to_stage_result()
    assert result.status is StageStatus.INCOMPLETE
    assert result.flag_reason is FlagReason.INCOMPLETE_DATA
    assert result.detail is not None and "Net" in result.detail


def test_booking_error_requires_at_least_one_problem() -> None:
    with pytest.raises(ValueError, match="at least one"):
        BookingError(())


# --- Contract with SheetsClient: AC1 on the wire, AC3 no call, AC4 one row, AC5 errors --


def _http_error(status: int) -> HttpError:
    return HttpError(httplib2.Response({"status": status}), b'{"error": "boom"}')


def _request(result: Any = None, error: Exception | None = None) -> MagicMock:
    req = MagicMock(name="request")
    if error is not None:
        req.execute.side_effect = error
    else:
        req.execute.return_value = result
    return req


class FakeLedger:
    """In-memory Ledger tab behind ``spreadsheets().values().get/append``."""

    def __init__(self) -> None:
        self.service = MagicMock(name="sheets")
        values = self.service.spreadsheets.return_value.values.return_value
        values.get.side_effect = self._get
        values.append.side_effect = self._append
        self.append_calls = values.append
        self.rows: list[list[Any]] = [list(LEDGER_HEADER)]
        self.append_error: Exception | None = None

    def _get(self, **kwargs: Any) -> MagicMock:
        rows = self.rows[:1] if kwargs["range"].endswith("A1:H1") else self.rows
        return _request({"values": [list(r) for r in rows]})

    def _append(self, **kwargs: Any) -> MagicMock:
        if self.append_error is not None:
            return _request(error=self.append_error)
        new_rows = kwargs["body"]["values"]
        self.rows.extend(new_rows)
        return _request({"updates": {"updatedRows": len(new_rows)}})

    def client(self) -> SheetsClient:
        return SheetsClient(self.service, SHEET_ID)


def _orchestrator_books(sheets: SheetsClient, **overrides: Any) -> LedgerRow:
    """What the orchestrator does at the booking step: build, then append once."""
    row = _build(**overrides)
    sheets.append_row(row)
    return row


def test_successful_booking_appends_exactly_one_row_in_column_order() -> None:
    ledger = FakeLedger()

    _orchestrator_books(ledger.client())

    assert ledger.append_calls.call_count == 1
    assert ledger.rows[1:] == [
        [
            "2405007ACMECORP",
            "Acme Corp Kft.",
            "INV-2024/0042",
            "direct",
            "HUF",
            100000,
            127000,
            "2024.05.31",
        ],
    ]
    kwargs = ledger.append_calls.call_args.kwargs
    assert kwargs["range"].endswith("!A:H")
    assert kwargs["valueInputOption"] == "RAW"


def test_missing_value_means_no_sheets_call_at_all() -> None:
    ledger = FakeLedger()

    with pytest.raises(BookingError):
        _orchestrator_books(ledger.client(), gross=None)

    ledger.service.spreadsheets.assert_not_called()
    assert ledger.rows == [list(LEDGER_HEADER)]


@pytest.mark.parametrize("status", [429, 500, 503])
def test_sheets_append_failure_propagates_so_the_invoice_is_not_marked_processed(
    status: int,
) -> None:
    ledger = FakeLedger()
    ledger.append_error = _http_error(status)

    # The error reaches the orchestrator unchanged, so it aborts before mark_read/label.
    with pytest.raises(HttpError):
        _orchestrator_books(ledger.client())

    assert ledger.append_calls.call_count == 1
    assert ledger.rows == [list(LEDGER_HEADER)]


def test_dedupe_gate_keys_on_the_provider_and_external_id_that_were_written() -> None:
    ledger = FakeLedger()
    sheets = ledger.client()
    gate = DuplicateGate(sheets)
    row = _build(provider="  Kővári   Kft ", external_invoice_id=" KV-77 ")

    assert not gate.is_duplicate(ext_id=row.inv_id_ext, provider=row.provider)
    sheets.append_row(row)

    # A re-run builds the same row from the same extraction and must be caught.
    rerun = _build(provider="Kővári Kft", external_invoice_id="KV-77")
    assert gate.is_duplicate(ext_id=rerun.inv_id_ext, provider=rerun.provider)
    # Same external ID from a different provider is not a duplicate (USR-004-03 AC3).
    other = _build(provider="Acme Corp Kft.", external_invoice_id="KV-77")
    assert not gate.is_duplicate(ext_id=other.inv_id_ext, provider=other.provider)


def test_build_is_pure_and_deterministic() -> None:
    builders: list[Callable[[], LedgerRow]] = [_build] * 5

    rows = {builder() for builder in builders}

    assert len(rows) == 1
