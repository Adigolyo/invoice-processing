############################################
# Cloud Scheduler: OIDC-authenticated trigger, one job per environment (Task 22)
############################################

resource "google_service_account" "scheduler_invoker" {
  for_each     = local.environments
  project      = var.project_id
  account_id   = "kibit-scheduler-${each.key}"
  display_name = "Kibit Cloud Scheduler invoker (${each.key})"
}

resource "google_cloud_scheduler_job" "poll" {
  for_each  = local.environments
  project   = var.project_id
  region    = var.region
  name      = "kibit-intake-poll-${each.key}"
  schedule  = each.value.schedule
  time_zone = "Europe/Budapest"

  # 30 min is Cloud Scheduler's maximum for HTTP targets. A run longer than this keeps
  # going on Cloud Run (up to request_timeout_seconds); the job only records the attempt
  # as DEADLINE_EXCEEDED.
  attempt_deadline = "${var.scheduler_attempt_deadline_seconds}s"

  http_target {
    http_method = "POST"
    uri         = "${google_cloud_run_v2_service.intake[each.key].uri}/run"

    oidc_token {
      service_account_email = google_service_account.scheduler_invoker[each.key].email
      audience              = local.oidc_audiences[each.key]
    }
  }

  # No retries: a retry of a timed-out or failed attempt could start while the first run
  # is still executing. A failed cycle is simply picked up by the next 10-minute tick
  # (every stage is idempotent and nothing is labelled before it succeeds).
  retry_config {
    retry_count = 0
  }

  depends_on = [
    google_project_service.apis,
    google_cloud_run_v2_service_iam_member.scheduler_can_invoke,
  ]
}
