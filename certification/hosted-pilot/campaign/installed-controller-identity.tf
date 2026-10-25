# Identity staging only. This phase creates no task definition, supervisor,
# service or launch policy; a later plan consumes real registration originals.
variable "installed_controller_identity" {
  type = object({
    runtimeId                   = string
    permissionsBoundaryArn      = string
    originalFundingExpiresEpoch = number
    control                     = object({ secretArn = string, versionId = string })
    workers = list(object({
      slotId     = string, stateMachineArn = string,
      clusterArn = string, taskRoleArn = string, commandQueueArn = string, replyQueueArn = string
    }))
  })
  default = null
  validation {
    condition = var.installed_controller_identity == null ? true : (
      can(regex("^[A-Za-z0-9_-]{1,128}$", var.installed_controller_identity.runtimeId)) &&
      var.installed_controller_identity.permissionsBoundaryArn == "arn:aws:iam::${var.account_id}:policy/honua-release-${trimprefix(trimsuffix(var.name_prefix, "-agent"), "honua-")}-foundation-installed-controller-boundary" &&
      floor(var.installed_controller_identity.originalFundingExpiresEpoch) == var.installed_controller_identity.originalFundingExpiresEpoch &&
      can(regex("^arn:aws:secretsmanager:${var.region}:${var.account_id}:secret:${var.name_prefix}/native-agent/control-[A-Za-z0-9]{6}$", var.installed_controller_identity.control.secretArn)) &&
      can(regex("^[A-Za-z0-9-]{32,64}$", var.installed_controller_identity.control.versionId)) &&
      length(var.installed_controller_identity.workers) >= 1 && length(var.installed_controller_identity.workers) <= 4 &&
      length(distinct([for w in var.installed_controller_identity.workers : w.slotId])) == length(var.installed_controller_identity.workers) &&
      alltrue([for w in var.installed_controller_identity.workers :
        can(regex("^[a-z0-9-]{3,40}$", w.slotId)) &&
        w.stateMachineArn == "arn:aws:states:${var.region}:${var.account_id}:stateMachine:honua-agent-${w.slotId}" &&
        can(regex("^arn:aws:ecs:${var.region}:${var.account_id}:cluster/[A-Za-z0-9_-]+$", w.clusterArn)) &&
        can(regex("^arn:aws:iam::${var.account_id}:role/honua-agent-${w.slotId}-worker$", w.taskRoleArn)) &&
        w.commandQueueArn == "arn:aws:sqs:${var.region}:${var.account_id}:honua-agent-${w.slotId}-commands.fifo" &&
        w.replyQueueArn == "arn:aws:sqs:${var.region}:${var.account_id}:honua-agent-${w.slotId}-replies.fifo" &&
        w.commandQueueArn != w.replyQueueArn
      ])
    )
    error_message = "Bind exact reviewed runtime/worker outputs and the original immutable dedicated control secret."
  }
}
locals {
  installed_identity = var.installed_controller_identity
  # The accounting module keys a for_each on role_arn, so the key must be known at plan time on the first
  # apply. The configured role name is plan-known and still carries the dependency; the computed .arn is not.
  installed_identity_roles      = local.installed_identity == null ? [] : [{ runtime_id = local.installed_identity.runtimeId, role_arn = "arn:aws:iam::${var.account_id}:role/${aws_iam_role.installed_controller[0].name}" }]
  installed_identity_tasks      = local.installed_identity == null ? [] : [for w in local.installed_identity.workers : "arn:aws:ecs:${var.region}:${var.account_id}:task/${split("/", w.clusterArn)[1]}/*"]
  installed_identity_prefix     = "agent-e2e/${local.candidate_hex}/"
  installed_identity_role_names = local.installed_identity == null ? [] : concat([aws_iam_role.installed_controller[0].arn, aws_iam_role.installed_controller_execution[0].arn], [for w in local.installed_identity.workers : w.taskRoleArn])
}
resource "aws_iam_role" "installed_controller" {
  count                = local.installed_identity == null ? 0 : 1
  name                 = "${var.name_prefix}-installed-controller"
  assume_role_policy   = local.ecs_trust
  permissions_boundary = local.installed_identity.permissionsBoundaryArn
  lifecycle {
    precondition {
      condition = (var.dispatch_budget != null && var.controller_model != null && var.native_bootstrap != null &&
        try(var.native_bootstrap.installedRuntime.runtimeId, "") == local.installed_identity.runtimeId &&
        var.issued_runtime_control_key != null &&
        local.installed_identity.control.secretArn == try(aws_secretsmanager_secret.runtime_control[0].arn, "") &&
        local.installed_identity.control.versionId == try(var.issued_runtime_control_key.versionId, "") &&
        alltrue([for w in local.installed_identity.workers : w.clusterArn == local.target_cluster_arn]) &&
        timeadd("1970-01-01T00:00:00Z", "${local.installed_identity.originalFundingExpiresEpoch}s") == try(var.native_bootstrap.expiresAt, "1970-01-01T00:00:00Z") &&
      alltrue([for w in local.installed_identity.workers : contains(var.worker_attempt_ids, w.slotId)]))
      error_message = "Identity staging needs original retained funding/model/expiry and exact allocated workers; it creates no admission."
    }
  }
}
resource "aws_iam_role" "installed_controller_execution" {
  count                = local.installed_identity == null ? 0 : 1
  name                 = "${var.name_prefix}-installed-controller-execution"
  assume_role_policy   = local.ecs_trust
  permissions_boundary = local.installed_identity.permissionsBoundaryArn
}
resource "aws_security_group" "installed_controller" {
  count       = local.installed_identity == null ? 0 : 1
  name        = "${var.name_prefix}-installed-controller"
  description = "Installed poller HTTPS and private DNS only; no SQL or ingress"
  vpc_id      = module.network.vpc_id
  ingress     = []
  egress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  dynamic "egress" {
    for_each = toset(["tcp", "udp"])
    content {
      from_port   = 53
      to_port     = 53
      protocol    = egress.value
      cidr_blocks = [var.network_cidr]
    }
  }
}
resource "aws_cloudwatch_log_group" "installed_controller" {
  count             = local.installed_identity == null ? 0 : 1
  name              = "/ecs/${var.name_prefix}-installed-controller"
  retention_in_days = 14
}
resource "aws_iam_role_policy" "installed_controller_execution" {
  count = local.installed_identity == null ? 0 : 1
  name  = "installed-controller-execution"
  role  = aws_iam_role.installed_controller_execution[0].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = "ecr:GetAuthorizationToken", Resource = "*" },
    { Effect = "Allow", Action = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"], Resource = "arn:aws:ecr:${var.region}:${var.account_id}:repository/${split("/", split("@", var.controller_image.uri)[0])[1]}" },
    { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.installed_controller[0].arn}:*" },
    { Effect = "Allow", Action = "secretsmanager:GetSecretValue", Resource = local.installed_identity.control.secretArn,
    Condition = { StringEquals = { "secretsmanager:VersionId" = local.installed_identity.control.versionId } } }
  ] })
}
resource "aws_iam_role_policy" "installed_controller" {
  count = local.installed_identity == null ? 0 : 1
  name  = "installed-controller-runtime"
  role  = aws_iam_role.installed_controller[0].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    # Candidate-state S3/head CAS policy is supplied by the accounting primitive,
    # with this role in installed_runtime_controllers, never state_admin_roles.
    { Effect = "Allow", Action = "bedrock:CountTokens", Resource = [var.controller_model.tokenCountingModelArn] },
    { Effect = "Allow", Action = "bedrock:InvokeModel", Resource = var.controller_model_resource_arns },
    { Effect = "Allow", Action = "states:DescribeStateMachine", Resource = concat([for w in local.installed_identity.workers : w.stateMachineArn], ["arn:aws:states:${var.region}:${var.account_id}:stateMachine:${var.name_prefix}-installed-controller"]) },
    { Effect = "Allow", Action = "states:StartExecution", Resource = [for w in local.installed_identity.workers : w.stateMachineArn],
    Condition = { DateLessThan = { "aws:CurrentTime" = try(var.native_bootstrap.expiresAt, "1970-01-01T00:00:00Z") } } },
    { Effect = "Allow", Action = ["states:DescribeExecution", "states:GetExecutionHistory"], Resource = concat([for w in local.installed_identity.workers : "arn:aws:states:${var.region}:${var.account_id}:execution:${split(":", w.stateMachineArn)[6]}:worker-*"], ["arn:aws:states:${var.region}:${var.account_id}:execution:${var.name_prefix}-installed-controller:*"]) },
    { Effect = "Allow", Action = "ecs:DescribeTasks", Resource = concat(local.installed_identity_tasks, ["arn:aws:ecs:${var.region}:${var.account_id}:task/${module.application.ecs_cluster_name}/*"]) },
    { Effect = "Allow", Action = "ecs:DescribeTaskDefinition", Resource = "*", Condition = { StringEquals = { "aws:RequestedRegion" = var.region } } },
    { Effect = "Allow", Action = ["ec2:DescribeNetworkInterfaces", "ec2:DescribeSecurityGroups"], Resource = "*", Condition = { StringEquals = { "aws:RequestedRegion" = var.region } } },
    { Effect = "Allow", Action = ["iam:ListAttachedRolePolicies", "iam:ListRolePolicies", "iam:GetRolePolicy", "iam:GetRole"], Resource = local.installed_identity_role_names },
    { Effect = "Allow", Action = "sqs:SendMessage", Resource = [for w in local.installed_identity.workers : w.commandQueueArn] },
    { Effect = "Allow", Action = ["sqs:ReceiveMessage", "sqs:DeleteMessage"], Resource = [for w in local.installed_identity.workers : w.replyQueueArn] },
    { Effect = "Allow", Action = "dynamodb:GetItem", Resource = "arn:aws:dynamodb:${var.region}:${var.account_id}:table/${module.accounting.claim_table_name}",
      Condition = { "ForAllValues:StringLike" = { "dynamodb:LeadingKeys" = concat(
        [for w in local.installed_identity.workers : "${var.candidate_digest}/runtime/${local.installed_identity.runtimeId}/slot/${w.slotId}"],
        [for suffix in ["", "/launched", "/stopped", "/retired"] : "${var.candidate_digest}/runtime/${local.installed_identity.runtimeId}/execution/execution-????????????????????????????????${suffix}"]
    ) } } }
  ] })
}
output "installed_controller_identity" {
  value = local.installed_identity == null ? null : {
    schema                      = "honua.installed-controller-identity-observation/v1", candidateKey = var.candidate_digest,
    runtimeId                   = local.installed_identity.runtimeId, region = var.region, accountId = var.account_id,
    taskRoleArn                 = aws_iam_role.installed_controller[0].arn, executionRoleArn = aws_iam_role.installed_controller_execution[0].arn,
    permissionsBoundaryArn      = local.installed_identity.permissionsBoundaryArn, assumeRolePolicy = jsondecode(local.ecs_trust), clusterArn = local.target_cluster_arn,
    vpcId                       = module.network.vpc_id, subnetIds = module.network.private_subnets, securityGroupIds = [aws_security_group.installed_controller[0].id],
    originalFundingExpiresEpoch = local.installed_identity.originalFundingExpiresEpoch,
    policies = { task = {
      (aws_iam_role_policy.installed_controller[0].name)          = jsondecode(aws_iam_role_policy.installed_controller[0].policy),
      ("honua-agent-${substr(local.candidate_hex, 0, 24)}-state") = jsondecode(module.accounting.installed_controller_policy_json)
    }, execution = { (aws_iam_role_policy.installed_controller_execution[0].name) = jsondecode(aws_iam_role_policy.installed_controller_execution[0].policy) } },
    activation = false, qualification = false
  }
}
