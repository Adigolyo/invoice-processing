############################################
# Kibit Invoice Intake — monitoring and alerting variables (Task 23)
#
# Only the variables monitoring.tf needs. `project_id` is declared in variables.tf.
# Every name here is prefixed `monitoring_` or `alert_`; `alert_email` is the factory's
# own name (iac-variables.tf) and must be declared in exactly one file.
############################################

variable "alert_email" {
  description = "Distribution email for error-outcome, failed-run and missed-run alerts (the bookkeeping function's inbox, per the Technical Design's alerting requirement)."
  type        = string

  validation {
    condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.alert_email))
    error_message = "alert_email must be a single email address."
  }
}

variable "monitoring_environments" {
  description = "Environments to monitor; one set of log-based metrics and alert policies each."
  type        = set(string)
  default     = ["staging", "production"]
}

variable "monitoring_service_names" {
  description = "Map of environment -> Cloud Run service name to monitor (the factory's naming: kibit-intake-<environment>). An environment missing here falls back to kibit-intake-<environment>."
  type        = map(string)
  default = {
    staging    = "kibit-intake-staging"
    production = "kibit-intake-production"
  }
}

variable "alert_evaluation_window_seconds" {
  description = "Alignment window for the error-outcome and failed-run alerts. Default 600s = one Cloud Scheduler cycle (*/10 * * * *)."
  type        = number
  default     = 600

  validation {
    condition     = var.alert_evaluation_window_seconds >= 60 && var.alert_evaluation_window_seconds % 60 == 0
    error_message = "alert_evaluation_window_seconds must be a whole number of minutes, at least 60."
  }
}

variable "alert_no_successful_run_minutes" {
  description = "Alert when no run has completed (event=run_summary) for this many minutes: the scheduler or the service is silently broken. Default 60 = six missed 10-minute cycles."
  type        = number
  default     = 60

  validation {
    condition     = var.alert_no_successful_run_minutes >= 10 && var.alert_no_successful_run_minutes <= 1440
    error_message = "alert_no_successful_run_minutes must be between 10 and 1440 (Cloud Monitoring's absence window limit is 24h)."
  }
}
