# Intentionally no inbound rules, target group, listener or DNS record. The
# chosen compatible authentication boundary must be implemented before exposure.
resource "aws_security_group" "console" {
  name_prefix = "${var.name_prefix}-console-"
  description = "Private preparation task; no inbound access before identity binding"
  vpc_id      = module.server.vpc_id
  ingress     = []
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
  name_prefix        = "${var.name_prefix}-exec-"
  assume_role_policy = data.aws_iam_policy_document.task_assume.json
}

resource "aws_iam_role" "console_task" {
  name_prefix        = "${var.name_prefix}-task-"
  assume_role_policy = data.aws_iam_policy_document.task_assume.json
}

resource "aws_iam_role_policy_attachment" "console_execution" {
  role       = aws_iam_role.console_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "console_secret" {
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [var.console_agent_secret_arn]
  }
  dynamic "statement" {
    for_each = length(var.secret_kms_key_arns) > 0 ? [1] : []
    content {
      actions   = ["kms:Decrypt"]
      resources = var.secret_kms_key_arns
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
    portMappings = [{ containerPort = 8080, protocol = "tcp" }],
    environment = [for name, value in {
      ASPNETCORE_ENVIRONMENT      = "Production"
      ASPNETCORE_URLS             = "http://+:8080"
      HONUA_SERVER_BASE_URL       = local.server_origin
      HONUA_AGENT_SERVER_BASE_URL = local.server_origin
      HONUA_AGENT_RELEASE_ID      = var.candidate.manifest_sha256
      HONUA_NATIVE_MODEL_ENABLED  = "false"
      Studio__EmbedJsStudio       = "true"
    } : { name = name, value = value }],
    secrets = [{ name = "HONUA_AGENT_API_KEY", valueFrom = var.console_agent_secret_arn }],
    logConfiguration = {
      logDriver = "awslogs", options = {
        "awslogs-group"  = aws_cloudwatch_log_group.console.name,
        "awslogs-region" = var.region, "awslogs-stream-prefix" = "console"
      }
    }
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
  depends_on = [aws_iam_role_policy_attachment.console_execution, aws_iam_role_policy.console_secret]
}
