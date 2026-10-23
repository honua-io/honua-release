variable "account_id" {
  description = "Explicit reviewed foundation account; provider and workflow refuse another account."
  type        = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "Supply an exact AWS account ID."
  }
}

variable "region" { type = string }
variable "owner" { type = string }
variable "expires_at" { type = string }
variable "name_prefix" {
  type = string
  validation {
    condition     = can(regex("^honua-pilot-[a-z0-9-]{4,16}$", var.name_prefix))
    error_message = "An isolated pilot prefix is required."
  }
}
variable "oidc_provider_arn" {
  description = "Existing account GitHub OIDC provider from the organization foundation."
  type        = string
  validation {
    condition     = can(regex("^arn:aws:iam::${var.account_id}:oidc-provider/token.actions.githubusercontent.com$", var.oidc_provider_arn))
    error_message = "Bind the existing GitHub OIDC provider in the reviewed account."
  }
}
variable "provision_policy_document" {
  description = "Reviewed account-specific least-privilege Terraform mutation policy. State/plan/bootstrap identity access is supplied separately and protected by explicit denies."
  type        = string
  validation {
    condition = try(jsondecode(var.provision_policy_document).Version == "2012-10-17" && alltrue([
      for statement in jsondecode(var.provision_policy_document).Statement :
      !(statement.Effect == "Allow" && contains(flatten([statement.Action]), "*"))
    ]), false)
    error_message = "Supply a reviewed explicit-action IAM policy; global action wildcard is refused."
  }
}
variable "runtime_secret_arns" {
  description = "Exact independent certificate, audit-chain and agent secret references; bootstrap never reads values."
  type        = list(string)
}
variable "runtime_kms_key_arns" {
  type    = list(string)
  default = []
}
variable "budget_ledger" {
  description = "Owner budget reservation metadata only. Source/config SHA bind a subsequent reviewed deployment; no secret values."
  type = object({
    schema                     = string
    expires_at                 = string
    source_sha                 = string
    config_sha256              = string
    total_budget_usd           = number
    cleanup_reserve_usd        = number
    spent_usd                  = number
    reserved_usd               = number
    deployment_reservation_usd = number
    active_paid_attempts       = number
  })
  validation {
    condition     = var.budget_ledger.schema == "honua.hosted-pilot-budget/v1" && var.budget_ledger.total_budget_usd == 2000 && var.budget_ledger.cleanup_reserve_usd == 200 && var.budget_ledger.spent_usd >= 0 && var.budget_ledger.reserved_usd >= 0 && var.budget_ledger.deployment_reservation_usd > 0 && var.budget_ledger.spent_usd + var.budget_ledger.reserved_usd + var.budget_ledger.deployment_reservation_usd <= 1800 && var.budget_ledger.active_paid_attempts >= 0 && var.budget_ledger.active_paid_attempts < 4
    error_message = "Preserve the approved pilot $2000/$200 reserve/four-attempt policy."
  }
}

variable "bootstrap_execution_id" {
  description = "Workflow execution ID for durable discovery after an interrupted initial custody API call."
  type        = string
  default     = null
  validation {
    condition     = var.bootstrap_execution_id == null ? true : can(regex("^[0-9]+$", var.bootstrap_execution_id))
    error_message = "Use the immutable GitHub workflow run ID."
  }
}
