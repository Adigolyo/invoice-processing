output "service_urls" {
  description = "Cloud Run service URL per environment."
  value       = { for env, svc in google_cloud_run_v2_service.intake : env => svc.uri }
}

output "staging_url" {
  description = "Staging Cloud Run service URL."
  value       = google_cloud_run_v2_service.intake["staging"].uri
}

output "production_url" {
  description = "Production Cloud Run service URL."
  value       = google_cloud_run_v2_service.intake["production"].uri
}

output "oidc_audiences" {
  description = "OIDC audience the Scheduler token is minted for, per environment (also OIDC_AUDIENCE)."
  value       = local.oidc_audiences
}

output "image_repository" {
  description = "Artifact Registry path for the intake-service image."
  value       = local.image_repository
}

output "runtime_service_account_emails" {
  description = "Runtime service account email per environment (Cloud Run service identity)."
  value       = { for env, sa in google_service_account.runtime : env => sa.email }
}

output "scheduler_service_account_emails" {
  description = "Scheduler invoker service account email per environment."
  value       = { for env, sa in google_service_account.scheduler_invoker : env => sa.email }
}

output "ci_deployer_service_account_email" {
  description = "Set as the GitHub variable CI_DEPLOYER_SA_EMAIL."
  value       = google_service_account.ci_deployer.email
}

output "ci_e2e_service_account_email" {
  description = "Set as the GitHub variable CI_E2E_SA_EMAIL."
  value       = google_service_account.ci_e2e.email
}

output "workload_identity_provider" {
  description = "Set as the GitHub variable WIF_PROVIDER."
  value       = google_iam_workload_identity_pool_provider.github.name
}
