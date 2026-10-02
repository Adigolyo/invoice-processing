############################################
# Kibit Invoice Intake — runtime identity, secrets and least-privilege IAM (Task 3)
#
# Mirrors the factory's iac-main.tf: same resource addresses, names and roles, so when
# Task 22 brings in the rest of iac-main.tf (Cloud Run, Scheduler, CI, monitoring) it must
# take these blocks from here rather than redefine them.
#
# Runtime SA (kibit-intake-<env>) gets exactly:
#   - roles/secretmanager.secretAccessor, scoped per secret (never project-wide)
#   - roles/logging.logWriter      (project)
#   - roles/aiplatform.user        (project; Vertex AI / Gemini)
# Nothing else. Secret *values* are never managed here (see deployment-runbook.md).
############################################

locals {
  iam_environments = toset(["staging", "production"])
}

############################################
# Runtime service account, one per environment
############################################

resource "google_service_account" "runtime" {
  for_each     = local.iam_environments
  project      = var.project_id
  account_id   = "kibit-intake-${each.key}"
  display_name = "Kibit Intake Runtime SA (${each.key})"
}

############################################
# Secret containers (values added out-of-band via `gcloud secrets versions add`)
############################################

resource "google_secret_manager_secret" "oauth_refresh_token" {
  for_each  = local.iam_environments
  project   = var.project_id
  secret_id = "kibit-oauth-refresh-token-${each.key}"

  replication {
    auto {}
  }
}

resource "google_secret_manager_secret" "gemini_api_key" {
  for_each  = local.iam_environments
  project   = var.project_id
  secret_id = "kibit-gemini-api-key-${each.key}"

  replication {
    auto {}
  }
}

# Not in the factory's iac-main.tf: the OAuth client that minted the refresh token. The
# factory documents do not say where the client ID/secret live; intake/clients/auth.py
# reads them from these secrets so no credential material is ever in env vars or code.
resource "google_secret_manager_secret" "oauth_client_id" {
  for_each  = local.iam_environments
  project   = var.project_id
  secret_id = "kibit-oauth-client-id-${each.key}"

  replication {
    auto {}
  }
}

resource "google_secret_manager_secret" "oauth_client_secret" {
  for_each  = local.iam_environments
  project   = var.project_id
  secret_id = "kibit-oauth-client-secret-${each.key}"

  replication {
    auto {}
  }
}

############################################
# Secret Accessor, scoped to each environment's own secrets only
############################################

resource "google_secret_manager_secret_iam_member" "oauth_access" {
  for_each  = local.iam_environments
  project   = var.project_id
  secret_id = google_secret_manager_secret.oauth_refresh_token[each.key].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runtime[each.key].email}"
}

resource "google_secret_manager_secret_iam_member" "gemini_access" {
  for_each  = local.iam_environments
  project   = var.project_id
  secret_id = google_secret_manager_secret.gemini_api_key[each.key].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runtime[each.key].email}"
}

resource "google_secret_manager_secret_iam_member" "oauth_client_id_access" {
  for_each  = local.iam_environments
  project   = var.project_id
  secret_id = google_secret_manager_secret.oauth_client_id[each.key].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runtime[each.key].email}"
}

resource "google_secret_manager_secret_iam_member" "oauth_client_secret_access" {
  for_each  = local.iam_environments
  project   = var.project_id
  secret_id = google_secret_manager_secret.oauth_client_secret[each.key].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runtime[each.key].email}"
}

############################################
# Project-level roles (log writing and Vertex AI only)
############################################

resource "google_project_iam_member" "runtime_log_writer" {
  for_each = local.iam_environments
  project  = var.project_id
  role     = "roles/logging.logWriter"
  member   = "serviceAccount:${google_service_account.runtime[each.key].email}"
}

resource "google_project_iam_member" "runtime_vertex_user" {
  for_each = local.iam_environments
  project  = var.project_id
  role     = "roles/aiplatform.user"
  member   = "serviceAccount:${google_service_account.runtime[each.key].email}"
}

output "runtime_service_account_emails" {
  description = "Runtime service account email per environment (Cloud Run service identity)."
  value       = { for env, sa in google_service_account.runtime : env => sa.email }
}
