variable "human_browser" {
  description = "Actual human-captured private Console storage and exact deployed production Console identity. No cookie values or manufactured session flags enter IaC."
  type = object({
    storageState      = object({ secretArn = string, versionId = string })
    consoleDeployment = object({ region = string, clusterArn = string, serviceArn = string, taskDefinitionArn = string, imageUri = string, edgeImageUri = string, issuer = string })
  })
  default = null
  validation {
    condition = var.human_browser == null ? true : (
      can(regex("^arn:aws:secretsmanager:${var.region}:${var.account_id}:secret:${var.name_prefix}/human-session/browser-[A-Za-z0-9]{6}$", var.human_browser.storageState.secretArn)) &&
      can(regex("^[A-Za-z0-9-]{32,64}$", var.human_browser.storageState.versionId)) &&
      can(regex("^arn:aws:ecs:${var.region}:${var.account_id}:service/honua-pilot-[a-z0-9-]+/[a-z0-9-]+$", var.human_browser.consoleDeployment.serviceArn)) &&
      can(regex("^arn:aws:ecs:${var.region}:${var.account_id}:task-definition/honua-pilot-[a-z0-9-]+-console:[1-9][0-9]*$", var.human_browser.consoleDeployment.taskDefinitionArn))
    )
    error_message = "Use actual immutable browser storage in this pilot namespace and exact reviewed Console service/task identities."
  }
}
resource "aws_iam_role_policy" "browser_execution" {
  count = var.human_browser == null ? 0 : 1
  role  = aws_iam_role.execution["controller"].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = "secretsmanager:GetSecretValue", Resource = var.human_browser.storageState.secretArn,
    Condition = { StringEquals = { "secretsmanager:VersionId" = var.human_browser.storageState.versionId } } }
  ] })
}
resource "aws_iam_role_policy" "browser_observation" {
  count = var.human_browser == null ? 0 : 1
  role  = aws_iam_role.controller.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = "ecs:DescribeServices", Resource = var.human_browser.consoleDeployment.serviceArn },
    { Effect = "Allow", Action = "ecs:DescribeTaskDefinition", Resource = "*", Condition = { StringEquals = { "aws:RequestedRegion" = var.region } } }
  ] })
}
