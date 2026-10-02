############################################
# Kibit Invoice Intake — Terraform variables
# From the factory's iac-variables.tf, plus the Task 22 additions marked below.
# alert_email (factory) is declared by monitoring*.tf (Task 23), which uses it.
############################################

variable "project_id" {
  description = "GCP project ID hosting both staging and production Cloud Run services (kibit-invoice-intake)."
  type        = string
}

variable "region" {
  description = "Primary GCP region."
  type        = string
  default     = "europe-west1"
}

variable "billing_account_id" {
  description = "GCP billing account ID (XXXXXX-XXXXXX-XXXXXX) for the budget alert. Empty skips the budget. Set via tfvars, never hardcoded."
  type        = string
  default     = ""
}

variable "monthly_budget_usd" {
  description = "Soft monthly budget cap in USD across both environments (Technical Intake ceiling is < $100/month)."
  type        = number
  default     = 80
}

variable "github_repository" {
  description = "GitHub repository in 'owner/repo' form; scopes Workload Identity Federation to this repo's workflows on main."
  type        = string
}

variable "image_tags" {
  description = "Environment -> image tag used when a Cloud Run service is first created (push it before the first full apply). Later deploys are made by CI with gcloud; Terraform ignores image changes."
  type        = map(string)
  default = {
    staging    = "bootstrap"
    production = "bootstrap"
  }
}

variable "ledger_spreadsheet_ids" {
  description = "Environment -> ledger spreadsheet ID (staging = sandbox account, production = shared mailbox account)."
  type        = map(string)
  default     = {}
}

variable "drive_root_folder_ids" {
  description = "Environment -> Drive root 'Invoices' folder ID. Optional cross-check of the Config tab's drive_root_folder_id."
  type        = map(string)
  default     = {}
}

############################################
# Task 22 additions
############################################

variable "request_timeout_seconds" {
  description = "Cloud Run request timeout and gunicorn worker timeout, in seconds. Sized for >= 100 invoices at ~8-12 s each (incl. TIG extraction). Cloud Run maximum is 3600."
  type        = number
  default     = 3600

  validation {
    condition     = var.request_timeout_seconds >= 60 && var.request_timeout_seconds <= 3600
    error_message = "request_timeout_seconds must be between 60 and 3600 (Cloud Run maximum)."
  }
}

variable "scheduler_attempt_deadline_seconds" {
  description = "How long Cloud Scheduler waits for POST /run before recording a failed attempt. Must be <= 1800 (Scheduler maximum for HTTP targets) and <= request_timeout_seconds."
  type        = number
  default     = 1800

  validation {
    condition     = var.scheduler_attempt_deadline_seconds >= 15 && var.scheduler_attempt_deadline_seconds <= 1800
    error_message = "scheduler_attempt_deadline_seconds must be between 15 and 1800."
  }
}

variable "ai_compass_base_url" {
  description = "Base URL of Kibit's AI Compass gateway (ADR 4 revised)."
  type        = string
  default     = "https://ai-compass.kibit.cloud"
}

variable "ai_compass_model" {
  description = "Claude model served by AI Compass."
  type        = string
  default     = "claude-sonnet-5-5"
}

variable "ai_compass_effort" {
  description = "output_config.effort for extraction calls (low | medium | high | xhigh | max)."
  type        = string
  default     = "medium"

  validation {
    condition     = contains(["low", "medium", "high", "xhigh", "max"], var.ai_compass_effort)
    error_message = "ai_compass_effort must be one of low, medium, high, xhigh, max."
  }
}

variable "enable_vertex_ai_user" {
  description = "Grant the runtime SAs roles/aiplatform.user and enable the Vertex AI API. Off: extraction uses AI Compass, and the optional Gemini backend authenticates with an API key, not Vertex AI."
  type        = bool
  default     = false
}

############################################
# NOTE on secret values:
# OAuth refresh tokens, OAuth client ID/secret and the AI Compass / Gemini API keys are
# intentionally NOT Terraform variables. Terraform provisions the Secret Manager
# containers only (secrets.tf); values are added out-of-band with
# `gcloud secrets versions add` (docs/deployment.md), keeping credentials out of tfvars,
# CI logs and Terraform state.
############################################
