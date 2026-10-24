variable "native_runtime_registration" {
  description = "One reviewed registrar declaration/quote. Null creates no privileged registrar task or launch authority."
  type = object({
    originalFundingExpiresEpoch = number
    bootstrapArtifactDigest     = string
    operation = object({
      operationId    = string, liabilityBasisDigest = string,
      reservationIds = object({ infrastructure = string, cleanup = string })
    })
    declaration         = object({ path = string, sha256 = string })
    maximumUsd          = object({ infrastructure = string, cleanup = string })
    maxExecutionSeconds = number
    liabilityQuote      = object({ path = string, sha256 = string })
  })
  default = null
  validation {
    condition = var.native_runtime_registration == null ? true : (
      var.native_runtime_registration.maxExecutionSeconds == floor(var.native_runtime_registration.maxExecutionSeconds) &&
      var.native_runtime_registration.maxExecutionSeconds > 0 && var.native_runtime_registration.maxExecutionSeconds <= 600 &&
      var.native_runtime_registration.originalFundingExpiresEpoch == floor(var.native_runtime_registration.originalFundingExpiresEpoch) && var.native_runtime_registration.originalFundingExpiresEpoch > 0 &&
      alltrue([for value in values(var.native_runtime_registration.maximumUsd) : can(tonumber(value)) && try(tonumber(value) > 0, false)]) &&
      can(regex("^certification/agent-e2e/installed-runtimes/[A-Za-z0-9_-]{1,128}\\.json$", var.native_runtime_registration.declaration.path)) &&
      can(regex("^sha256:[a-f0-9]{64}$", var.native_runtime_registration.declaration.sha256)) &&
      can(regex("^certification/hosted-pilot/quotes/[A-Za-z0-9_./-]+\\.json$", var.native_runtime_registration.liabilityQuote.path)) &&
      !contains(split("/", var.native_runtime_registration.liabilityQuote.path), "..") &&
      can(regex("^sha256:[a-f0-9]{64}$", var.native_runtime_registration.liabilityQuote.sha256))
    )
    error_message = "Use an exact reviewed declaration and original positive bounded registrar quote; no default allowance or renewed lifetime."
  }
}

locals {
  runtime_registration_credentials = var.native_runtime_registration == null ? {} : {
    registrar = local.native_root_version
    control = {
      secretArn = try(aws_secretsmanager_secret.runtime_control[0].arn, "")
      versionId = try(var.issued_runtime_control_key.versionId, "")
    }
    task = {
      secretArn = aws_secretsmanager_secret.api["agent"].arn
      versionId = try(var.issued_native_keys.agent.versionId, "")
    }
  }
  runtime_registration_configuration = var.native_runtime_registration == null ? null : merge({ for key, value in var.native_runtime_registration : key => value if key != "originalFundingExpiresEpoch" }, {
    schemaVersion        = 1, region = var.region, accountId = var.account_id, candidateKey = var.candidate_digest,
    state                = module.accounting.state_config, generationDigest = try(var.dispatch_budget.generationDigest, null),
    clusterArn           = local.target_cluster_arn,
    taskDefinitionFamily = "arn:aws:ecs:${var.region}:${var.account_id}:task-definition/${var.name_prefix}-runtime-registration",
    imageUri             = var.controller_image.uri, credentials = local.runtime_registration_credentials
  })
  runtime_registration_secret_names = {
    registrar = "PILOT_FIXTURE_ADMIN_PASSWORD", control = "HONUA_EXTERNAL_EXECUTION_CONTROL_KEY", task = "HONUA_AGENT_REQUESTER_KEY"
  }
  runtime_registration_worker_machines = [for attempt in sort(tolist(var.worker_attempt_ids)) :
    "arn:aws:states:${var.region}:${var.account_id}:stateMachine:honua-agent-${attempt}"
  ]
}

resource "aws_ecs_task_definition" "runtime_registration" {
  count                    = var.native_runtime_registration == null ? 0 : 1
  family                   = "${var.name_prefix}-runtime-registration"
  cpu                      = "256"
  memory                   = "1024"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  task_role_arn            = try(aws_iam_role.native_bootstrap[0].arn, "")
  execution_role_arn       = try(aws_iam_role.native_bootstrap_execution[0].arn, "")
  runtime_platform {
    cpu_architecture        = "X86_64"
    operating_system_family = "LINUX"
  }
  volume { name = "work" }
  container_definitions = jsonencode([{
    name        = "runtime-registration", image = var.controller_image.uri, essential = true,
    command     = ["python", "tools/hosted_pilot/native_runtime_registration.py"],
    user        = "10001:10001", readonlyRootFilesystem = true,
    environment = [{ name = "PILOT_NATIVE_RUNTIME_REGISTRATION_CONFIG", value = jsonencode(local.runtime_registration_configuration) }],
    secrets = [for kind, ref in local.runtime_registration_credentials : {
      name = local.runtime_registration_secret_names[kind], valueFrom = "${try(ref.secretArn, "")}:::${try(ref.versionId, "")}"
    }],
    mountPoints     = [{ sourceVolume = "work", containerPath = "/work", readOnly = false }],
    linuxParameters = { capabilities = { drop = ["ALL"] } },
    logConfiguration = { logDriver = "awslogs", options = {
      awslogs-group = aws_cloudwatch_log_group.controller["fixture"].name, awslogs-region = var.region, awslogs-stream-prefix = "runtime-registration"
    } }
  }])
  lifecycle {
    precondition {
      condition = (
        local.native_installed_runtime != null && var.native_bootstrap != null &&
        var.issued_runtime_control_key != null && var.issued_native_keys != null &&
        var.dispatch_budget != null && length(var.worker_attempt_ids) > 0 &&
        try(var.native_runtime_registration.declaration.path == "certification/agent-e2e/installed-runtimes/${local.native_installed_runtime.runtimeId}.json", false) &&
        try(var.native_runtime_registration.operation.operationId == "runtime-registration-${local.native_installed_runtime.runtimeId}", false) &&
        try(var.native_runtime_registration.bootstrapArtifactDigest == var.native_bootstrap.bootstrapArtifactDigest, false) &&
        try(timecmp(timeadd("1970-01-01T00:00:00Z", "${var.native_runtime_registration.originalFundingExpiresEpoch}s"), var.native_bootstrap.expiresAt) == 0, false)
      )
      error_message = "Registrar staging needs actual original root/control/task versions, fixed installed workers, declaration and original dispatch/accounting custody."
    }
    precondition {
      condition = var.native_runtime_registration.operation.liabilityBasisDigest == "sha256:${sha256(jsonencode({
        for key, value in local.runtime_registration_configuration : key => value if key != "operation"
      }))}"
      error_message = "Registrar quote reservations must bind this exact original source configuration and credentials."
    }
  }
}

# Reuse the privileged bootstrap execution role without granting the registrar
# task any secret-service reads. Only three immutable values reach its container.
resource "aws_iam_role_policy" "runtime_registration_injection" {
  count = var.native_runtime_registration == null ? 0 : 1
  role  = try(aws_iam_role.native_bootstrap_execution[0].id, "")
  policy = jsonencode({ Version = "2012-10-17", Statement = flatten([
    for kind, credential in local.runtime_registration_credentials : [
      { Effect = "Allow", Action = "secretsmanager:GetSecretValue", Resource = try(credential.secretArn, ""),
      Condition = { StringEquals = { "secretsmanager:VersionId" = try(credential.versionId, "") } } },
      { Effect = "Deny", Action = "secretsmanager:GetSecretValue", Resource = try(credential.secretArn, ""),
      Condition = { StringNotEquals = { "secretsmanager:VersionId" = try(credential.versionId, "") } } }
    ]
  ]) })
}

resource "aws_iam_role_policy" "runtime_registration_observation" {
  count = var.native_runtime_registration == null ? 0 : 1
  role  = try(aws_iam_role.native_bootstrap[0].id, "")
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = "ecr:BatchGetImage",
    Resource = "arn:aws:ecr:${var.region}:${var.account_id}:repository/${split("/", split("@", var.controller_image.uri)[0])[1]}" },
    { Effect = "Allow", Action = "states:DescribeStateMachine", Resource = local.runtime_registration_worker_machines },
    { Effect = "Allow", Action = "ecs:DescribeTaskDefinition", Resource = "*", Condition = { StringEquals = { "aws:RequestedRegion" = var.region } } }
  ] })
}
