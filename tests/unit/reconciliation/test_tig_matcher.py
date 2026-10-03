"""Task 18 / USR-005-01: locating the TIG in the thread and comparing it to the invoice.

Hermetic: the Gmail client and the extractor are fakes. Hand-built cases mirror the
committed fixture PDFs in ``tests/fixtures/tig_pairs/``; the data-driven test replays all
32 pairs of ``answer_key.json`` through ``compare``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from intake.clients.gmail_client import AttachmentContent, Thread, ThreadMessage
from intake.config import Config
from intake.extraction import DocumentKind, UnsupportedDocumentError
from intake.models import (
    Attachment,
    ComparisonOutcome,
    ComparisonResult,
    Discrepancy,
    DiscrepancyField,
    FlagReason,
    InvoiceExtraction,
    LineItem,
    Route,
    StageResult,
    StageStatus,
)
from intake.reconciliation import (
    NotTigRouteError,
    TigDocument,
    TigLookup,
    TigLookupStatus,
    compare,
    find_tig,
    is_tig_filename,
)

ANSWER_KEY = Path(__file__).resolve().parents[2] / "fixtures" / "tig_pairs" / "answer_key.json"
TOLERANCE = Decimal("0.01")
PDF = "application/pdf"
D = Decimal


# --- fakes and builders -----------------------------------------------------------------


def _config(**overrides: Any) -> Config:
    raw: dict[str, Any] = {
        "invoice_keywords": ["számla", "invoice"],
        "attachment_mime_allowlist": [PDF, "image/jpeg", "image/png"],
        "tig_subject_indicators": ["TIG", "teljesítésigazolás"],
        "currency_map": {"Ft": "HUF", "€": "EUR"},
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


def _message(
    message_id: str,
    *filenames: str,
    subject: str = "Kék projekt, 2026. szeptember",
    sender: str = "pm@kibit.example",
    internal_date: int = 0,
    mime_type: str = PDF,
) -> ThreadMessage:
    return ThreadMessage(
        message_id=message_id,
        thread_id="t-1",
        sender=sender,
        subject=subject,
        rfc822_message_id=f"<{message_id}@mail.example>",
        references=None,
        internal_date=internal_date,
        attachments=tuple(
            Attachment(filename=name, mime_type=mime_type, attachment_id=f"att-{message_id}-{i}")
            for i, name in enumerate(filenames)
        ),
    )


def _thread(*messages: ThreadMessage) -> Thread:
    return Thread(thread_id="t-1", messages=messages)


class FakeGmail:
    """Serves attachment bytes as ``b"<message_id>/<filename>"``; records downloads."""

    def __init__(self, thread: Thread) -> None:
        self._by_id = {m.message_id: m for m in thread.messages}
        self.downloads: list[str] = []

    def download_attachments(self, message_id: str) -> list[AttachmentContent]:
        self.downloads.append(message_id)
        return [
            AttachmentContent(a, f"{message_id}/{a.filename}".encode())
            for a in self._by_id[message_id].attachments
        ]


@dataclass
class FakeExtractor:
    """Returns a queued ``StageResult`` (or raises a queued exception) per call."""

    outcomes: list[StageResult[InvoiceExtraction] | Exception]
    calls: list[tuple[bytes, str, DocumentKind]] = field(default_factory=list)

    def extract(
        self, content: bytes, mime_type: str, *, kind: DocumentKind = DocumentKind.INVOICE
    ) -> StageResult[InvoiceExtraction]:
        self.calls.append((content, mime_type, kind))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _line(
    description: str, quantity: str | None, unit_price: str | None, net: str | None
) -> LineItem:
    return LineItem(
        description=description,
        quantity=None if quantity is None else D(quantity),
        unit_price=None if unit_price is None else D(unit_price),
        net=None if net is None else D(net),
    )


# TIG-2026-09-KEK-C (multi-line, printed total) and its mismatching invoice INV-C-2026-02.
KEK_C_TIG_LINES = (
    _line("Nyomdai szolgáltatás (szórólap)", "950", "0.40", "380.00"),
    _line("Ruhatisztítás, szőnyegmosás", "42", "14.50", "609.00"),
)
KEK_C_INVOICE = InvoiceExtraction(
    supplier="C Kft.",
    invoice_number="INV-C-2026-02",
    currency="EUR",
    net="1 073,00 EUR",
    gross="1 362,71 EUR",
    line_items=(
        _line(
            "Nyomdai szolgáltatás (szórólap) – Kék projekt, 2026. szeptember",
            "950",
            "0.40",
            "380.00",
        ),
        _line(
            "Ruhatisztítás, szőnyegmosás – Kék projekt, 2026. szeptember", "42", "16.50", "693.00"
        ),
    ),
)
# TIG-2026-09-KEK-A (single line, no total row) and its matching AAM invoice INV_A-2026-01.
KEK_A_TIG_LINES = (_line("Nyomdai szolgáltatás (szórólap)", "1200", "60", "72000"),)
KEK_A_INVOICE = InvoiceExtraction(
    supplier="A Kft.",
    invoice_number="INV_A-2026-01",
    currency="Ft",
    net="72 000 Ft",
    gross="72 000 Ft",
    line_items=(
        _line(
            "Nyomdai szolgáltatás (szórólap) – Kék projekt, 2026. szeptember", "1200", "60", "72000"
        ),
    ),
)


def _tig(lines: tuple[LineItem, ...], total_net: str | None = None) -> TigDocument:
    return TigDocument(
        message_id="m-tig",
        filename="TIG.pdf",
        lines=lines,
        total_net=None if total_net is None else D(total_net),
    )


def _tig_extraction(
    lines: tuple[LineItem, ...] = KEK_C_TIG_LINES, net: str | None = "989,00 EUR"
) -> InvoiceExtraction:
    return InvoiceExtraction(
        supplier="C Kft.",
        invoice_number="TIG-2026-09-KEK-C",
        currency="EUR",
        net=net,
        line_items=lines,
        supplier_country="HU",
    )


def _compare(invoice: InvoiceExtraction, tig: TigDocument, **kwargs: Any) -> ComparisonResult:
    kwargs.setdefault("route", Route.TIG)
    return compare(invoice, tig, kwargs.pop("tolerance", TOLERANCE), **kwargs)


# --- compare: acceptance criteria -------------------------------------------------------


def test_ac1_matching_single_line_invoice_and_tig_is_a_match() -> None:
    result = _compare(KEK_A_INVOICE, _tig(KEK_A_TIG_LINES), currency_iso="HUF")

    assert result == ComparisonResult.match()
    assert result.outcome is ComparisonOutcome.MATCH


def test_ac1_matching_multi_line_with_printed_total_is_a_match() -> None:
    invoice = InvoiceExtraction(net="989,00 EUR", line_items=KEK_C_TIG_LINES)

    result = _compare(invoice, _tig(KEK_C_TIG_LINES, "989.00"), currency_iso="EUR")

    assert result == ComparisonResult.match()


def test_lines_are_matched_by_position_not_by_exact_description() -> None:
    # Invoice descriptions append "– Kék projekt, 2026. szeptember" to the TIG's.
    invoice = InvoiceExtraction(
        net="989,00 EUR",
        line_items=(
            _line("Nyomdai szolgáltatás (szórólap) – Kék projekt", "950", "0.40", "380.00"),
            _line("Ruhatisztítás, szőnyegmosás – Kék projekt", "42", "14.50", "609.00"),
        ),
    )

    assert _compare(invoice, _tig(KEK_C_TIG_LINES, "989.00")).outcome is ComparisonOutcome.MATCH


def test_ac3_unit_price_mismatch_is_attributed_to_the_line_and_total_flagged() -> None:
    result = _compare(KEK_C_INVOICE, _tig(KEK_C_TIG_LINES, "989.00"), currency_iso="EUR")

    assert result.is_mismatch
    assert result.discrepancies == (
        Discrepancy(
            DiscrepancyField.UNIT_PRICE,
            invoice_value=D("16.50"),
            tig_value=D("14.50"),
            line_index=1,
            line_description="Ruhatisztítás, szőnyegmosás – Kék projekt, 2026. szeptember",
        ),
        Discrepancy(DiscrepancyField.TOTAL_NET, invoice_value=D("1073.00"), tig_value=D("989.00")),
    )


def test_ac2_quantity_mismatch_on_line_two_is_identified() -> None:
    # QA scenario: line item 2's quantity differs (TIG 10 units, invoice 12 units).
    tig_lines = (
        _line("Tréning", "1", "100", "100"),
        _line("Catering", "10", "5", "50"),
    )
    invoice = InvoiceExtraction(
        net="160",
        line_items=(_line("Tréning", "1", "100", "100"), _line("Catering", "12", "5", "60")),
    )

    result = _compare(invoice, _tig(tig_lines))

    assert result.is_mismatch
    assert result.discrepancies[0] == Discrepancy(
        DiscrepancyField.QUANTITY, D("12"), D("10"), line_index=1, line_description="Catering"
    )
    # No printed TIG total and a line discrepancy already explains it: no derived total.
    assert len(result.discrepancies) == 1


def test_quantity_and_unit_price_both_reported_on_the_same_line() -> None:
    tig = _tig((_line("Jogi tanácsadás", "11", "103.50", "1138.50"),))
    invoice = InvoiceExtraction(
        net="1 300,00 EUR", line_items=(_line("Jogi tanácsadás", "12", "108.33", "1300.00"),)
    )

    fields = [d.field for d in _compare(invoice, tig, currency_iso="EUR").discrepancies]

    assert fields == [DiscrepancyField.QUANTITY, DiscrepancyField.UNIT_PRICE]


def test_ac4_total_net_mismatch_detected_even_when_lines_agree() -> None:
    # QA scenario: an extra undocumented fee shows up only in the invoice total.
    invoice = InvoiceExtraction(net="1 039,00 EUR", line_items=KEK_C_TIG_LINES)

    result = _compare(invoice, _tig(KEK_C_TIG_LINES, "989.00"), currency_iso="EUR")

    assert result.discrepancies == (
        Discrepancy(DiscrepancyField.TOTAL_NET, D("1039.00"), D("989.00")),
    )


def test_ac4_single_line_tig_without_printed_total_uses_the_line_net_sum() -> None:
    invoice = InvoiceExtraction(net="75 000 Ft", line_items=KEK_A_INVOICE.line_items)

    result = _compare(invoice, _tig(KEK_A_TIG_LINES), currency_iso="HUF")

    assert result.discrepancies == (
        Discrepancy(DiscrepancyField.TOTAL_NET, D("75000"), D("72000")),
    )


def test_derived_total_falls_back_to_quantity_times_unit_price_without_line_nets() -> None:
    tig = _tig((_line("Könyvelés", "2", "187000", None),))
    invoice = InvoiceExtraction(
        net="374 000 Ft", line_items=(_line("Könyvelés", "2", "187000", None),)
    )

    assert _compare(invoice, tig, currency_iso="HUF").outcome is ComparisonOutcome.MATCH


@pytest.mark.parametrize("invoice_net", ["989,01 EUR", "988,99 EUR", "989,00 EUR"])
def test_ac5_difference_within_tolerance_is_a_match(invoice_net: str) -> None:
    # QA scenario: exactly at the ±0.01 boundary is still a match.
    invoice = InvoiceExtraction(net=invoice_net, line_items=KEK_C_TIG_LINES)

    result = _compare(invoice, _tig(KEK_C_TIG_LINES, "989.00"), currency_iso="EUR")

    assert result == ComparisonResult.match()


@pytest.mark.parametrize("invoice_net", ["989,02 EUR", "988,98 EUR"])
def test_ac5_one_cent_over_tolerance_is_a_mismatch(invoice_net: str) -> None:
    invoice = InvoiceExtraction(net=invoice_net, line_items=KEK_C_TIG_LINES)

    result = _compare(invoice, _tig(KEK_C_TIG_LINES, "989.00"), currency_iso="EUR")

    assert [d.field for d in result.discrepancies] == [DiscrepancyField.TOTAL_NET]


def test_tolerance_is_configurable() -> None:
    invoice = InvoiceExtraction(net="989,40 EUR", line_items=KEK_C_TIG_LINES)
    tig = _tig(KEK_C_TIG_LINES, "989.00")

    assert _compare(invoice, tig, tolerance=D("0.5")).outcome is ComparisonOutcome.MATCH
    assert _compare(invoice, tig, tolerance=D("0")).is_mismatch


def test_tolerance_from_config_is_accepted() -> None:
    invoice = InvoiceExtraction(net="989,01 EUR", line_items=KEK_C_TIG_LINES)

    result = compare(
        invoice, _tig(KEK_C_TIG_LINES, "989.00"), _config().rounding_tolerance, route=Route.TIG
    )

    assert result.outcome is ComparisonOutcome.MATCH


def test_negative_tolerance_is_rejected() -> None:
    with pytest.raises(ValueError, match="tolerance"):
        _compare(KEK_A_INVOICE, _tig(KEK_A_TIG_LINES), tolerance=D("-0.01"))


def test_numerically_equal_values_with_different_precision_match() -> None:
    invoice = InvoiceExtraction(
        net="989",
        line_items=(
            _line("Nyomdai szolgáltatás (szórólap)", "950.0", "0.4", "380"),
            _line("Ruhatisztítás, szőnyegmosás", "42", "14.5", "609"),
        ),
    )

    assert _compare(invoice, _tig(KEK_C_TIG_LINES, "989.00")).outcome is ComparisonOutcome.MATCH


def test_line_unit_prices_are_compared_exactly_not_within_total_tolerance() -> None:
    tig = _tig((_line("Óradíj", "10", "100.00", "1000.00"),), "1000.00")
    invoice = InvoiceExtraction(
        net="1000.00", line_items=(_line("Óradíj", "10", "100.001", "1000.00"),)
    )

    result = _compare(invoice, tig)

    assert [d.field for d in result.discrepancies] == [DiscrepancyField.UNIT_PRICE]


# --- compare: line-count differences and unreadable values ---------------------------------


def test_extra_invoice_line_without_tig_counterpart_is_flagged() -> None:
    invoice = InvoiceExtraction(
        net="1 039,00 EUR",
        line_items=(*KEK_C_TIG_LINES, _line("Kiszállási díj", "1", "50.00", "50.00")),
    )

    result = _compare(invoice, _tig(KEK_C_TIG_LINES, "989.00"), currency_iso="EUR")

    assert result.discrepancies == (
        Discrepancy(DiscrepancyField.QUANTITY, D("1"), None, 2, "Kiszállási díj"),
        Discrepancy(DiscrepancyField.TOTAL_NET, D("1039.00"), D("989.00")),
    )


def test_tig_line_missing_from_the_invoice_is_flagged() -> None:
    invoice = InvoiceExtraction(net="380,00 EUR", line_items=KEK_C_TIG_LINES[:1])

    result = _compare(invoice, _tig(KEK_C_TIG_LINES, "989.00"), currency_iso="EUR")

    assert result.discrepancies == (
        Discrepancy(DiscrepancyField.QUANTITY, None, D("42"), 1, "Ruhatisztítás, szőnyegmosás"),
        Discrepancy(DiscrepancyField.TOTAL_NET, D("380.00"), D("989.00")),
    )


def test_invoice_without_line_items_is_a_mismatch_not_a_silent_match() -> None:
    invoice = InvoiceExtraction(net="72 000 Ft")

    result = _compare(invoice, _tig(KEK_A_TIG_LINES), currency_iso="HUF")

    assert result.discrepancies == (
        Discrepancy(
            DiscrepancyField.QUANTITY, None, D("1200"), 0, "Nyomdai szolgáltatás (szórólap)"
        ),
    )


@pytest.mark.parametrize(
    ("invoice_line", "expected_field"),
    [
        (_line("Nyomdai szolgáltatás", None, "60", "72000"), DiscrepancyField.QUANTITY),
        (_line("Nyomdai szolgáltatás", "1200", None, "72000"), DiscrepancyField.UNIT_PRICE),
    ],
)
def test_unreadable_invoice_line_value_is_flagged_never_matched(
    invoice_line: LineItem, expected_field: DiscrepancyField
) -> None:
    invoice = InvoiceExtraction(net="72 000 Ft", line_items=(invoice_line,))

    result = _compare(invoice, _tig(KEK_A_TIG_LINES), currency_iso="HUF")

    assert result.is_mismatch
    (discrepancy,) = result.discrepancies
    assert discrepancy.field is expected_field
    assert discrepancy.invoice_value is None
    assert discrepancy.line_index == 0


def test_unreadable_tig_line_value_is_flagged_never_matched() -> None:
    tig = _tig((_line("Nyomdai szolgáltatás (szórólap)", None, "60", "72000"),))

    result = _compare(KEK_A_INVOICE, tig, currency_iso="HUF")

    assert result.discrepancies[0].field is DiscrepancyField.QUANTITY
    assert result.discrepancies[0].tig_value is None


@pytest.mark.parametrize("invoice_net", [None, "1.234", "körülbelül 989"])
def test_unreadable_invoice_total_is_flagged_against_a_printed_tig_total(
    invoice_net: str | None,
) -> None:
    invoice = InvoiceExtraction(net=invoice_net, line_items=KEK_C_TIG_LINES)

    result = _compare(invoice, _tig(KEK_C_TIG_LINES, "989.00"), currency_iso="EUR")

    assert result.discrepancies == (Discrepancy(DiscrepancyField.TOTAL_NET, None, D("989.00")),)


def test_huf_currency_resolves_lone_thousands_separator_in_invoice_total() -> None:
    invoice = InvoiceExtraction(net="72.000 Ft", line_items=KEK_A_INVOICE.line_items)

    assert _compare(invoice, _tig(KEK_A_TIG_LINES), currency_iso="HUF").outcome is (
        ComparisonOutcome.MATCH
    )


def test_unreadable_invoice_total_without_tig_total_is_flagged() -> None:
    invoice = InvoiceExtraction(net=None, line_items=KEK_A_INVOICE.line_items)

    result = _compare(invoice, _tig(KEK_A_TIG_LINES), currency_iso="HUF")

    assert result.discrepancies == (Discrepancy(DiscrepancyField.TOTAL_NET, None, D("72000")),)


def test_no_derived_total_when_tig_lines_cannot_be_summed() -> None:
    # Unreadable TIG quantity already flags the line; no total can be derived.
    tig = _tig((_line("Óradíj", None, "100", None),))
    invoice = InvoiceExtraction(net="1000", line_items=(_line("Óradíj", "10", "100", "1000"),))

    result = _compare(invoice, tig)

    assert [d.field for d in result.discrepancies] == [DiscrepancyField.QUANTITY]


# --- AC6: TIG route only ------------------------------------------------------------------


@pytest.mark.parametrize("route", [Route.DIRECT, Route.AMBIGUOUS])
def test_ac6_compare_refuses_non_tig_routes(route: Route) -> None:
    with pytest.raises(NotTigRouteError):
        compare(KEK_A_INVOICE, _tig(KEK_A_TIG_LINES), TOLERANCE, route=route)


@pytest.mark.parametrize("route", [Route.DIRECT, Route.AMBIGUOUS])
def test_ac6_find_tig_refuses_non_tig_routes_without_touching_gmail_or_gemini(
    route: Route,
) -> None:
    thread = _thread(_message("m1", "TIG-2026-09-KEK-A.pdf"), _message("m2", "INV_A-2026-01.pdf"))
    gmail, extractor = FakeGmail(thread), FakeExtractor([])

    with pytest.raises(NotTigRouteError):
        find_tig(thread, "m2", route=route, gmail=gmail, extractor=extractor, config=_config())

    assert gmail.downloads == []
    assert extractor.calls == []


def test_not_tig_route_error_is_a_value_error() -> None:
    assert issubclass(NotTigRouteError, ValueError)


# --- answer key: all 32 fixture pairs -----------------------------------------------------


def _key_line(line: dict[str, Any]) -> LineItem:
    return LineItem(
        description=line["description"],
        quantity=D(line["quantity"]),
        unit_price=D(line["unit_price"]),
        net=D(line["net"]),
    )


def _answer_key_pairs() -> list[dict[str, Any]]:
    if not ANSWER_KEY.exists():
        return []
    pairs: list[dict[str, Any]] = json.loads(ANSWER_KEY.read_text(encoding="utf-8"))["pairs"]
    return pairs


@pytest.mark.skipif(not ANSWER_KEY.exists(), reason="answer_key.json not present")
def test_answer_key_covers_32_pairs_with_7_mismatches() -> None:
    pairs = _answer_key_pairs()

    assert len(pairs) == 32
    assert sum(p["expected_outcome"] == "mismatch" for p in pairs) == 7


@pytest.mark.skipif(not ANSWER_KEY.exists(), reason="answer_key.json not present")
@pytest.mark.parametrize("pair", _answer_key_pairs(), ids=lambda p: p["pair"])
def test_compare_reproduces_the_answer_key(pair: dict[str, Any]) -> None:
    key_tig, key_invoice = pair["tig"], pair["invoice"]
    tig = TigDocument(
        message_id="m-tig",
        filename=key_tig["file"],
        lines=tuple(_key_line(line) for line in key_tig["lines"]),
        total_net=None if key_tig["total_net"] is None else D(key_tig["total_net"]),
        currency=key_tig["currency"],
        number=key_tig["number"],
    )
    invoice = InvoiceExtraction(
        supplier=key_invoice["supplier"],
        invoice_number=key_invoice["invoice_number"],
        currency=key_invoice["currency_raw"],
        net=key_invoice["net_raw"],
        gross=key_invoice["gross_raw"],
        line_items=tuple(_key_line(line) for line in key_invoice["lines"]),
    )

    result = compare(invoice, tig, TOLERANCE, route=Route.TIG, currency_iso=key_invoice["currency"])

    assert result.outcome.value == pair["expected_outcome"]
    actual = [
        (d.line_index, d.field.value, d.invoice_value, d.tig_value) for d in result.discrepancies
    ]
    expected = [
        (d["line_index"], d["field"], D(d["invoice_value"]), D(d["tig_value"]))
        for d in pair["discrepancies"]
    ]
    assert actual == expected


# --- find_tig: locating the TIG -------------------------------------------------------------


def _find(
    thread: Thread,
    invoice_message_id: str,
    *outcomes: StageResult[InvoiceExtraction] | Exception,
    config: Config | None = None,
) -> tuple[TigLookup, FakeGmail, FakeExtractor]:
    gmail, extractor = FakeGmail(thread), FakeExtractor(list(outcomes))
    lookup = find_tig(
        thread,
        invoice_message_id,
        route=Route.TIG,
        gmail=gmail,
        extractor=extractor,
        config=config or _config(),
    )
    return lookup, gmail, extractor


def test_finds_tig_attachment_in_an_earlier_message_and_extracts_it_as_a_tig() -> None:
    thread = _thread(
        _message("m1", "TIG-2026-09-KEK-C.pdf"),
        _message("m2", "INV-C-2026-02.pdf", sender="billing@c-kft.example"),
    )

    lookup, gmail, extractor = _find(thread, "m2", StageResult.ok(_tig_extraction()))

    assert lookup.status is TigLookupStatus.FOUND
    assert lookup.found and not lookup.is_missing and not lookup.needs_review
    assert lookup.flag_reason is None
    assert extractor.calls == [(b"m1/TIG-2026-09-KEK-C.pdf", PDF, DocumentKind.TIG)]
    assert gmail.downloads == ["m1"]
    document = lookup.document
    assert document is not None
    assert document.message_id == "m1"
    assert document.filename == "TIG-2026-09-KEK-C.pdf"
    assert document.lines == KEK_C_TIG_LINES
    assert document.total_net == D("989.00")
    assert document.number == "TIG-2026-09-KEK-C"
    assert document.currency == "EUR"


def test_found_tig_feeds_compare() -> None:
    thread = _thread(_message("m1", "TIG-2026-09-KEK-C.pdf"), _message("m2", "INV-C-2026-02.pdf"))
    lookup, _, _ = _find(thread, "m2", StageResult.ok(_tig_extraction()))
    assert lookup.document is not None

    result = compare(KEK_C_INVOICE, lookup.document, TOLERANCE, route=Route.TIG)

    assert [d.field for d in result.discrepancies] == [
        DiscrepancyField.UNIT_PRICE,
        DiscrepancyField.TOTAL_NET,
    ]


def test_single_line_tig_without_printed_total_has_no_total() -> None:
    thread = _thread(_message("m1", "TIG-2026-09-KEK-A.pdf"), _message("m2", "INV_A-2026-01.pdf"))

    lookup, _, _ = _find(thread, "m2", StageResult.ok(_tig_extraction(KEK_A_TIG_LINES, net=None)))

    assert lookup.document is not None
    assert lookup.document.total_net is None


def test_huf_tig_total_with_lone_thousands_separator_is_read_with_config_currency_map() -> None:
    extraction = InvoiceExtraction(
        currency="Ft", net="72.000 Ft", line_items=KEK_A_TIG_LINES, supplier_country="HU"
    )
    thread = _thread(_message("m1", "TIG-1.pdf"), _message("m2", "INV.pdf"))

    lookup, _, _ = _find(thread, "m2", StageResult.ok(extraction))

    assert lookup.document is not None
    assert lookup.document.total_net == D("72000")


def test_thread_without_any_tig_is_missing_not_unreadable() -> None:
    thread = _thread(
        _message("m1", "logo.png", mime_type="image/png"),
        _message("m2", "INV_A-2026-01.pdf"),
    )

    lookup, gmail, extractor = _find(thread, "m2")

    assert lookup.status is TigLookupStatus.MISSING
    assert lookup.is_missing and not lookup.found and not lookup.needs_review
    assert lookup.flag_reason is FlagReason.MISSING_TIG
    assert lookup.document is None
    assert gmail.downloads == [] and extractor.calls == []


def test_invoice_alone_in_thread_is_missing() -> None:
    thread = _thread(_message("m1", "INV_A-2026-01.pdf"))

    assert _find(thread, "m1")[0].status is TigLookupStatus.MISSING


def test_tig_attached_to_the_invoice_message_itself_is_not_used() -> None:
    # Only messages earlier than the invoice's count, so the invoice can never be its own TIG.
    thread = _thread(_message("m1", "INV_A-2026-01.pdf", "TIG-2026-09-KEK-A.pdf"))

    lookup, _, extractor = _find(thread, "m1")

    assert lookup.status is TigLookupStatus.MISSING
    assert extractor.calls == []


def test_tig_sent_after_the_invoice_is_ignored() -> None:
    thread = _thread(_message("m1", "INV_A-2026-01.pdf"), _message("m2", "TIG-2026-09-KEK-A.pdf"))

    assert _find(thread, "m1")[0].status is TigLookupStatus.MISSING


def test_later_arriving_tig_before_a_later_invoice_message_is_found() -> None:
    # USR-005-04 AC4 shape: invoice re-evaluated after a TIG arrives -- but only if earlier.
    thread = _thread(
        _message("m1", "TIG-2026-09-KEK-A.pdf", internal_date=1),
        _message("m2", "INV_A-2026-01.pdf", internal_date=2),
    )

    lookup, _, _ = _find(thread, "m2", StageResult.ok(_tig_extraction(KEK_A_TIG_LINES, None)))

    assert lookup.found


def test_message_with_a_later_internal_date_is_not_earlier_even_if_listed_first() -> None:
    thread = _thread(
        _message("m1", "TIG-2026-09-KEK-A.pdf", internal_date=500),
        _message("m2", "INV_A-2026-01.pdf", internal_date=100),
    )

    assert _find(thread, "m2")[0].status is TigLookupStatus.MISSING


def test_unknown_invoice_message_id_is_an_error() -> None:
    thread = _thread(_message("m1", "TIG-2026-09-KEK-A.pdf"))

    with pytest.raises(ValueError, match="m-unknown"):
        _find(thread, "m-unknown")


def test_latest_earlier_tig_wins_over_an_older_one() -> None:
    thread = _thread(
        _message("m1", "TIG-2026-09-KEK-C.pdf", internal_date=1),
        _message("m2", "TIG-2026-09-KEK-C-javitott.pdf", internal_date=2),
        _message("m3", "INV-C-2026-02.pdf", internal_date=3),
    )

    lookup, gmail, extractor = _find(thread, "m3", StageResult.ok(_tig_extraction()))

    assert lookup.document is not None
    assert lookup.document.message_id == "m2"
    assert gmail.downloads == ["m2"]
    assert len(extractor.calls) == 1


def test_two_tig_attachments_in_the_same_message_are_ambiguous() -> None:
    thread = _thread(
        _message("m1", "TIG-2026-09-KEK-C.pdf", "TIG-2026-09-PIROS-D.pdf"),
        _message("m2", "INV-C-2026-02.pdf"),
    )

    lookup, _, extractor = _find(thread, "m2")

    assert lookup.status is TigLookupStatus.AMBIGUOUS
    assert lookup.needs_review and not lookup.is_missing
    assert lookup.flag_reason is FlagReason.INCOMPLETE_DATA
    assert lookup.message_id == "m1"
    assert lookup.detail is not None and "2" in lookup.detail
    assert extractor.calls == []


def test_unreadable_latest_tig_does_not_fall_back_to_an_older_one() -> None:
    thread = _thread(
        _message("m1", "TIG-old.pdf", internal_date=1),
        _message("m2", "TIG-new.pdf", internal_date=2),
        _message("m3", "INV.pdf", internal_date=3),
    )

    lookup, _, extractor = _find(
        thread, "m3", StageResult.incomplete(FlagReason.INCOMPLETE_DATA, "missing line_items")
    )

    assert lookup.status is TigLookupStatus.UNREADABLE
    assert lookup.message_id == "m2"
    assert len(extractor.calls) == 1


@pytest.mark.parametrize(
    "filename",
    [
        "TIG-2026-09-KEK-A.pdf",
        "tig_2026_09.pdf",
        "TIG.pdf",
        "Kek_TIG_szeptember.pdf",
        "Teljesítésigazolás szeptember.pdf",
        "teljesitesigazolas.PDF",
        "TELJESÍTÉSIGAZOLÁS-KEK.pdf",
    ],
)
def test_tig_filenames_are_recognised(filename: str) -> None:
    assert is_tig_filename(filename)


@pytest.mark.parametrize(
    "filename",
    ["INV_A-2026-01.pdf", "antigen.pdf", "prestige.pdf", "contiguous.pdf", "szamla.pdf", ""],
)
def test_non_tig_filenames_are_not_recognised(filename: str) -> None:
    assert not is_tig_filename(filename)


def test_subject_indicator_identifies_tig_when_filename_does_not() -> None:
    thread = _thread(
        _message("m1", "igazolas_szeptember.pdf", subject="Teljesítésigazolás – Kék, szeptember"),
        _message("m2", "INV-C-2026-02.pdf", subject="Re: Teljesítésigazolás – Kék, szeptember"),
    )

    lookup, _, extractor = _find(thread, "m2", StageResult.ok(_tig_extraction()))

    assert lookup.found
    assert extractor.calls[0][0] == b"m1/igazolas_szeptember.pdf"


def test_subject_indicator_fallback_skips_a_copy_of_the_invoice_attachment() -> None:
    # An earlier forward of the same invoice file must not be mistaken for the TIG.
    thread = _thread(
        _message("m1", "INV-C-2026-02.pdf", subject="TIG szeptember"),
        _message("m2", "INV-C-2026-02.pdf", subject="Re: TIG szeptember"),
    )

    assert _find(thread, "m2")[0].status is TigLookupStatus.MISSING


def test_subject_indicator_matches_whole_terms_only() -> None:
    thread = _thread(
        _message("m1", "doc.pdf", subject="Contiguous prestige"),
        _message("m2", "INV.pdf"),
    )

    assert _find(thread, "m2")[0].status is TigLookupStatus.MISSING


def test_filename_match_takes_precedence_over_subject_indicator() -> None:
    thread = _thread(
        _message("m1", "TIG-2026-09-KEK-C.pdf", internal_date=1),
        _message("m2", "melleklet.pdf", subject="TIG kiegészítés", internal_date=2),
        _message("m3", "INV.pdf", internal_date=3),
    )

    lookup, _, _ = _find(thread, "m3", StageResult.ok(_tig_extraction()))

    assert lookup.document is not None
    assert lookup.document.message_id == "m1"


def test_non_document_attachments_under_a_tig_subject_are_ignored() -> None:
    thread = _thread(
        _message("m1", "signature.ics", subject="TIG", mime_type="text/calendar"),
        _message("m2", "INV.pdf"),
    )

    assert _find(thread, "m2")[0].status is TigLookupStatus.MISSING


def test_image_tig_is_extracted_with_its_mime_type() -> None:
    thread = _thread(
        _message("m1", "TIG-scan.jpg", mime_type="image/jpeg"), _message("m2", "INV.pdf")
    )

    lookup, _, extractor = _find(thread, "m2", StageResult.ok(_tig_extraction()))

    assert lookup.found
    assert extractor.calls[0][1] == "image/jpeg"


# --- find_tig: a TIG that is there but unusable is NOT "no TIG" ----------------------------


def _unreadable(
    *outcomes: StageResult[InvoiceExtraction] | Exception,
    filename: str = "TIG-2026-09-KEK-C.pdf",
    mime_type: str = PDF,
    config: Config | None = None,
) -> TigLookup:
    thread = _thread(_message("m1", filename, mime_type=mime_type), _message("m2", "INV.pdf"))
    lookup, _, _ = _find(thread, "m2", *outcomes, config=config)
    return lookup


def _assert_unreadable(lookup: TigLookup, detail_fragment: str) -> None:
    assert lookup.status is TigLookupStatus.UNREADABLE
    assert lookup.needs_review and not lookup.is_missing and not lookup.found
    assert lookup.flag_reason is FlagReason.INCOMPLETE_DATA
    assert lookup.document is None
    assert lookup.message_id == "m1"
    assert lookup.detail is not None and detail_fragment in lookup.detail


def test_tig_failing_extraction_validation_is_unreadable() -> None:
    lookup = _unreadable(
        StageResult.incomplete(FlagReason.INCOMPLETE_DATA, "Gemini response rejected")
    )

    _assert_unreadable(lookup, "Gemini response rejected")
    assert lookup.filename == "TIG-2026-09-KEK-C.pdf"


def test_tig_without_line_items_is_unreadable() -> None:
    extraction = _tig_extraction(lines=())
    lookup = _unreadable(
        StageResult[InvoiceExtraction](
            StageStatus.INCOMPLETE,
            value=extraction,
            flag_reason=FlagReason.INCOMPLETE_DATA,
            detail="missing required field(s): line_items",
        )
    )

    _assert_unreadable(lookup, "line_items")


def test_tig_extraction_error_result_is_unreadable() -> None:
    _assert_unreadable(_unreadable(StageResult.failed("boom")), "boom")


@pytest.mark.parametrize(
    "bad_line",
    [
        _line("Nyomdai szolgáltatás", None, "0.40", "380.00"),
        _line("Nyomdai szolgáltatás", "950", None, "380.00"),
    ],
)
def test_tig_line_with_unreadable_quantity_or_unit_price_is_unreadable(bad_line: LineItem) -> None:
    extraction = _tig_extraction(lines=(bad_line, KEK_C_TIG_LINES[1]))

    _assert_unreadable(_unreadable(StageResult.ok(extraction)), "line 0")


def test_tig_with_unparseable_printed_total_is_unreadable() -> None:
    extraction = _tig_extraction(net="kb. 989 EUR")

    _assert_unreadable(_unreadable(StageResult.ok(extraction)), "total")


def test_tig_named_attachment_with_unsupported_type_is_unreadable_not_missing() -> None:
    lookup = _unreadable(
        filename="TIG-2026-09-KEK-C.docx",
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )

    _assert_unreadable(lookup, "MIME")


def test_tig_named_attachment_outside_the_config_allowlist_is_unreadable() -> None:
    lookup = _unreadable(
        filename="TIG-scan.png",
        mime_type="image/png",
        config=_config(attachment_mime_allowlist=[PDF]),
    )

    _assert_unreadable(lookup, "MIME")


def test_extractor_rejecting_the_document_is_unreadable() -> None:
    _assert_unreadable(_unreadable(UnsupportedDocumentError("document is empty")), "empty")


def test_attachment_missing_from_download_is_unreadable() -> None:
    thread = _thread(_message("m1", "TIG-1.pdf"), _message("m2", "INV.pdf"))

    class EmptyGmail(FakeGmail):
        def download_attachments(self, message_id: str) -> list[AttachmentContent]:
            self.downloads.append(message_id)
            return []

    lookup = find_tig(
        thread,
        "m2",
        route=Route.TIG,
        gmail=EmptyGmail(thread),
        extractor=FakeExtractor([]),
        config=_config(),
    )

    _assert_unreadable(lookup, "download")


def test_gemini_or_api_errors_propagate_for_retry() -> None:
    thread = _thread(_message("m1", "TIG-1.pdf"), _message("m2", "INV.pdf"))

    with pytest.raises(RuntimeError, match="503"):
        _find(thread, "m2", RuntimeError("503 Service Unavailable"))


def test_gmail_download_errors_propagate_for_retry() -> None:
    thread = _thread(_message("m1", "TIG-1.pdf"), _message("m2", "INV.pdf"))

    class FailingGmail(FakeGmail):
        def download_attachments(self, message_id: str) -> list[AttachmentContent]:
            raise ConnectionError("gmail down")

    with pytest.raises(ConnectionError):
        find_tig(
            thread,
            "m2",
            route=Route.TIG,
            gmail=FailingGmail(thread),
            extractor=FakeExtractor([]),
            config=_config(),
        )


# --- TigLookup / TigDocument contracts ------------------------------------------------------


def test_tig_lookup_constructors_enforce_their_invariants() -> None:
    document = _tig(KEK_A_TIG_LINES)

    assert TigLookup.found_document(document).document is document
    assert TigLookup.missing().document is None
    with pytest.raises(ValueError):
        TigLookup(TigLookupStatus.FOUND)
    with pytest.raises(ValueError):
        TigLookup(TigLookupStatus.MISSING, document=document)
    with pytest.raises(ValueError):
        TigLookup(TigLookupStatus.UNREADABLE)


def test_tig_document_rejects_float_totals_and_freezes_lines() -> None:
    with pytest.raises(TypeError):
        TigDocument(message_id="m", filename="f", lines=(), total_net=0.5)  # type: ignore[arg-type]

    document = TigDocument(message_id="m", filename="f", lines=list(KEK_A_TIG_LINES))  # type: ignore[arg-type]

    assert document.lines == KEK_A_TIG_LINES
