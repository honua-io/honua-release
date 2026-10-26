# This separate root deliberately wraps one legacy-provider worker module.
# An attempt is planned/applied independently and cannot extend a prior epoch.
module "worker" {
  source           = "git::https://github.com/honua-io/honua-iac.git//infrastructure/terraform/internal/aws-agent-worker?ref=7765d919aa2eab19890efa13e57f89ae4266fbf8"
  region           = var.region
  account_id       = var.account_id
  attempt_id       = var.attempt_id
  installed_slot   = var.installed_slot
  candidate_digest = var.candidate_digest
  # The pinned primitive validates these legacy arguments even in installed
  # mode. Its installed path never consumes them as launch/creation evidence.
  # Reuse a known original expiry and fixed maximum, never fabricate issued_at.
  issued_epoch             = var.installed_slot == null ? var.issued_epoch : var.installed_slot.original_funding_expires_epoch
  lifetime_seconds         = var.installed_slot == null ? var.lifetime_seconds : 6000
  cluster_arn              = var.cluster_arn
  subnet_ids               = var.subnet_ids
  security_group_ids       = var.security_group_ids
  image_uri                = var.worker_image
  mcp_configuration        = var.mcp_configuration
  claim_table_name         = var.claim_table_name
  cleanup_alarm_topic_arn  = var.cleanup_alarm_topic_arn
  permissions_boundary_arn = var.permissions_boundary_arn
}
resource "aws_iam_role_policy" "controller_worker" {
  name   = "worker-${var.attempt_id}-control"
  role   = element(reverse(split("/", var.controller_role_arn)), 0)
  policy = module.worker.controller_policy
}
data "aws_sfn_state_machine" "worker" {
  name = split(":stateMachine:", module.worker.lifecycle.state_machine_arn)[1]
}
output "worker_backend" {
  description = "Actual task/transport identity to review into backend attempts configuration; model execution and paid attempt reservations remain separate."
  value = var.installed_slot == null ? {
    attemptId          = var.attempt_id
    imageUri           = var.worker_image
    publicBundleDigest = var.public_bundle_digest
    taskRoleArn        = module.worker.lifecycle.worker_role_arn
    transport          = module.worker.transport
    evidence           = { region = var.region, bucket = var.accounting.state.bucket }
    lifecycle = {
      region              = var.region
      stateMachineArn     = module.worker.lifecycle.state_machine_arn
      definitionDigest    = "sha256:${sha256(jsonencode(jsondecode(data.aws_sfn_state_machine.worker.definition)))}"
      stateMachineRoleArn = data.aws_sfn_state_machine.worker.role_arn
      clusterArn          = var.cluster_arn
      taskDefinitionArn   = module.worker.lifecycle.task_definition_arn
      claimTable          = var.claim_table_name
      expiresEpoch        = var.issued_epoch + var.lifetime_seconds
    }
  } : null
  precondition {
    condition     = var.accounting.state.region == var.region && timecmp(var.expires_at, timeadd("1970-01-01T00:00:00Z", "${var.installed_slot == null ? var.issued_epoch + var.lifetime_seconds : var.installed_slot.original_funding_expires_epoch}s")) == 0
    error_message = "The worker must use its original accounting region and fixed absolute expiry; no sliding lifetime."
  }
  precondition {
    condition     = startswith(var.controller_role_arn, "arn:aws:iam::${var.account_id}:") && startswith(var.cluster_arn, "arn:aws:ecs:${var.region}:${var.account_id}:cluster/${var.name_prefix}")
    error_message = "The worker must remain in its original account and isolated pilot cluster."
  }
  precondition {
    condition     = can(regex("^arn:aws:secretsmanager:${var.region}:${var.account_id}:secret:honua-agent-${var.attempt_id}-mcp-[A-Za-z0-9]{6}$", var.mcp_configuration.secret_arn))
    error_message = "Only the exact attempt-owned MCP secret may enter a worker; candidate staging or another attempt's credential cannot be substituted."
  }
}
output "installed_worker_slot" {
  description = "Source-bound installed allocation with independently read lifecycle revision and original positive quote. No native execution or retirement is implied."
  value = var.installed_slot == null ? null : merge(module.worker.installed_worker_slot, {
    stateMachineRoleArn = data.aws_sfn_state_machine.worker.role_arn
    definitionDigest    = "sha256:${sha256(jsonencode(jsondecode(data.aws_sfn_state_machine.worker.definition)))}"
    liability           = var.execution_liability
  })
  precondition {
    condition = var.installed_slot == null ? true : var.mcp_bootstrap_binding != null && var.worker_publication != null && var.execution_liability != null && var.execution_quote != null && var.installation_quote != null && contains(var.accounting_installed_worker_slots, {
      runtime_id = var.installed_slot.runtime_id, slot_id = var.installed_slot.slot_id
    })
    error_message = "The actual accounting policy must include this exact reviewed installed allocation."
  }
}
output "worker_cleanup" { value = module.worker.independent_cleanup }
output "mcp_configuration_binding" { value = module.worker.mcp_configuration_binding }
output "required_accounting_claim_writers" {
  description = "Review these exact existing module role bindings into campaign.worker_attempt_ids before any runtime launch; workers themselves never receive ledger/claim writes."
  value       = [for suffix in ["lifecycle"] : "arn:aws:iam::${var.account_id}:role/honua-agent-${var.attempt_id}-${suffix}"]
}
