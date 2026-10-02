############################################
# CI/CD: Workload Identity Federation (keyless GitHub Actions auth) (Task 22)
#
# Two identities, both reachable only from workflows of var.github_repository running on
# refs/heads/main:
#   kibit-ci-deployer  pushes images, deploys new revisions (gcloud), smoke-tests.
#                      No Secret Manager access.
#   kibit-ci-e2e       runs the fixture-set release gate against the SANDBOX account:
#                      reads the staging secrets only, never production's.
############################################

resource "google_iam_workload_identity_pool" "github" {
  project                   = var.project_id
  workload_identity_pool_id = "github-actions-pool"
  display_name              = "GitHub Actions"

  depends_on = [google_project_service.apis]
}

resource "google_iam_workload_identity_pool_provider" "github" {
  project                            = var.project_id
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github-actions-provider"
  display_name                       = "GitHub Actions OIDC"

  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
    "attribute.ref"        = "assertion.ref"
  }

  # Only this repository, and only workflows running on main (deploy.yml is triggered by
  # workflow_run / workflow_dispatch on main), can exchange a GitHub token.
  attribute_condition = "assertion.repository == \"${var.github_repository}\" && assertion.ref == \"refs/heads/main\""

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

locals {
  github_principal_set = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository/${var.github_repository}"
}

############################################
# CI deployer
############################################

resource "google_service_account" "ci_deployer" {
  project      = var.project_id
  account_id   = "kibit-ci-deployer"
  display_name = "GitHub Actions CI deployer"
}

resource "google_service_account_iam_member" "ci_wif_binding" {
  service_account_id = google_service_account.ci_deployer.name
  role               = "roles/iam.workloadIdentityUser"
  member             = local.github_principal_set
}

resource "google_artifact_registry_repository_iam_member" "ci_push" {
  project    = var.project_id
  location   = var.region
  repository = google_artifact_registry_repository.intake_images.repository_id
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.ci_deployer.email}"
}

resource "google_cloud_run_v2_service_iam_member" "ci_can_deploy" {
  for_each = local.environments
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.intake[each.key].name
  role     = "roles/run.developer"
  member   = "serviceAccount:${google_service_account.ci_deployer.email}"
}

# Deploying a revision that runs as the runtime SA requires iam.serviceAccounts.actAs on
# that SA (missing from the factory design; without it `gcloud run services update` fails).
resource "google_service_account_iam_member" "ci_acts_as_runtime" {
  for_each           = local.environments
  service_account_id = google_service_account.runtime[each.key].name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.ci_deployer.email}"
}

############################################
# CI release gate (fixture-set E2E against the sandbox account = staging)
############################################

resource "google_service_account" "ci_e2e" {
  project      = var.project_id
  account_id   = "kibit-ci-e2e"
  display_name = "GitHub Actions fixture-set E2E (sandbox only)"
}

resource "google_service_account_iam_member" "ci_e2e_wif_binding" {
  service_account_id = google_service_account.ci_e2e.name
  role               = "roles/iam.workloadIdentityUser"
  member             = local.github_principal_set
}

locals {
  # Staging (sandbox) secrets the E2E suite needs to build the same clients the service
  # builds (GCP_PROJECT_ID + ENVIRONMENT=staging).
  ci_e2e_secret_ids = {
    oauth_refresh_token = google_secret_manager_secret.oauth_refresh_token["staging"].secret_id
    oauth_client_id     = google_secret_manager_secret.oauth_client_id["staging"].secret_id
    oauth_client_secret = google_secret_manager_secret.oauth_client_secret["staging"].secret_id
    ai_compass_api_key  = google_secret_manager_secret.ai_compass_api_key["staging"].secret_id
  }
}

resource "google_secret_manager_secret_iam_member" "ci_e2e_staging_access" {
  for_each  = local.ci_e2e_secret_ids
  project   = var.project_id
  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.ci_e2e.email}"
}
