############################################
# FinOps: monthly billing budget (factory iac-main.tf)
#
# Created only when billing_account_id is set (the caller needs Billing Account
# Administrator or Billing Account Costs Manager on that account). Notifications go to
# the billing account's default IAM recipients; the factory's e-mail notification channel
# is a monitoring resource and belongs to monitoring*.tf (Task 23), which may add it to
# this rule.
############################################

resource "google_billing_budget" "monthly_cap" {
  count = var.billing_account_id == "" ? 0 : 1

  billing_account = var.billing_account_id
  display_name    = "Kibit Invoice Intake — monthly budget"

  budget_filter {
    projects = ["projects/${data.google_project.this.number}"]
  }

  amount {
    specified_amount {
      currency_code = "USD"
      units         = tostring(var.monthly_budget_usd)
    }
  }

  threshold_rules {
    threshold_percent = 0.5
  }
  threshold_rules {
    threshold_percent = 0.9
  }
  threshold_rules {
    threshold_percent = 1.0
  }

  # No all_updates_rule: threshold e-mails go to the billing account's default IAM
  # recipients (Billing Account Administrators/Users).

  depends_on = [google_project_service.apis]
}

data "google_project" "this" {
  project_id = var.project_id
}
