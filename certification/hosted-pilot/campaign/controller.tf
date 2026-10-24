locals {
  ecs_trust = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "ecs-tasks.amazonaws.com" }, Condition = { StringEquals = { "aws:SourceAccount" = var.account_id }, ArnLike = { "aws:SourceArn" = "arn:aws:ecs:${var.region}:${var.account_id}:*" } } }] })
  custody = {
    bucket         = aws_s3_bucket.handoff.id
    requestPrefix  = "controller/requests/${local.candidate_hex}/"
    sourcePrefix   = "controller/sources/${local.candidate_hex}/"
    bundlePrefix   = "controller/bundles/${local.candidate_hex}/"
    outcomePrefix  = "controller/outcomes/${local.candidate_hex}/"
    evidencePrefix = "controller/evidence/${local.candidate_hex}/"
  }
  target_cluster_arn = "arn:aws:ecs:${var.region}:${var.account_id}:cluster/${module.application.ecs_cluster_name}"
}
resource "aws_iam_role" "controller" {
  name                 = "${var.name_prefix}-controller"
  permissions_boundary = var.permissions_boundary_arn
  assume_role_policy   = local.ecs_trust
}
resource "aws_iam_role" "execution" {
  for_each             = toset(["controller", "fixture"])
  name                 = "${var.name_prefix}-${each.key}-execution"
  permissions_boundary = var.permissions_boundary_arn
  assume_role_policy   = local.ecs_trust
}
resource "aws_iam_role" "dispatcher" {
  name                 = "${var.name_prefix}-controller-dispatch"
  permissions_boundary = var.permissions_boundary_arn
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Action = "sts:AssumeRoleWithWebIdentity", Principal = { Federated = var.oidc_provider_arn }, Condition = { StringEquals = {
    "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com",
    "token.actions.githubusercontent.com:sub" = "repo:honua-io/honua-release:ref:refs/heads/trunk"
  } } }] })
}
module "accounting" {
  source              = "git::https://github.com/honua-io/honua-iac.git//infrastructure/terraform/internal/aws-agent-controller?ref=484e9d762e9b9ec0483170f548fa0bb306623bd2"
  region              = var.region
  account_id          = var.account_id
  candidate_digest    = var.candidate_digest
  controller_role_arn = aws_iam_role.controller.arn
  # Dispatcher IAM grants only GetObjectVersion/GetItem on retained state. Its
  # explicit resource-policy exception permits observation, never ledger writes.
  state_admin_role_arns  = setunion(var.state_admin_role_arns, [aws_iam_role.dispatcher.arn, aws_iam_role.fixture.arn, aws_iam_role.initializer.arn], [for role in aws_iam_role.native_bootstrap : role.arn], [for role in aws_iam_role.setup_retirement : role.arn])
  claim_writer_role_arns = local.worker_claim_writers
}
resource "aws_s3_bucket" "handoff" {
  bucket        = "${var.name_prefix}-controller-${var.account_id}"
  force_destroy = false
  lifecycle { prevent_destroy = true }
}
resource "aws_s3_bucket_public_access_block" "handoff" {
  bucket                  = aws_s3_bucket.handoff.id
  block_public_acls       = true
  ignore_public_acls      = true
  block_public_policy     = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_ownership_controls" "handoff" {
  bucket = aws_s3_bucket.handoff.id
  rule { object_ownership = "BucketOwnerEnforced" }
}
resource "aws_s3_bucket_versioning" "handoff" {
  bucket = aws_s3_bucket.handoff.id
  versioning_configuration { status = "Enabled" }
}
resource "aws_s3_bucket_server_side_encryption_configuration" "handoff" {
  bucket = aws_s3_bucket.handoff.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}
resource "aws_s3_bucket_policy" "handoff" {
  bucket     = aws_s3_bucket.handoff.id
  depends_on = [aws_s3_bucket_public_access_block.handoff]
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Sid = "TlsOnly", Effect = "Deny", Principal = "*", Action = "s3:*", Resource = [aws_s3_bucket.handoff.arn, "${aws_s3_bucket.handoff.arn}/*"], Condition = { Bool = { "aws:SecureTransport" = "false" } } },
    { Sid = "RetainImmutableCustody", Effect = "Deny", Principal = "*", Action = ["s3:DeleteObject", "s3:DeleteObjectVersion"], Resource = "${aws_s3_bucket.handoff.arn}/*" },
    { Sid = "ConditionalWritesOnly", Effect = "Deny", Principal = "*", Action = "s3:PutObject", Resource = "${aws_s3_bucket.handoff.arn}/*", Condition = { StringNotEquals = { "s3:if-none-match" = "*" } } },
    { Sid = "TrustedCustodyOnly", Effect = "Deny", Principal = "*", Action = "s3:*", Resource = [aws_s3_bucket.handoff.arn, "${aws_s3_bucket.handoff.arn}/*"], Condition = { ArnNotEquals = { "aws:PrincipalArn" = concat([aws_iam_role.controller.arn, aws_iam_role.dispatcher.arn, aws_iam_role.fixture.arn], [for role in aws_iam_role.native_bootstrap : role.arn], [for role in aws_iam_role.setup_retirement : role.arn], tolist(var.state_admin_role_arns)) } } }
  ] })
}
resource "aws_dynamodb_table" "claims" {
  for_each                    = toset(["dispatch", "fixture"])
  name                        = "${var.name_prefix}-controller-${each.key}-claims"
  billing_mode                = "PAY_PER_REQUEST"
  hash_key                    = "claimKey"
  deletion_protection_enabled = true
  attribute {
    name = "claimKey"
    type = "S"
  }
  point_in_time_recovery { enabled = true }
  server_side_encryption { enabled = true }
  lifecycle { prevent_destroy = true }
}
resource "random_password" "fixture" {
  for_each = local.fixture_roles
  length   = 48
  special  = false
}
resource "aws_secretsmanager_secret" "fixture" {
  for_each                = local.fixture_roles
  name                    = "${var.name_prefix}/fixture/${each.key}"
  recovery_window_in_days = 30
}
resource "aws_secretsmanager_secret_version" "fixture" {
  for_each      = local.fixture_roles
  secret_id     = aws_secretsmanager_secret.fixture[each.key].id
  secret_string = "postgresql://${each.value}:${random_password.fixture[each.key].result}@${module.application.db_endpoint}:5432/${local.database}?sslmode=verify-full&sslrootcert=/controller/certification/hosted-pilot/controller/rds-global-bundle.pem"
}
# Values are populated only by native API-key issuance in the checked provisioner.
# Empty secrets are foundation staging; missing versions prevent task startup.
resource "aws_secretsmanager_secret" "api" {
  for_each                = toset(["setup", "observer", "agent"])
  name                    = "${var.name_prefix}/native-agent/${each.key}"
  recovery_window_in_days = 30
}
resource "aws_cloudwatch_log_group" "controller" {
  for_each          = toset(["controller", "fixture"])
  name              = "/ecs/${var.name_prefix}-${each.key}"
  retention_in_days = 14
}
locals {
  fixture_sql_secrets = {
    HONUA_AGENT_FIXTURE_CLEANUP_DSN  = "${aws_secretsmanager_secret.fixture["cleanup"].arn}:::${aws_secretsmanager_secret_version.fixture["cleanup"].version_id}"
    HONUA_AGENT_FIXTURE_OBSERVER_DSN = "${aws_secretsmanager_secret.fixture["observer"].arn}:::${aws_secretsmanager_secret_version.fixture["observer"].version_id}"
  }
  controller_secrets = merge(local.fixture_sql_secrets, local.runtime_control_injection, var.issued_native_keys == null ? {} : {
    HONUA_AGENT_SETUP_KEY     = "${aws_secretsmanager_secret.api["setup"].arn}:::${var.issued_native_keys.setup.versionId}"
    HONUA_AGENT_OBSERVER_KEY  = "${aws_secretsmanager_secret.api["observer"].arn}:::${var.issued_native_keys.observer.versionId}"
    HONUA_AGENT_REQUESTER_KEY = "${aws_secretsmanager_secret.api["agent"].arn}:::${var.issued_native_keys.agent.versionId}"
    }, var.human_operator_credential == null ? {} : {
    HONUA_SOURCE_CONTRIBUTOR_OPERATOR_TOKEN = "${var.human_operator_credential.secretArn}:::${var.human_operator_credential.versionId}"
    }, var.human_browser == null ? {} : {
    HONUA_CONSOLE_BROWSER_STORAGE_STATE = "${var.human_browser.storageState.secretArn}:::${var.human_browser.storageState.versionId}"
  })
  fixture_secrets = merge(local.fixture_sql_secrets, {
    PILOT_FIXTURE_MASTER_CONNECTION = module.application.db_connection_secret_arn
    PILOT_FIXTURE_ADMIN_PASSWORD    = module.application.admin_password_secret_arn
  })
}
resource "aws_iam_role_policy" "execution" {
  for_each = aws_iam_role.execution
  role     = each.value.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["ecr:GetAuthorizationToken"], Resource = "*" },
    { Effect = "Allow", Action = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"], Resource = "arn:aws:ecr:${var.region}:${var.account_id}:repository/${split("/", split("@", var.controller_image.uri)[0])[1]}" },
    { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.controller[each.key].arn}:*" },
    { Effect = "Allow", Action = "secretsmanager:GetSecretValue", Resource = each.key == "fixture" ? concat([module.application.db_connection_secret_arn, module.application.admin_password_secret_arn], [for secret in aws_secretsmanager_secret.fixture : secret.arn]) : concat([for secret in aws_secretsmanager_secret.fixture : secret.arn], var.issued_native_keys == null ? [] : [for secret in aws_secretsmanager_secret.api : secret.arn], var.human_operator_credential == null ? [] : [var.human_operator_credential.secretArn]) },
    { Effect = "Allow", Action = "kms:Decrypt", Resource = "arn:aws:kms:${var.region}:${var.account_id}:key/*", Condition = { StringEquals = { "kms:ViaService" = "secretsmanager.${var.region}.amazonaws.com", "aws:ResourceTag/ResetBoundary" = var.name_prefix }, ArnLike = { "kms:EncryptionContext:SecretARN" = "arn:aws:secretsmanager:${var.region}:${var.account_id}:secret:${var.name_prefix}*" } } }
  ] })
}
resource "aws_ecs_task_definition" "controller" {
  family                   = "${var.name_prefix}-campaign-controller"
  cpu                      = "512"
  memory                   = "2048"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  task_role_arn            = aws_iam_role.controller.arn
  execution_role_arn       = aws_iam_role.execution["controller"].arn
  runtime_platform {
    cpu_architecture        = "X86_64"
    operating_system_family = "LINUX"
  }
  volume { name = "work" }
  container_definitions = jsonencode([{
    name        = "campaign-controller", image = var.controller_image.uri, essential = true,
    command     = ["python", "tools/hosted_pilot/controller_entry.py"], user = "10001:10001", readonlyRootFilesystem = true,
    mountPoints = [{ sourceVolume = "work", containerPath = "/work", readOnly = false }],
    environment = concat([
      { name = "PILOT_CONTROLLER_CUSTODY", value = jsonencode({ region = var.region, accountId = var.account_id, bucket = aws_s3_bucket.handoff.id, requestPrefix = local.custody.requestPrefix }) },
      { name = "PILOT_FIXTURE_HANDOFF", value = jsonencode(local.fixture_handoff) }
      ], var.controller_model == null ? [] : [
      { name = "TERMINAL_MODEL_NAME", value = var.controller_model.modelId },
      { name = "HONUA_CONTROLLER_MODEL_RUNTIME", value = jsonencode(var.controller_model) }
    ]),
    secrets          = [for name, ref in local.controller_secrets : { name = name, valueFrom = ref }],
    linuxParameters  = { capabilities = { drop = ["ALL"] } },
    logConfiguration = { logDriver = "awslogs", options = { awslogs-group = aws_cloudwatch_log_group.controller["controller"].name, awslogs-region = var.region, awslogs-stream-prefix = "controller" } }
  }])
}
resource "aws_iam_role" "lifecycle" {
  name                 = "${var.name_prefix}-controller-lifecycle"
  permissions_boundary = var.permissions_boundary_arn
  assume_role_policy   = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "states.amazonaws.com" }, Condition = { StringEquals = { "aws:SourceAccount" = var.account_id }, ArnLike = { "aws:SourceArn" = "arn:aws:states:${var.region}:${var.account_id}:stateMachine:${var.name_prefix}-*" } } }] })
}
locals {
  controller_definition = {
    StartAt = "RunController", TimeoutSeconds = var.controller_max_seconds,
    States = { RunController = {
      Type = "Task", Resource = "arn:aws:states:::ecs:runTask.sync", End = true,
      Parameters = {
        Cluster              = local.target_cluster_arn, TaskDefinition = aws_ecs_task_definition.controller.arn,
        LaunchType           = "FARGATE", Count = 1, EnableExecuteCommand = false,
        NetworkConfiguration = { AwsvpcConfiguration = { Subnets = module.network.private_subnets, SecurityGroups = [aws_security_group.controller.id], AssignPublicIp = "DISABLED" } },
        Overrides            = { ContainerOverrides = [{ Name = "campaign-controller", Environment = [{ Name = "PILOT_CONTROLLER_REQUEST", "Value.$" = "$.request" }] }] }
      }
    } }
  }
}
resource "aws_sfn_state_machine" "controller" {
  name       = "${var.name_prefix}-campaign-controller"
  role_arn   = aws_iam_role.lifecycle.arn
  type       = "STANDARD"
  publish    = true
  definition = jsonencode(local.controller_definition)
}
resource "aws_iam_role_policy" "lifecycle" {
  role = aws_iam_role.lifecycle.id
  policy = jsonencode({ Version = "2012-10-17", Statement = concat([
    { Effect = "Allow", Action = ["ecs:DescribeTasks", "ecs:StopTask"], Resource = "arn:aws:ecs:${var.region}:${var.account_id}:task/${module.application.ecs_cluster_name}/*" },
    { Effect = "Allow", Action = ["events:PutTargets", "events:PutRule", "events:DescribeRule"], Resource = "arn:aws:events:${var.region}:${var.account_id}:rule/StepFunctionsGetEventsForECSTaskRule" }
    ], [for statement in [
      { Effect = "Allow", Action = "ecs:RunTask", Resource = aws_ecs_task_definition.controller.arn, Condition = { ArnEquals = { "ecs:cluster" = local.target_cluster_arn } } },
      { Effect = "Allow", Action = "iam:PassRole", Resource = [aws_iam_role.controller.arn, aws_iam_role.execution["controller"].arn], Condition = { StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" } } }
  ] : statement if var.dispatch_budget != null]) })
}
