# E2E fixture suite (Task 24: release gate)

`tests/e2e/fixture_set/` runs the 32 synthetic TIG/invoice pairs of
`tests/fixtures/tig_pairs/` through the real pipeline against the **sandbox** Google
Workspace account (`invoice@kibitfinance.com`), twice back to back, and checks every
result against `tests/fixtures/tig_pairs/answer_key.json`.

> **Deviation from the execution plan (approved).** Tasks 22 (Cloud Run / Scheduler
> deployment) and 23 (observability) are not done yet, so the suite does not call a
> deployed `POST /run`. It drives the pipeline **locally** with
> `intake.pipeline.runtime.run_with_credentials(credentials, env)`, the same factory
> `python -m intake` and `POST /run` use, with the local OAuth token
> (`secrets/token.json`) instead of Secret Manager. Once Task 22 lands, the run step
> should switch to the deployed endpoint; the seeding and all checks stay the same.

## Running it

It is opt-in, and costs real API calls: it creates a spreadsheet and a Drive folder,
inserts 64 emails into the sandbox inbox (`users.messages.insert`: nothing is sent) and
sends 64 synthetic PDFs (32 invoices, 32 TIGs) to AI Compass (Claude Sonnet 5.5). A run
takes roughly 15–30 minutes.

```bash
.venv/bin/pytest -m e2e tests/e2e -s
# from a worktree, pointing at the main checkout's credentials:
KIBIT_ENV_FILE=/path/to/invoice-processing/.env \
KIBIT_SECRETS_DIR=/path/to/invoice-processing/secrets \
  .venv/bin/pytest -m e2e tests/e2e -s
```

| Variable | Default | Meaning |
|---|---|---|
| `KIBIT_ENV_FILE` | `<repo>/.env` | `AI_COMPASS_*` (and other extractor settings). The real environment wins. |
| `KIBIT_SECRETS_DIR` | `<repo>/secrets` | holds `token.json` for the sandbox mailbox |
| `KIBIT_E2E_RESULTS_DIR` | `tests/e2e/results` | where the run's JSON results go (gitignored) |
| `KIBIT_E2E_COOLDOWN_S` | `90` | pause before run 1 and before run 2, so the per-user Gmail quota ("units per minute") recovers. Without it, live runs hit `rateLimitExceeded` while polling. |
| `KIBIT_E2E_RESUME` | unset | `<run-id>` of a run that was seeded but whose first pipeline run failed before processing anything (e.g. that quota error). Continues it without creating a new ledger, folder or emails. |

Every test is marked `live` and `e2e`. Without `-m e2e`, or without the token, the
AI Compass key or the answer key, the tests skip cleanly. `pytest tests/unit` never
collects them.

`LEDGER_SPREADSHEET_ID` / `DRIVE_ROOT_FOLDER_ID` in `.env` (the demo ledger) are
**ignored**: each run uses its own fresh ledger and folder, and never touches the demo
ones. Nothing is ever deleted.

## What one run does

1. **Preflight.** The answer key must be sound (`verify.answer_key_problems`), the token
   must belong to the sandbox mailbox, and no unfinished invoice mail of an earlier E2E
   run may still be unread in the inbox. Such a mail would be booked into this run's
   ledger, so the run stops. Resume that run (`KIBIT_E2E_RESUME`) or mark the mail read by hand.
2. **Fresh ledger and folder**, named by the run id (UTC timestamp `YYYYMMDD-HHMMSS`):
   spreadsheet **"Kibit E2E ledger &lt;run-id&gt;"** (Ledger header A–H plus a Config tab) and
   Drive folder **"Invoices E2E &lt;run-id&gt;"**. The Config tab is the sandbox one
   (`scripts/setup_sandbox.py: config_rows`), except that `contractor_identifiers` lists the
   11 fixture contractor domains `a-kft.example` … `k-kft.example`. Each domain is derived
   from the answer key's supplier `"X Kft."`.
3. **Seeding.** Each pair becomes one thread: the project manager's TIG mail
   (`projekt@kibitfinance.com`, `TIG-*.pdf`), then the contractor's invoice as a reply
   (`szamlazas@<x>-kft.example`, `In-Reply-To`/`References` set, same `threadId`). The
   Message-IDs are unique per run (`<e2e-<run-id>-<pair>-tig|invoice@kibit-e2e.example>`),
   and subjects end in `[Kibit E2E <run-id>]`.
4. **Run 1** (after a cooldown, `KIBIT_E2E_COOLDOWN_S`): `run_with_credentials` with `LEDGER_SPREADSHEET_ID` / `DRIVE_ROOT_FOLDER_ID`
   overridden to the fresh ones. Then a snapshot of the Ledger (read with
   `UNFORMATTED_VALUE`), the Drive folder tree, and each thread's labels, read state and
   drafts.
5. **Run 2** (E2E 6), after another cooldown, then a second snapshot.
6. Results go to `tests/e2e/results/<run-id>.json`: both snapshots, both run summaries,
   the per-pair verdicts and table, timings, token use, and what the model extracted from
   each document (for diagnosis).

Other unread mail in the sandbox inbox is processed by the run like any cycle would
process it. Earlier demo TIG mails are skipped as `tig_document_only`, and the demo
missing-TIG invoice stays `awaiting_tig`. The summary checks are therefore scoped to
**this run's** messages. Everything else is recorded under `summary_scope.other` in the
results file.

## What is checked

Per pair (`verify.evaluate_pairs`; every failure is collected, then reported as one table):

| Check | Expected |
|---|---|
| label | invoice message labelled `Kibit/Processed` (match) or `Kibit/Pending` (mismatch), and no other Kibit label |
| read state | invoice message marked read |
| run summary | invoice status `processed` / `pending` |
| ledger | exactly one row with `INV_ID_ext` = printed invoice number. Provider, `INV_type` (`tig`), Currency, Net, Gross and Due date equal `expected_ledger`. Amounts are compared numerically. |
| registry number | `INV_ID_int` = expected `yymm` + 3-digit sequence + `normalize_supplier(provider)` |
| Drive | `<registry>.pdf` exists in folder `<yymm>` |
| draft | mismatch: exactly one draft, addressed to the contractor, with exactly one line per answer-key discrepancy. Each line states the invoice value and the TIG value (`format_hungarian_amount`, quantities without trailing zeros). Match: no draft. |

Run-wide (`verify.global_problems`):

- per month, the sequence numbers are exactly 1..n (no gaps, no repeats);
- no ledger row or Drive file that belongs to no pair;
- exactly 7 drafts across the run's threads;
- the TIG mails are untouched (no Kibit label, still unread, summary `skipped`). Opening a
  run's thread in the Gmail web UI marks the whole conversation read and fails this check,
  so do not open the run's threads until it has finished;
- no `error` / `needs_review` / `awaiting_tig` result for this run's messages.

Second run (`verify.idempotency_problems`, E2E 6):

- no new ledger rows, no new Drive files, no new drafts;
- labels and read state of every seeded message unchanged;
- no seeded invoice processed again.

## Re-verifying a finished run

`scripts/verify_fixture_answer_key.py` (the factory CI calls it with `--strict`):

```bash
.venv/bin/python scripts/verify_fixture_answer_key.py --strict              # answer key only
.venv/bin/python scripts/verify_fixture_answer_key.py --strict \
    --results tests/e2e/results/<run-id>.json                                # recorded run
.venv/bin/python scripts/verify_fixture_answer_key.py --strict \
    --spreadsheet <ledger-id> --folder <folder-id>                           # live re-read
```

It prints the per-pair PASS/FAIL table. With `--strict` it exits 1 on any failure. The
live mode finds the run id from the spreadsheet title and the messages by their
Message-IDs. It checks everything except the run summary, which is not stored in the
Workspace.

## Answer-key cross-reference

| Design E2E journey | Covered here by |
|---|---|
| E2E 1: clean supplier (direct route) → Processed | not by this suite: every fixture pair is a contractor (TIG-route) invoice. Unit-tested in-memory in `tests/unit/pipeline/test_fixture_pipeline.py` and shown in the sandbox demo (`scripts/setup_sandbox.py --seed-demo`) |
| E2E 2: contractor + matching TIG → Processed, no draft | the 25 `match` pairs |
| E2E 3: contractor + TIG mismatch → filed, booked, draft, Pending | the 7 `mismatch` pairs (`discrepancies[]` give the draft's expected values) |
| E2E 4: no TIG in the thread → AwaitingTIG, later TIG → proceeds | not by this suite (unit: `tests/unit/pipeline/test_orchestrator.py`; sandbox demo `missing-tig`) |
| E2E 5: ambiguous/incomplete → NeedsReview, never reprocessed | not by this suite (unit: `tests/unit/pipeline/test_orchestrator.py`) |
| E2E 6: two runs back to back → no duplicate rows/files, no gaps | the second run over all 32 pairs |

The answer key's schema and conventions are in `tests/fixtures/README.md`. The pairs give
13 invoices in `2610` and 19 in `2611` (by performance date), 17 in EUR and 15 in HUF,
from suppliers A Kft. to K Kft.
