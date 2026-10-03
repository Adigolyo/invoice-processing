# Observability: logs, Error Reporting and alerts

Task 23. Code: `intake/observability/logging.py` (formatter, redaction, correlation),
`intake/pipeline/orchestrator.py` (what a run logs), `intake/app.py` and
`intake/__main__.py` (entrypoints). Infrastructure: `infra/monitoring.tf`,
`infra/monitoring_variables.tf`.

## How logs are written

- **Cloud Run** (`K_SERVICE` is set): one JSON object per line on stderr. Cloud Run's
  logging agent turns each line into a structured log entry: `severity`, `message` and
  `time` become the entry's own fields; `logging.googleapis.com/sourceLocation`,
  `logging.googleapis.com/trace`, `logging.googleapis.com/spanId` and
  `logging.googleapis.com/trace_sampled` are lifted into the entry; every other key is
  in `jsonPayload`.
- **Locally** (`python -m intake`): the same fields on one readable line on stderr, e.g.
  `10:02:13 INFO intake.pipeline.orchestrator: stage=route outcome=ok ... [stage=route outcome=ok message_id=18c... duration_ms=3 run_id=4f1...]`.
  The run summary JSON still goes to stdout.
- Level: the `intake` package logs at `LOG_LEVEL` (default `INFO`); third-party
  libraries at `WARNING` and above. `configure_logging(json=...)` forces either format.

## Record schema

Every record has `severity`, `message`, `time` (UTC, RFC 3339), `logger` and
`logging.googleapis.com/sourceLocation`, plus `run_id` when it belongs to a run. Other
fields appear only when set (`None` is omitted).

| Field | Type | Meaning |
|---|---|---|
| `run_id` | code | One per polling cycle (`POST /run` or `python -m intake`); every record of that run carries it. 32 hex characters. |
| `event` | code | `run_started`, `candidate_outcome`, `run_summary`, `run_completed`, `run_failed`. Absent on per-stage records. |
| `message_id` | code | Gmail message ID: the correlation ID of one invoice email. |
| `thread_id` | code | Gmail thread ID (allowed; not currently logged). |
| `stage` | code | `route`, `attachments`, `extract`, `normalise`, `reconcile`, `file`, `book`, `finalise`, `evaluate`. On an `error` candidate outcome: the stage that failed. |
| `outcome` | code | Per stage: `ok`, `flagged`, `error`, `skipped`. Per candidate (`event=candidate_outcome`): `processed`, `pending`, `needs_review`, `awaiting_tig`, `duplicate`, `error`, `skipped`. Per extraction: `ok`, `incomplete`, `invalid_response`. |
| `reason` | code | Fixed reason code, e.g. `ambiguous_route`, `missing_tig`, `normalisation_failed:due_date`, `terminal_label`, or the exception type for an error. |
| `duration_ms` | int | Stage duration; on `candidate_outcome` the whole candidate. |
| `error_type` | code | Exception class name (never its message). |
| `stack_trace` | text | ERROR records with an exception only: Python traceback layout with file, line and function per frame, and the exception **type** only. |
| `@type`, `serviceContext` | | Added next to `stack_trace` so Error Reporting files the record as an event (`service` = `K_SERVICE`, `version` = `K_REVISION`). |
| `candidates`, `processed`, `pending`, `needs_review`, `awaiting_tig`, `duplicate`, `errors`, `skipped` | int | `run_started` (candidates) and `run_summary` (all counts). |
| `document_kind`, `mime_type`, `size_bytes`, `model`, `effort`, `stop_reason`, `input_tokens`, `output_tokens`, `response_sha256`, `line_item_count`, `missing_fields` | | Extraction call metadata. `response_sha256` fingerprints the model's answer so a suspect extraction can be compared across runs without logging it. `missing_fields` lists schema field names only. |
| `environment`, `draft_status`, `config_keys` | | Credential build, TIG draft-reply status, number of Config keys loaded. |
| `redacted_fields` | list | Names of extra fields that were dropped by the redaction guard. Should never appear; if it does, a logging call is passing something it should not. |
| `logging.googleapis.com/trace`, `.../spanId`, `.../trace_sampled` | | Only when the request carried `X-Cloud-Trace-Context` or `traceparent` and `GCP_PROJECT_ID` is set. |

### Records one run writes

1. `event=run_started` with `candidates`.
2. Per candidate, one record per stage (`stage`, `outcome`, `message_id`, `duration_ms`,
   `reason` when not `ok`): INFO, or WARNING if the stage raised.
3. Per candidate, one `event=candidate_outcome`: INFO, or **ERROR** for `outcome=error`
   (with `stage`, `error_type` and `stack_trace`, so it is also an Error Reporting event).
4. `event=run_summary` once, INFO, with the counts per outcome.
5. `POST /run`: `event=run_completed` (INFO), or `event=run_failed` (ERROR, with
   `error_type` and `stack_trace`) when the run aborted before finishing (Config,
   Ledger header, credentials, polling). `python -m intake` logs `run_failed` too.

## What is never logged

- Invoice or TIG content: supplier/provider names, invoice numbers, amounts, currencies
  as printed, dates, line items, document text, model responses.
- Email content: subjects, bodies, sender addresses, attachment filenames.
- Secrets: OAuth refresh tokens, client secrets, API keys, bearer tokens.
- Exception messages (they can quote any of the above), source lines and local
  variables, including those of chained exceptions.
- Registry numbers (they embed the supplier identifier).

### The redaction guard

Enforced in the formatter for every record, whoever logs it:

1. Only the field names in the table above (`ALLOWED_FIELDS`) are serialised from a
   record's `extra`; any other name is dropped and listed in `redacted_fields`.
2. An allowed field must also hold a safe value: `code` fields a token of at most 200
   characters from `[A-Za-z0-9_.:,/+=-]` (no spaces, no `@`: free text, amounts with
   spaces, names and email addresses fail), `int` fields a plain integer (no `Decimal`,
   no `bool`, no numeric string). Otherwise the field is dropped the same way.
3. Exceptions become `error_type` plus, on ERROR, a message-free `stack_trace`.

`message` is the rendered log message; the codebase builds it only from fixed text and
codes. Tests: `tests/unit/observability/test_logging.py`,
`tests/unit/pipeline/test_orchestrator_observability.py`,
`tests/unit/test_entrypoint_observability.py`,
`tests/unit/extraction/test_extraction_log_fields.py`.

## Querying in Cloud Logging

Base filter for an environment (replace the service name):

```
resource.type="cloud_run_revision"
resource.labels.service_name="kibit-intake-staging"
```

Then add:

| Question | Filter |
|---|---|
| Everything that happened to one email | `jsonPayload.message_id="18c2f0a1b2c3d4e5"` |
| Everything in one run | `jsonPayload.run_id="4f1c..."` |
| Run summaries | `jsonPayload.event="run_summary"` |
| Failed candidates | `jsonPayload.event="candidate_outcome" AND jsonPayload.outcome="error"` |
| Emails flagged for review, and why | `jsonPayload.event="candidate_outcome" AND jsonPayload.outcome="needs_review"` (see `reason`) |
| Failed runs | `jsonPayload.event="run_failed"` |
| Slow extraction calls | `jsonPayload.stage="extract" AND jsonPayload.duration_ms>30000` |
| A leaking logging call | `jsonPayload.redacted_fields:*` |
| Every `/run` request and its status | `httpRequest.requestUrl:"/run"` |

From a `run_failed` or `candidate_outcome` error, the `run_id` leads to the run's other
records and the Error Reporting entry links back to the log entry.

## Metrics and alerts (`infra/monitoring.tf`)

Per environment in `monitoring_environments` (default `staging`, `production`), for the
Cloud Run service named in `monitoring_service_names` (default `kibit-intake-<env>`):

| Log-based metric | Counts | Labels |
|---|---|---|
| `kibit_candidate_outcomes_<env>` | `event=candidate_outcome` entries | `outcome` |
| `kibit_run_failures_<env>` | `event=run_failed` at ERROR | |
| `kibit_run_completions_<env>` | `event=run_summary` | |

`kibit_candidate_outcomes_<env>` also charts the `needs_review` / `awaiting_tig` volume
per run (the QA strategy's backlog-visibility gap) without another metric.

| Alert policy | Fires when | Window |
|---|---|---|
| `Kibit (<env>): pipeline error outcome` | any candidate with `outcome=error` | `alert_evaluation_window_seconds` (600 s = one scheduler cycle) |
| `Kibit (<env>): run failed` | a `run_failed` entry, or any HTTP 5xx from the service (`run.googleapis.com/request_count`, `response_code_class=5xx`) | same |
| `Kibit (<env>): no successful run` | no `run_summary` for `alert_no_successful_run_minutes` (default 60) | absence |

All three email `alert_email` through the notification channel "Kibit bookkeeping
alerts", and carry a "what to do" text in their documentation. A Cloud Monitoring
absence condition only fires for a metric that has had data, so the
"no successful run" alert becomes active after the first completed run in that
environment.

## Verifying in staging (induced failures)

Run these after deploying the staging revision and applying `infra/` (by the deployer,
not from a developer machine). Use the sandbox mailbox only.

1. **Structured logs.** Force-run `kibit-intake-poll-staging` in Cloud Scheduler. In
   Logs Explorer with the base filter, check one `run_started`, stage records, one
   `candidate_outcome` per email and one `run_summary`, all with the same `run_id`, and
   that no entry contains a supplier name, invoice number or amount
   (search the run's entries for a known fixture value, e.g. its supplier name).
2. **Candidate error → Error Reporting + alert email.** Make one candidate fail while the
   run continues: e.g. add an invalid secret version to `kibit-ai-compass-api-key-staging`
   (the extractor is built, but every model call is rejected) with one invoice email
   unread in the sandbox inbox, then force-run. Expect: a `candidate_outcome` ERROR entry with `error_type`
   (an authentication error) and `stage=extract`; a new group in Error Reporting for
   service `kibit-intake-staging`; the "pipeline error outcome" email within the 10-minute
   window. The email stays unread and unlabelled. Disable the bad secret version; the next
   run processes the email.
3. **Run failure.** Rename the `Ledger` tab's column A header in the sandbox sheet, then
   force-run. Expect: HTTP 500 on `/run`, an `event=run_failed` ERROR entry with
   `error_type=LedgerHeaderError`, an Error Reporting event, the "run failed" email. No
   email is touched. Restore the header.
   The same alert fires when the ledger spreadsheet or the Drive root folder is in the
   Drive trash (`error_type=DriveTrashedError`): Drive keeps trashed files working by ID,
   so the service refuses to run rather than keep writing into the trash. Restore the
   file (Drive -> Trash -> Restore).
4. **Missed runs.** Pause the staging scheduler job for longer than
   `alert_no_successful_run_minutes` (or temporarily lower it to 10 and `terraform apply`).
   Expect the "no successful run" email. Resume the job; the incident closes after the
   next `run_summary`.
5. **Leak check.** Logs Explorer: `jsonPayload.redacted_fields:*` over the test window must
   return nothing.
