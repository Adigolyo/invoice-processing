############################################
# Kibit Invoice Intake — log-based metrics and alert policies (Task 23)
#
# Built on the application's structured logs (intake/observability/logging.py,
# docs/observability.md). Per environment (var.monitoring_environments):
#
#   metrics  kibit_candidate_outcomes_<env>  one count per candidate, label `outcome`
#            kibit_run_failures_<env>        runs that failed before finishing
#            kibit_run_completions_<env>     runs that finished (event=run_summary)
#   alerts   "Kibit (<env>): pipeline error outcome"   any candidate outcome = error
#            "Kibit (<env>): run failed"               run_failed log or HTTP 5xx
#            "Kibit (<env>): no successful run"        no run_summary for N minutes
#
# All alerts email var.alert_email through one notification channel.
#
# Names follow the factory's iac-main.tf: the `email` notification channel ("Kibit
# bookkeeping alerts"), the kibit_*_<env> metric names and the "Kibit (<env>): ..."
# display names. The factory's severity=ERROR metric `kibit_run_errors_<env>` is replaced
# by the outcome-labelled metric plus the run-failure metric, so one failure sends one
# email instead of two. The Cloud Run service is referenced by name
# (var.monitoring_service_names), not by resource address, so this file does not depend
# on how the service itself is defined.
############################################

locals {
  monitoring_services = {
    for env in var.monitoring_environments :
    env => lookup(var.monitoring_service_names, env, "kibit-intake-${env}")
  }

  # Log entries written by one environment's Cloud Run service.
  monitoring_log_filter = {
    for env, service in local.monitoring_services :
    env => "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"${service}\""
  }

  alert_window = "${var.alert_evaluation_window_seconds}s"
}

############################################
# Notification channel (one, shared by both environments)
############################################

resource "google_monitoring_notification_channel" "email" {
  project      = var.project_id
  display_name = "Kibit bookkeeping alerts"
  type         = "email"

  labels = {
    email_address = var.alert_email
  }
}

############################################
# Log-based metrics
############################################

# One entry per candidate per run: event=candidate_outcome, jsonPayload.outcome is one of
# processed | pending | needs_review | awaiting_tig | error | skipped.
resource "google_logging_metric" "candidate_outcomes" {
  for_each = local.monitoring_services
  project  = var.project_id
  name     = "kibit_candidate_outcomes_${each.key}"
  filter   = "${local.monitoring_log_filter[each.key]} AND jsonPayload.event=\"candidate_outcome\""

  description = "Kibit intake (${each.key}): candidates evaluated, by final outcome."

  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "INT64"
    unit         = "1"
    display_name = "Kibit candidate outcomes (${each.key})"

    labels {
      key         = "outcome"
      value_type  = "STRING"
      description = "processed, pending, needs_review, awaiting_tig, error or skipped"
    }
  }

  label_extractors = {
    "outcome" = "EXTRACT(jsonPayload.outcome)"
  }
}

# A run that aborted before processing candidates (Config, Ledger header, credentials,
# polling): POST /run logs event=run_failed at ERROR and answers 500.
resource "google_logging_metric" "run_failures" {
  for_each = local.monitoring_services
  project  = var.project_id
  name     = "kibit_run_failures_${each.key}"
  filter   = "${local.monitoring_log_filter[each.key]} AND jsonPayload.event=\"run_failed\" AND severity>=ERROR"

  description = "Kibit intake (${each.key}): runs that failed before finishing."

  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "INT64"
    unit         = "1"
    display_name = "Kibit failed runs (${each.key})"
  }
}

# A run that finished (whatever its candidates' outcomes): event=run_summary, once per run.
resource "google_logging_metric" "run_completions" {
  for_each = local.monitoring_services
  project  = var.project_id
  name     = "kibit_run_completions_${each.key}"
  filter   = "${local.monitoring_log_filter[each.key]} AND jsonPayload.event=\"run_summary\""

  description = "Kibit intake (${each.key}): runs that completed and logged their summary."

  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "INT64"
    unit         = "1"
    display_name = "Kibit completed runs (${each.key})"
  }
}

############################################
# Alert policies
############################################

resource "google_monitoring_alert_policy" "error_outcome" {
  for_each     = local.monitoring_services
  project      = var.project_id
  display_name = "Kibit (${each.key}): pipeline error outcome"
  combiner     = "OR"
  severity     = "ERROR"

  conditions {
    display_name = "A candidate ended with outcome=error"
    condition_threshold {
      filter          = "resource.type=\"cloud_run_revision\" AND metric.type=\"logging.googleapis.com/user/${google_logging_metric.candidate_outcomes[each.key].name}\" AND metric.labels.outcome=\"error\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"

      aggregations {
        alignment_period     = local.alert_window
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
      }

      trigger {
        count = 1
      }
    }
  }

  alert_strategy {
    auto_close = "86400s"
  }

  documentation {
    mime_type = "text/markdown"
    content   = <<-EOT
      **An invoice email could not be processed** (environment: `${each.key}`, Cloud Run service `${each.value}`).

      The email was left **unread and unlabelled**, so the next run (every 10 minutes) retries it
      automatically. Nothing was half-booked: no label, read state or ledger row was changed for it.

      What to do:
      1. In Cloud Logging, find the failed candidate:
         `${local.monitoring_log_filter[each.key]} AND jsonPayload.event="candidate_outcome" AND jsonPayload.outcome="error"`
         It names the `message_id`, the failed `stage` and the `error_type` (never invoice content).
      2. Open the same `run_id` / `message_id` in Error Reporting for the stack trace.
      3. If the next runs succeed, the error was transient (API outage, quota): no action needed.
         If the same `message_id` keeps failing, process that email by hand (file it, book it,
         label it `Kibit/Processed`) and raise a bug with the `error_type` and `stage`.

      Runbook: docs/observability.md in the repository.
    EOT
  }

  notification_channels = [google_monitoring_notification_channel.email.name]
}

resource "google_monitoring_alert_policy" "run_failed" {
  for_each     = local.monitoring_services
  project      = var.project_id
  display_name = "Kibit (${each.key}): run failed"
  combiner     = "OR"
  severity     = "ERROR"

  conditions {
    display_name = "A run logged event=run_failed"
    condition_threshold {
      filter          = "resource.type=\"cloud_run_revision\" AND metric.type=\"logging.googleapis.com/user/${google_logging_metric.run_failures[each.key].name}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"

      aggregations {
        alignment_period     = local.alert_window
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
      }

      trigger {
        count = 1
      }
    }
  }

  conditions {
    display_name = "Cloud Run answered HTTP 5xx"
    condition_threshold {
      filter          = "${local.monitoring_log_filter[each.key]} AND metric.type=\"run.googleapis.com/request_count\" AND metric.labels.response_code_class=\"5xx\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"

      aggregations {
        alignment_period     = local.alert_window
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
      }

      trigger {
        count = 1
      }
    }
  }

  alert_strategy {
    auto_close = "86400s"
  }

  documentation {
    mime_type = "text/markdown"
    content   = <<-EOT
      **A whole intake run failed** (environment: `${each.key}`, Cloud Run service `${each.value}`).

      The run stopped before or while polling, so **no email was touched**; the next scheduled run
      retries everything. Typical causes: the ledger's `Config` tab or `Ledger` header row was
      edited, the OAuth refresh token was revoked or expired, a Google API outage, a timeout.

      What to do:
      1. In Cloud Logging, run
         `${local.monitoring_log_filter[each.key]} AND jsonPayload.event="run_failed"`
         and read `error_type`; the same entry is in Error Reporting with its stack trace.
      2. `ConfigError` / `LedgerHeaderError`: fix the `Config` tab or restore the `Ledger` header
         (columns A-H). `RefreshError` / auth errors: re-run the OAuth consent flow
         (deployment runbook) and add the new refresh token as a secret version.
      3. A 5xx without a run_failed entry means the request died outside the pipeline
         (container crash, out of memory, the 540s timeout): check the Cloud Run revision logs.
      4. If it does not recur on the next run, it was transient.

      Runbook: docs/observability.md in the repository.
    EOT
  }

  notification_channels = [google_monitoring_notification_channel.email.name]
}

resource "google_monitoring_alert_policy" "no_successful_run" {
  for_each     = local.monitoring_services
  project      = var.project_id
  display_name = "Kibit (${each.key}): no successful run"
  combiner     = "OR"
  severity     = "WARNING"

  conditions {
    display_name = "No run_summary for ${var.alert_no_successful_run_minutes} minutes"
    condition_absent {
      filter   = "resource.type=\"cloud_run_revision\" AND metric.type=\"logging.googleapis.com/user/${google_logging_metric.run_completions[each.key].name}\""
      duration = "${var.alert_no_successful_run_minutes * 60}s"

      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
      }

      trigger {
        count = 1
      }
    }
  }

  alert_strategy {
    auto_close = "86400s"
  }

  documentation {
    mime_type = "text/markdown"
    content   = <<-EOT
      **No intake run has completed for ${var.alert_no_successful_run_minutes} minutes** (environment: `${each.key}`, Cloud Run service `${each.value}`).

      Invoices are not being processed. Either Cloud Scheduler stopped calling `POST /run`, the
      calls are rejected (401: OIDC audience or scheduler service account changed), or every run
      fails (then the "run failed" alert fires too).

      What to do:
      1. Cloud Scheduler: is job `kibit-intake-poll-${each.key}` enabled, and what did its last
         attempts return?
      2. Cloud Logging: `${local.monitoring_log_filter[each.key]} AND httpRequest.requestUrl:"/run"`
         shows each call and its status; a 401 means the scheduler's OIDC token is refused
         (check `OIDC_AUDIENCE` and `SCHEDULER_SERVICE_ACCOUNT_EMAIL`).
      3. Trigger one run by hand (Cloud Scheduler "Force run") and confirm an
         `event="run_summary"` entry appears.

      Runbook: docs/observability.md in the repository.
    EOT
  }

  notification_channels = [google_monitoring_notification_channel.email.name]
}

output "monitoring_alert_policy_names" {
  description = "Alert policy resource names per environment."
  value = {
    for env in keys(local.monitoring_services) : env => {
      error_outcome     = google_monitoring_alert_policy.error_outcome[env].name
      run_failed        = google_monitoring_alert_policy.run_failed[env].name
      no_successful_run = google_monitoring_alert_policy.no_successful_run[env].name
    }
  }
}
