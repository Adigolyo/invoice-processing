"""Check the TIG-pair answer key and (optionally) re-verify a finished E2E run against it.

    .venv/bin/python scripts/verify_fixture_answer_key.py --strict
        answer key only: 32 pairs, 25 match / 7 mismatch, 17 EUR / 15 HUF, files present,
        discrepancies exactly on mismatches, contractor names usable (no network)
    .venv/bin/python scripts/verify_fixture_answer_key.py --strict \
            --results tests/e2e/results/<run-id>.json
        ...plus the recorded first/second run snapshots of an E2E run (no network)
    .venv/bin/python scripts/verify_fixture_answer_key.py --strict --spreadsheet ID --folder ID
        ...plus the run's ledger, Drive folder and Gmail threads re-read live (needs
        secrets/token.json; the run id is taken from the spreadsheet title)

Prints a per-pair PASS/FAIL table. With ``--strict`` any failure exits 1. Never prints
secrets; never changes anything in the Workspace.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.e2e.fixture_set import verify  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "tig_pairs"


def _live_snapshot(args: argparse.Namespace, pairs: list[verify.PairExpectation]) -> dict[str, Any]:
    from intake.__main__ import local_credentials
    from tests.e2e.fixture_set import workspace

    services = workspace.build_services(local_credentials(args.secrets_dir))
    title = workspace.spreadsheet_title_of(services.sheets, args.spreadsheet)
    run_id = verify.run_id_from_title(title)
    if run_id is None:
        raise SystemExit(f"spreadsheet {args.spreadsheet} is not an E2E ledger ({title!r})")
    seeded = workspace.find_seeded(services.gmail, pairs, run_id)
    return workspace.snapshot(
        services,
        run_id=run_id,
        spreadsheet_id=args.spreadsheet,
        folder_id=args.folder,
        seeded=seeded,
        summary=None,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--strict", action="store_true", help="exit 1 on any failure")
    parser.add_argument("--answer-key", type=Path, default=FIXTURES / "answer_key.json")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--results", type=Path, help="an E2E results JSON file")
    source.add_argument("--spreadsheet", help="an E2E run's ledger spreadsheet ID (live)")
    parser.add_argument("--folder", help="that run's Drive root folder ID (with --spreadsheet)")
    parser.add_argument("--secrets-dir", type=Path, default=ROOT / "secrets")
    parser.add_argument(
        "--require-run",
        action="store_true",
        help="fail unless a complete run (first and second) is verified (release gate)",
    )
    args = parser.parse_args(argv)
    if bool(args.spreadsheet) != bool(args.folder):
        parser.error("--spreadsheet and --folder go together")

    failures = 0
    pairs = verify.load_answer_key(args.answer_key)
    key_problems = verify.answer_key_problems(pairs, args.answer_key.parent)
    print(f"answer key: {len(pairs)} pairs, {'OK' if not key_problems else 'PROBLEMS'}")
    for problem in key_problems:
        print(f"  {problem}")
    failures += len(key_problems)

    first: dict[str, Any] | None = None
    second: dict[str, Any] | None = None
    if args.results:
        record = json.loads(args.results.read_text(encoding="utf-8"))
        first, second = record.get("run1"), record.get("run2")
        seeded = record.get("seeded")
        if isinstance(seeded, dict) and seeded:
            # A subset run (e.g. the release gate's smoke subset) is judged on the pairs
            # it seeded; the answer key itself was checked in full above.
            pairs = [p for p in pairs if p.pair in seeded]
            print(f"judging the {len(pairs)} seeded pair(s)")
        if first is None:
            print(f"{args.results}: no first-run snapshot recorded")
            failures += 1
    elif args.spreadsheet:
        first = _live_snapshot(args, pairs)

    if first is not None:
        verdicts = verify.evaluate_pairs(pairs, first)
        print(f"\nrun {first.get('run_id')}:")
        print(verify.format_table(verdicts))
        failures += sum(not v.passed for v in verdicts)
        problems = verify.global_problems(pairs, first)
        print(f"\nrun-wide: {'OK' if not problems else 'PROBLEMS'}")
        for problem in problems:
            print(f"  {problem}")
        failures += len(problems)
    if first is not None and second is not None:
        problems = verify.idempotency_problems(first, second)
        print(f"\nsecond run (idempotency): {'OK' if not problems else 'PROBLEMS'}")
        for problem in problems:
            print(f"  {problem}")
        failures += len(problems)

    if args.require_run and (first is None or (args.results and second is None)):
        print("no E2E run to verify: the release gate needs a complete run (run 1 and run 2)")
        failures += 1

    print(f"\n{'FAIL' if failures else 'PASS'} ({failures} problem(s))")
    return 1 if failures and args.strict else 0


if __name__ == "__main__":
    sys.exit(main())
