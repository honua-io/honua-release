variable "native_bootstrap" {
  description = "Optional one-shot native SQL/key provisioning under existing authenticated bootstrap history. Null retains foundation staging."
  type = object({
    bootstrapArtifactDigest = string
    operation               = object({ operationId = string, liabilityBasisDigest = string, reservationIds = object({ infrastructure = string, cleanup = string }) })
    targetId                = string
    itemId                  = string
    expiresAt               = string
    readGrants              = list(string)
    datasetImportScopes     = list(string)
    managedProcessIds       = list(string)
  })
  default = null
  validation {
    condition = var.native_bootstrap == null ? true : (
      can(regex("^[A-Za-z0-9_-]{1,128}$", var.native_bootstrap.targetId)) &&
      length(var.native_bootstrap.datasetImportScopes) == length(toset(var.native_bootstrap.datasetImportScopes)) &&
      alltrue([for scope in var.native_bootstrap.datasetImportScopes :
        can(regex("^${var.native_bootstrap.targetId}/[a-z_][a-z0-9_]{0,62}/[a-z][a-z0-9_]{0,62}$", scope))
      ]) &&
      length(var.native_bootstrap.managedProcessIds) == length(toset(var.native_bootstrap.managedProcessIds)) &&
      alltrue([for process in var.native_bootstrap.managedProcessIds : process == "analytics.buffer-aggregate-managed"]) &&
      alltrue([for grant in var.native_bootstrap.readGrants : can(regex("^read:[a-z0-9_-]+/[0-9]+$", grant))])
    )
    error_message = "Use the observed native target, exact new-table import destinations, the supported managed process, and separate exact read grants."
  }
}

locals {
  native_bootstrap_config = var.native_bootstrap == null ? null : merge(var.native_bootstrap, {
    schemaVersion        = 2, region = var.region, accountId = var.account_id, candidateKey = var.candidate_digest,
    generationDigest     = var.dispatch_budget == null ? null : var.dispatch_budget.generationDigest,
    state                = module.accounting.state_config, databaseHost = nonsensitive(module.application.db_endpoint), database = local.database,
    appRole              = local.app_role, cleanupRole = local.fixture_roles.cleanup, observerRole = local.fixture_roles.observer,
    endpoint             = local.origin, secretArns = { for kind, secret in aws_secretsmanager_secret.api : kind => secret.arn },
    bucket               = aws_s3_bucket.handoff.id, receiptKey = "native/bootstrap/${local.candidate_hex}/${var.native_bootstrap.operation.operationId}.json",
    clusterArn           = local.target_cluster_arn, imageUri = var.controller_image.uri,
    taskDefinitionFamily = "arn:aws:ecs:${var.region}:${var.account_id}:task-definition/${var.name_prefix}-native-bootstrap"
  })
}
resource "aws_iam_role" "native_bootstrap" {
  count                = var.native_bootstrap == null ? 0 : 1
  name                 = "${var.name_prefix}-native-bootstrap"
  permissions_boundary = var.permissions_boundary_arn
  assume_role_policy   = local.ecs_trust
}
resource "aws_ecs_task_definition" "native_bootstrap" {
  count                    = var.native_bootstrap == null ? 0 : 1
  family                   = "${var.name_prefix}-native-bootstrap"
  cpu                      = "256"
  memory                   = "1024"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  task_role_arn            = aws_iam_role.native_bootstrap[0].arn
  execution_role_arn       = aws_iam_role.execution["fixture"].arn
  runtime_platform {
    cpu_architecture        = "X86_64"
    operating_system_family = "LINUX"
  }
  volume { name = "work" }
  container_definitions = jsonencode([{
    name             = "native-bootstrap", image = var.controller_image.uri, essential = true,
    command          = ["python", "tools/hosted_pilot/native_bootstrap.py"], user = "10001:10001", readonlyRootFilesystem = true,
    mountPoints      = [{ sourceVolume = "work", containerPath = "/work", readOnly = false }],
    environment      = [{ name = "PILOT_NATIVE_BOOTSTRAP_CONFIG", value = jsonencode(local.native_bootstrap_config) }],
    secrets          = [for name, ref in local.fixture_secrets : { name = name, valueFrom = ref }],
    linuxParameters  = { capabilities = { drop = ["ALL"] } },
    logConfiguration = { logDriver = "awslogs", options = { awslogs-group = aws_cloudwatch_log_group.controller["fixture"].name, awslogs-region = var.region, awslogs-stream-prefix = "native-bootstrap" } }
  }])
}
resource "aws_iam_role_policy" "native_bootstrap" {
  count = var.native_bootstrap == null ? 0 : 1
  role  = aws_iam_role.native_bootstrap[0].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["s3:GetBucketVersioning", "s3:ListBucketVersions"], Resource = local.state_bucket_arn },
    { Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject"], Resource = "${local.state_bucket_arn}/agent-e2e/${local.candidate_hex}/*" },
    { Effect = "Allow", Action = "s3:PutObject", Resource = "${aws_s3_bucket.handoff.arn}/native/bootstrap/${local.candidate_hex}/*", Condition = { StringEquals = { "s3:if-none-match" = "*" } } },
    { Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:UpdateItem"], Resource = local.state_table_arn, Condition = { "ForAllValues:StringEquals" = { "dynamodb:LeadingKeys" = [var.candidate_digest] } } },
    { Effect = "Allow", Action = "secretsmanager:PutSecretValue", Resource = [for secret in aws_secretsmanager_secret.api : secret.arn] },
    { Effect = "Allow", Action = "ecs:DescribeTasks", Resource = "arn:aws:ecs:${var.region}:${var.account_id}:task/${module.application.ecs_cluster_name}/*" }
  ] })
}
resource "aws_iam_role_policy" "dispatch_native_bootstrap" {
  count = var.native_bootstrap == null || var.dispatch_budget == null ? 0 : 1
  role  = aws_iam_role.dispatcher.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = "ecs:RunTask", Resource = aws_ecs_task_definition.native_bootstrap[0].arn, Condition = { ArnEquals = { "ecs:cluster" = local.target_cluster_arn } } },
    { Effect = "Allow", Action = "iam:PassRole", Resource = [aws_iam_role.native_bootstrap[0].arn, aws_iam_role.execution["fixture"].arn], Condition = { StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" } } }
  ] })
}
# A missing allowance disables new launches, while retaining observation and
# bounded stop authority for any original task that still needs cleanup.
resource "aws_iam_role_policy" "observe_native_bootstrap" {
  count = var.native_bootstrap == null ? 0 : 1
  role  = aws_iam_role.dispatcher.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = "ecs:DescribeTaskDefinition", Resource = aws_ecs_task_definition.native_bootstrap[0].arn },
    { Effect = "Allow", Action = "ecs:StopTask", Resource = "arn:aws:ecs:${var.region}:${var.account_id}:task/${module.application.ecs_cluster_name}/*" },
    { Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion"], Resource = "${aws_s3_bucket.handoff.arn}/native/bootstrap/${local.candidate_hex}/*" }
  ] })
}
output "native_bootstrap_handoff" {
  description = "Review this actual nonsecret task binding into source before invoking the checked private bootstrap workflow. A null output grants no initialization path."
  value = var.native_bootstrap == null || var.dispatch_budget == null ? null : {
    schemaVersion     = 1, region = var.region, accountId = var.account_id, clusterArn = local.target_cluster_arn,
    taskDefinitionArn = aws_ecs_task_definition.native_bootstrap[0].arn, taskRoleArn = aws_iam_role.native_bootstrap[0].arn,
    executionRoleArn  = aws_iam_role.execution["fixture"].arn, containerName = "native-bootstrap", imageUri = var.controller_image.uri,
    subnetIds         = module.network.private_subnets, securityGroupIds = [aws_security_group.controller.id],
    configuration     = local.native_bootstrap_config, configurationSha256 = "sha256:${sha256(jsonencode(local.native_bootstrap_config))}",
    dispatchBudget    = merge(var.dispatch_budget, { candidateKey = var.candidate_digest, claimTable = aws_dynamodb_table.claims["dispatch"].name })
  }
}
