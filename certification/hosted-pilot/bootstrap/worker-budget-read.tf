# Saved-plan executors may authenticate retained per-worker liabilities. Runtime
# state resource policies still restrict these roles to reviewed candidate roots.
resource "aws_iam_role_policy" "worker_budget_read" {
  for_each = var.enable_agent_controller ? local.lanes : toset([])
  role     = aws_iam_role.executor[each.key].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = "dynamodb:GetItem", Resource = "arn:aws:dynamodb:${var.region}:${local.account}:table/honua-agent-*-state", Condition = { "ForAllValues:StringLike" = { "dynamodb:LeadingKeys" = ["sha256:*"] } } },
    { Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion"], Resource = "arn:aws:s3:::honua-agent-*/agent-e2e/*" },
    { Effect = "Allow", Action = ["sns:GetTopicAttributes", "sns:ListSubscriptionsByTopic"], Resource = ["arn:aws:sns:${var.region}:${local.account}:honua-agent-*", "arn:aws:sns:${var.region}:${local.account}:${var.name_prefix}*"] }
  ] })
}
