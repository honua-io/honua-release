variable "region" { type = string }
variable "account_id" {
  type = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "Use the reviewed AWS account."
  }
}
variable "name_prefix" {
  type = string
  validation {
    condition     = can(regex("^honua-pilot-[a-z0-9-]{4,16}-agent$", var.name_prefix))
    error_message = "Use a separate isolated agent campaign prefix."
  }
}
variable "owner" { type = string }
variable "expires_at" {
  type = string
  validation {
    condition     = can(timecmp(var.expires_at, "2020-01-01T00:00:00Z"))
    error_message = "Use an explicit RFC3339 expiry."
  }
}
variable "candidate_digest" {
  type = string
  validation {
    condition     = can(regex("^sha256:[a-f0-9]{64}$", var.candidate_digest))
    error_message = "Bind the exact original candidate accounting identity."
  }
}
variable "permissions_boundary_arn" { type = string }
variable "oidc_provider_arn" { type = string }
variable "state_admin_role_arns" { type = set(string) }
variable "network_cidr" {
  type    = string
  default = "10.87.0.0/16"
}
variable "target_domain" {
  type = string
  validation {
    condition     = can(regex("^[a-z0-9-]+\\.pilot\\.demo\\.honua\\.io$", var.target_domain))
    error_message = "Use an isolated campaign hostname in the delegated demo zone."
  }
}
variable "route53_zone_id" { type = string }
variable "server_image" {
  type = string
  validation {
    condition     = can(regex("@sha256:[a-f0-9]{64}$", var.server_image))
    error_message = "Use the reviewed native Server image digest."
  }
}
variable "controller_image" {
  type = object({ uri = string, sourceSha = string, treeId = string, archiveSha256 = string, runtimeContractSha256 = string })
  validation {
    condition     = can(regex("@sha256:[a-f0-9]{64}$", var.controller_image.uri)) && can(regex("^[a-f0-9]{40}$", var.controller_image.sourceSha)) && can(regex("^[a-f0-9]{40}$", var.controller_image.treeId)) && can(regex("^[a-f0-9]{64}$", var.controller_image.archiveSha256)) && can(regex("^sha256:[a-f0-9]{64}$", var.controller_image.runtimeContractSha256))
    error_message = "Retain genuine image, source, archive, tree and runtime dependency contract provenance."
  }
}
variable "operation_certificate_secret_arn" { type = string }
variable "audit_chain_secret_arn" { type = string }
variable "operation_certificate_kms_key_arn" {
  type    = string
  default = null
}
variable "audit_chain_kms_key_arn" {
  type    = string
  default = null
}
variable "deletion_protection" {
  type    = bool
  default = true
}
variable "issued_agent_key_id" {
  description = "Actual nonsecret UUID returned by native POST /api/v1/admin/api-keys. Null is bootstrap staging, never model readiness."
  type        = string
  default     = null
  validation {
    condition     = var.issued_agent_key_id == null ? true : try(var.issued_agent_key_id == var.issued_native_keys.agent.apiKeyId, false)
    error_message = "The legacy key ID can only repeat the actual native-bootstrap key with its retained immutable secret version."
  }
}
variable "issued_native_keys" {
  description = "Actual private native-bootstrap receipt identities/immutable versions, reviewed into source after issuance. No secret values."
  type = object({
    setup    = object({ apiKeyId = string, versionId = string })
    observer = object({ apiKeyId = string, versionId = string })
    agent    = object({ apiKeyId = string, versionId = string })
  })
  default = null
  validation {
    condition = var.issued_native_keys == null ? true : alltrue([for record in values(var.issued_native_keys) :
      can(regex("^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$", record.apiKeyId)) && can(regex("^[a-f0-9]{64}$", record.versionId))
    ]) && length(toset([for record in values(var.issued_native_keys) : record.apiKeyId])) == 3
    error_message = "Bind three distinct actual native-issued key IDs and immutable secret versions from the retained receipt."
  }
}
variable "dispatch_budget" {
  description = "Original authenticated shared-operation binding; this root never creates or resets a campaign allowance."
  type        = object({ state = object({ region = string, bucket = string, table = string }), generationDigest = string, reservationId = string, operationId = string, basisDigest = string, perDispatchCeilingUsd = string, maximumDispatches = number })
  default     = null
  validation {
    condition     = var.dispatch_budget == null ? true : can(regex("^sha256:[a-f0-9]{64}$", var.dispatch_budget.generationDigest)) && can(regex("^sha256:[a-f0-9]{64}$", var.dispatch_budget.basisDigest)) && var.dispatch_budget.maximumDispatches >= 1 && var.dispatch_budget.maximumDispatches <= 256 && tonumber(var.dispatch_budget.perDispatchCeilingUsd) > 0
    error_message = "Bind existing durable generation and original reservation, with bounded dispatch capacity."
  }
}
variable "controller_max_seconds" {
  type    = number
  default = 7200
  validation {
    condition     = var.controller_max_seconds >= 60 && var.controller_max_seconds <= 7200
    error_message = "Bound every private controller invocation."
  }
}

variable "controller_model_resource_arns" {
  description = "Exact reviewed Bedrock inference profile and member models. Empty grants no model calls; foundation permissions boundary must independently allow these same resources."
  type        = list(string)
  default     = []
  validation {
    condition = alltrue([for arn in var.controller_model_resource_arns :
      can(regex("^arn:aws:bedrock:[a-z0-9-]+:([0-9]{12})?:(inference-profile|foundation-model)/[a-zA-Z0-9.:-]+$", arn)) &&
      (split(":", arn)[4] == "" || split(":", arn)[4] == var.account_id)
    ])
    error_message = "Bind exact same-account profiles and their exact regional foundation models; no wildcard resources."
  }
}
