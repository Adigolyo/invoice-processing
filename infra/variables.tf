############################################
# Kibit Invoice Intake — Terraform Variables (subset needed by iam.tf)
# Copied verbatim from the factory's iac-variables.tf; Task 22 adds the rest.
############################################

variable "project_id" {
  description = "GCP project ID hosting both staging and production Cloud Run services."
  type        = string
}
