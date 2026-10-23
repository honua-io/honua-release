output "controller_handoff" {
  description = "Nonsecret checked-source handoff contract. Snapshot this reviewed output into certification/agent-e2e/controller-handoff.json before dispatch. Does not assert readiness or create an accounting generation."
  value = {
    schemaVersion     = 1
    region            = var.region
    accountId         = var.account_id
    clusterArn        = local.target_cluster_arn
    taskDefinitionArn = aws_ecs_task_definition.controller.arn
    taskRoleArn       = aws_iam_role.controller.arn
    executionRoleArn  = aws_iam_role.execution["controller"].arn
    containerName     = "campaign-controller"
    image             = var.controller_image
    network           = { subnetIds = module.network.private_subnets, securityGroupIds = [aws_security_group.controller.id] }
    lifecycle         = { stateMachineArn = aws_sfn_state_machine.controller.state_machine_version_arn, stateMachineRoleArn = aws_iam_role.lifecycle.arn, definitionDigest = "sha256:${sha256(jsonencode(local.controller_definition))}" }
    custody           = local.custody
    dispatchBudget    = merge(var.dispatch_budget, { candidateKey = var.candidate_digest, claimTable = aws_dynamodb_table.claims["dispatch"].name })
    maxSeconds        = var.controller_max_seconds
  }
  precondition {
    condition     = var.dispatch_budget.state == module.accounting.state_config
    error_message = "The original retained candidate accounting namespace must match this root; import/reconcile existing history instead of resetting or substituting a fresh allowance."
  }
}
output "dispatcher_role_arn" { value = aws_iam_role.dispatcher.arn }
output "fixture_handoff" {
  description = "Exact nonsecret private P10 transfer task; SQL role provisioning is a separate checked prerequisite."
  value       = local.fixture_handoff
}
output "target_readiness" {
  value = { service = module.target.ecs_service_name, workerSecurityGroupIds = [aws_security_group.worker.id] }
}
output "fixture_secret_versions" {
  description = "Only immutable references; credential values stay private in state and ECS secret injection. SQL roles still require the checked private provisioner."
  value       = { for kind, version in aws_secretsmanager_secret_version.fixture : kind => { arn = aws_secretsmanager_secret.fixture[kind].arn, versionId = version.version_id } }
}
output "fixture_staging" {
  value = {
    status                    = "awaiting-private-provisioning-and-native-qualification"
    database                  = local.database
    appRole                   = local.app_role
    cleanupRole               = local.fixture_roles.cleanup
    observerRole              = local.fixture_roles.observer
    endpoint                  = local.origin
    workerNetwork             = { subnetIds = module.network.private_subnets, securityGroupIds = [aws_security_group.worker.id] }
    controllerSecurityGroupId = aws_security_group.controller.id
    nativeAgentConfigured     = var.issued_agent_key_id != null
    modelReady                = false
  }
}
