variable "worker_attempt_ids" {
  description = "Exact reviewed isolated worker roots whose fixed lifecycle/reaper roles may append one-time launch/stop claims. This creates no paid admission or ledger allowance."
  type        = set(string)
  default     = []
  validation {
    condition     = length(var.worker_attempt_ids) <= 4 && alltrue([for attempt in var.worker_attempt_ids : can(regex("^[a-z0-9-]{3,40}$", attempt))])
    error_message = "Stage at most four exact worker roots at once; retain claims/history when retiring them."
  }
}
locals {
  worker_claim_writers = toset(flatten([for attempt in var.worker_attempt_ids : [
    for suffix in ["lifecycle"] : "arn:aws:iam::${var.account_id}:role/honua-agent-${attempt}-${suffix}"
  ]]))
}
output "worker_claims" {
  value = {
    table          = module.accounting.claim_table_name, writerRoleArns = local.worker_claim_writers
    installedSlots = var.installed_worker_slots
    administratorRoleArns = setunion(var.state_admin_role_arns, [aws_iam_role.dispatcher.arn, aws_iam_role.fixture.arn, aws_iam_role.initializer.arn],
    [for role in aws_iam_role.native_bootstrap : role.arn], [for role in aws_iam_role.setup_retirement : role.arn])
  }
}

variable "installed_worker_slots" {
  description = "Exact optional installed runtime/physical-slot allocations; no paid admission is created."
  type        = list(object({ runtime_id = string, slot_id = string }))
  default     = []
  validation {
    condition = length(var.installed_worker_slots) <= 4 && length(distinct([for slot in var.installed_worker_slots : slot.slot_id])) == length(var.installed_worker_slots) && alltrue([
      for slot in var.installed_worker_slots : can(regex("^[A-Za-z0-9_-]{1,128}$", slot.runtime_id)) && contains(var.worker_attempt_ids, slot.slot_id)
    ])
    error_message = "Every reviewed installed slot must name an existing fixed worker root."
  }
}
