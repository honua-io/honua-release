# Privileged SQL custody is confined to this separate one-shot task. The
# controller receives only the exact task definition and immutable handoff.
resource "aws_iam_role" "fixture" {
  name                 = "${var.name_prefix}-fixture-provisioner"
  permissions_boundary = var.permissions_boundary_arn
  assume_role_policy   = local.ecs_trust
}
locals {
  fixture_config = {
    schemaVersion        = 1
    region               = var.region
    accountId            = var.account_id
    candidateKey         = var.candidate_digest
    generationDigest     = var.dispatch_budget.generationDigest
    state                = var.dispatch_budget.state
    bucket               = aws_s3_bucket.handoff.id
    requestPrefix        = "fixture/requests/${local.candidate_hex}/"
    receiptPrefix        = "fixture/receipts/${local.candidate_hex}/"
    claimTable           = aws_dynamodb_table.claims["fixture"].name
    clusterArn           = local.target_cluster_arn
    taskDefinitionFamily = "arn:aws:ecs:${var.region}:${var.account_id}:task-definition/${var.name_prefix}-fixture-provisioner"
    imageUri             = var.controller_image.uri
    # Endpoint is routing metadata; the module marks related database outputs
    # sensitive together. No password or connection string enters this config.
    databaseHost = nonsensitive(module.target.db_endpoint)
    database     = local.database
    appRole      = local.app_role
    cleanupRole  = local.fixture_roles.cleanup
  }
  fixture_handoff = {
    schemaVersion           = 1
    region                  = var.region
    accountId               = var.account_id
    clusterArn              = local.target_cluster_arn
    taskDefinitionArn       = aws_ecs_task_definition.fixture.arn
    containerName           = "fixture-provisioner"
    imageUri                = var.controller_image.uri
    controllerRoleArn       = aws_iam_role.fixture.arn
    executionRoleArn        = aws_iam_role.execution["fixture"].arn
    subnetIds               = module.network.private_subnets
    securityGroupIds        = [aws_security_group.controller.id]
    requestBucket           = aws_s3_bucket.handoff.id
    requestPrefix           = local.fixture_config.requestPrefix
    receiptPrefix           = local.fixture_config.receiptPrefix
    claimTable              = aws_dynamodb_table.claims["fixture"].name
    provisionerConfigSha256 = "sha256:${sha256(jsonencode(local.fixture_config))}"
  }
}
resource "aws_ecs_task_definition" "fixture" {
  family                   = "${var.name_prefix}-fixture-provisioner"
  cpu                      = "256"
  memory                   = "512"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  task_role_arn            = aws_iam_role.fixture.arn
  execution_role_arn       = aws_iam_role.execution["fixture"].arn
  runtime_platform {
    cpu_architecture        = "X86_64"
    operating_system_family = "LINUX"
  }
  container_definitions = jsonencode([{
    name             = "fixture-provisioner", image = var.controller_image.uri, essential = true,
    command          = ["python", "tools/hosted_pilot/fixture_provisioner.py", "transfer-ownership"], user = "10001:10001", readonlyRootFilesystem = true,
    environment      = [{ name = "PILOT_PROVISIONER_CONFIG", value = jsonencode(local.fixture_config) }],
    secrets          = [{ name = "PILOT_FIXTURE_MASTER_CONNECTION", valueFrom = module.target.db_connection_secret_arn }],
    linuxParameters  = { capabilities = { drop = ["ALL"] } },
    logConfiguration = { logDriver = "awslogs", options = { awslogs-group = aws_cloudwatch_log_group.controller["fixture"].name, awslogs-region = var.region, awslogs-stream-prefix = "fixture" } }
  }])
}
resource "aws_iam_role_policy" "fixture" {
  role = aws_iam_role.fixture.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = "s3:GetBucketVersioning", Resource = aws_s3_bucket.handoff.arn },
    { Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion"], Resource = ["${aws_s3_bucket.handoff.arn}/${local.fixture_config.requestPrefix}*", "${local.state_bucket_arn}/agent-e2e/${local.candidate_hex}/*"] },
    { Effect = "Allow", Action = "s3:PutObject", Resource = "${aws_s3_bucket.handoff.arn}/${local.fixture_config.receiptPrefix}*", Condition = { StringEquals = { "s3:if-none-match" = "*" } } },
    { Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:ConditionCheckItem"], Resource = local.state_table_arn, Condition = { "ForAllValues:StringEquals" = { "dynamodb:LeadingKeys" = [var.candidate_digest] } } },
    { Effect = "Allow", Action = ["dynamodb:PutItem"], Resource = aws_dynamodb_table.claims["fixture"].arn },
    { Effect = "Allow", Action = "ecs:DescribeTasks", Resource = "arn:aws:ecs:${var.region}:${var.account_id}:task/${module.target.ecs_cluster_name}/*" }
  ] })
}
