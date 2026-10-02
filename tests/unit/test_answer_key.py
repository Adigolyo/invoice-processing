"""Internal consistency of the TIG/invoice pair answer key (tests/fixtures/tig_pairs).

The answer key is hand-transcribed from the fixture PDFs; these checks catch typos
(arithmetic that no longer adds up, a discrepancy list that contradicts the expected
outcome, a file that was renamed) without parsing any PDF.
"""

import json
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
PAIRS_DIR = FIXTURES / "tig_pairs"
ANSWER_KEY = PAIRS_DIR / "answer_key.json"
SERIES_KEY = FIXTURES / "invoice_series" / "answer_key.json"


def _load(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        data: dict[str, Any] = json.load(fh)
    return data


KEY = _load(ANSWER_KEY)
PAIRS: list[dict[str, Any]] = KEY["pairs"]
PAIR_IDS = [p["pair"] for p in PAIRS]


def _d(value: str) -> Decimal:
    return Decimal(value)


def _lines(pair: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [("tig", ln) for ln in pair["tig"]["lines"]] + [
        ("invoice", ln) for ln in pair["invoice"]["lines"]
    ]


# --- whole-key counts --------------------------------------------------------


def test_answer_key_has_32_unique_pairs_matching_the_folders() -> None:
    folders = sorted(p.name for p in PAIRS_DIR.iterdir() if p.is_dir())
    assert len(PAIRS) == 32
    assert sorted(PAIR_IDS) == folders


def test_outcome_and_kind_counts_match_the_summary_table() -> None:
    assert Counter(p["expected_outcome"] for p in PAIRS) == {"match": 25, "mismatch": 7}
    assert Counter(p["tig_kind"] for p in PAIRS) == {"single_line": 20, "multi_line": 12}
    assert Counter((p["tig_kind"], p["expected_outcome"]) for p in PAIRS) == {
        ("single_line", "match"): 17,
        ("single_line", "mismatch"): 3,
        ("multi_line", "match"): 8,
        ("multi_line", "mismatch"): 4,
    }


def test_no_unresolved_transcription_notes() -> None:
    assert KEY["notes"] == []


# --- per pair ----------------------------------------------------------------


@pytest.mark.parametrize("pair", PAIRS, ids=PAIR_IDS)
def test_referenced_files_exist(pair: dict[str, Any]) -> None:
    folder = PAIRS_DIR / pair["pair"]
    assert (folder / pair["tig"]["file"]).is_file()
    assert (folder / pair["invoice"]["file"]).is_file()
    assert pair["tig"]["file"] == f"{pair['tig']['number']}.pdf"


@pytest.mark.parametrize("pair", PAIRS, ids=PAIR_IDS)
def test_tig_kind_matches_line_count_and_total_row(pair: dict[str, Any]) -> None:
    tig = pair["tig"]
    if pair["tig_kind"] == "single_line":
        assert len(tig["lines"]) == 1
        assert tig["total_net_raw"] is None and tig["total_net"] is None
    else:
        assert 2 <= len(tig["lines"]) <= 3
        assert tig["total_net_raw"] is not None


@pytest.mark.parametrize("pair", PAIRS, ids=PAIR_IDS)
def test_line_net_equals_quantity_times_unit_price(pair: dict[str, Any]) -> None:
    for side, line in _lines(pair):
        assert _d(line["quantity"]) * _d(line["unit_price"]) == _d(line["net"]), (side, line)


@pytest.mark.parametrize("pair", PAIRS, ids=PAIR_IDS)
def test_totals_equal_sum_of_lines(pair: dict[str, Any]) -> None:
    tig, inv = pair["tig"], pair["invoice"]
    if tig["total_net"] is not None:
        assert sum(_d(ln["net"]) for ln in tig["lines"]) == _d(tig["total_net"])
    assert sum(_d(ln["net"]) for ln in inv["lines"]) == _d(inv["net"])
    if inv["vat"] is None:
        assert inv["vat_exempt_aam"] is True
        assert inv["gross"] == inv["net"]
    else:
        assert _d(inv["net"]) + _d(inv["vat"]) == _d(inv["gross"])
        assert sum(_d(ln["vat"]) for ln in inv["lines"]) == _d(inv["vat"])
        for line in inv["lines"]:
            assert _d(line["net"]) + _d(line["vat"]) == _d(line["gross"])


@pytest.mark.parametrize("pair", PAIRS, ids=PAIR_IDS)
def test_discrepancies_empty_iff_match(pair: dict[str, Any]) -> None:
    has_discrepancies = bool(pair["discrepancies"])
    assert has_discrepancies == (pair["expected_outcome"] == "mismatch")


@pytest.mark.parametrize("pair", PAIRS, ids=PAIR_IDS)
def test_discrepancies_agree_with_the_transcribed_lines(pair: dict[str, Any]) -> None:
    tig_lines, inv_lines = pair["tig"]["lines"], pair["invoice"]["lines"]
    assert len(tig_lines) == len(inv_lines)
    expected = []
    for index, (inv, tig) in enumerate(zip(inv_lines, tig_lines, strict=True)):
        for field in ("quantity", "unit_price"):
            if _d(inv[field]) != _d(tig[field]):
                expected.append((index, field, inv[field], tig[field]))
    total = pair["tig"]["total_net"]
    if total is not None and _d(pair["invoice"]["net"]) != _d(total):
        expected.append((None, "total_net", pair["invoice"]["net"], total))
    actual = [
        (d["line_index"], d["field"], d["invoice_value"], d["tig_value"])
        for d in pair["discrepancies"]
    ]
    assert actual == expected


@pytest.mark.parametrize("pair", PAIRS, ids=PAIR_IDS)
def test_expected_ledger_is_consistent_with_the_invoice(pair: dict[str, Any]) -> None:
    inv = pair["invoice"]
    ledger = inv["expected_ledger"]
    assert ledger["provider"] == inv["supplier"]
    assert ledger["inv_id_ext"] == inv["invoice_number"]
    assert ledger["currency"] == inv["currency"] == pair["tig"]["currency"]
    assert ledger["gross"] == inv["gross"]
    assert ledger["net"] == inv["net"]
    if inv["currency"] == "HUF":
        assert ledger["net"].isdigit() and ledger["gross"].isdigit()
    assert ledger["performance_date"] == inv["performance_date_raw"].rstrip(".")
    assert ledger["due_date"] == inv["due_date_raw"].rstrip(".")
    perf = ledger["performance_date"]
    assert ledger["yymm"] == perf[2:4] + perf[5:7]


# --- local-only invoice series -------------------------------------------------


@pytest.mark.skipif(not SERIES_KEY.exists(), reason="local-only invoice_series fixtures absent")
def test_local_invoice_series_key_holds_no_buyer_fields() -> None:
    series = _load(SERIES_KEY)["invoices"]
    assert len(series) == 9
    forbidden = {"buyer", "billed_to", "purchaser_email", "customer_tax_id", "transaction_id"}
    for invoice in series:
        assert forbidden.isdisjoint(invoice)
        assert invoice["supplier_country"] == "IE"
        assert _d(invoice["net"]) == sum(_d(ln["net"]) for ln in invoice["lines"])
