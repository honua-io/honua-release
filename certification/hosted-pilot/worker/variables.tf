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
variable "issued_epoch" {
  type    = number
  default = null
  validation {
    condition     = var.installed_slot != null ? true : try(var.issued_epoch == floor(var.issued_epoch) && var.issued_epoch > 0, false)
    error_message = "Static mode requires its actual fixed original issued epoch."
  }
}
variable "lifetime_seconds" {
  type    = number
  default = null
  validation {
    condition     = var.installed_slot != null ? true : try(var.lifetime_seconds == floor(var.lifetime_seconds) && var.lifetime_seconds >= 60 && var.lifetime_seconds <= 6000, false)
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
variable "accounting_claim_roles" {
  description = "Complete reviewed campaign.worker_claims administratorRoleArns and writerRoleArns, renamed administrators/workers. Readiness compares the whole actual resource policy, never a subset exemption."
  type        = object({ administrators = set(string), workers = set(string) })
  validation {
    condition = (length(var.accounting_claim_roles.administrators) > 0 && length(var.accounting_claim_roles.workers) <= 4 &&
      length(setintersection(var.accounting_claim_roles.administrators, var.accounting_claim_roles.workers)) == 0 &&
      contains(var.accounting_claim_roles.workers, "arn:aws:iam::${var.account_id}:role/honua-agent-${var.attempt_id}-lifecycle") &&
      alltrue([for arn in var.accounting_claim_roles.administrators : can(regex("^arn:aws:iam::${var.account_id}:role/[A-Za-z0-9+=,.@_/-]+$", arn))]) &&
    alltrue([for arn in var.accounting_claim_roles.workers : can(regex("^arn:aws:iam::${var.account_id}:role/honua-agent-[a-z0-9-]{3,40}-lifecycle$", arn))]))
    error_message = "Use every actual reviewed accounting administrator and at most four exact fixed lifecycle roles, including this worker; wildcards or another account are refused."
  }
}
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

variable "installed_slot" {
  description = "Optional fixed installed allocation. Null preserves static campaign workers; never pass per-turn W/T/E or plaintext credentials."
  type        = object({ runtime_id = string, slot_id = string, original_funding_expires_epoch = number, max_execution_seconds = number })
  default     = null
}
variable "accounting_installed_worker_slots" {
  description = "Complete reviewed campaign installed allocations; readiness replays the whole actual table policy."
  type        = list(object({ runtime_id = string, slot_id = string }))
  default     = []
  validation {
    condition = length(var.accounting_installed_worker_slots) <= 4 && length(distinct([for slot in var.accounting_installed_worker_slots : slot.slot_id])) == length(var.accounting_installed_worker_slots) && alltrue([
      for slot in var.accounting_installed_worker_slots : can(regex("^[A-Za-z0-9_-]{1,128}$", slot.runtime_id)) && can(regex("^[a-z0-9-]{3,40}$", slot.slot_id)) && contains(var.accounting_claim_roles.workers, "arn:aws:iam::${var.account_id}:role/honua-agent-${slot.slot_id}-lifecycle")
    ])
    error_message = "Use the complete source-reviewed set of at most four installed lifecycle allocations."
  }
}

variable "mcp_bootstrap_binding" {
  description = "Original immutable pending installed MCP completion reference; no credential bytes or activation."
  type        = object({ key = string, sha256 = string })
  default     = null
  validation {
    condition = var.mcp_bootstrap_binding == null ? true : (
      endswith(var.mcp_bootstrap_binding.key, "/completed") && can(regex("^sha256:[a-f0-9]{64}$", var.mcp_bootstrap_binding.sha256))
    )
    error_message = "Use the actual retained pending MCP completion, independently authenticated before plan/apply."
  }
}

variable "worker_publication" {
  description = "Original successful worker-image publication artifact; required by installed readiness, never a caller capability flag."
  type        = object({ sourceSha = string, runId = number, runAttempt = number, artifactId = number, artifactDigest = string })
  default     = null
  validation {
    condition = var.worker_publication == null ? true : (
      can(regex("^[a-f0-9]{40}$", var.worker_publication.sourceSha)) &&
      can(regex("^sha256:[a-f0-9]{64}$", var.worker_publication.artifactDigest)) &&
      alltrue([for number in [var.worker_publication.runId, var.worker_publication.runAttempt, var.worker_publication.artifactId] : number > 0 && number == floor(number)])
    )
    error_message = "Bind the exact original publication source/run/attempt and artifact ID/digest."
  }
}

# These are separate from the once-per-installation accounting operation.
# These references reserve the separate quote contract. Source-bound request
# counters and complete quote verification remain required before activation.
variable "execution_quote" {
  type    = object({ path = string, sha256 = string })
  default = null
}
variable "installation_quote" {
  type    = object({ path = string, sha256 = string })
  default = null
}
variable "execution_liability" {
  type    = object({ basisDigest = string, maximumInfrastructureUsd = string, maximumCleanupUsd = string })
  default = null
}
