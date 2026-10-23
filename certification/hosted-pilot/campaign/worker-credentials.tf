# The worker module independently enforces exact attempt/account/region secret
# ownership. These destinations complement the retained candidate staging secret.
resource "aws_secretsmanager_secret" "worker_attempt_mcp" {
  for_each                = var.worker_attempt_ids
  name                    = "honua-agent-${each.value}-mcp"
  recovery_window_in_days = 30
}
resource "aws_iam_role_policy" "controller_worker_mcp" {
  count = length(var.worker_attempt_ids) == 0 ? 0 : 1
  role  = aws_iam_role.controller.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["secretsmanager:PutSecretValue", "secretsmanager:GetSecretValue"], Resource = [for secret in aws_secretsmanager_secret.worker_attempt_mcp : secret.arn] }
  ] })
}
output "worker_attempt_mcp_destinations" {
  description = "Actual Terraform-owned per-attempt destinations for private context preparation. No version is invented; use the observed native preparation receipt."
  value       = { for attempt, secret in aws_secretsmanager_secret.worker_attempt_mcp : attempt => secret.arn }
}
