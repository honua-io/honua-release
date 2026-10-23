locals {
  console_secret_kms_keys = distinct(compact(concat(var.secret_kms_key_arns, [var.identity_secrets_kms_key_arn])))
}

resource "aws_security_group" "console" {
  name_prefix = "${var.name_prefix}-console-"
  description = "Only the ALB reaches the authenticated edge; Console binds loopback"
  vpc_id      = module.server.vpc_id
  ingress {
    from_port       = 8080
    to_port         = 8080
    protocol        = "tcp"
    security_groups = [module.server.alb_security_group_id]
  }
  egress {
    description = "TLS to Server, registry, secrets and telemetry through private-subnet NAT"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_cloudwatch_log_group" "console" {
  name              = "/honua/pilot/${var.name_prefix}/console"
  retention_in_days = 14
}

data "aws_iam_policy_document" "task_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "console_execution" {
  name_prefix          = "${var.name_prefix}-exec-"
  assume_role_policy   = data.aws_iam_policy_document.task_assume.json
  permissions_boundary = var.permissions_boundary_arn
}

resource "aws_iam_role" "console_task" {
  name_prefix          = "${var.name_prefix}-task-"
  assume_role_policy   = data.aws_iam_policy_document.task_assume.json
  permissions_boundary = var.permissions_boundary_arn
}

resource "aws_iam_role_policy_attachment" "console_execution" {
  role       = aws_iam_role.console_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "console_secret" {
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [var.console_agent_secret_arn, aws_secretsmanager_secret.edge.arn, aws_secretsmanager_secret.oidc_client["console_edge"].arn]
  }
  dynamic "statement" {
    for_each = length(local.console_secret_kms_keys) > 0 ? [1] : []
    content {
      actions   = ["kms:Decrypt"]
      resources = local.console_secret_kms_keys
      condition {
        test     = "StringEquals"
        variable = "kms:ViaService"
        values   = ["secretsmanager.${var.region}.amazonaws.com"]
      }
    }
  }
}

resource "aws_iam_role_policy" "console_secret" {
  role   = aws_iam_role.console_execution.id
  policy = data.aws_iam_policy_document.console_secret.json
}

resource "aws_ecs_task_definition" "console" {
  family                   = "${var.name_prefix}-console"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "512"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.console_execution.arn
  task_role_arn            = aws_iam_role.console_task.arn
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }
  container_definitions = jsonencode([{
    name         = "console", image = var.candidate.console_image, essential = true,
    portMappings = [],
    environment = [for name, value in {
      ASPNETCORE_ENVIRONMENT                                   = "Production"
      ASPNETCORE_URLS                                          = "http://127.0.0.1:8081"
      HONUA_SERVER_BASE_URL                                    = local.server_origin
      HONUA_AGENT_SERVER_BASE_URL                              = local.server_origin
      HONUA_AGENT_RELEASE_ID                                   = var.candidate.manifest_sha256
      HONUA_NATIVE_MODEL_ENABLED                               = "false"
      Studio__EmbedJsStudio                                    = "true"
      Honua__Console__Auth__Mode                               = "EdgeForwarded"
      Honua__Console__Auth__EdgeForwarded__ServerSessionIssuer = "https://${aws_cognito_user_pool.pilot.endpoint}"
    } : { name = name, value = value }],
    secrets = [
      { name = "HONUA_AGENT_API_KEY", valueFrom = var.console_agent_secret_arn },
      { name = "Honua__Console__Auth__EdgeForwarded__SharedSecret", valueFrom = "${aws_secretsmanager_secret.edge.arn}:shared::" }
    ],
    logConfiguration = {
      logDriver = "awslogs", options = {
        "awslogs-group"  = aws_cloudwatch_log_group.console.name,
        "awslogs-region" = var.region, "awslogs-stream-prefix" = "console"
      }
    }
    }, {
    name         = "edge", image = var.edge_proxy_image, essential = true,
    portMappings = [{ containerPort = 8080, protocol = "tcp" }],
    entryPoint   = ["/bin/sh", "-ec"],
    command      = ["umask 077; printf '%s' \"$HONUA_EDGE_CONFIG\" > /tmp/edge.json; exec /bin/oauth2-proxy --alpha-config=/tmp/edge.json"],
    environment = [for name, value in {
      HONUA_EDGE_CONFIG                 = local.edge_config
      OAUTH2_PROXY_REDIRECT_URL         = var.identity_redirects.edge_callback_url
      OAUTH2_PROXY_REVERSE_PROXY        = "true"
      OAUTH2_PROXY_WHITELIST_DOMAINS    = trimprefix(var.identity_redirects.console_origin, "https://")
      OAUTH2_PROXY_EMAIL_DOMAINS        = "*"
      OAUTH2_PROXY_COOKIE_NAME          = "__Host-honua-pilot"
      OAUTH2_PROXY_COOKIE_SECURE        = "true"
      OAUTH2_PROXY_COOKIE_HTTPONLY      = "true"
      OAUTH2_PROXY_COOKIE_SAMESITE      = "lax"
      OAUTH2_PROXY_COOKIE_EXPIRE        = "8h"
      OAUTH2_PROXY_COOKIE_REFRESH       = "10m"
      OAUTH2_PROXY_SKIP_PROVIDER_BUTTON = "true"
      OAUTH2_PROXY_REQUEST_LOGGING      = "false"
      OAUTH2_PROXY_AUTH_LOGGING         = "false"
    } : { name = name, value = value }],
    secrets = [
      { name = "OAUTH2_PROXY_CLIENT_SECRET", valueFrom = aws_secretsmanager_secret.oidc_client["console_edge"].arn },
      { name = "HONUA_EDGE_SHARED_SECRET", valueFrom = "${aws_secretsmanager_secret.edge.arn}:shared::" },
      { name = "OAUTH2_PROXY_COOKIE_SECRET", valueFrom = "${aws_secretsmanager_secret.edge.arn}:cookie::" }
    ],
    logConfiguration = { logDriver = "awslogs", options = {
      "awslogs-group"  = aws_cloudwatch_log_group.console.name,
      "awslogs-region" = var.region, "awslogs-stream-prefix" = "edge"
    } }
  }])
}

resource "aws_ecs_service" "console" {
  name                   = "${var.name_prefix}-console"
  cluster                = module.server.ecs_cluster_name
  task_definition        = aws_ecs_task_definition.console.arn
  desired_count          = 1
  launch_type            = "FARGATE"
  enable_execute_command = false
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
  network_configuration {
    subnets          = module.server.private_subnet_ids
    security_groups  = [aws_security_group.console.id]
    assign_public_ip = false
  }
  load_balancer {
    target_group_arn = aws_lb_target_group.console.arn
    container_name   = "edge"
    container_port   = 8080
  }
  depends_on = [aws_iam_role_policy_attachment.console_execution, aws_iam_role_policy.console_secret, aws_lb_listener_rule.console, aws_secretsmanager_secret_version.edge, aws_secretsmanager_secret_version.oidc_client]
}
