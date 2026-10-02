# CLAUDE.md

@.claude/software-factory.md

## Project

- **Name:** Invoice processing
- **project_id:** `44eed337-e80e-4ca8-b6e2-eb1814b72816`
- **Purpose:** Automate Kibit's invoice intake: Gmail → Gemini extraction → named PDF in
  Drive → Google Sheets ledger row, with contractor invoices reconciled against TIGs.

## Conventions

- **Stack:** Python 3.12 service on Cloud Run, triggered by Cloud Scheduler. Gemini for
  extraction. OAuth refresh token in Secret Manager. No database: Gmail labels, Drive and
  Sheets are the only persistence. Configuration lives in the ledger sheet's `Config` tab,
  so never hardcode keyword lists, contractor IDs, currency maps or label names.
- **Directory layout** (package `intake/`):
  - `clients/`: Gmail, Drive, Sheets and auth wrappers
  - `extraction/`: the Gemini extractor
  - `normalization/`: dates, amounts, currency, origin, performance date
  - `routing/`: polling and the route classifier
  - `registry/`: supplier ID, sequence, registry number, filing
  - `ledger/`: booking and dedupe
  - `reconciliation/`: TIG matcher, draft reply, outcome rules
  - `pipeline/`: the orchestrator and labels
  - `observability/`
  - `config.py`, `models.py`, `app.py`

  Tests are split three ways:
  - `tests/unit/<area>/`, mirroring the `intake/` package
  - `tests/integration/`
  - `tests/e2e/fixture_set/`

  Infrastructure lives under `infra/` (Terraform). Dependencies are pinned in
  `requirements.txt` / `requirements-dev.txt`, and tool config is in `pyproject.toml` and
  `.ruff.toml`.
- **Invariant:** only `intake/pipeline/orchestrator.py` may call `apply_label`,
  `mark_read` or `append_row`.
- **Test command:** `pytest tests/unit`. Unit tests are hermetic, with mocked clients and
  Gemini. CI requires 85% coverage.
- **Lint/format command:** `ruff check .`, `ruff format --check .` and `mypy intake/`. Use `ruff format .` to fix formatting.
- **Run command:** TODO, since the specification doesn't give a local run command.
- **Branching:** each task uses the source and target branches given in the execution
  plan (`execution-plan-invoice-processing-kibit.md`).
