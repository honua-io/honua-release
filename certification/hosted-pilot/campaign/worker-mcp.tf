# A source-reviewed context-preparation task writes one immutable agent-only
# payload. Worker task definitions must pin its returned version, never latest.
resource "aws_secretsmanager_secret" "worker_mcp" {
  name                    = "honua-agent-${substr(local.candidate_hex, 0, 24)}-mcp"
  recovery_window_in_days = 30
}
resource "aws_iam_role_policy" "controller_mcp_configuration" {
  role = aws_iam_role.controller.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = "secretsmanager:PutSecretValue", Resource = aws_secretsmanager_secret.worker_mcp.arn }
  ] })
}
output "worker_mcp_secret_arn" {
  description = "Existing private agent-only MCP destination. No version exists until actual native context setup; null/unknown versions cannot authorize a model worker."
  value       = aws_secretsmanager_secret.worker_mcp.arn
}
