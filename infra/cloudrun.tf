############################################
# Cloud Run: one service per environment (Task 22)
#
# Concurrency model (ADR 5): one instance, one request at a time. The pipeline derives
# sequence numbers and the duplicate check from live Drive/Sheets contents and assumes no
# other run touches the same mailbox concurrently. With max_instance_count = 1 and
# max_instance_request_concurrency = 1, a second POST /run while a run is in progress is
# queued briefly and then rejected (429) instead of starting a parallel run. Residual
# overlap risk (deploys, local runs) is documented in docs/deployment.md.
#
# Egress: default (direct internet). The service needs outbound HTTPS to Google APIs
# (Gmail, Drive, Sheets, Secret Manager, OAuth token endpoint) and to the AI Compass
# gateway (https://ai-compass.kibit.cloud). No VPC connector or egress restriction.
#
# Image: Terraform sets the image when the service is created (var.image_tags); later
# deploys are made by CI with `gcloud run services update --image` (deploy.yml), so
# Terraform ignores image drift instead of fighting the pipeline.
############################################

resource "google_cloud_run_v2_service" "intake" {
  for_each = local.environments
  project  = var.project_id
  name     = "kibit-intake-${each.key}"
  location = var.region

  # Public HTTPS endpoint so Cloud Scheduler can reach it; access is controlled by the
  # run.invoker bindings below (no allUsers) and by the app's own OIDC check on /run.
  ingress = "INGRESS_TRAFFIC_ALL"

  # The Scheduler's OIDC token is minted for this fixed audience (see main.tf).
  custom_audiences = [local.oidc_audiences[each.key]]

  template {
    service_account                  = google_service_account.runtime[each.key].email
    execution_environment            = "EXECUTION_ENVIRONMENT_GEN2"
    timeout                          = "${var.request_timeout_seconds}s"
    max_instance_request_concurrency = 1

    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }

    containers {
      image = "${local.image_repository}:${lookup(var.image_tags, each.key, "bootstrap")}"

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = each.value.cpu
          memory = each.value.memory
        }
        # CPU only while a request is being served (scale-to-zero billing).
        cpu_idle = true
      }

      # Startup only. Deliberately no liveness probe: the single gunicorn worker is busy
      # for the whole run and could not answer /healthz, so a liveness probe would kill
      # the instance mid-run.
      startup_probe {
        http_get {
          path = "/healthz"
        }
        period_seconds    = 5
        failure_threshold = 12
      }

      env {
        name  = "ENVIRONMENT"
        value = each.key
      }
      env {
        name  = "GCP_PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "LEDGER_SPREADSHEET_ID"
        value = lookup(var.ledger_spreadsheet_ids, each.key, "")
      }
      env {
        # Optional cross-check: the Drive root comes from the Config tab; when set, the
        # run refuses to file into a different folder.
        name  = "DRIVE_ROOT_FOLDER_ID"
        value = lookup(var.drive_root_folder_ids, each.key, "")
      }
      env {
        name  = "OIDC_AUDIENCE"
        value = local.oidc_audiences[each.key]
      }
      env {
        name  = "SCHEDULER_SERVICE_ACCOUNT_EMAIL"
        value = google_service_account.scheduler_invoker[each.key].email
      }
      env {
        name  = "EXTRACTOR_BACKEND"
        value = "ai_compass"
      }
      env {
        name  = "AI_COMPASS_BASE_URL"
        value = var.ai_compass_base_url
      }
      env {
        name  = "AI_COMPASS_MODEL"
        value = var.ai_compass_model
      }
      env {
        name  = "AI_COMPASS_EFFORT"
        value = var.ai_compass_effort
      }
      env {
        # Read by the Dockerfile's gunicorn command: the worker timeout matches the
        # Cloud Run request timeout so gunicorn never kills a run Cloud Run still allows.
        name  = "GUNICORN_TIMEOUT"
        value = tostring(var.request_timeout_seconds)
      }
    }
  }

  traffic {
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
    percent = 100
  }

  lifecycle {
    ignore_changes = [
      template[0].containers[0].image,
      client,
      client_version,
    ]
  }

  depends_on = [
    google_project_service.apis,
    google_secret_manager_secret_iam_member.oauth_access,
    google_secret_manager_secret_iam_member.oauth_client_id_access,
    google_secret_manager_secret_iam_member.oauth_client_secret_access,
    google_secret_manager_secret_iam_member.ai_compass_access,
  ]
}

############################################
# Invokers: who may call the service through Cloud Run IAM
############################################

# The Scheduler SA is the only principal the app accepts on POST /run.
resource "google_cloud_run_v2_service_iam_member" "scheduler_can_invoke" {
  for_each = local.environments
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.intake[each.key].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler_invoker[each.key].email}"
}

# The CI deployer may reach the service for the post-deploy smoke test (GET /healthz with
# an identity token). It cannot trigger a run: the app rejects any POST /run whose token
# is not the Scheduler SA's (401), which the smoke test also asserts.
resource "google_cloud_run_v2_service_iam_member" "ci_can_invoke" {
  for_each = local.environments
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.intake[each.key].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.ci_deployer.email}"
}
