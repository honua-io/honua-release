# One reviewed installed runtime allocation. Null preserves existing foundation
# and full-corpus controller/worker policies. This creates no task or admission.
variable "installed_controller_boundary" {
  type = object({
    candidate_key         = string
    runtime_id            = string
    funding_expires_epoch = number
    cluster_arn           = string
    control               = object({ secret_arn = string, version_id = string })
    workers = list(object({
      slot_id           = string, state_machine_arn = string, task_role_arn = string,
      command_queue_arn = string, reply_queue_arn = string
    }))
  })
  default = null
  validation {
    condition = var.installed_controller_boundary == null ? true : (
      can(regex("^sha256:[a-f0-9]{64}$", var.installed_controller_boundary.candidate_key)) &&
      can(regex("^[A-Za-z0-9_-]{1,128}$", var.installed_controller_boundary.runtime_id)) &&
      var.installed_controller_boundary.funding_expires_epoch > 0 &&
      floor(var.installed_controller_boundary.funding_expires_epoch) == var.installed_controller_boundary.funding_expires_epoch &&
      can(regex("^arn:aws:ecs:${var.region}:${local.account}:cluster/${var.name_prefix}[A-Za-z0-9_-]*$", var.installed_controller_boundary.cluster_arn)) &&
      can(regex("^arn:aws:secretsmanager:${var.region}:${local.account}:secret:${var.name_prefix}-agent/native-agent/control-[A-Za-z0-9]{6}$", var.installed_controller_boundary.control.secret_arn)) &&
      can(regex("^[A-Za-z0-9-]{32,64}$", var.installed_controller_boundary.control.version_id)) &&
      length(var.installed_controller_boundary.workers) >= 1 && length(var.installed_controller_boundary.workers) <= 4 &&
      length(toset([for worker in var.installed_controller_boundary.workers : worker.slot_id])) == length(var.installed_controller_boundary.workers) &&
      alltrue([for worker in var.installed_controller_boundary.workers :
        can(regex("^[a-z0-9-]{3,40}$", worker.slot_id)) &&
        worker.state_machine_arn == "arn:aws:states:${var.region}:${local.account}:stateMachine:honua-agent-${worker.slot_id}" &&
        worker.task_role_arn == "arn:aws:iam::${local.account}:role/honua-agent-${worker.slot_id}-worker" &&
        worker.command_queue_arn == "arn:aws:sqs:${var.region}:${local.account}:honua-agent-${worker.slot_id}-commands.fifo" &&
        worker.reply_queue_arn == "arn:aws:sqs:${var.region}:${local.account}:honua-agent-${worker.slot_id}-replies.fifo"
      ])
    )
    error_message = "Installed cap needs original candidate/runtime/expiry and exact observed worker/control identities in this cell."
  }
}

locals {
  installed_cap           = var.installed_controller_boundary
  installed_cap_workers   = local.installed_cap == null ? [] : local.installed_cap.workers
  installed_cap_hex       = local.installed_cap == null ? "" : trimprefix(local.installed_cap.candidate_key, "sha256:")
  installed_cap_base      = "honua-agent-${substr(local.installed_cap_hex, 0, 24)}"
  installed_cap_prefix    = "agent-e2e/${local.installed_cap_hex}/"
  installed_cap_bucket    = "arn:aws:s3:::${local.installed_cap_base}-${local.account}"
  installed_cap_state     = "arn:aws:dynamodb:${var.region}:${local.account}:table/${local.installed_cap_base}-state"
  installed_cap_claims    = "arn:aws:dynamodb:${var.region}:${local.account}:table/${local.installed_cap_base}-claims"
  installed_cap_runtime   = local.installed_cap == null ? "" : "${local.installed_cap.candidate_key}/runtime/${local.installed_cap.runtime_id}"
  installed_cap_slot_keys = [for worker in local.installed_cap_workers : "${local.installed_cap_runtime}/slot/${worker.slot_id}"]
  installed_cap_e_key     = "${local.installed_cap_runtime}/execution/execution-????????????????????????????????"
  installed_cap_roles = concat([
    "arn:aws:iam::${local.account}:role/${var.name_prefix}-agent-installed-controller",
    "arn:aws:iam::${local.account}:role/${var.name_prefix}-agent-installed-controller-execution"
  ], [for worker in local.installed_cap_workers : worker.task_role_arn])
  installed_cap_expiry     = local.installed_cap == null ? "1970-01-01T00:00:00Z" : timeadd("1970-01-01T00:00:00Z", "${local.installed_cap.funding_expires_epoch}s")
  installed_cap_supervisor = "${var.name_prefix}-agent-installed-controller"
  # These APIs authorize disjoint ARN resource types. Joining the unchanged
  # finite resources removes repeated JSON structure, not scope restrictions.
  installed_cap_observation_resources = concat([
    aws_ecr_repository.controller.arn,
    "arn:aws:logs:${var.region}:${local.account}:log-group:/ecs/${var.name_prefix}-agent-installed-controller:*",
    local.installed_cap_bucket, "${local.installed_cap_bucket}/${local.installed_cap_prefix}*",
    "arn:aws:ecs:${var.region}:${local.account}:task/${try(split("/", local.installed_cap.cluster_arn)[1], "")}/*",
    "arn:aws:states:${var.region}:${local.account}:stateMachine:${local.installed_cap_supervisor}",
    "arn:aws:states:${var.region}:${local.account}:execution:${local.installed_cap_supervisor}:*"
    ], local.installed_cap_roles,
    [for worker in local.installed_cap_workers : worker.state_machine_arn],
  [for worker in local.installed_cap_workers : "arn:aws:states:${var.region}:${local.account}:execution:honua-agent-${worker.slot_id}:worker-*"])
  installed_cap_policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Deny", Action = ["iam:Create*", "iam:Delete*", "iam:Put*", "iam:Update*", "iam:Attach*", "iam:Detach*", "iam:Set*", "iam:Tag*", "iam:Untag*", "iam:PassRole", "organizations:*", "account:*"], Resource = "*" },
    { Effect = "Deny", Action = "kms:*", Resource = aws_kms_key.custody.arn },
    { Effect = "Allow", Action = ["ecr:GetAuthorizationToken", "ecs:DescribeTaskDefinition", "ec2:DescribeNetworkInterfaces", "ec2:DescribeSecurityGroups", "ec2:DescribeSubnets"], Resource = "*", Condition = { StringEquals = { "aws:RequestedRegion" = var.region } } },
    { Effect = "Allow", Action = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability", "logs:CreateLogStream", "logs:PutLogEvents", "iam:GetRole", "iam:GetRolePolicy", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies", "s3:GetBucketVersioning", "s3:ListBucketVersions", "s3:GetObject", "s3:GetObjectVersion", "states:DescribeStateMachine", "states:DescribeExecution", "states:GetExecutionHistory", "ecs:DescribeTasks"], Resource = local.installed_cap_observation_resources },
    { Effect = "Allow", Action = "s3:PutObject", Resource = "${local.installed_cap_bucket}/${local.installed_cap_prefix}*.json", Condition = { StringEquals = { "s3:if-none-match" = "*", "s3:x-amz-server-side-encryption" = "AES256" } } },
    { Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:ConditionCheckItem"], Resource = local.installed_cap_state, Condition = { "ForAllValues:StringEquals" = { "dynamodb:LeadingKeys" = [try(local.installed_cap.candidate_key, "")] } } },
    { Effect = "Allow", Action = "dynamodb:GetItem", Resource = local.installed_cap_claims, Condition = { "ForAllValues:StringLike" = { "dynamodb:LeadingKeys" = concat(local.installed_cap_slot_keys, [for suffix in ["", "/launched", "/stopped", "/retired"] : "${local.installed_cap_e_key}${suffix}"]) } } },
    { Effect = "Allow", Action = "dynamodb:PutItem", Resource = local.installed_cap_claims, Condition = { "ForAllValues:StringLike" = { "dynamodb:LeadingKeys" = ["${local.installed_cap_e_key}/retired"] }, StringEquals = { "dynamodb:EnclosingOperation" = "TransactWriteItems" } } },
    { Effect = "Allow", Action = "dynamodb:UpdateItem", Resource = local.installed_cap_claims, Condition = { "ForAllValues:StringEquals" = { "dynamodb:LeadingKeys" = local.installed_cap_slot_keys }, StringEquals = { "dynamodb:EnclosingOperation" = "TransactWriteItems" } } },
    { Effect = "Allow", Action = ["sqs:SendMessage", "states:StartExecution", "bedrock:InvokeModel"], Resource = concat([for worker in local.installed_cap_workers : worker.command_queue_arn], [for worker in local.installed_cap_workers : worker.state_machine_arn], var.controller_model_resource_arns), Condition = { DateLessThan = { "aws:CurrentTime" = local.installed_cap_expiry } } },
    { Effect = "Allow", Action = ["sqs:ReceiveMessage", "sqs:DeleteMessage"], Resource = [for worker in local.installed_cap_workers : worker.reply_queue_arn], Condition = { DateLessThan = { "aws:CurrentTime" = local.installed_cap_expiry } } },
    { Effect = "Allow", Action = "bedrock:CountTokens", Resource = [for arn in var.controller_model_resource_arns : arn if startswith(arn, "arn:aws:bedrock:${var.region}::foundation-model/")], Condition = { DateLessThan = { "aws:CurrentTime" = local.installed_cap_expiry } } },
    { Effect = "Allow", Action = "secretsmanager:GetSecretValue", Resource = try(local.installed_cap.control.secret_arn, ""), Condition = { StringEquals = { "secretsmanager:VersionId" = try(local.installed_cap.control.version_id, "") } } }
  ] })
}

resource "aws_iam_policy" "installed_controller_boundary" {
  count  = var.installed_controller_boundary == null ? 0 : 1
  name   = "${local.foundation_prefix}-installed-controller-boundary"
  policy = local.installed_cap_policy
  lifecycle {
    precondition {
      condition = (var.enable_agent_controller && length(var.controller_model_resource_arns) > 0 &&
        length([for arn in var.controller_model_resource_arns : arn if startswith(arn, "arn:aws:bedrock:${var.region}::foundation-model/")]) > 0 &&
      timecmp(local.installed_cap_expiry, var.budget_ledger.expires_at) == 0)
      error_message = "Installed cap needs original retained model/expiry; it cannot renew the existing funding window."
    }
    precondition {
      # Base64 length checks UTF-8 bytes, including any escaped input values.
      condition     = length(base64encode(local.installed_cap_policy)) <= 8192
      error_message = "Exact installed boundary exceeds IAM6144 bytes; reduce reviewed allocation rather than broaden it."
    }
  }
}

output "installed_controller_boundary_arn" {
  value = var.installed_controller_boundary == null ? null : aws_iam_policy.installed_controller_boundary[0].arn
}
