# This separate root deliberately wraps one legacy-provider worker module.
# An attempt is planned/applied independently and cannot extend a prior epoch.
module "worker" {
  source                   = "git::https://github.com/honua-io/honua-iac.git//infrastructure/terraform/internal/aws-agent-worker?ref=484e9d762e9b9ec0483170f548fa0bb306623bd2"
  region                   = var.region
  account_id               = var.account_id
  attempt_id               = var.attempt_id
  candidate_digest         = var.candidate_digest
  issued_epoch             = var.issued_epoch
  lifetime_seconds         = var.lifetime_seconds
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
  value = {
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
  }
  precondition {
    condition     = var.accounting.state.region == var.region && timecmp(var.expires_at, timeadd("1970-01-01T00:00:00Z", "${var.issued_epoch + var.lifetime_seconds}s")) == 0
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
output "worker_cleanup" { value = module.worker.independent_cleanup }
output "mcp_configuration_binding" { value = module.worker.mcp_configuration_binding }
output "required_accounting_claim_writers" {
  description = "Review these exact existing module role bindings into campaign.worker_attempt_ids before any runtime launch; workers themselves never receive ledger/claim writes."
  value       = [for suffix in ["lifecycle"] : "arn:aws:iam::${var.account_id}:role/honua-agent-${var.attempt_id}-${suffix}"]
}
