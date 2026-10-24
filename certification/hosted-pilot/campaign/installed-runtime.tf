# Separate controller-only custody for the immutable key issued by native
# bootstrap. Empty storage is preparation, never registration or activation.
variable "issued_runtime_control_key" {
  description = "Actual issued control key identity/version; separate from the three legacy task/setup/observer keys."
  type        = object({ apiKeyId = string, versionId = string })
  default     = null
  validation {
    condition = var.issued_runtime_control_key == null ? true : (
      can(regex("^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$", var.issued_runtime_control_key.apiKeyId)) &&
      var.issued_runtime_control_key.apiKeyId != "00000000-0000-0000-0000-000000000000" &&
      can(regex("^[A-Za-z0-9-]{32,64}$", var.issued_runtime_control_key.versionId))
    )
    error_message = "Use the actual original native-issued control key and immutable secret version."
  }
}

resource "aws_secretsmanager_secret" "runtime_control" {
  count                   = local.native_installed_runtime == null ? 0 : 1
  name                    = "${var.name_prefix}/native-agent/control"
  recovery_window_in_days = 30
  lifecycle {
    precondition {
      condition = var.issued_runtime_control_key == null ? true : (
        var.issued_native_keys != null && !contains([
          try(var.issued_native_keys.agent.apiKeyId, ""),
          try(var.issued_native_keys.setup.apiKeyId, ""),
          try(var.issued_native_keys.observer.apiKeyId, "")
        ], var.issued_runtime_control_key.apiKeyId)
      )
      error_message = "The controller key must be a distinct actual issuance, never a task/setup/observer key."
    }
  }
}

locals {
  runtime_control_injection = local.native_installed_runtime == null || var.issued_runtime_control_key == null ? {} : {
    HONUA_EXTERNAL_EXECUTION_CONTROL_KEY = "${aws_secretsmanager_secret.runtime_control[0].arn}:::${var.issued_runtime_control_key.versionId}"
  }
}

resource "aws_iam_role_policy" "issue_runtime_control" {
  count = local.native_installed_runtime == null ? 0 : 1
  role  = aws_iam_role.native_bootstrap[0].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = "secretsmanager:PutSecretValue", Resource = aws_secretsmanager_secret.runtime_control[0].arn }
  ] })
}

resource "aws_iam_role_policy" "inject_runtime_control" {
  count = local.native_installed_runtime == null || var.issued_runtime_control_key == null ? 0 : 1
  role  = aws_iam_role.execution["controller"].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = "secretsmanager:GetSecretValue", Resource = aws_secretsmanager_secret.runtime_control[0].arn,
    Condition = { StringEquals = { "secretsmanager:VersionId" = var.issued_runtime_control_key.versionId } } },
    { Effect = "Deny", Action = "secretsmanager:GetSecretValue", Resource = aws_secretsmanager_secret.runtime_control[0].arn,
    Condition = { StringNotEquals = { "secretsmanager:VersionId" = var.issued_runtime_control_key.versionId } } }
  ] })
}

# DescribeTaskDefinition does not support resource-level IAM. Both consumers
# independently require and compare exact original task revision identities.
resource "aws_iam_role_policy" "inspect_installed_bootstrap" {
  count = local.native_installed_runtime == null || local.native_memberships != null ? 0 : 1
  role  = aws_iam_role.native_bootstrap[0].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = "ecs:DescribeTaskDefinition", Resource = "*",
    Condition = { StringEquals = { "aws:RequestedRegion" = var.region } } }
  ] })
}

output "installed_runtime_credential_preparation" {
  description = "Nonsecret pending installation metadata. Actual native registration and activation remain separate."
  value = local.native_installed_runtime == null ? null : {
    runtimeId        = local.native_installed_runtime.runtimeId
    controlSecretArn = aws_secretsmanager_secret.runtime_control[0].arn
    controlKey       = var.issued_runtime_control_key
    registrarRoot    = local.native_root_version
    activation       = false
    qualification    = false
  }
  precondition {
    condition     = var.issued_runtime_control_key == null || local.native_installed_runtime != null
    error_message = "An issued control key cannot be silently ignored without its original installed runtime."
  }
}
