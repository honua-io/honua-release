locals {
  state_bucket_arn  = "arn:aws:s3:::${module.accounting.state_config.bucket}"
  state_table_arn   = "arn:aws:dynamodb:${var.region}:${var.account_id}:table/${module.accounting.state_config.table}"
  execution_pattern = "arn:aws:states:${var.region}:${var.account_id}:execution:${var.name_prefix}-campaign-controller:*"
}
resource "aws_iam_role_policy" "dispatcher" {
  role = aws_iam_role.dispatcher.id
  policy = jsonencode({ Version = "2012-10-17", Statement = concat([
    { Effect = "Allow", Action = ["s3:GetBucketVersioning"], Resource = aws_s3_bucket.handoff.arn },
    { Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion"], Resource = ["${aws_s3_bucket.handoff.arn}/controller/*", "${local.state_bucket_arn}/agent-e2e/${local.candidate_hex}/*"] },
    { Effect = "Allow", Action = "s3:PutObject", Resource = [for prefix in [local.custody.requestPrefix, local.custody.sourcePrefix, local.custody.bundlePrefix] : "${aws_s3_bucket.handoff.arn}/${prefix}*"], Condition = { StringEquals = { "s3:if-none-match" = "*" } } },
    { Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:ConditionCheckItem"], Resource = local.state_table_arn, Condition = { "ForAllValues:StringEquals" = { "dynamodb:LeadingKeys" = [var.candidate_digest] } } },
    { Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"], Resource = aws_dynamodb_table.claims["dispatch"].arn },
    { Effect = "Allow", Action = "states:DescribeStateMachine", Resource = aws_sfn_state_machine.controller.state_machine_version_arn },
    { Effect = "Allow", Action = ["states:DescribeExecution", "states:GetExecutionHistory"], Resource = local.execution_pattern },
    { Effect = "Allow", Action = "ecs:DescribeTaskDefinition", Resource = aws_ecs_task_definition.controller.arn },
    { Effect = "Allow", Action = "ecs:DescribeTasks", Resource = "arn:aws:ecs:${var.region}:${var.account_id}:task/${module.target.ecs_cluster_name}/*" },
    { Effect = "Allow", Action = "ecr:BatchGetImage", Resource = "arn:aws:ecr:${var.region}:${var.account_id}:repository/${split("/", split("@", var.controller_image.uri)[0])[1]}" }
  ], var.dispatch_budget == null ? [] : [{ Effect = "Allow", Action = "states:StartExecution", Resource = aws_sfn_state_machine.controller.state_machine_version_arn }]) })
}
resource "aws_iam_role_policy" "controller" {
  role = aws_iam_role.controller.id
  policy = jsonencode({ Version = "2012-10-17", Statement = concat([
    { Effect = "Allow", Action = "s3:GetBucketVersioning", Resource = aws_s3_bucket.handoff.arn },
    { Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion"], Resource = "${aws_s3_bucket.handoff.arn}/*" },
    { Effect = "Allow", Action = "s3:PutObject", Resource = concat([for prefix in [local.custody.outcomePrefix, local.custody.evidencePrefix] : "${aws_s3_bucket.handoff.arn}/${prefix}*"], ["${aws_s3_bucket.handoff.arn}/fixture/*"]), Condition = { StringEquals = { "s3:if-none-match" = "*" } } },
    { Effect = "Allow", Action = "dynamodb:GetItem", Resource = aws_dynamodb_table.claims["dispatch"].arn },
    { Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"], Resource = aws_dynamodb_table.claims["fixture"].arn },
    { Effect = "Allow", Action = ["states:DescribeExecution", "states:GetExecutionHistory"], Resource = local.execution_pattern },
    { Effect = "Allow", Action = ["ecs:DescribeTasks", "ecs:StopTask"], Resource = "arn:aws:ecs:${var.region}:${var.account_id}:task/${module.target.ecs_cluster_name}/*" },
    { Effect = "Allow", Action = "ecs:DescribeTaskDefinition", Resource = "arn:aws:ecs:${var.region}:${var.account_id}:task-definition/${var.name_prefix}-*" },
    { Effect = "Allow", Action = ["ecs:ListTasks", "ecs:DescribeServices"], Resource = "*", Condition = { ArnEquals = { "ecs:cluster" = local.target_cluster_arn } } },
    { Effect = "Allow", Action = "elasticache:DescribeReplicationGroups", Resource = "arn:aws:elasticache:${var.region}:${var.account_id}:replicationgroup:${var.name_prefix}*" }
    ], [for statement in [
      { Effect = "Allow", Action = "ecs:RunTask", Resource = aws_ecs_task_definition.fixture.arn, Condition = { ArnEquals = { "ecs:cluster" = local.target_cluster_arn } } },
      { Effect = "Allow", Action = "ecs:TagResource", Resource = "arn:aws:ecs:${var.region}:${var.account_id}:task/${module.target.ecs_cluster_name}/*", Condition = { StringEquals = { "ecs:CreateAction" = "RunTask" } } },
      { Effect = "Allow", Action = "iam:PassRole", Resource = [aws_iam_role.fixture.arn, aws_iam_role.execution["fixture"].arn], Condition = { StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" } } }
    ] : statement if var.dispatch_budget != null], var.dispatch_budget == null || length(var.controller_model_resource_arns) == 0 ? [] : [
    { Effect = "Allow", Action = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"], Resource = var.controller_model_resource_arns }
  ]) })
}
