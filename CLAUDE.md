# CLAUDE.md

@.claude/software-factory.md

## Project

- **Name:** Invoice processing
- **project_id:** `44eed337-e80e-4ca8-b6e2-eb1814b72816`
- **Purpose:** Automate Kibit's invoice intake: Gmail → AI extraction → named PDF in
  Drive → Google Sheets ledger row, with contractor invoices reconciled against TIGs.

## Conventions

- **Stack:** Python 3.12 service on Cloud Run, triggered by Cloud Scheduler. Extraction
  uses Claude Sonnet 5.5 through the firm's AI Compass gateway (`EXTRACTOR_BACKEND=ai_compass`,
  default; replaces the design's Gemini choice, ADR 4); Gemini remains selectable.
  OAuth refresh token in Secret Manager. No database: Gmail labels, Drive and
  Sheets are the only persistence. Configuration lives in the ledger sheet's `Config` tab,
  so never hardcode keyword lists, contractor IDs, currency maps or label names.
- **Directory layout** (package `intake/`):
  - `clients/`: Gmail, Drive, Sheets and auth wrappers
  - `extraction/`: the `Extractor` protocol, the AI Compass (Claude) and Gemini backends
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
  `remove_label`, `mark_read` or `append_row`.
- **Test command:** `pytest tests/unit`. Unit tests are hermetic, with mocked clients and
  extractors. CI requires 85% coverage.
- **Lint/format command:** `ruff check .`, `ruff format --check .` and `mypy intake/`. Use `ruff format .` to fix formatting.
- **Run command:** `python -m intake` runs one cycle locally (`python -m intake --authorize` once first). Deployed: `POST /run` on Cloud Run.
- **Branching:** each task uses the source and target branches given in the execution
  plan (`execution-plan-invoice-processing-kibit.md`).
