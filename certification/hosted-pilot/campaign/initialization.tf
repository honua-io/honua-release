# This role can establish the retained candidate generation only through the
# checked initializer. It cannot launch tasks, invoke a model or access secrets.
resource "aws_iam_role" "initializer" {
  name                 = "${var.name_prefix}-accounting-initializer"
  permissions_boundary = var.permissions_boundary_arn
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = "sts:AssumeRoleWithWebIdentity", Principal = { Federated = var.oidc_provider_arn },
    Condition = { StringEquals = {
      "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com",
      "token.actions.githubusercontent.com:sub" = "repo:honua-io/honua-release:environment:hosted-pilot-accounting-initialize"
    } }
  }] })
}
resource "aws_iam_role_policy" "initializer" {
  count = var.dispatch_budget == null ? 1 : 0
  role  = aws_iam_role.initializer.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["s3:GetBucketVersioning", "s3:ListBucketVersions"], Resource = local.state_bucket_arn },
    { Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject"], Resource = "${local.state_bucket_arn}/agent-e2e/${local.candidate_hex}/*" },
    { Effect = "Allow", Action = "dynamodb:DescribeTable", Resource = local.state_table_arn },
    { Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:UpdateItem"], Resource = local.state_table_arn,
    Condition = { "ForAllValues:StringEquals" = { "dynamodb:LeadingKeys" = [var.candidate_digest] } } }
  ] })
}
output "accounting_initializer" {
  value = { roleArn = aws_iam_role.initializer.arn, state = module.accounting.state_config, candidateKey = var.candidate_digest,
  initializationEnabled = var.dispatch_budget == null, protectedEnvironment = "hosted-pilot-accounting-initialize" }
}
