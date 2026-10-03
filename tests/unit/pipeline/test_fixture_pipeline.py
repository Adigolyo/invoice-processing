"""Task 21: a full in-memory pipeline run over real TIG/invoice fixture pairs.

The extractor is faked from ``tests/fixtures/tig_pairs/answer_key.json`` (the values a
correct extraction would return, as printed), keyed by the real fixture PDF bytes; Gmail,
Drive and Sheets are in-memory fakes. Three journeys run in one cycle:

- a HUF invoice from a non-contractor sender (direct route) -> Processed;
- a contractor invoice whose earlier TIG matches -> Processed, no draft;
- a contractor invoice whose earlier TIG disagrees -> Pending, with one draft.

A second cycle then proves the run is idempotent (no new rows, files, drafts or labels).
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from intake.models import InvoiceExtraction, LineItem
from intake.pipeline.orchestrator import PipelineClients, run_cycle
from tests.unit.pipeline.fakes import (
    LABELS,
    FakeDrive,
    FakeExtractor,
    FakeGmail,
    FakeSheets,
    make_config,
    ok,
)

PAIRS_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "tig_pairs"
KEY: dict[str, Any] = json.loads((PAIRS_DIR / "answer_key.json").read_text(encoding="utf-8"))
PAIRS = {p["pair"]: p for p in KEY["pairs"]}

DIRECT = "TIG-2026-09-KEK-A__INV_A-2026-01"  # A Kft., HUF, used as a supplier invoice
MATCH = "TIG-2026-09-ZOLD-B__INV-B_2026_01"  # B Kft., HUF, multi-line TIG, match
MISMATCH = "TIG-2026-09-KEK-C__INV-C-2026-02"  # C Kft., EUR, unit price + total mismatch
PDF = "application/pdf"


def _lines(lines: list[dict[str, Any]]) -> tuple[LineItem, ...]:
    return tuple(
        LineItem(
            description=ln["description"],
            quantity=Decimal(ln["quantity"]),
            unit_price=Decimal(ln["unit_price"]),
            net=Decimal(ln["net"]),
        )
        for ln in lines
    )


def _invoice(pair: dict[str, Any]) -> InvoiceExtraction:
    inv = pair["invoice"]
    return InvoiceExtraction(
        supplier=inv["supplier"],
        invoice_number=inv["invoice_number"],
        currency=inv["currency_raw"],
        net=inv["net_raw"],
        gross=inv["gross_raw"],
        due_date=inv["due_date_raw"],
        issue_date=inv["issue_date_raw"],
        supply_date=inv["supply_date_raw"],
        performance_date=inv["performance_date_raw"],
        supplier_country=inv["supplier_country"],
        line_items=_lines(inv["lines"]),
    )


def _tig(pair: dict[str, Any]) -> InvoiceExtraction:
    tig = pair["tig"]
    return InvoiceExtraction(
        supplier=tig["contractor"],
        invoice_number=tig["number"],
        currency=tig["currency"],
        net=tig["total_net_raw"],
        supplier_country="HU",
        line_items=_lines(tig["lines"]),
    )


def _bytes(pair_id: str, filename: str) -> bytes:
    return (PAIRS_DIR / pair_id / filename).read_bytes()


def _contractor(pair: dict[str, Any]) -> str:
    letter = pair["invoice"]["supplier"][0].lower()
    return f"{pair['invoice']['supplier']} <szamla@{letter}-kft.example>"


def test_fixture_pairs_end_in_their_documented_states() -> None:
    config = make_config(contractor_identifiers="b-kft.example, c-kft.example")
    gmail, drive, sheets, extractor = FakeGmail(), FakeDrive(), FakeSheets(config), FakeExtractor()

    direct = PAIRS[DIRECT]
    direct_pdf = _bytes(DIRECT, direct["invoice"]["file"])
    gmail.add_message(
        "direct",
        sender="A Kft. <invoices@a-kft-billing.example>",
        subject=f"Számla {direct['invoice']['invoice_number']}",
        files=[(direct["invoice"]["file"], PDF, direct_pdf)],
    )
    extractor.answers[direct_pdf] = ok(_invoice(direct))

    for pair_id, thread in ((MATCH, "t-match"), (MISMATCH, "t-mismatch")):
        pair = PAIRS[pair_id]
        tig_pdf = _bytes(pair_id, pair["tig"]["file"])
        inv_pdf = _bytes(pair_id, pair["invoice"]["file"])
        gmail.add_message(
            f"{thread}-tig",
            thread_id=thread,
            sender="Projektvezető <pm@kibit.example>",
            subject=f"TIG {pair['tig']['number']}",
            files=[(pair["tig"]["file"], PDF, tig_pdf)],
            labels=("INBOX",),
        )
        gmail.add_message(
            f"{thread}-inv",
            thread_id=thread,
            sender=_contractor(pair),
            subject=f"Re: TIG {pair['tig']['number']} - számla",
            files=[(pair["invoice"]["file"], PDF, inv_pdf)],
        )
        extractor.answers[tig_pdf] = ok(_tig(pair))
        extractor.answers[inv_pdf] = ok(_invoice(pair))

    def cycle() -> dict[str, int]:
        summary = run_cycle(
            sheets, lambda _: PipelineClients(gmail=gmail, drive=drive, extractor=extractor)
        )
        return summary.to_dict()

    assert cycle() == {
        "processed": 2,
        "pending": 1,
        "needs_review": 0,
        "awaiting_tig": 0,
        "errors": 0,
        "skipped": 0,
    }

    # Final labels and read state.
    assert gmail.kibit_labels_of("direct") == {LABELS.processed}
    assert gmail.kibit_labels_of("t-match-inv") == {LABELS.processed}
    assert gmail.kibit_labels_of("t-mismatch-inv") == {LABELS.pending}
    assert not any(gmail.is_unread(m) for m in ("direct", "t-match-inv", "t-mismatch-inv"))
    assert gmail.writes_for("t-match-tig") == gmail.writes_for("t-mismatch-tig") == []

    # Drive: one renamed copy each, in the performance month's folder, in run order.
    assert drive.filenames("2610") == [
        "2610_001_AKFT.pdf",
        "2610_002_BKFT.pdf",
        "2610_003_CKFT.pdf",
    ]

    # Ledger rows match the answer key's expected ledger values.
    rows = {row.inv_id_ext: row for row in sheets.rows}
    assert len(sheets.rows) == 3
    for pair_id, number, inv_type in (
        (DIRECT, "2610_001_AKFT", "direct"),
        (MATCH, "2610_002_BKFT", "tig"),
        (MISMATCH, "2610_003_CKFT", "tig"),
    ):
        expected = PAIRS[pair_id]["invoice"]["expected_ledger"]
        row = rows[expected["inv_id_ext"]]
        assert row.inv_id_int == number
        assert row.inv_type == inv_type
        assert row.provider == expected["provider"]
        assert row.currency == expected["currency"]
        assert row.net == Decimal(expected["net"])
        assert row.gross == Decimal(expected["gross"])
        assert row.due_date.strftime("%Y.%m.%d") == expected["due_date"]
        assert row.due_date == date(2026, 11, 4)

    # Exactly one draft, on the mismatch thread, naming the unit-price discrepancy.
    ((thread_id, body),) = gmail.drafts
    assert thread_id == "t-mismatch"
    assert "egységár" in body
    assert "nettó végösszeg" in body.lower()

    # Second cycle: nothing left to do, nothing duplicated.
    writes = list(gmail.writes)
    assert cycle()["processed"] == 0
    assert gmail.writes == writes
    assert len(sheets.rows) == 3
    assert drive.uploads == 3
    assert len(gmail.drafts) == 1
