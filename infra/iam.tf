############################################
# Kibit Invoice Intake — runtime identity and project-level roles (Tasks 3, 22)
#
# Same resource addresses as the factory's iac-main.tf. Task 22 moved the Secret Manager
# containers and their accessor bindings to secrets.tf (addresses unchanged) and put the
# Vertex AI role behind enable_vertex_ai_user.
#
# Runtime SA (kibit-intake-<env>) gets exactly:
#   - roles/secretmanager.secretAccessor, per secret (secrets.tf; never project-wide)
#   - roles/logging.logWriter (project)
#   - roles/aiplatform.user (project) ONLY when enable_vertex_ai_user = true. Extraction
#     goes to the AI Compass gateway (ADR 4 revised) and the optional Gemini backend uses
#     an API key, so Vertex AI is not used by default.
############################################

resource "google_service_account" "runtime" {
  for_each     = local.environments
  project      = var.project_id
  account_id   = "kibit-intake-${each.key}"
  display_name = "Kibit Intake Runtime SA (${each.key})"
}

resource "google_project_iam_member" "runtime_log_writer" {
  for_each = local.environments
  project  = var.project_id
  role     = "roles/logging.logWriter"
  member   = "serviceAccount:${google_service_account.runtime[each.key].email}"
}

resource "google_project_iam_member" "runtime_vertex_user" {
  for_each = var.enable_vertex_ai_user ? local.environments : {}
  project  = var.project_id
  role     = "roles/aiplatform.user"
  member   = "serviceAccount:${google_service_account.runtime[each.key].email}"
}
