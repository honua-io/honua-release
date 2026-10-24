locals {
  agent_role_pattern     = "arn:aws:iam::${local.account}:role/honua-agent-*"
  workload_role_patterns = concat(["arn:aws:iam::${local.account}:role/${var.name_prefix}*"], var.enable_agent_controller ? [local.agent_role_pattern] : [])
  workload_boundary_arns = concat([aws_iam_policy.runtime_boundary.arn], var.enable_agent_controller ? [aws_iam_policy.agent_boundary[0].arn, aws_iam_policy.native_bootstrap_boundary[0].arn] : [])
  deployment_state_keys  = concat([local.state_key], var.enable_agent_controller ? ["honua/hosted-pilot/${var.name_prefix}/campaign.tfstate", "honua/hosted-pilot/${var.name_prefix}/workers/*"] : [])
}

# Identity policies further narrow each principal to its concrete task, queues,
# secret versions and candidate. This cap cannot create resources or administer
# credentials. Workers receive no controller/SQL policies despite sharing a cap.
resource "aws_iam_policy" "agent_boundary" {
  count = var.enable_agent_controller ? 1 : 0
  name  = "${local.foundation_prefix}-agent-boundary"
  policy = jsonencode({ Version = "2012-10-17", Statement = concat([
    { Sid = "NoIdentityAdministration", Effect = "Deny", Action = ["iam:Create*", "iam:Delete*", "iam:Put*", "iam:Update*", "iam:Attach*", "iam:Detach*", "iam:Set*", "iam:Tag*", "iam:Untag*", "organizations:*", "account:*"], Resource = "*" },
    { Sid = "ReadDeclaredWorkerCustody", Effect = "Allow", Action = ["iam:GetRolePolicy", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies"], Resource = local.agent_role_pattern },
    { Sid = "ExcludeFoundationCustodyKey", Effect = "Deny", Action = "kms:*", Resource = aws_kms_key.custody.arn },
    { Effect = "Allow", Action = "ecr:GetAuthorizationToken", Resource = "*" },
    { Effect = "Allow", Action = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"], Resource = [aws_ecr_repository.controller.arn, aws_ecr_repository.worker.arn] },
    { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = ["arn:aws:logs:${var.region}:${local.account}:log-group:/ecs/${var.name_prefix}*:*", "arn:aws:logs:${var.region}:${local.account}:log-group:/honua/agent-e2e/*:*"] },
    { Effect = "Allow", Action = "secretsmanager:GetSecretValue", Resource = concat(["arn:aws:secretsmanager:${var.region}:${local.account}:secret:${var.name_prefix}*", "arn:aws:secretsmanager:${var.region}:${local.account}:secret:honua-agent-*-mcp-*"], var.runtime_secret_arns) },
    { Effect = "Allow", Action = ["secretsmanager:PutSecretValue", "secretsmanager:DescribeSecret", "secretsmanager:ListSecretVersionIds"], Resource = ["arn:aws:secretsmanager:${var.region}:${local.account}:secret:${var.name_prefix}*/fixture/*", "arn:aws:secretsmanager:${var.region}:${local.account}:secret:${var.name_prefix}*/native-agent/*", "arn:aws:secretsmanager:${var.region}:${local.account}:secret:honua-agent-*-mcp-*"] },
    { Effect = "Allow", Action = ["kms:Decrypt", "kms:DescribeKey"], Resource = "arn:aws:kms:${var.region}:${local.account}:key/*", Condition = { StringEquals = { "kms:ViaService" = "secretsmanager.${var.region}.amazonaws.com" }, StringLike = { "aws:ResourceTag/ResetBoundary" = "${var.name_prefix}*" } } },
    { Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject", "s3:GetBucketVersioning", "s3:ListBucket", "s3:ListBucketVersions"], Resource = ["arn:aws:s3:::honua-agent-*", "arn:aws:s3:::honua-agent-*/*", "arn:aws:s3:::${var.name_prefix}*", "arn:aws:s3:::${var.name_prefix}*/*"] },
    { Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:ConditionCheckItem", "dynamodb:DescribeTable", "dynamodb:Query"], Resource = ["arn:aws:dynamodb:${var.region}:${local.account}:table/honua-agent-*", "arn:aws:dynamodb:${var.region}:${local.account}:table/${var.name_prefix}*-controller-*"] },
    { Effect = "Allow", Action = ["sqs:GetQueueAttributes", "sqs:SendMessage", "sqs:ReceiveMessage", "sqs:DeleteMessage"], Resource = "arn:aws:sqs:${var.region}:${local.account}:honua-agent-*" },
    { Effect = "Allow", Action = ["states:StartExecution", "states:DescribeStateMachine", "states:DescribeExecution", "states:GetExecutionHistory", "states:StopExecution"], Resource = ["arn:aws:states:${var.region}:${local.account}:*:honua-agent-*", "arn:aws:states:${var.region}:${local.account}:*:${var.name_prefix}*"] },
    { Effect = "Allow", Action = ["events:PutTargets", "events:PutRule", "events:DescribeRule"], Resource = "arn:aws:events:${var.region}:${local.account}:rule/StepFunctionsGetEventsForECSTaskRule" },
    { Effect = "Allow", Action = "ecs:RunTask", Resource = "arn:aws:ecs:${var.region}:${local.account}:task-definition/honua-*:*", Condition = { ArnLike = { "ecs:cluster" = "arn:aws:ecs:${var.region}:${local.account}:cluster/${var.name_prefix}*" } } },
    { Effect = "Allow", Action = "ecs:ListTasks", Resource = "*", Condition = { ArnLike = { "ecs:cluster" = "arn:aws:ecs:${var.region}:${local.account}:cluster/${var.name_prefix}*" } } },
    { Effect = "Allow", Action = ["ecs:TagResource", "ecs:DescribeTasks", "ecs:StopTask"], Resource = "arn:aws:ecs:${var.region}:${local.account}:task/${var.name_prefix}*/*" },
    { Effect = "Allow", Action = "iam:PassRole", Resource = [local.agent_role_pattern, "arn:aws:iam::${local.account}:role/${var.name_prefix}*"], Condition = { StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" } } },
    { Effect = "Allow", Action = ["ecs:DescribeServices", "ecs:DescribeTaskDefinition", "elasticache:DescribeReplicationGroups", "rds:DescribeDBInstances", "cloudwatch:GetMetricData", "cloudwatch:GetMetricStatistics"], Resource = "*" }
    ], length(var.controller_model_resource_arns) == 0 ? [
    { Sid = "NoUnreviewedModel", Effect = "Deny", Action = ["bedrock:*"], Resource = ["*"] }
    ] : [
    { Sid = "ReviewedModelsOnly", Effect = "Allow", Action = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"], Resource = var.controller_model_resource_arns }
    ], length([for arn in var.controller_model_resource_arns : arn if startswith(arn, "arn:aws:bedrock:${var.region}::foundation-model/")]) == 0 ? [] : [
    { Sid = "ReviewedTokenCountingOnly", Effect = "Allow", Action = "bedrock:CountTokens", Resource = [for arn in var.controller_model_resource_arns : arn if startswith(arn, "arn:aws:bedrock:${var.region}::foundation-model/")] }
    ], length(var.runtime_kms_key_arns) == 0 ? [] : [
    { Effect = "Allow", Action = ["kms:Decrypt", "kms:DescribeKey"], Resource = var.runtime_kms_key_arns }
  ]) })
}

resource "aws_iam_policy" "agent_read" {
  count = var.enable_agent_controller ? 1 : 0
  name  = "${local.foundation_prefix}-agent-read"
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["iam:GetRole", "iam:GetRolePolicy", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies"], Resource = local.agent_role_pattern },
    { Effect = "Allow", Action = ["sqs:GetQueueAttributes", "sqs:ListQueueTags", "sqs:GetQueueUrl"], Resource = "arn:aws:sqs:${var.region}:${local.account}:honua-agent-*" },
    { Effect = "Allow", Action = ["dynamodb:DescribeTable", "dynamodb:DescribeContinuousBackups", "dynamodb:DescribeTimeToLive", "dynamodb:ListTagsOfResource", "dynamodb:GetResourcePolicy"], Resource = ["arn:aws:dynamodb:${var.region}:${local.account}:table/honua-agent-*", "arn:aws:dynamodb:${var.region}:${local.account}:table/${var.name_prefix}*-controller-*"] },
    { Effect = "Allow", Action = ["states:DescribeStateMachine", "states:ListTagsForResource"], Resource = ["arn:aws:states:${var.region}:${local.account}:stateMachine:honua-agent-*", "arn:aws:states:${var.region}:${local.account}:stateMachine:${var.name_prefix}*"] },
    { Effect = "Allow", Action = ["scheduler:GetSchedule", "scheduler:GetScheduleGroup", "scheduler:ListTagsForResource"], Resource = ["arn:aws:scheduler:${var.region}:${local.account}:schedule/honua-agent-*/honua-agent-*", "arn:aws:scheduler:${var.region}:${local.account}:schedule-group/honua-agent-*"] },
    { Effect = "Allow", Action = ["cloudwatch:DescribeAlarms", "cloudwatch:ListTagsForResource"], Resource = "*" },
    { Effect = "Allow", Action = ["ecr:DescribeRepositories", "ecr:GetLifecyclePolicy", "ecr:GetRepositoryPolicy", "ecr:ListTagsForResource"], Resource = [aws_ecr_repository.controller.arn, aws_ecr_repository.worker.arn] }
  ] })
}

resource "aws_iam_role_policy_attachment" "agent_read" {
  for_each   = var.enable_agent_controller ? local.lanes : toset([])
  role       = aws_iam_role.executor[each.key].name
  policy_arn = aws_iam_policy.agent_read[0].arn
}
