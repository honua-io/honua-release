# A planned cleanup attempt, never an observation of resource deletion.
# This must be staged before a target-only destroy plan is prepared.
variable "shared_retirement" {
  description = "Original funding deadline and fixed cleanup-attempt start; null preserves legacy staging."
  type        = object({ original_funding_expires_at = string, retirement_at = string })
  default     = null
  validation {
    condition = var.shared_retirement == null ? true : try(
      timeadd(var.shared_retirement.retirement_at, "15m") == var.shared_retirement.original_funding_expires_at,
      false
    )
    error_message = "Scheduled cleanup must start fifteen minutes before the original funding expiry."
  }
}

resource "terraform_data" "shared_retirement" {
  count = var.shared_retirement == null ? 0 : 1
  input = {
    candidate_key               = var.candidate_digest
    original_funding_expires_at = var.shared_retirement.original_funding_expires_at
    retirement_at               = var.shared_retirement.retirement_at
  }
  # Destroy reverses these edges: the original locked wait must complete before
  # target deletion. The checked helper verifies the complete selected plan.
  depends_on = [module.application, module.network, aws_security_group.controller, aws_security_group.worker]
  provisioner "local-exec" {
    when    = destroy
    command = "exec python3 ../../../tools/hosted_pilot/shared_retirement.py"
    environment = {
      PILOT_WAIT_FUNDING_TIME    = self.input.original_funding_expires_at
      PILOT_WAIT_RETIREMENT_TIME = self.input.retirement_at
    }
  }
}
