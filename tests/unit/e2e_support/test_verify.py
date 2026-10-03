"""Pure helpers of the Task 24 E2E fixture suite (``tests/e2e/fixture_set/verify.py``).

The live suite only gathers Workspace state into a JSON-able snapshot; every judgement
about that snapshot is made here, so it is unit-tested without any network: a snapshot
built to be exactly right passes, and each kind of wrong state is reported against the
right pair (all failures collected, never only the first).
"""

from __future__ import annotations

import copy
import dataclasses
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from intake.normalization.amounts import format_hungarian_amount
from tests.e2e.fixture_set import verify
from tests.e2e.fixture_set.verify import (
    LABELS,
    PairExpectation,
    answer_key_problems,
    contractor_domain,
    draft_problems,
    evaluate_pairs,
    format_table,
    global_problems,
    idempotency_problems,
    load_answer_key,
    message_id_for,
    month_sequence_problems,
    run_id_from_title,
    spreadsheet_title,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "tig_pairs"
ANSWER_KEY = FIXTURES / "answer_key.json"
RUN_ID = "20261002-120000"

PAIRS = load_answer_key(ANSWER_KEY)
BY_NAME = {p.pair: p for p in PAIRS}
MISMATCH = "TIG-2026-09-KEK-C__INV-C-2026-02"  # unit price + total net
QTY_MISMATCH = "TIG-2026-10-MAGENTA-J__INV_J_2026_03_MAGENTA"  # quantity 2 vs 1
MATCH = "TIG-2026-09-KEK-A__INV_A-2026-01"


def _draft_body(pair: PairExpectation) -> str:
    lines = []
    for d in pair.discrepancies:
        if d.field == "quantity":
            inv = format_hungarian_amount(Decimal(d.invoice_value).normalize())
            tig = format_hungarian_amount(Decimal(d.tig_value).normalize())
        else:
            inv = f"{format_hungarian_amount(Decimal(d.invoice_value))} {pair.currency}"
            tig = f"{format_hungarian_amount(Decimal(d.tig_value))} {pair.currency}"
        lines.append(f"- {d.field}: a számlán {inv}, a teljesítésigazoláson {tig}")
    return "Tisztelt Partnerünk!\n\n" + "\n".join(lines) + "\n\nÜdvözlettel:\n"


def perfect_snapshot() -> dict[str, Any]:
    """The Workspace state a fully correct first run leaves behind."""
    seq: dict[str, int] = {}
    ledger: list[list[Any]] = []
    drive: dict[str, list[str]] = {}
    messages: dict[str, Any] = {}
    candidates: list[dict[str, Any]] = []
    for i, p in enumerate(PAIRS):
        seq[p.yymm] = seq.get(p.yymm, 0) + 1
        registry = f"{p.yymm}_{seq[p.yymm]:03d}_{p.supplier8}"
        net: Any = int(p.net) if p.net == p.net.to_integral_value() else float(p.net)
        gross: Any = int(p.gross) if p.gross == p.gross.to_integral_value() else float(p.gross)
        ledger.append(
            [registry, p.provider, p.inv_id_ext, "tig", p.currency, net, gross, p.due_date]
        )
        drive.setdefault(p.yymm, []).append(f"{registry}.pdf")
        label = LABELS["processed"] if p.outcome == "match" else LABELS["pending"]
        drafts = []
        if p.outcome == "mismatch":
            drafts.append({"to": f"szamlazas@{p.contractor_domain}", "body": _draft_body(p)})
        messages[p.pair] = {
            "invoice": {"id": f"inv{i}", "thread_id": f"t{i}", "labels": [label], "unread": False},
            "tig": {"id": f"tig{i}", "thread_id": f"t{i}", "labels": [], "unread": True},
            "drafts": drafts,
        }
        candidates.append(
            {
                "message_id": f"inv{i}",
                "status": "processed" if p.outcome == "match" else "pending",
                "reason": "ok",
                "registry_number": registry,
            }
        )
        candidates.append(
            {
                "message_id": f"tig{i}",
                "status": "skipped",
                "reason": "tig_document_only",
                "registry_number": None,
            }
        )
    candidates.append(  # a demo mail outside this run: reported, not judged
        {"message_id": "demo", "status": "awaiting_tig", "reason": "missing_tig"}
    )
    return {
        "run_id": RUN_ID,
        "ledger": ledger,
        "drive": {"folders": drive, "root_files": []},
        "messages": messages,
        "summary": {"candidates": candidates},
    }


def _verdict(snapshot: dict[str, Any], pair: str) -> verify.PairVerdict:
    return {v.pair: v for v in evaluate_pairs(PAIRS, snapshot)}[pair]


def _row(snapshot: dict[str, Any], pair: str) -> list[Any]:
    inv = BY_NAME[pair].inv_id_ext
    return next(r for r in snapshot["ledger"] if r[2] == inv)


# --- answer key ---------------------------------------------------------------------------------


def test_answer_key_loads_32_pairs_with_derived_fields() -> None:
    assert len(PAIRS) == 32
    p = BY_NAME[MISMATCH]
    assert p.outcome == "mismatch"
    assert p.contractor_domain == "c-kft.example"
    assert p.supplier8 == "CKFT"
    assert p.yymm == "2610"
    assert p.net == Decimal("1073.00") and p.gross == Decimal("1362.71")
    assert p.tig_number == "TIG-2026-09-KEK-C"
    assert [d.field for d in p.discrepancies] == ["unit_price", "total_net"]


def test_committed_answer_key_has_no_problems() -> None:
    assert answer_key_problems(PAIRS, FIXTURES) == []


def test_answer_key_problems_are_all_reported() -> None:
    broken = list(PAIRS)
    broken[0] = dataclasses.replace(broken[0], outcome="mismatch")  # mismatch without discrepancies
    broken[1] = dataclasses.replace(broken[1], invoice_file="missing.pdf")
    problems = answer_key_problems(broken, FIXTURES)
    assert any("25 match" in p for p in problems)
    assert any("no discrepancies" in p and broken[0].pair in p for p in problems)
    assert any("missing.pdf" in p for p in problems)


@pytest.mark.parametrize(
    ("supplier", "domain"), [("A Kft.", "a-kft.example"), ("K Kft.", "k-kft.example")]
)
def test_contractor_domain(supplier: str, domain: str) -> None:
    assert contractor_domain(supplier) == domain


@pytest.mark.parametrize("supplier", ["Acme Kft.", "A Zrt.", "", "a Kft."])
def test_contractor_domain_rejects_other_names(supplier: str) -> None:
    with pytest.raises(ValueError, match="contractor"):
        contractor_domain(supplier)


# --- naming -------------------------------------------------------------------------------------


def test_message_ids_are_unique_per_run_pair_and_kind() -> None:
    ids = {message_id_for(RUN_ID, p.pair, k) for p in PAIRS for k in ("tig", "invoice")}
    assert len(ids) == 64
    mid = message_id_for(RUN_ID, "TIG-2026-09-MAGENTA-G__INV.G.2026.01_MAGENTA", "invoice")
    assert mid.startswith("<e2e-20261002-120000-") and mid.endswith("@kibit-e2e.example>")
    assert message_id_for("20261002-120001", MATCH, "tig") != message_id_for(RUN_ID, MATCH, "tig")


def test_run_id_round_trips_through_the_spreadsheet_title() -> None:
    assert run_id_from_title(spreadsheet_title(RUN_ID)) == RUN_ID
    assert run_id_from_title("Kibit invoice ledger (sandbox)") is None


# --- per-pair verdicts --------------------------------------------------------------------------


def test_perfect_snapshot_passes_every_check() -> None:
    snapshot = perfect_snapshot()
    verdicts = evaluate_pairs(PAIRS, snapshot)
    assert [v.failures for v in verdicts if v.failures] == []
    assert all(v.passed for v in verdicts)
    assert global_problems(PAIRS, snapshot) == []
    v = _verdict(snapshot, MISMATCH)
    assert (v.label, v.ledger, v.drive, v.draft) == ("Pending", "ok", "ok", "ok")
    assert v.registry_number is not None and v.registry_number.endswith("CKFT")
    assert _verdict(snapshot, MATCH).draft == "none (ok)"


def test_wrong_label_and_unread_are_reported() -> None:
    snapshot = perfect_snapshot()
    snapshot["messages"][MATCH]["invoice"].update(labels=[LABELS["pending"]], unread=True)
    v = _verdict(snapshot, MATCH)
    assert not v.passed
    assert v.label == "Pending, unread"
    assert any("expected Processed" in f for f in v.failures)
    assert any("still unread" in f for f in v.failures)


def test_missing_message_is_reported() -> None:
    snapshot = perfect_snapshot()
    del snapshot["messages"][MATCH]
    v = _verdict(snapshot, MATCH)
    assert not v.passed and v.label == "-"


def test_ledger_value_mismatches_are_all_reported() -> None:
    snapshot = perfect_snapshot()
    row = _row(snapshot, MISMATCH)
    row[1], row[4], row[5], row[6], row[7] = "C Kft", "HUF", 1073.01, 1362, "2026.11.05"
    v = _verdict(snapshot, MISMATCH)
    assert v.ledger.startswith("WRONG")
    joined = " | ".join(v.failures)
    for column in ("Provider", "Currency", "Net", "Gross", "Due date"):
        assert column in joined


def test_amounts_compare_numerically_and_dates_accept_serials() -> None:
    snapshot = perfect_snapshot()
    row = _row(snapshot, MISMATCH)
    row[5], row[6] = "1073", Decimal("1362.710")
    row[7] = 46330  # Sheets serial for 2026.11.04
    assert _verdict(snapshot, MISMATCH).passed


def test_inv_type_must_be_the_tig_route() -> None:
    snapshot = perfect_snapshot()
    _row(snapshot, MATCH)[3] = "direct"
    assert any("INV_type" in f for f in _verdict(snapshot, MATCH).failures)


def test_missing_and_duplicate_ledger_rows() -> None:
    snapshot = perfect_snapshot()
    snapshot["ledger"] = [r for r in snapshot["ledger"] if r[2] != BY_NAME[MATCH].inv_id_ext]
    assert _verdict(snapshot, MATCH).ledger == "missing"

    snapshot = perfect_snapshot()
    snapshot["ledger"].append(list(_row(snapshot, MATCH)))
    v = _verdict(snapshot, MATCH)
    assert v.ledger == "2 rows" and not v.passed


def test_registry_number_shape_is_checked() -> None:
    snapshot = perfect_snapshot()
    row = _row(snapshot, MISMATCH)
    good = row[0]
    row[0] = "2611" + good[4:]  # wrong month
    v = _verdict(snapshot, MISMATCH)
    assert any("registry number" in f for f in v.failures)

    row[0] = good[:9] + "CKFTX"
    assert any("registry number" in f for f in _verdict(snapshot, MISMATCH).failures)


def test_missing_drive_file_is_reported() -> None:
    snapshot = perfect_snapshot()
    registry = _row(snapshot, MATCH)[0]
    snapshot["drive"]["folders"]["2610"].remove(f"{registry}.pdf")
    v = _verdict(snapshot, MATCH)
    assert v.drive == "missing" and not v.passed


def test_draft_checks() -> None:
    snapshot = perfect_snapshot()
    snapshot["messages"][MISMATCH]["drafts"] = []
    assert _verdict(snapshot, MISMATCH).draft == "missing"

    snapshot = perfect_snapshot()
    snapshot["messages"][MATCH]["drafts"] = [{"to": "x", "body": "y"}]
    v = _verdict(snapshot, MATCH)
    assert v.draft == "UNEXPECTED (1)" and not v.passed

    snapshot = perfect_snapshot()
    drafts = snapshot["messages"][MISMATCH]["drafts"]
    drafts.append(dict(drafts[0]))
    assert _verdict(snapshot, MISMATCH).draft == "2 drafts"

    snapshot = perfect_snapshot()
    snapshot["messages"][MISMATCH]["drafts"][0]["to"] = "someone@else.example"
    assert any("addressed" in f for f in _verdict(snapshot, MISMATCH).failures)


def test_summary_status_of_the_invoice_message_is_checked() -> None:
    snapshot = perfect_snapshot()
    for c in snapshot["summary"]["candidates"]:
        if c["message_id"] == snapshot["messages"][MATCH]["invoice"]["id"]:
            c["status"], c["reason"] = "error", "HttpError"
    v = _verdict(snapshot, MATCH)
    assert any("summary" in f and "error" in f for f in v.failures)


def test_invoice_absent_from_the_summary_is_reported() -> None:
    snapshot = perfect_snapshot()
    inv_id = snapshot["messages"][MATCH]["invoice"]["id"]
    snapshot["summary"]["candidates"] = [
        c for c in snapshot["summary"]["candidates"] if c["message_id"] != inv_id
    ]
    assert any("not in the run summary" in f for f in _verdict(snapshot, MATCH).failures)


# --- draft bodies -------------------------------------------------------------------------------


def test_draft_body_must_name_every_discrepancy_with_both_values() -> None:
    pair = BY_NAME[MISMATCH]
    assert draft_problems(_draft_body(pair), pair) == []
    body = _draft_body(pair).replace("14,50 EUR", "15,50 EUR")
    problems = draft_problems(body, pair)
    assert len(problems) == 1 and "unit_price" in problems[0]


def test_quantity_values_do_not_match_inside_longer_numbers() -> None:
    pair = BY_NAME[QTY_MISMATCH]  # invoice 2, TIG 1
    assert draft_problems(_draft_body(pair), pair) == []
    body = "- tétel: a számlán 12, a teljesítésigazoláson 11\n"
    assert draft_problems(body, pair) != []


def test_extra_discrepancy_lines_are_reported() -> None:
    pair = BY_NAME[QTY_MISMATCH]
    body = _draft_body(pair) + "- 2. tétel: a számlán 5, a teljesítésigazoláson 4\n"
    assert any("2 discrepancy lines" in p for p in draft_problems(body, pair))


# --- run-wide checks ----------------------------------------------------------------------------


def test_month_sequence_problems() -> None:
    assert month_sequence_problems(["2610_001_AKFT", "2610_002_BKFT", "2611_001_CKFT"]) == []
    gap = month_sequence_problems(["2610_001_AKFT", "2610_003_BKFT"])
    assert gap and "2610" in gap[0]
    dup = month_sequence_problems(["2610_001_AKFT", "2610_001_BKFT"])
    assert dup and "2610" in dup[0]
    assert month_sequence_problems(["garbage"]) != []
    # The pipeline writes YYMM_seq_SUPPLIER; the old unseparated form is a failure.
    assert month_sequence_problems(["2610001AKFT"]) != []


def test_unseparated_registry_number_is_a_failure() -> None:
    snapshot = perfect_snapshot()
    row = _row(snapshot, MISMATCH)
    row[0] = row[0].replace("_", "")
    assert any("registry number" in f for f in _verdict(snapshot, MISMATCH).failures)


def test_global_problems_catch_extras_gaps_and_touched_tigs() -> None:
    snapshot = perfect_snapshot()
    snapshot["ledger"].append(["2610_099_ZKFT", "Z Kft.", "INV-Z", "tig", "EUR", 1, 1, "x"])
    snapshot["drive"]["folders"]["2610"].append("stray.pdf")
    snapshot["drive"]["root_files"].append("loose.pdf")
    snapshot["messages"][MATCH]["tig"]["labels"] = [LABELS["processed"]]
    for c in snapshot["summary"]["candidates"]:
        if c["message_id"] == snapshot["messages"][MATCH]["tig"]["id"]:
            c["status"] = "needs_review"
    problems = " | ".join(global_problems(PAIRS, snapshot))
    assert "INV-Z" in problems
    assert "stray.pdf" in problems and "loose.pdf" in problems
    assert "2610" in problems  # 099 leaves a gap
    assert "TIG message" in problems
    assert "needs_review" in problems


def test_global_draft_total_must_equal_mismatch_count() -> None:
    snapshot = perfect_snapshot()
    snapshot["messages"][MISMATCH]["drafts"] = []
    assert any("drafts" in p for p in global_problems(PAIRS, snapshot))


# --- idempotency --------------------------------------------------------------------------------


def test_identical_second_snapshot_is_idempotent() -> None:
    first = perfect_snapshot()
    second = copy.deepcopy(first)
    second["summary"] = {
        "candidates": [c for c in first["summary"]["candidates"] if c["status"] == "skipped"]
    }
    assert idempotency_problems(first, second) == []


def test_idempotency_problems_report_every_drift() -> None:
    first = perfect_snapshot()
    second = copy.deepcopy(first)
    second["ledger"].append(list(second["ledger"][0]))
    second["drive"]["folders"]["2611"].append("2611_020_AKFT.pdf")
    second["messages"][MISMATCH]["drafts"].append({"to": "x", "body": "y"})
    second["messages"][MATCH]["invoice"]["labels"] = [LABELS["pending"]]
    second["messages"][MATCH]["tig"]["unread"] = False
    inv_id = first["messages"][MATCH]["invoice"]["id"]
    second["summary"] = {
        "candidates": [{"message_id": inv_id, "status": "processed", "reason": "ok"}]
    }
    problems = " | ".join(idempotency_problems(first, second))
    assert "ledger rows 32 -> 33" in problems
    assert "2611_020_AKFT.pdf" in problems
    assert "drafts" in problems and MISMATCH in problems
    assert "labels" in problems and MATCH in problems
    assert "read state" in problems
    assert "re-processed" in problems


# --- table --------------------------------------------------------------------------------------


def test_table_has_a_row_per_pair_and_marks_failures() -> None:
    snapshot = perfect_snapshot()
    snapshot["messages"][MATCH]["invoice"]["labels"] = []
    table = format_table(evaluate_pairs(PAIRS, snapshot))
    lines = table.splitlines()
    assert lines[0].split()[:3] == ["pair", "expected", "label"]
    body = lines[2:34]
    assert all(ln.startswith("TIG-") for ln in body)
    assert lines[34] == ""
    assert sum(" FAIL" in ln for ln in body) == 1
    assert "31/32 PASS" in table


def test_snapshot_without_a_run_summary_skips_only_the_summary_check() -> None:
    snapshot = perfect_snapshot()
    snapshot["summary"] = {}
    assert all(v.passed for v in evaluate_pairs(PAIRS, snapshot))
    snapshot["messages"][MATCH]["invoice"]["unread"] = True
    assert not _verdict(snapshot, MATCH).passed


def test_draft_check_accepts_the_production_wording() -> None:
    """The body ``compose_discrepancy_body`` writes (as read back from a real draft)."""
    from intake.models import Discrepancy, DiscrepancyField
    from intake.reconciliation.draft_reply import compose_discrepancy_body

    pair = BY_NAME[MISMATCH]
    body = compose_discrepancy_body(
        [
            Discrepancy(
                line_index=1,
                field=DiscrepancyField.UNIT_PRICE,
                invoice_value=Decimal("16.50"),
                tig_value=Decimal("14.50"),
                line_description="Ruhatisztítás, szőnyegmosás – Kék projekt, 2026. szeptember",
            ),
            Discrepancy(
                line_index=None,
                field=DiscrepancyField.TOTAL_NET,
                invoice_value=Decimal("1073.00"),
                tig_value=Decimal("989.00"),
            ),
        ],
        currency="EUR",
        invoice_number="INV-C-2026-02",
        tig_number="TIG-2026-09-KEK-C",
        invoice_line_count=2,
        tig_line_count=2,
    )
    assert draft_problems(body, pair) == []
    assert draft_problems(body.replace("989,00", "988,00"), pair) != []
