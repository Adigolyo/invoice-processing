############################################
# Kibit Invoice Intake — Terraform root (Task 22)
# Cloud: GCP | Region: europe-west1 | Environments: staging, production
#
# Brought in from the factory's iac-main.tf and split by concern:
#   main.tf       providers, backend, shared locals, API enablement, Artifact Registry
#   iam.tf        runtime service accounts + project-level roles (Task 3)
#   secrets.tf    Secret Manager containers + per-secret accessor bindings
#   cloudrun.tf   Cloud Run services + invoker bindings
#   scheduler.tf  Cloud Scheduler jobs + their invoker service accounts
#   cicd.tf       Workload Identity Federation + CI service accounts
#   budget.tf     billing budget
#   outputs.tf    outputs used by the runbook and CI
# Log-based metrics and alert policies live in monitoring*.tf (Task 23).
# Every deviation from the factory design is documented in docs/deployment.md.
############################################

terraform {
  required_version = ">= 1.7.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.40"
    }
  }

  # Remote state. The bucket is created once, out-of-band, before the first
  # `terraform init` (docs/deployment.md, step 1).
  backend "gcs" {
    bucket = "kibit-invoice-intake-tfstate"
    prefix = "infra/state"
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

############################################
# Locals
############################################

locals {
  # Both environments live in one project, separated by resource naming. Staging is bound
  # to the sandbox Workspace account, production to the shared production mailbox.
  environments = {
    staging = {
      cpu      = "1"
      memory   = "512Mi"
      schedule = "*/10 * * * *"
    }
    production = {
      cpu      = "1"
      memory   = "512Mi"
      schedule = "*/10 * * * *"
    }
  }

  # Fixed OIDC audience per environment. The Scheduler job mints its token for this
  # audience, Cloud Run accepts it as a custom audience, and the app verifies it exactly
  # (OIDC_AUDIENCE). A fixed string avoids the cycle of a service referencing its own URI.
  oidc_audiences = { for env in keys(local.environments) : env => "kibit-intake-${env}" }

  image_repository = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.intake_images.repository_id}/intake-service"

  required_apis = concat(
    [
      "run.googleapis.com",
      "cloudscheduler.googleapis.com",
      "secretmanager.googleapis.com",
      "artifactregistry.googleapis.com",
      "logging.googleapis.com",
      "monitoring.googleapis.com",
      "iam.googleapis.com",
      "iamcredentials.googleapis.com",
      "sts.googleapis.com",
      "cloudbilling.googleapis.com",
      "billingbudgets.googleapis.com",
      # Workspace APIs called with the mailbox's OAuth token; the OAuth client lives in
      # this project, so these must be enabled here.
      "gmail.googleapis.com",
      "drive.googleapis.com",
      "sheets.googleapis.com",
    ],
    var.enable_vertex_ai_user ? ["aiplatform.googleapis.com"] : [],
  )
}

############################################
# Project API enablement
############################################

resource "google_project_service" "apis" {
  for_each = toset(local.required_apis)
  project  = var.project_id
  service  = each.value

  disable_dependent_services = false
  disable_on_destroy         = false
}

############################################
# Artifact Registry (shared across environments; images are promoted, never rebuilt)
############################################

resource "google_artifact_registry_repository" "intake_images" {
  project       = var.project_id
  location      = var.region
  repository_id = "kibit-intake"
  description   = "Container images for the Kibit invoice intake service"
  format        = "DOCKER"

  depends_on = [google_project_service.apis]
}
