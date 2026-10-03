"""Pure judgement of an E2E fixture-set run against ``tests/fixtures/tig_pairs/answer_key.json``.

The live suite (``conftest.py``) and ``scripts/verify_fixture_answer_key.py`` only gather
Workspace state into a JSON-able *snapshot*; everything decided about that snapshot lives
here, without any network access, so it is unit-tested (``tests/unit/e2e_support/``).

Snapshot shape (also what ``tests/e2e/results/<run-id>.json`` stores per run)::

    {
      "run_id": str,
      "ledger": [[A, B, C, D, E, F, G, H], ...],   # Ledger rows after the header,
                                                     # read with UNFORMATTED_VALUE
      "drive": {"folders": {"<YYMM>": [file names]}, "root_files": [file names]},
      "messages": {"<pair>": {
          "invoice": {"id", "thread_id", "labels": [Kibit label names], "unread": bool},
          "tig":     {"id", "thread_id", "labels": [...], "unread": bool},
          "drafts":  [{"to": str, "body": str}]}},   # DRAFT messages in the thread
      "summary": {"counts": {...}, "candidates": [{"message_id", "status", "reason",
                                                   "registry_number"}]}
    }

Every check collects all of its failures; nothing stops at the first one.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final

from intake.normalization.amounts import format_hungarian_amount
from intake.registry.supplier_id import normalize_supplier

SEQUENCE_WIDTH: Final = 3
FILED_EXTENSION: Final = ".pdf"
INV_TYPE_TIG: Final = "tig"  # intake.ledger.booking.INV_TYPE_BY_ROUTE[Route.TIG]
MESSAGE_ID_DOMAIN: Final = "kibit-e2e.example"
SPREADSHEET_TITLE_PREFIX: Final = "Kibit E2E ledger "
FOLDER_NAME_PREFIX: Final = "Invoices E2E "

LABELS: Final[Mapping[str, str]] = {
    "processed": "Kibit/Processed",
    "pending": "Kibit/Pending",
    "needs_review": "Kibit/NeedsReview",
    "awaiting_tig": "Kibit/AwaitingTIG",
    "duplicate": "Kibit/Duplicate",
}
_SHORT_LABEL: Final = {name: name.split("/", 1)[1] for name in LABELS.values()}
_EXPECTED_STATUS: Final = {"match": "processed", "mismatch": "pending"}
_BAD_STATUSES: Final = frozenset({"error", "needs_review", "awaiting_tig", "duplicate"})
_LEDGER_COLUMNS: Final = (
    "INV_ID_int",
    "Provider",
    "INV_ID_ext",
    "INV_type",
    "Currency",
    "Net",
    "Gross",
    "Due date",
)
_CONTRACTOR: Final = re.compile(r"([A-Z]) Kft\.")
_SHEETS_EPOCH: Final = date(1899, 12, 30)

# Answer-key facts (tests/fixtures/README.md): the suite's oracle must stay what it claims.
EXPECTED_PAIRS: Final = 32
EXPECTED_OUTCOMES: Final = {"match": 25, "mismatch": 7}
EXPECTED_CURRENCIES: Final = {"EUR": 17, "HUF": 15}


# --- the answer key -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExpectedDiscrepancy:
    line_index: int | None
    field: str
    invoice_value: str
    tig_value: str


@dataclass(frozen=True, slots=True)
class PairExpectation:
    """One answer-key pair, reduced to what the E2E suite seeds and checks."""

    pair: str
    outcome: str
    supplier: str
    invoice_file: str
    tig_file: str
    tig_number: str
    provider: str
    inv_id_ext: str
    currency: str
    net: Decimal
    gross: Decimal
    due_date: str
    performance_date: str
    yymm: str
    discrepancies: tuple[ExpectedDiscrepancy, ...] = ()

    @property
    def contractor_domain(self) -> str:
        return contractor_domain(self.supplier)

    @property
    def sender(self) -> str:
        return f"szamlazas@{self.contractor_domain}"

    @property
    def supplier8(self) -> str:
        return normalize_supplier(self.provider)

    @property
    def expected_label(self) -> str:
        return LABELS[_EXPECTED_STATUS[self.outcome]]


def contractor_domain(supplier: str) -> str:
    """``"A Kft."`` -> ``"a-kft.example"`` (the fake contractor sender domains)."""
    match = _CONTRACTOR.fullmatch(supplier)
    if match is None:
        raise ValueError(f"not a fixture contractor name ('X Kft.'): {supplier!r}")
    return f"{match.group(1).lower()}-kft.example"


def load_answer_key(path: Path) -> list[PairExpectation]:
    data = json.loads(path.read_text(encoding="utf-8"))
    pairs: list[PairExpectation] = []
    for raw in data["pairs"]:
        invoice, ledger = raw["invoice"], raw["invoice"]["expected_ledger"]
        pairs.append(
            PairExpectation(
                pair=raw["pair"],
                outcome=raw["expected_outcome"],
                supplier=invoice["supplier"],
                invoice_file=invoice["file"],
                tig_file=raw["tig"]["file"],
                tig_number=raw["tig"]["number"],
                provider=ledger["provider"],
                inv_id_ext=ledger["inv_id_ext"],
                currency=ledger["currency"],
                net=Decimal(ledger["net"]),
                gross=Decimal(ledger["gross"]),
                due_date=ledger["due_date"],
                performance_date=ledger["performance_date"],
                yymm=ledger["yymm"],
                discrepancies=tuple(
                    ExpectedDiscrepancy(
                        line_index=d["line_index"],
                        field=d["field"],
                        invoice_value=d["invoice_value"],
                        tig_value=d["tig_value"],
                    )
                    for d in raw["discrepancies"]
                ),
            )
        )
    return pairs


def answer_key_problems(pairs: Sequence[PairExpectation], fixtures_dir: Path) -> list[str]:
    """What makes the answer key unusable as this suite's oracle (empty when sound)."""
    problems: list[str] = []
    if len(pairs) != EXPECTED_PAIRS:
        problems.append(f"expected {EXPECTED_PAIRS} pairs, found {len(pairs)}")
    outcomes = Counter(p.outcome for p in pairs)
    if dict(outcomes) != EXPECTED_OUTCOMES:
        problems.append(
            f"expected {EXPECTED_OUTCOMES['match']} match / {EXPECTED_OUTCOMES['mismatch']} "
            f"mismatch pairs, found {dict(outcomes)}"
        )
    currencies = Counter(p.currency for p in pairs)
    if dict(currencies) != EXPECTED_CURRENCIES:
        problems.append(f"expected currencies {EXPECTED_CURRENCIES}, found {dict(currencies)}")
    duplicates = [k for k, n in Counter((p.inv_id_ext, p.provider) for p in pairs).items() if n > 1]
    if duplicates:
        problems.append(f"duplicate (invoice number, provider) keys: {duplicates}")
    for p in pairs:
        where = f"{p.pair}:"
        if p.outcome not in _EXPECTED_STATUS:
            problems.append(f"{where} unknown expected_outcome {p.outcome!r}")
        elif p.outcome == "mismatch" and not p.discrepancies:
            problems.append(f"{where} mismatch with no discrepancies")
        elif p.outcome == "match" and p.discrepancies:
            problems.append(f"{where} match but lists discrepancies")
        for name in (p.invoice_file, p.tig_file):
            if not (fixtures_dir / p.pair / name).is_file():
                problems.append(f"{where} file {name} not found")
        try:
            contractor_domain(p.supplier)
        except ValueError as exc:
            problems.append(f"{where} {exc}")
        if p.provider != p.supplier:
            problems.append(f"{where} ledger provider {p.provider!r} != supplier {p.supplier!r}")
        perf = p.performance_date.split(".")
        if len(perf) != 3 or p.yymm != perf[0][2:] + perf[1]:
            problems.append(f"{where} yymm {p.yymm} does not match {p.performance_date}")
    return problems


# --- naming -------------------------------------------------------------------------------------


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def message_id_for(run_id: str, pair: str, kind: str) -> str:
    """RFC 822 Message-ID of the seeded ``kind`` (``tig`` / ``invoice``) message of a pair."""
    return f"<e2e-{_slug(run_id)}-{_slug(pair)}-{kind}@{MESSAGE_ID_DOMAIN}>"


def run_marker(run_id: str) -> str:
    """Subject suffix tagging every seeded message of a run (searchable in Gmail)."""
    return f"[Kibit E2E {run_id}]"


def spreadsheet_title(run_id: str) -> str:
    return f"{SPREADSHEET_TITLE_PREFIX}{run_id}"


def folder_name(run_id: str) -> str:
    return f"{FOLDER_NAME_PREFIX}{run_id}"


def run_id_from_title(title: str) -> str | None:
    if not title.startswith(SPREADSHEET_TITLE_PREFIX):
        return None
    return title[len(SPREADSHEET_TITLE_PREFIX) :].strip() or None


# --- per-pair verdicts --------------------------------------------------------------------------


@dataclass
class PairVerdict:
    pair: str
    expected_outcome: str
    label: str = "-"
    registry_number: str | None = None
    ledger: str = "-"
    drive: str = "-"
    draft: str = "-"
    failures: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures

    def to_dict(self) -> dict[str, Any]:
        return {
            "pair": self.pair,
            "expected_outcome": self.expected_outcome,
            "label": self.label,
            "registry_number": self.registry_number,
            "ledger": self.ledger,
            "drive": self.drive,
            "draft": self.draft,
            "passed": self.passed,
            "failures": list(self.failures),
        }


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return Decimal(str(value).strip())
    except InvalidOperation:
        return None


def _date_text(value: Any) -> str:
    """A Due date cell as ``YYYY.MM.DD`` (text as-is; a Sheets date serial converted)."""
    if isinstance(value, int | float) and not isinstance(value, bool):
        return (_SHEETS_EPOCH + timedelta(days=int(value))).strftime("%Y.%m.%d")
    return str(value).strip()


def _cell(row: Sequence[Any], index: int) -> Any:
    return row[index] if index < len(row) else None


def _ledger_problems(row: Sequence[Any], p: PairExpectation) -> list[str]:
    problems: list[str] = []
    expected_text = {1: p.provider, 3: INV_TYPE_TIG, 4: p.currency}
    for index, expected in expected_text.items():
        actual = _cell(row, index)
        if str(actual).strip() != expected:
            problems.append(f"{_LEDGER_COLUMNS[index]} {actual!r} != {expected!r}")
    for index, expected_amount in ((5, p.net), (6, p.gross)):
        actual = _cell(row, index)
        if _decimal(actual) != expected_amount:
            problems.append(f"{_LEDGER_COLUMNS[index]} {actual!r} != {expected_amount}")
    due = _cell(row, 7)
    if due is None or _date_text(due) != p.due_date:
        problems.append(f"Due date {due!r} != {p.due_date!r}")
    return problems


def _registry_problem(registry: str, p: PairExpectation) -> str | None:
    pattern = rf"{re.escape(p.yymm)}_\d{{{SEQUENCE_WIDTH}}}_{re.escape(p.supplier8)}"
    if re.fullmatch(pattern, registry):
        return None
    return (
        f"registry number {registry!r} is not {p.yymm}_<{SEQUENCE_WIDTH}-digit seq>_{p.supplier8}"
    )


def _format_value(field_name: str, value: str) -> str:
    amount = Decimal(value)
    return format_hungarian_amount(amount.normalize() if field_name == "quantity" else amount)


def _discrepancy_lines(body: str) -> list[str]:
    return [
        ln.strip()
        for ln in body.splitlines()
        if ln.strip().startswith("- ") and "a számlán" in ln and "a teljesítésigazoláson" in ln
    ]


def draft_problems(body: str, p: PairExpectation) -> list[str]:
    """Every expected discrepancy must appear with its invoice and TIG value, and only those."""
    lines = _discrepancy_lines(body)
    problems: list[str] = []
    if len(lines) != len(p.discrepancies):
        problems.append(
            f"draft has {len(lines)} discrepancy lines, expected {len(p.discrepancies)}"
        )
    currency = r"(?: [A-Z]{3})?"
    for d in p.discrepancies:
        inv, tig = _format_value(d.field, d.invoice_value), _format_value(d.field, d.tig_value)
        pattern = re.compile(
            rf"a számlán {re.escape(inv)}{currency}, "
            rf"a teljesítésigazoláson {re.escape(tig)}{currency}$"
        )
        if not any(pattern.search(ln) for ln in lines):
            problems.append(f"draft does not state {d.field}: invoice {inv} vs TIG {tig}")
    return problems


def _label_text(labels: Iterable[str], unread: bool) -> str:
    kibit = sorted(_SHORT_LABEL[name] for name in labels if name in _SHORT_LABEL)
    text = ", ".join(kibit) or "none"
    return f"{text}, unread" if unread else text


def _candidate_index(snapshot: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    summary = snapshot.get("summary") or {}
    return {c["message_id"]: c for c in summary.get("candidates", [])}


def evaluate_pairs(
    pairs: Sequence[PairExpectation], snapshot: Mapping[str, Any]
) -> list[PairVerdict]:
    """One verdict per answer-key pair, each with every failure found for it."""
    ledger: list[Sequence[Any]] = snapshot.get("ledger", [])
    folders: Mapping[str, list[str]] = (snapshot.get("drive") or {}).get("folders", {})
    messages: Mapping[str, Any] = snapshot.get("messages", {})
    candidates = _candidate_index(snapshot)
    # A snapshot re-read from Workspace afterwards has no run summary to check.
    has_summary = "candidates" in (snapshot.get("summary") or {})
    verdicts: list[PairVerdict] = []
    for p in pairs:
        v = PairVerdict(pair=p.pair, expected_outcome=p.outcome)
        verdicts.append(v)
        expected_short = _SHORT_LABEL[p.expected_label]

        # Gmail: final label + read state, and the run summary's status.
        seeded = messages.get(p.pair)
        if seeded is None:
            v.failures.append("seeded messages not found")
        else:
            invoice = seeded["invoice"]
            kibit = sorted(n for n in invoice["labels"] if n in _SHORT_LABEL)
            v.label = _label_text(invoice["labels"], invoice["unread"])
            if kibit != [p.expected_label]:
                v.failures.append(f"labels {kibit or 'none'}, expected {expected_short}")
            if invoice["unread"]:
                v.failures.append("invoice message still unread")
            candidate = candidates.get(invoice["id"])
            if has_summary and candidate is None:
                v.failures.append("invoice message not in the run summary")
            elif candidate is not None and candidate["status"] != _EXPECTED_STATUS[p.outcome]:
                v.failures.append(
                    f"summary status {candidate['status']} ({candidate.get('reason')}), "
                    f"expected {_EXPECTED_STATUS[p.outcome]}"
                )

        # Sheets: exactly one row for the printed invoice number, values as expected.
        rows = [r for r in ledger if str(_cell(r, 2)).strip() == p.inv_id_ext]
        if not rows:
            v.ledger = "missing"
            v.failures.append(f"no ledger row with INV_ID_ext {p.inv_id_ext!r}")
        elif len(rows) > 1:
            v.ledger = f"{len(rows)} rows"
            v.failures.append(f"{len(rows)} ledger rows with INV_ID_ext {p.inv_id_ext!r}")
        else:
            row = rows[0]
            problems = _ledger_problems(row, p)
            v.ledger = "ok" if not problems else "WRONG: " + "; ".join(problems)
            v.failures.extend(f"ledger {x}" for x in problems)
            v.registry_number = str(_cell(row, 0) or "").strip() or None
            registry_problem = _registry_problem(v.registry_number or "", p)
            if registry_problem:
                v.failures.append(registry_problem)

        # Drive: <registry>.pdf in the YYMM folder.
        if v.registry_number:
            filename = f"{v.registry_number}{FILED_EXTENSION}"
            if filename in folders.get(p.yymm, []):
                v.drive = "ok"
            else:
                v.drive = "missing"
                v.failures.append(f"Drive file {p.yymm}/{filename} not found")

        # Gmail drafts: one per mismatch, none per match.
        if seeded is not None:
            drafts = seeded.get("drafts", [])
            if p.outcome == "match":
                v.draft = "none (ok)" if not drafts else f"UNEXPECTED ({len(drafts)})"
                if drafts:
                    v.failures.append(f"{len(drafts)} draft(s) on a matching pair")
            elif not drafts:
                v.draft = "missing"
                v.failures.append("no draft reply on a mismatching pair")
            elif len(drafts) > 1:
                v.draft = f"{len(drafts)} drafts"
                v.failures.append(f"{len(drafts)} draft replies, expected exactly 1")
            else:
                problems = draft_problems(drafts[0]["body"], p)
                if p.contractor_domain not in drafts[0].get("to", ""):
                    problems.append(f"draft addressed to {drafts[0].get('to')!r}, not {p.sender}")
                v.draft = "ok" if not problems else "WRONG"
                v.failures.extend(problems)
    return verdicts


# --- run-wide checks ----------------------------------------------------------------------------


def month_sequence_problems(registry_numbers: Iterable[str]) -> list[str]:
    """Per YYMM the sequence numbers must be exactly 1..n (no gaps, no repeats)."""
    by_month: dict[str, list[int]] = defaultdict(list)
    problems: list[str] = []
    for number in registry_numbers:
        match = re.fullmatch(rf"(\d{{4}})_(\d{{{SEQUENCE_WIDTH}}})_[A-Z0-9]{{1,8}}", number)
        if match is None:
            problems.append(f"malformed registry number {number!r}")
            continue
        by_month[match.group(1)].append(int(match.group(2)))
    for month, seqs in sorted(by_month.items()):
        if sorted(seqs) != list(range(1, len(seqs) + 1)):
            problems.append(
                f"month {month}: sequence numbers {sorted(seqs)} are not 1..{len(seqs)}"
            )
    return problems


def global_problems(pairs: Sequence[PairExpectation], snapshot: Mapping[str, Any]) -> list[str]:
    """Checks that span pairs: extra rows/files, sequences, drafts, TIG mails, run summary."""
    problems: list[str] = []
    ledger: list[Sequence[Any]] = snapshot.get("ledger", [])
    known = {p.inv_id_ext for p in pairs}
    for row in ledger:
        if str(_cell(row, 2)).strip() not in known:
            problems.append(f"unexpected ledger row {list(row)}")
    problems.extend(month_sequence_problems(str(_cell(r, 0)).strip() for r in ledger))

    drive = snapshot.get("drive") or {}
    expected_files = {
        (str(_cell(r, 0)).strip()[:4], f"{str(_cell(r, 0)).strip()}{FILED_EXTENSION}")
        for r in ledger
    }
    for month, names in sorted(drive.get("folders", {}).items()):
        for name in names:
            if (month, name) not in expected_files:
                problems.append(f"unexpected Drive file {month}/{name}")
    for name in drive.get("root_files", []):
        problems.append(f"unexpected Drive file in the root folder: {name}")

    messages: Mapping[str, Any] = snapshot.get("messages", {})
    drafts = sum(len(m.get("drafts", [])) for m in messages.values())
    mismatches = sum(1 for p in pairs if p.outcome == "mismatch")
    if drafts != mismatches:
        problems.append(f"{drafts} drafts in the run's threads, expected {mismatches}")

    candidates = _candidate_index(snapshot)
    for pair, seeded in sorted(messages.items()):
        tig = seeded["tig"]
        kibit = [n for n in tig["labels"] if n in _SHORT_LABEL]
        if kibit or not tig["unread"]:
            problems.append(
                f"{pair}: TIG message was changed (labels {kibit}, unread={tig['unread']})"
            )
        candidate = candidates.get(tig["id"])
        if candidate is not None and candidate["status"] != "skipped":
            problems.append(f"{pair}: TIG message got summary status {candidate['status']}")
    run_ids = {m[k]["id"] for m in messages.values() for k in ("invoice", "tig") if k in m}
    bad = Counter(
        c["status"]
        for mid, c in candidates.items()
        if mid in run_ids and c["status"] in _BAD_STATUSES
    )
    for status, count in sorted(bad.items()):
        problems.append(f"run summary has {count} {status} result(s) for this run's messages")
    return problems


def run_scoped_counts(snapshot: Mapping[str, Any]) -> dict[str, dict[str, int]]:
    """Summary status counts split into this run's messages and everything else."""
    messages: Mapping[str, Any] = snapshot.get("messages", {})
    run_ids = {m[k]["id"] for m in messages.values() for k in ("invoice", "tig") if k in m}
    ours: Counter[str] = Counter()
    others: Counter[str] = Counter()
    for mid, c in _candidate_index(snapshot).items():
        (ours if mid in run_ids else others)[c["status"]] += 1
    return {"run": dict(ours), "other": dict(others)}


# --- idempotency (E2E 6) ------------------------------------------------------------------------


def idempotency_problems(first: Mapping[str, Any], second: Mapping[str, Any]) -> list[str]:
    """A second cycle over the same inbox must change nothing this run created."""
    problems: list[str] = []
    rows1, rows2 = first.get("ledger", []), second.get("ledger", [])
    if len(rows1) != len(rows2):
        problems.append(f"ledger rows {len(rows1)} -> {len(rows2)}")
    elif [list(r) for r in rows1] != [list(r) for r in rows2]:
        problems.append("ledger rows changed")

    def files(snapshot: Mapping[str, Any]) -> set[str]:
        drive = snapshot.get("drive") or {}
        names = {f"{m}/{n}" for m, ns in drive.get("folders", {}).items() for n in ns}
        return names | {f"/{n}" for n in drive.get("root_files", [])}

    added, removed = sorted(files(second) - files(first)), sorted(files(first) - files(second))
    if added:
        problems.append(f"new Drive files: {added}")
    if removed:
        problems.append(f"Drive files disappeared: {removed}")

    m1, m2 = first.get("messages", {}), second.get("messages", {})
    for pair in sorted(m1):
        a, b = m1[pair], m2.get(pair)
        if b is None:
            problems.append(f"{pair}: messages missing in the second snapshot")
            continue
        if len(a.get("drafts", [])) != len(b.get("drafts", [])):
            problems.append(
                f"{pair}: drafts {len(a.get('drafts', []))} -> {len(b.get('drafts', []))}"
            )
        for kind in ("invoice", "tig"):
            if sorted(a[kind]["labels"]) != sorted(b[kind]["labels"]):
                problems.append(f"{pair}: {kind} labels {a[kind]['labels']} -> {b[kind]['labels']}")
            if a[kind]["unread"] != b[kind]["unread"]:
                problems.append(
                    f"{pair}: {kind} read state changed (unread {a[kind]['unread']} -> "
                    f"{b[kind]['unread']})"
                )

    invoice_ids = {m["invoice"]["id"] for m in m1.values()}
    for mid, c in _candidate_index(second).items():
        if mid in invoice_ids and c["status"] != "skipped":
            problems.append(f"invoice message {mid} re-processed in the second run: {c['status']}")
    return problems


# --- reporting ----------------------------------------------------------------------------------


def format_table(verdicts: Sequence[PairVerdict]) -> str:
    """A fixed-width PASS/FAIL table, one row per pair, then failure details."""
    header = ("pair", "expected", "label", "registry", "ledger", "drive", "draft", "result")
    rows = [
        (
            v.pair,
            v.expected_outcome,
            v.label,
            v.registry_number or "-",
            v.ledger if len(v.ledger) <= 24 else v.ledger[:21] + "...",
            v.drive,
            v.draft,
            "PASS" if v.passed else "FAIL",
        )
        for v in verdicts
    ]
    widths = [max(len(str(r[i])) for r in (header, *rows)) for i in range(len(header))]

    def line(cells: Sequence[str]) -> str:
        return "  ".join(str(c).ljust(w) for c, w in zip(cells, widths, strict=True)).rstrip()

    out = [line(header), line(["-" * w for w in widths]), *(line(r) for r in rows)]
    passed = sum(v.passed for v in verdicts)
    out.append(f"\n{passed}/{len(verdicts)} PASS")
    for v in verdicts:
        for failure in v.failures:
            out.append(f"  {v.pair}: {failure}")
    return "\n".join(out)


__all__ = [
    "LABELS",
    "ExpectedDiscrepancy",
    "PairExpectation",
    "PairVerdict",
    "answer_key_problems",
    "contractor_domain",
    "draft_problems",
    "evaluate_pairs",
    "folder_name",
    "format_table",
    "global_problems",
    "idempotency_problems",
    "load_answer_key",
    "message_id_for",
    "month_sequence_problems",
    "run_id_from_title",
    "run_marker",
    "run_scoped_counts",
    "spreadsheet_title",
]
