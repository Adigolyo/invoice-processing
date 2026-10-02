############################################
# Secret Manager: empty containers + per-secret accessor bindings (Tasks 3, 22)
#
# Terraform owns the containers and IAM only. Values are added out-of-band with
# `gcloud secrets versions add` (docs/deployment.md, step 4), so no credential is ever in
# tfvars, Terraform state, CI logs or Cloud Run env vars. The service reads them at
# runtime through the Secret Manager API:
#   intake/clients/auth.py                 kibit-oauth-{refresh-token,client-id,client-secret}-<env>
#   intake/extraction/claude_extractor.py  kibit-ai-compass-api-key-<env>
#   intake/extraction/gemini_extractor.py  kibit-gemini-api-key-<env> (only if EXTRACTOR_BACKEND=gemini)
#
# Each secret is readable by its own environment's runtime SA only.
############################################

resource "google_secret_manager_secret" "oauth_refresh_token" {
  for_each  = local.environments
  project   = var.project_id
  secret_id = "kibit-oauth-refresh-token-${each.key}"

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret" "oauth_client_id" {
  for_each  = local.environments
  project   = var.project_id
  secret_id = "kibit-oauth-client-id-${each.key}"

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret" "oauth_client_secret" {
  for_each  = local.environments
  project   = var.project_id
  secret_id = "kibit-oauth-client-secret-${each.key}"

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret" "ai_compass_api_key" {
  for_each  = local.environments
  project   = var.project_id
  secret_id = "kibit-ai-compass-api-key-${each.key}"

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}

# Optional backend (EXTRACTOR_BACKEND=gemini). Kept so switching backends needs no infra
# change; it may stay empty while the AI Compass backend is selected.
resource "google_secret_manager_secret" "gemini_api_key" {
  for_each  = local.environments
  project   = var.project_id
  secret_id = "kibit-gemini-api-key-${each.key}"

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}

############################################
# Secret Accessor, scoped to each environment's own secrets
############################################

resource "google_secret_manager_secret_iam_member" "oauth_access" {
  for_each  = local.environments
  project   = var.project_id
  secret_id = google_secret_manager_secret.oauth_refresh_token[each.key].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runtime[each.key].email}"
}

resource "google_secret_manager_secret_iam_member" "oauth_client_id_access" {
  for_each  = local.environments
  project   = var.project_id
  secret_id = google_secret_manager_secret.oauth_client_id[each.key].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runtime[each.key].email}"
}

resource "google_secret_manager_secret_iam_member" "oauth_client_secret_access" {
  for_each  = local.environments
  project   = var.project_id
  secret_id = google_secret_manager_secret.oauth_client_secret[each.key].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runtime[each.key].email}"
}

resource "google_secret_manager_secret_iam_member" "ai_compass_access" {
  for_each  = local.environments
  project   = var.project_id
  secret_id = google_secret_manager_secret.ai_compass_api_key[each.key].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runtime[each.key].email}"
}

resource "google_secret_manager_secret_iam_member" "gemini_access" {
  for_each  = local.environments
  project   = var.project_id
  secret_id = google_secret_manager_secret.gemini_api_key[each.key].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runtime[each.key].email}"
}
