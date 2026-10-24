locals {
  registration_config_digest = var.native_runtime_registration == null ? null : "sha256:${sha256(jsonencode(local.runtime_registration_configuration))}"
  registration_claim_key     = var.native_runtime_registration == null ? null : "runtime-registration:${trimprefix(local.registration_config_digest, "sha256:")}"
  registration_machine_arn   = "arn:aws:states:${var.region}:${var.account_id}:stateMachine:${var.name_prefix}-runtime-registration"
  registration_reaper_arn    = "${local.registration_machine_arn}-reaper"
  registration_installation = var.native_runtime_registration == null ? null : {
    candidateKey                = var.candidate_digest, configurationDigest = local.registration_config_digest,
    originalFundingExpiresEpoch = var.native_runtime_registration.originalFundingExpiresEpoch,
    maxExecutionSeconds         = var.native_runtime_registration.maxExecutionSeconds,
    clusterArn                  = local.target_cluster_arn, taskDefinitionArn = aws_ecs_task_definition.runtime_registration[0].arn,
    claimTable                  = aws_dynamodb_table.claims["dispatch"].name, claimKey = local.registration_claim_key,
    startedBy                   = "reg-${substr(trimprefix(local.registration_config_digest, "sha256:"), 0, 32)}",
    subnetIds                   = module.network.private_subnets, securityGroupIds = [aws_security_group.controller.id]
  }
}

resource "aws_iam_role" "registration_lifecycle" {
  count                = var.native_runtime_registration == null ? 0 : 1
  name                 = "${var.name_prefix}-registration-lifecycle"
  permissions_boundary = var.permissions_boundary_arn
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "states.amazonaws.com" },
    Condition = { StringEquals = { "aws:SourceAccount" = var.account_id },
    ArnLike = { "aws:SourceArn" = [local.registration_machine_arn, "${local.registration_machine_arn}:*"] } }
  }] })
}
resource "aws_iam_role_policy" "registration_lifecycle" {
  count = var.native_runtime_registration == null ? 0 : 1
  role  = aws_iam_role.registration_lifecycle[0].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:UpdateItem"], Resource = aws_dynamodb_table.claims["dispatch"].arn,
    Condition = { "ForAllValues:StringEquals" = { "dynamodb:LeadingKeys" = [local.registration_claim_key] } } },
    { Effect = "Allow", Action = "ecs:RunTask", Resource = aws_ecs_task_definition.runtime_registration[0].arn,
      Condition = { ArnEquals = { "ecs:cluster" = local.target_cluster_arn },
    DateLessThan = { "aws:CurrentTime" = try(var.native_bootstrap.expiresAt, "1970-01-01T00:00:00Z") } } },
    { Effect = "Allow", Action = "ecs:TagResource", Resource = "arn:aws:ecs:${var.region}:${var.account_id}:task/${module.application.ecs_cluster_name}/*",
    Condition = { StringEquals = { "ecs:CreateAction" = "RunTask", "aws:RequestTag/honua-registration" = local.registration_config_digest } } },
    { Effect = "Allow", Action = "iam:PassRole", Resource = [try(aws_iam_role.native_bootstrap[0].arn, ""), try(aws_iam_role.native_bootstrap_execution[0].arn, "")],
    Condition = { StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" } } },
    { Effect = "Allow", Action = "ecs:DescribeTasks", Resource = "arn:aws:ecs:${var.region}:${var.account_id}:task/${module.application.ecs_cluster_name}/*" },
    { Effect = "Allow", Action = "ecs:StopTask", Resource = "arn:aws:ecs:${var.region}:${var.account_id}:task/${module.application.ecs_cluster_name}/*",
    Condition = { StringEquals = { "aws:ResourceTag/honua-registration" = local.registration_config_digest } } }
  ] })
}
resource "aws_sfn_state_machine" "runtime_registration" {
  count      = var.native_runtime_registration == null ? 0 : 1
  name       = "${var.name_prefix}-runtime-registration"
  role_arn   = aws_iam_role.registration_lifecycle[0].arn
  type       = "STANDARD"
  publish    = true
  definition = templatefile("${path.module}/runtime-registration.asl.json.tftpl", { installation_json = jsonencode(local.registration_installation) })
  depends_on = [aws_iam_role_policy.registration_lifecycle, aws_scheduler_schedule.registration_reaper]
}

# The independent observer survives the parent execution. It never receives an
# application credential and cannot modify state, release claims or settle cost.
resource "aws_iam_role" "registration_reaper" {
  count                = var.native_runtime_registration == null ? 0 : 1
  name                 = "${var.name_prefix}-registration-reaper"
  permissions_boundary = var.permissions_boundary_arn
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "states.amazonaws.com" },
    Condition = { StringEquals = { "aws:SourceAccount" = var.account_id },
    ArnLike = { "aws:SourceArn" = [local.registration_reaper_arn, "${local.registration_reaper_arn}:*"] } }
  }] })
}
resource "aws_iam_role_policy" "registration_reaper" {
  count = var.native_runtime_registration == null ? 0 : 1
  role  = aws_iam_role.registration_reaper[0].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = "dynamodb:GetItem", Resource = aws_dynamodb_table.claims["dispatch"].arn,
    Condition = { "ForAllValues:StringEquals" = { "dynamodb:LeadingKeys" = [local.registration_claim_key] } } },
    { Effect = "Allow", Action = "ecs:ListTasks", Resource = "*", Condition = { ArnEquals = { "ecs:cluster" = local.target_cluster_arn } } },
    { Effect = "Allow", Action = "ecs:DescribeTasks", Resource = "arn:aws:ecs:${var.region}:${var.account_id}:task/${module.application.ecs_cluster_name}/*" },
    { Effect = "Allow", Action = "ecs:StopTask", Resource = "arn:aws:ecs:${var.region}:${var.account_id}:task/${module.application.ecs_cluster_name}/*",
    Condition = { StringEquals = { "aws:ResourceTag/honua-registration" = local.registration_config_digest } } }
  ] })
}
resource "aws_sfn_state_machine" "registration_reaper" {
  count      = var.native_runtime_registration == null ? 0 : 1
  name       = "${var.name_prefix}-runtime-registration-reaper"
  role_arn   = aws_iam_role.registration_reaper[0].arn
  type       = "STANDARD"
  publish    = true
  definition = templatefile("${path.module}/runtime-registration-reaper.asl.json.tftpl", { installation_json = jsonencode(local.registration_installation) })
  depends_on = [aws_iam_role_policy.registration_reaper]
}
resource "aws_scheduler_schedule_group" "registration" {
  count = var.native_runtime_registration == null ? 0 : 1
  name  = "${var.name_prefix}-registration"
}
resource "aws_iam_role" "registration_schedule" {
  count                = var.native_runtime_registration == null ? 0 : 1
  name                 = "${var.name_prefix}-registration-clock"
  permissions_boundary = var.permissions_boundary_arn
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "scheduler.amazonaws.com" },
    Condition = { StringEquals = { "aws:SourceAccount" = var.account_id },
    ArnEquals = { "aws:SourceArn" = aws_scheduler_schedule_group.registration[0].arn } }
  }] })
}
resource "aws_iam_role_policy" "registration_schedule" {
  count = var.native_runtime_registration == null ? 0 : 1
  role  = aws_iam_role.registration_schedule[0].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = "states:StartExecution", Resource = aws_sfn_state_machine.registration_reaper[0].arn,
    Condition = { "ForAnyValue:StringEquals" = { "states:StateMachineQualifier" = element(reverse(split(":", aws_sfn_state_machine.registration_reaper[0].state_machine_version_arn)), 0) } } }
  ] })
}
resource "aws_scheduler_schedule" "registration_reaper" {
  count                        = var.native_runtime_registration == null ? 0 : 1
  name                         = "${var.name_prefix}-registration-reaper"
  group_name                   = aws_scheduler_schedule_group.registration[0].name
  schedule_expression          = "rate(1 minute)"
  schedule_expression_timezone = "UTC"
  end_date                     = var.expires_at
  state                        = "ENABLED"
  flexible_time_window { mode = "OFF" }
  target {
    arn      = aws_sfn_state_machine.registration_reaper[0].state_machine_version_arn
    role_arn = aws_iam_role.registration_schedule[0].arn
    input    = "{}"
    retry_policy {
      maximum_event_age_in_seconds = 60
      maximum_retry_attempts       = 0
    }
  }
  depends_on = [aws_iam_role_policy.registration_schedule]
}

resource "aws_iam_role_policy" "dispatch_runtime_registration" {
  count = var.native_runtime_registration == null || var.dispatch_budget == null ? 0 : 1
  role  = aws_iam_role.dispatcher.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = "states:StartExecution", Resource = aws_sfn_state_machine.runtime_registration[0].arn,
      Condition = { DateLessThan = { "aws:CurrentTime" = try(var.native_bootstrap.expiresAt, "1970-01-01T00:00:00Z") },
    "ForAnyValue:StringEquals" = { "states:StateMachineQualifier" = element(reverse(split(":", aws_sfn_state_machine.runtime_registration[0].state_machine_version_arn)), 0) } } },
    { Effect = "Allow", Action = "states:DescribeStateMachine", Resource = aws_sfn_state_machine.runtime_registration[0].arn,
    Condition = { "ForAnyValue:StringEquals" = { "states:StateMachineQualifier" = element(reverse(split(":", aws_sfn_state_machine.runtime_registration[0].state_machine_version_arn)), 0) } } },
    { Effect = "Allow", Action = ["states:DescribeExecution", "states:GetExecutionHistory"], Resource = "arn:aws:states:${var.region}:${var.account_id}:execution:${var.name_prefix}-runtime-registration:*" },
    { Effect = "Allow", Action = "states:DescribeStateMachine", Resource = aws_sfn_state_machine.registration_reaper[0].arn,
    Condition = { "ForAnyValue:StringEquals" = { "states:StateMachineQualifier" = element(reverse(split(":", aws_sfn_state_machine.registration_reaper[0].state_machine_version_arn)), 0) } } },
    { Effect = "Allow", Action = "scheduler:GetSchedule", Resource = aws_scheduler_schedule.registration_reaper[0].arn }
  ] })
}

output "native_runtime_registration_handoff" {
  value = var.native_runtime_registration == null || var.dispatch_budget == null ? null : {
    schemaVersion       = 1, region = var.region, accountId = var.account_id,
    configuration       = local.runtime_registration_configuration, configurationSha256 = local.registration_config_digest,
    taskDefinitionArn   = aws_ecs_task_definition.runtime_registration[0].arn,
    taskRoleArn         = try(aws_iam_role.native_bootstrap[0].arn, null), executionRoleArn = try(aws_iam_role.native_bootstrap_execution[0].arn, null),
    stateMachineArn     = aws_sfn_state_machine.runtime_registration[0].state_machine_version_arn,
    stateMachineBaseArn = aws_sfn_state_machine.runtime_registration[0].arn,
    stateMachineRoleArn = aws_iam_role.registration_lifecycle[0].arn,
    definitionDigest    = "sha256:${sha256(jsonencode(jsondecode(aws_sfn_state_machine.runtime_registration[0].definition)))}",
    reaper = { stateMachineArn = aws_sfn_state_machine.registration_reaper[0].state_machine_version_arn,
      definitionDigest    = "sha256:${sha256(jsonencode(jsondecode(aws_sfn_state_machine.registration_reaper[0].definition)))}",
      stateMachineRoleArn = aws_iam_role.registration_reaper[0].arn,
      scheduleGroup       = aws_scheduler_schedule_group.registration[0].name, scheduleName = aws_scheduler_schedule.registration_reaper[0].name,
    roleArn = aws_iam_role.registration_schedule[0].arn, endDate = var.expires_at },
    installation   = local.registration_installation,
    dispatchBudget = merge(var.dispatch_budget, { candidateKey = var.candidate_digest, claimTable = aws_dynamodb_table.claims["dispatch"].name })
  }
}
