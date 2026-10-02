# ADR 4 (revised): Invoice extraction via the AI Compass gateway (Claude Sonnet 5.5)

- **Status:** Accepted, 2026-10-02. Supersedes ADR 4 of the factory technical design
  (`technical-design-invoice-processing-kibit.md`, "Gemini for multimodal extraction").
- **Decided by:** project owner.

## Context

The technical design chose Gemini for multimodal extraction, behind an `Extractor`
interface so the model could be swapped. In practice, every generation call to the
project's Gemini API returned `403 PERMISSION_DENIED`: Google denied access at project
level. Kibit runs its own AI gateway, **AI Compass** (`https://ai-compass.kibit.cloud`),
a LiteLLM proxy that exposes the Anthropic Messages API and offers Claude models.

## Decision

Extract invoices and TIGs with **Claude Sonnet 5.5 (`claude-sonnet-5-5`) through AI
Compass**, using the official `anthropic` Python SDK pointed at the gateway's base URL.

- One `messages.create` call per document. The PDF or image goes in as a base64
  `document`/`image` block, and the output is constrained by the shared JSON schema via
  `output_config.format`, with effort `medium` by default.
- Prompts, schema, validation and line-item conversion live in
  `intake/extraction/common.py` and are shared with the Gemini backend.
- `EXTRACTOR_BACKEND` selects the backend: `ai_compass` (default) or `gemini`.
- Configuration:
  - `AI_COMPASS_BASE_URL`, `AI_COMPASS_MODEL`, `AI_COMPASS_EFFORT`.
  - The key comes from `AI_COMPASS_API_KEY` for local runs, or from Secret Manager
    `kibit-ai-compass-api-key-<env>` when deployed.
- Any stop reason other than `end_turn` (`refusal`, `max_tokens`, …) is an extraction
  failure and sends the email to NeedsReview. API errors propagate, so the run retries.

## Consequences

- Invoice data leaves Google Cloud for the Kibit gateway. This stays inside Kibit's own
  infrastructure, which fits the discovery constraint that financial data is not shared
  externally.
- **Cost:** about 4.2k input and 0.2k output tokens per invoice, and about 3.9k / 0.2k
  per TIG.
- **Measured quality:** the live test on 3 demo pairs matched the answer key on every
  field.
- HEIC/HEIF images aren't accepted by Claude and are rejected before any call, which ends
  in NeedsReview.
- **Terraform:** still needs the `kibit-ai-compass-api-key-*` secret and its accessor
  binding (Task 22). Cloud Run also needs network egress to the gateway.
- The Gemini backend stays available but is not the default.
