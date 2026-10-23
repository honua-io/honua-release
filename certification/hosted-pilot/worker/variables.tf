variable "region" { type = string }
variable "account_id" { type = string }
variable "name_prefix" {
  description = "Original hosted foundation prefix; worker IAM names remain separate honua-agent-attempt resources."
  type        = string
}
variable "owner" { type = string }
variable "expires_at" { type = string }
variable "candidate_digest" { type = string }
variable "attempt_id" {
  type = string
  validation {
    condition     = can(regex("^[a-z0-9-]{3,40}$", var.attempt_id))
    error_message = "Bind a unique reviewed worker identity, never reuse a failed attempt."
  }
}
variable "issued_epoch" { type = number }
variable "lifetime_seconds" {
  type = number
  validation {
    condition     = var.lifetime_seconds == floor(var.lifetime_seconds) && var.lifetime_seconds >= 60 && var.lifetime_seconds <= 6000
    error_message = "Worker lifetime must remain bounded to 60..6000 seconds."
  }
}
variable "cluster_arn" { type = string }
variable "subnet_ids" { type = set(string) }
variable "security_group_ids" { type = set(string) }
variable "worker_image" { type = string }
variable "public_bundle_digest" {
  type = string
  validation {
    condition     = can(regex("^sha256:[a-f0-9]{64}$", var.public_bundle_digest))
    error_message = "Use the actual public selected-case image build receipt."
  }
}
variable "controller_role_arn" {
  type = string
  validation {
    condition     = can(regex("^arn:aws:iam::[0-9]{12}:role/honua-pilot-[a-z0-9-]+-agent-controller$", var.controller_role_arn))
    error_message = "Attach runtime dispatch only to the exact isolated private controller."
  }
}
variable "claim_table_name" { type = string }
variable "cleanup_alarm_topic_arn" { type = string }
variable "permissions_boundary_arn" { type = string }
variable "mcp_configuration" {
  description = "Actual native context preparation receipt metadata; no raw credential."
  type        = object({ secret_arn = string, version_id = string, kms_key_arn = optional(string) })
}
variable "accounting" {
  description = "Original authenticated per-worker infrastructure/cleanup operation. This root and workflow never initialize a candidate allowance."
  type = object({
    state                   = object({ region = string, bucket = string, table = string })
    generationDigest        = string
    bootstrapArtifactDigest = string
    operation               = object({ operationId = string, liabilityBasisDigest = string, reservationIds = object({ infrastructure = string, cleanup = string }) })
    maximumUsd              = object({ infrastructure = string, cleanup = string })
  })
}
