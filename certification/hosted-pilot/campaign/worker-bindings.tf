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
  value = { table = module.accounting.claim_table_name, writerRoleArns = local.worker_claim_writers }
}
