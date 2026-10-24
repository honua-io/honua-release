# IAM role reads cover only declared attempts; task-definition reads require a
# region-scoped wildcard in AWS IAM. Exact task revisions remain runtime pins.
# This grants no launch, secret access or worker identity administration.
resource "aws_iam_role_policy" "worker_custody_observer" {
  count = length(var.worker_attempt_ids) == 0 ? 0 : 1
  role  = aws_iam_role.controller.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["iam:GetRolePolicy", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies"],
    Resource = [for attempt in sort(tolist(var.worker_attempt_ids)) : "arn:aws:iam::${var.account_id}:role/honua-agent-${attempt}-worker"] },
    { Effect = "Allow", Action = "ecs:DescribeTaskDefinition",
    Resource = "*", Condition = { StringEquals = { "aws:RequestedRegion" = var.region } } }
  ] })
}
