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
    reservation_id             = optional(string)
  })
  validation {
    condition = (
      var.budget_ledger.schema == "honua.hosted-pilot-budget/v1" && var.budget_ledger.total_budget_usd == 2000 && var.budget_ledger.cleanup_reserve_usd == 200 && var.budget_ledger.spent_usd >= 0 && var.budget_ledger.reserved_usd >= 0 && var.budget_ledger.deployment_reservation_usd > 0 && var.budget_ledger.spent_usd + var.budget_ledger.reserved_usd + var.budget_ledger.deployment_reservation_usd <= 1800 && var.budget_ledger.active_paid_attempts >= 0 && var.budget_ledger.active_paid_attempts < 4 &&
      can(regex("^[a-f0-9]{40}$", var.budget_ledger.source_sha)) && can(regex("^[a-f0-9]{64}$", var.budget_ledger.config_sha256)) &&
      (var.budget_ledger.reservation_id == null ? true : can(regex("^[A-Za-z0-9][A-Za-z0-9._-]{2,119}$", var.budget_ledger.reservation_id)))
    )
    error_message = "Preserve approved pilot limits and bind an actual source/config before creating any owner ledger."
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

variable "enable_agent_controller" {
  description = "Opt in to isolated controller/worker orchestration; does not initialize or reset a campaign accounting ledger."
  type        = bool
  default     = false
}

variable "controller_model_resource_arns" {
  description = "Reviewed exact Bedrock profile and member-model resources. Empty refuses model invocation. No wildcard, provider change or model substitution at runtime. Pass the profile ARN plus the three routed foundation-model ARNs for Claude Opus 5.5, Claude Sonnet 5.5, GPT-6.1 Sol, GPT-6.1 Terra, and, while it remains the shipped default, Claude Sonnet 4.6."
  type        = list(string)
  default     = []
  validation {
    condition = alltrue([for arn in var.controller_model_resource_arns :
      can(regex("^arn:aws:bedrock:[a-z0-9-]+:([0-9]{12})?:(inference-profile|foundation-model)/[a-zA-Z0-9.:-]+$", arn)) &&
      (split(":", arn)[4] == "" || split(":", arn)[4] == var.account_id)
    ])
    error_message = "Use exact model/profile ARNs in this account (foundation models have no account component)."
  }
}

variable "bootstrap_creation_identity" {
  description = "Source/config identity stored atomically with AWS custody creation for artifact-independent partial recovery. The checked-in bootstrap helper supplies this map."
  type        = map(string)
  default     = {}
  validation {
    condition = length(var.bootstrap_creation_identity) == 0 || (
      toset(keys(var.bootstrap_creation_identity)) == toset(["foundation_source_sha", "release_root_sha", "config_path", "config_sha256", "provider_lock_sha256"]) &&
      alltrue([for value in values(var.bootstrap_creation_identity) : length(value) > 0 && length(value) <= 256])
    )
    error_message = "Supply the complete source/config/lock creation identity or an empty map for validation-only plans."
  }
}

variable "bootstrap_custody_creation_tags" {
  description = "Original identity independently observed on the retained custody key and both buckets. Only custody overrides current producer tags; never caller-provided to the checked helper."
  type        = map(string)
  default     = {}
  validation {
    condition = length(var.bootstrap_custody_creation_tags) == 0 || (
      toset(keys(var.bootstrap_custody_creation_tags)) == toset(["ManagedBy", "BootstrapExecution", "BootstrapFoundationSha", "BootstrapReleaseSha", "BootstrapConfigPath", "BootstrapConfigSha256", "BootstrapLockSha256"]) &&
      try(var.bootstrap_custody_creation_tags.ManagedBy == "honua-release-hosted-pilot-bootstrap", false) &&
      can(regex("^[0-9]+$", var.bootstrap_custody_creation_tags.BootstrapExecution)) &&
      alltrue([for key in ["BootstrapFoundationSha", "BootstrapReleaseSha"] : can(regex("^[a-f0-9]{40}$", var.bootstrap_custody_creation_tags[key]))]) &&
      alltrue([for key in ["BootstrapConfigSha256", "BootstrapLockSha256"] : can(regex("^[a-f0-9]{64}$", var.bootstrap_custody_creation_tags[key]))]) &&
      can(regex("^certification/hosted-pilot/bootstrap/deployments/[A-Za-z0-9._-]+\\.tfvars\\.json$", var.bootstrap_custody_creation_tags.BootstrapConfigPath)) &&
      alltrue([for value in values(var.bootstrap_custody_creation_tags) : length(value) > 0 && length(value) <= 256])
    )
    error_message = "Use only the complete independently observed original custody identity."
  }
}
