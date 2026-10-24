# Identity-only checked apply output is separate from the retained ledger/head.
# Default empty preserves the candidate-independent image preparation authority.
variable "installed_worker_identity_scopes" {
  type    = list(object({ candidate_digest = string, runtime_id = string, slot_id = string }))
  default = []
  validation {
    condition = length(var.installed_worker_identity_scopes) <= 4 && alltrue([
      for scope in var.installed_worker_identity_scopes :
      can(regex("^sha256:[a-f0-9]{64}$", scope.candidate_digest)) &&
      can(regex("^[A-Za-z0-9_-]{1,128}$", scope.runtime_id)) &&
      can(regex("^[a-z0-9-]{3,40}$", scope.slot_id))
      ]) && length(toset([for scope in var.installed_worker_identity_scopes :
        "${scope.candidate_digest}/${scope.runtime_id}/${scope.slot_id}"
    ])) == length(var.installed_worker_identity_scopes)
    error_message = "Review at most four distinct exact candidate/runtime/physical-slot identity namespaces."
  }
}

locals {
  installed_worker_identity_objects = flatten([
    for scope in var.installed_worker_identity_scopes : [
      for object in ["installation", "review", "readiness"] :
      "arn:aws:s3:::honua-agent-${substr(trimprefix(scope.candidate_digest, "sha256:"), 0, 24)}-${local.account}/agent-e2e/${trimprefix(scope.candidate_digest, "sha256:")}/installed-workers/${scope.runtime_id}/${scope.slot_id}/*/*/${object}.json"
    ]
  ])
}

resource "aws_iam_role_policy" "installed_worker_identity_output" {
  count = length(var.installed_worker_identity_scopes) == 0 ? 0 : 1
  role  = aws_iam_role.executor["apply"].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Sid      = "RetainExactInstalledWorkerIdentities", Effect = "Allow", Action = "s3:PutObject",
      Resource = local.installed_worker_identity_objects,
      Condition = { StringEquals = {
        "s3:if-none-match" = "*", "s3:x-amz-server-side-encryption" = "AES256"
      } }
    }
  ] })
  lifecycle {
    precondition {
      condition     = var.enable_agent_controller
      error_message = "Installed identity retention requires the reviewed original controller/accounting foundation."
    }
  }
}
