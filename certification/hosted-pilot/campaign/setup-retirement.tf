variable "setup_key_retirement" {
  description = "Actual source-reviewed saved fixture/original bootstrap references. This task only revokes its original setup key; it cannot mint an agent key, approve, or initialize accounting."
  type = object({
    bootstrapArtifactDigest    = string
    nativeBootstrapOperationId = string
    operation                  = object({ operationId = string, liabilityBasisDigest = string, reservationIds = object({ infrastructure = string, cleanup = string }) })
    fixtureReference           = object({ key = string, sha256 = string })
    preparationOutcome         = object({ bucket = string, key = string, versionId = string, sha256 = string })
    mode                       = string
    originalClaim              = object({ table = string, key = string, bindingDigest = string, taskArn = string, taskDefinitionArn = string })
  })
  default = null
  validation {
    condition     = var.setup_key_retirement == null ? true : (var.setup_key_retirement.mode == "retire" ? var.setup_key_retirement.originalClaim == null : var.setup_key_retirement.mode == "observe" && var.setup_key_retirement.originalClaim != null)
    error_message = "Retirement has one effect mode with no prior claim, or observation-only recovery bound to its original permanent task claim."
  }
}
locals {
  retirement_enabled = var.setup_key_retirement != null
  retirement_ready   = local.retirement_enabled && var.dispatch_budget != null && var.issued_native_keys != null
  retirement_config = !local.retirement_ready ? null : merge(var.setup_key_retirement, {
    schemaVersion    = 1, region = var.region, accountId = var.account_id, candidateKey = var.candidate_digest,
    generationDigest = var.dispatch_budget.generationDigest, state = module.accounting.state_config,
    endpoint         = local.origin, setupKey = merge(var.issued_native_keys.setup, { secretArn = aws_secretsmanager_secret.api["setup"].arn }),
    bucket           = aws_s3_bucket.handoff.id, receiptKey = "native/setup-key-retirement/${local.candidate_hex}/${var.issued_native_keys.setup.apiKeyId}/${var.setup_key_retirement.mode == "retire" ? "retire" : "observe-${trimprefix(var.setup_key_retirement.originalClaim.key, "dispatch:")}"}.json",
    clusterArn       = local.target_cluster_arn, taskDefinitionFamily = "arn:aws:ecs:${var.region}:${var.account_id}:task-definition/${var.name_prefix}-setup-key-retirement",
    imageUri         = var.controller_image.uri, rootSecretArn = module.target.admin_password_secret_arn,
    taskRoleArn      = aws_iam_role.setup_retirement[0].arn, executionRoleArn = aws_iam_role.retirement_execution[0].arn,
    claimTable       = aws_dynamodb_table.claims["dispatch"].name
  })
}
resource "aws_iam_role" "setup_retirement" {
  count                = local.retirement_enabled ? 1 : 0
  name                 = "${var.name_prefix}-setup-retire"
  permissions_boundary = var.permissions_boundary_arn
  assume_role_policy   = local.ecs_trust
}
resource "aws_iam_role" "retirement_execution" {
  count                = local.retirement_enabled ? 1 : 0
  name                 = "${var.name_prefix}-retire-execution"
  permissions_boundary = var.permissions_boundary_arn
  assume_role_policy   = local.ecs_trust
}
resource "aws_cloudwatch_log_group" "setup_retirement" {
  count             = local.retirement_enabled ? 1 : 0
  name              = "/ecs/${var.name_prefix}-setup-retirement"
  retention_in_days = 14
}
resource "aws_iam_role_policy" "retirement_execution" {
  count = local.retirement_enabled ? 1 : 0
  role  = aws_iam_role.retirement_execution[0].id
  policy = jsonencode({ Version = "2012-10-17", Statement = concat([
    { Effect = "Allow", Action = "ecr:GetAuthorizationToken", Resource = "*" },
    { Effect = "Allow", Action = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"], Resource = "arn:aws:ecr:${var.region}:${var.account_id}:repository/${split("/", split("@", var.controller_image.uri)[0])[1]}" },
    { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.setup_retirement[0].arn}:*" },
    { Effect = "Allow", Action = "secretsmanager:GetSecretValue", Resource = module.target.admin_password_secret_arn }
    ], var.issued_native_keys == null ? [] : [
    { Effect = "Allow", Action = "secretsmanager:GetSecretValue", Resource = aws_secretsmanager_secret.api["setup"].arn,
    Condition = { StringEquals = { "secretsmanager:VersionId" = var.issued_native_keys.setup.versionId } } }
  ]) })
}
resource "aws_ecs_task_definition" "setup_retirement" {
  count                    = local.retirement_enabled ? 1 : 0
  family                   = "${var.name_prefix}-setup-key-retirement"
  cpu                      = "256"
  memory                   = "1024"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  task_role_arn            = aws_iam_role.setup_retirement[0].arn
  execution_role_arn       = aws_iam_role.retirement_execution[0].arn
  runtime_platform {
    cpu_architecture        = "X86_64"
    operating_system_family = "LINUX"
  }
  volume { name = "work" }
  container_definitions = jsonencode([{
    name        = "setup-key-retirement", image = var.controller_image.uri, essential = true,
    command     = ["python", "tools/hosted_pilot/setup_key_retirement.py", "run"], user = "10001:10001", readonlyRootFilesystem = true,
    mountPoints = [{ sourceVolume = "work", containerPath = "/work", readOnly = false }],
    environment = [{ name = "PILOT_SETUP_RETIREMENT_CONFIG", value = jsonencode(local.retirement_config) }],
    secrets = var.issued_native_keys == null ? [] : [
      { name = "PILOT_SETUP_RETIRE_ROOT_KEY", valueFrom = module.target.admin_password_secret_arn },
      { name = "PILOT_SETUP_RETIRE_KEY", valueFrom = "${aws_secretsmanager_secret.api["setup"].arn}:::${var.issued_native_keys.setup.versionId}" }
    ],
    linuxParameters  = { capabilities = { drop = ["ALL"] } },
    logConfiguration = { logDriver = "awslogs", options = { awslogs-group = aws_cloudwatch_log_group.setup_retirement[0].name, awslogs-region = var.region, awslogs-stream-prefix = "retire" } }
  }])
}
resource "aws_iam_role_policy" "setup_retirement" {
  count = local.retirement_enabled ? 1 : 0
  role  = aws_iam_role.setup_retirement[0].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["s3:GetBucketVersioning", "s3:ListBucketVersions"], Resource = local.state_bucket_arn },
    { Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject"], Resource = "${local.state_bucket_arn}/agent-e2e/${local.candidate_hex}/*" },
    { Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion"], Resource = ["${aws_s3_bucket.handoff.arn}/controller/outcomes/${local.candidate_hex}/*", "${aws_s3_bucket.handoff.arn}/controller/evidence/${local.candidate_hex}/*"] },
    { Effect = "Allow", Action = "s3:PutObject", Resource = "${aws_s3_bucket.handoff.arn}/native/setup-key-retirement/${local.candidate_hex}/*", Condition = { StringEquals = { "s3:if-none-match" = "*" } } },
    { Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:UpdateItem"], Resource = local.state_table_arn, Condition = { "ForAllValues:StringEquals" = { "dynamodb:LeadingKeys" = [var.candidate_digest] } } },
    { Effect = "Allow", Action = "dynamodb:GetItem", Resource = aws_dynamodb_table.claims["dispatch"].arn },
    { Effect = "Allow", Action = "ecs:DescribeTasks", Resource = "arn:aws:ecs:${var.region}:${var.account_id}:task/${module.target.ecs_cluster_name}/*" },
    { Effect = "Allow", Action = "ecs:DescribeTaskDefinition", Resource = ["arn:aws:ecs:${var.region}:${var.account_id}:task-definition/${var.name_prefix}-setup-key-retirement:*", "arn:aws:ecs:${var.region}:${var.account_id}:task-definition/${var.name_prefix}-campaign-controller:*"] },
    { Effect = "Allow", Action = "states:DescribeExecution", Resource = local.execution_pattern }
  ] })
}
resource "aws_iam_role_policy" "dispatch_setup_retirement" {
  count = local.retirement_ready ? 1 : 0
  role  = aws_iam_role.dispatcher.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = "ecs:RunTask", Resource = aws_ecs_task_definition.setup_retirement[0].arn, Condition = { ArnEquals = { "ecs:cluster" = local.target_cluster_arn } } },
    { Effect = "Allow", Action = "iam:PassRole", Resource = [aws_iam_role.setup_retirement[0].arn, aws_iam_role.retirement_execution[0].arn], Condition = { StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" } } }
  ] })
}
resource "aws_iam_role_policy" "observe_setup_retirement" {
  count = local.retirement_enabled ? 1 : 0
  role  = aws_iam_role.dispatcher.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = "ecs:DescribeTaskDefinition", Resource = aws_ecs_task_definition.setup_retirement[0].arn },
    { Effect = "Allow", Action = "ecs:StopTask", Resource = "arn:aws:ecs:${var.region}:${var.account_id}:task/${module.target.ecs_cluster_name}/*" },
    { Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion"], Resource = "${aws_s3_bucket.handoff.arn}/native/setup-key-retirement/${local.candidate_hex}/*" }
  ] })
}
output "setup_retirement_handoff" {
  value = !local.retirement_ready ? null : {
    schemaVersion     = 1, region = var.region, accountId = var.account_id, clusterArn = local.target_cluster_arn,
    taskDefinitionArn = aws_ecs_task_definition.setup_retirement[0].arn, taskRoleArn = aws_iam_role.setup_retirement[0].arn,
    executionRoleArn  = aws_iam_role.retirement_execution[0].arn, imageUri = var.controller_image.uri,
    subnetIds         = module.network.private_subnets, securityGroupIds = [aws_security_group.controller.id],
    configuration     = local.retirement_config, configurationSha256 = "sha256:${sha256(jsonencode(local.retirement_config))}",
    dispatchBudget    = merge(var.dispatch_budget, { candidateKey = var.candidate_digest, claimTable = aws_dynamodb_table.claims["dispatch"].name })
  }
  precondition {
    condition     = var.setup_key_retirement == null ? true : var.issued_native_keys != null
    error_message = "Retirement must name actual native-issued original keys; staging cannot invent credential IDs or versions."
  }
}
