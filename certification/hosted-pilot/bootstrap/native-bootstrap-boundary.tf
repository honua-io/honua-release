locals {
  native_membership_principal = "arn:aws:iam::${local.account}:role/${var.name_prefix}-agent-native-bootstrap"
  native_membership_objects = [
    "${aws_s3_bucket.custody["plans"].arn}/hosted-pilot/plans/*/review.json",
    "${aws_s3_bucket.custody["plans"].arn}/hosted-pilot/plans/*/human-identities-*.json"
  ]
}

# The private bootstrap is neither a campaign worker nor an application task.
# Its execution role injects separately pinned credentials; this task cap grants
# no secret reads, model calls, identity administration or foundation-state reads.
# Exact operation identity policies further restrict accounting and object keys.
resource "aws_iam_policy" "native_bootstrap_boundary" {
  count = var.enable_agent_controller ? 1 : 0
  name  = "${local.foundation_prefix}-native-bootstrap-boundary"
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Deny", Action = ["iam:*", "organizations:*", "account:*", "bedrock:*", "secretsmanager:GetSecretValue"], Resource = "*" },
    { Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject", "s3:GetBucketVersioning", "s3:ListBucketVersions"],
    Resource = ["arn:aws:s3:::honua-agent-*", "arn:aws:s3:::honua-agent-*/*", "arn:aws:s3:::${var.name_prefix}*", "arn:aws:s3:::${var.name_prefix}*/*"] },
    { Sid = "ReadOriginalMembershipObjects", Effect = "Allow", Action = "s3:GetObjectVersion", Resource = local.native_membership_objects,
    Condition = { ArnEquals = { "aws:PrincipalArn" = local.native_membership_principal } } },
    { Sid = "DecryptOriginalMembershipObjects", Effect = "Allow", Action = "kms:Decrypt", Resource = aws_kms_key.custody.arn,
      Condition = { ArnEquals = { "aws:PrincipalArn" = local.native_membership_principal },
        StringEquals = { "kms:ViaService" = "s3.${var.region}.amazonaws.com" },
    ArnLike = { "kms:EncryptionContext:aws:s3:arn" = local.native_membership_objects } } },
    { Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:UpdateItem"], Resource = ["arn:aws:dynamodb:${var.region}:${local.account}:table/honua-agent-*", "arn:aws:dynamodb:${var.region}:${local.account}:table/${var.name_prefix}*-controller-*"] },
    { Effect = "Allow", Action = "secretsmanager:PutSecretValue", Resource = "arn:aws:secretsmanager:${var.region}:${local.account}:secret:${var.name_prefix}-agent/native-agent/*" },
    { Effect = "Allow", Action = "ecs:DescribeTasks", Resource = "arn:aws:ecs:${var.region}:${local.account}:task/${var.name_prefix}*/*" },
    { Effect = "Allow", Action = "ecs:DescribeServices", Resource = "arn:aws:ecs:${var.region}:${local.account}:service/${var.name_prefix}*/*" },
    # https://docs.aws.amazon.com/service-authorization/latest/reference/list_ecs.html
    # DescribeTaskDefinition has no resource-level IAM authorization. The
    # source-bound verifier still requests and validates exact task revisions.
    { Effect = "Allow", Action = "ecs:DescribeTaskDefinition", Resource = "*", Condition = { StringEquals = { "aws:RequestedRegion" = var.region } } }
  ] })
}

output "native_bootstrap_permissions_boundary_arn" {
  value = var.enable_agent_controller ? aws_iam_policy.native_bootstrap_boundary[0].arn : null
}
