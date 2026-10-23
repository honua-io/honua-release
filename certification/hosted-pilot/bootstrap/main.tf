data "aws_caller_identity" "current" {}
locals {
  account = data.aws_caller_identity.current.account_id
  # Bootstrap resources deliberately live outside the task/service namespace.
  foundation_prefix = "honua-release-${trimprefix(var.name_prefix, "honua-")}-foundation"
  repository        = "honua-release"
  state_key         = "honua/hosted-pilot/${var.name_prefix}/terraform.tfstate"
  lanes             = toset(["plan", "apply"])
}
resource "aws_kms_key" "custody" {
  description             = "Hosted pilot encrypted state and private saved plans"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  lifecycle { prevent_destroy = true }
}
resource "aws_s3_bucket" "custody" {
  for_each      = toset(["state", "plans"])
  bucket        = "${local.foundation_prefix}-${each.key}-${local.account}"
  force_destroy = false
  lifecycle { prevent_destroy = true }
}
resource "aws_s3_bucket_public_access_block" "custody" {
  for_each                = aws_s3_bucket.custody
  bucket                  = each.value.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_ownership_controls" "custody" {
  for_each = aws_s3_bucket.custody
  bucket   = each.value.id
  rule { object_ownership = "BucketOwnerEnforced" }
}
resource "aws_s3_bucket_versioning" "custody" {
  for_each = aws_s3_bucket.custody
  bucket   = each.value.id
  versioning_configuration { status = "Enabled" }
}
resource "aws_s3_bucket_server_side_encryption_configuration" "custody" {
  for_each = aws_s3_bucket.custody
  bucket   = each.value.id
  rule {
    # Membership read authority is restricted by the individual object ARN in
    # KMS's S3 encryption context. Bucket-level context must never replace it.
    bucket_key_enabled = false
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.custody.arn
    }
  }
}
resource "aws_s3_bucket_policy" "custody" {
  for_each = aws_s3_bucket.custody
  bucket   = each.value.id
  policy = jsonencode({ Version = "2012-10-17", Statement = concat([
    { Sid = "TlsOnly", Effect = "Deny", Principal = "*", Action = "s3:*", Resource = [each.value.arn, "${each.value.arn}/*"], Condition = { Bool = { "aws:SecureTransport" = "false" } } },
    { Sid = "KmsOnly", Effect = "Deny", Principal = "*", Action = "s3:PutObject", Resource = "${each.value.arn}/*", Condition = { StringNotEqualsIfExists = { "s3:x-amz-server-side-encryption" = "aws:kms" } } }
    ], [for statement in [
      { Sid = "RetainBootstrapRecovery", Effect = "Deny", Principal = "*", Action = ["s3:DeleteObject", "s3:DeleteObjectVersion"], Resource = "${each.value.arn}/honua/hosted-pilot/${var.name_prefix}/foundation-recovery/*" },
      { Sid = "ConditionalBootstrapRecovery", Effect = "Deny", Principal = "*", Action = "s3:PutObject", Resource = "${each.value.arn}/honua/hosted-pilot/${var.name_prefix}/foundation-recovery/*", Condition = { Null = { "s3:if-none-match" = "true" } } }
      ] : statement if each.key == "state"], [for statement in [
      { Sid = "ImmutableArtifacts", Effect = "Deny", Principal = "*", Action = ["s3:DeleteObject", "s3:DeleteObjectVersion"], Resource = ["${each.value.arn}/hosted-pilot/plans/*", "${each.value.arn}/hosted-pilot/reservations/*"] },
      { Sid = "ConditionalArtifacts", Effect = "Deny", Principal = "*", Action = "s3:PutObject", Resource = ["${each.value.arn}/hosted-pilot/plans/*", "${each.value.arn}/hosted-pilot/reservations/*"], Condition = { Null = { "s3:if-none-match" = "true" } } }
  ] : statement if each.key == "plans"]) })
}

resource "aws_iam_role" "executor" {
  for_each             = local.lanes
  name                 = "${local.foundation_prefix}-${each.key}"
  max_session_duration = 3600
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = "sts:AssumeRoleWithWebIdentity", Principal = { Federated = var.oidc_provider_arn },
    Condition = { StringEquals = {
      "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com",
      "token.actions.githubusercontent.com:sub" = "repo:honua-io/honua-release:environment:hosted-pilot-${each.key}"
    } }
  }] })
}
resource "aws_iam_role" "backend" {
  name                 = "${local.foundation_prefix}-backend"
  max_session_duration = 3600
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = "sts:AssumeRole", Principal = { AWS = [for role in aws_iam_role.executor : role.arn] }
  }] })
}
resource "aws_iam_role_policy" "backend" {
  role = aws_iam_role.backend.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["s3:ListBucket", "s3:GetBucketLocation"], Resource = aws_s3_bucket.custody["state"].arn },
    { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject"], Resource = flatten([for key in local.deployment_state_keys : ["${aws_s3_bucket.custody["state"].arn}/${key}", "${aws_s3_bucket.custody["state"].arn}/${key}.tflock"]]) },
    { Effect = "Allow", Action = "s3:DeleteObject", Resource = [for key in local.deployment_state_keys : "${aws_s3_bucket.custody["state"].arn}/${key}.tflock"] },
    { Effect = "Allow", Action = ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey"], Resource = aws_kms_key.custody.arn }
  ] })
}
resource "aws_iam_role_policy" "read" {
  for_each = aws_iam_role.executor
  role     = each.value.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["ec2:Describe*", "ecs:Describe*", "ecs:List*", "elasticloadbalancing:Describe*", "rds:Describe*", "rds:ListTagsForResource", "elasticache:Describe*", "elasticache:ListTagsForResource", "logs:Describe*", "logs:ListTagsForResource", "acm:DescribeCertificate", "acm:ListTagsForCertificate", "route53:GetHostedZone", "route53:GetChange", "route53:ListResourceRecordSets", "cognito-idp:DescribeUserPool", "cognito-idp:DescribeUserPoolClient", "cognito-idp:DescribeUserPoolDomain", "cognito-idp:GetGroup", "cognito-idp:ListTagsForResource", "secretsmanager:DescribeSecret", "secretsmanager:ListSecretVersionIds", "kms:DescribeKey", "kms:GetKeyPolicy", "kms:GetKeyRotationStatus", "kms:ListResourceTags"], Resource = "*" },
    { Effect = "Allow", Action = ["iam:GetRole", "iam:GetRolePolicy", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies", "iam:GetPolicy", "iam:GetPolicyVersion"], Resource = ["arn:aws:iam::${local.account}:role/${var.name_prefix}*", "arn:aws:iam::${local.account}:policy/${var.name_prefix}*", aws_iam_policy.runtime_boundary.arn] },
    { Effect = "Allow", Action = ["s3:ListBucket", "s3:GetBucketLocation", "s3:GetBucketTagging", "s3:GetBucketPolicy", "s3:GetBucketVersioning", "s3:GetBucketPublicAccessBlock", "s3:GetEncryptionConfiguration", "s3:GetBucketAcl", "s3:GetBucketOwnershipControls", "s3:GetLifecycleConfiguration", "s3:GetBucketCors", "s3:GetBucketLogging", "s3:GetReplicationConfiguration", "s3:GetAccelerateConfiguration", "s3:GetBucketObjectLockConfiguration", "s3:GetBucketWebsite", "s3:GetBucketRequestPayment"], Resource = "arn:aws:s3:::${var.name_prefix}-*" }
  ] })
}
resource "aws_iam_role_policy" "custody" {
  for_each = aws_iam_role.executor
  role     = each.value.id
  policy = jsonencode({ Version = "2012-10-17", Statement = concat([
    { Effect = "Allow", Action = "sts:AssumeRole", Resource = aws_iam_role.backend.arn },
    { Effect = "Allow", Action = ["s3:GetBucketVersioning", "s3:GetBucketPublicAccessBlock", "s3:ListBucket"], Resource = aws_s3_bucket.custody["plans"].arn },
    { Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion"], Resource = "${aws_s3_bucket.custody["plans"].arn}/hosted-pilot/*" },
    { Effect = "Allow", Action = "s3:PutObject", Resource = "${aws_s3_bucket.custody["plans"].arn}/hosted-pilot/reservations/*", Condition = { Null = { "s3:if-none-match" = "false" } } },
    { Effect = "Allow", Action = ["kms:Decrypt", "kms:GenerateDataKey"], Resource = aws_kms_key.custody.arn },
    { Effect = "Allow", Action = "secretsmanager:GetSecretValue", Resource = "arn:aws:secretsmanager:${var.region}:${local.account}:secret:${var.name_prefix}*" },
    { Effect = "Deny", Action = ["s3:PutObject", "s3:DeleteObject", "s3:DeleteObjectVersion"], Resource = "${aws_s3_bucket.custody["plans"].arn}/hosted-pilot/budget/*" },
    { Effect = "Deny", Action = ["kms:DisableKey", "kms:ScheduleKeyDeletion", "kms:PutKeyPolicy", "kms:CreateGrant"], Resource = aws_kms_key.custody.arn },
    { Effect = "Deny", Action = ["s3:PutBucket*", "s3:DeleteBucket*"], Resource = [for bucket in aws_s3_bucket.custody : bucket.arn] },
    { Effect = "Deny", Action = ["iam:CreatePolicyVersion", "iam:SetDefaultPolicyVersion", "iam:DeletePolicy", "iam:DeletePolicyVersion"], Resource = "arn:aws:iam::${local.account}:policy/${local.foundation_prefix}*" },
    { Effect = "Deny", Action = ["iam:CreateRole", "iam:PutRolePermissionsBoundary", "iam:PassRole", "iam:PutRolePolicy", "iam:AttachRolePolicy", "iam:DetachRolePolicy", "iam:DeleteRolePolicy", "iam:UpdateAssumeRolePolicy", "iam:DeleteRole", "iam:UpdateRole"], NotResource = local.workload_role_patterns },
    { Effect = "Deny", Action = ["iam:DeleteRolePermissionsBoundary"], Resource = "*" },
    { Effect = "Deny", Action = ["iam:CreateRole", "iam:PutRolePermissionsBoundary"], Resource = "*", Condition = { ArnNotEquals = { "iam:PermissionsBoundary" = local.workload_boundary_arns } } },
    { Effect = "Deny", Action = ["iam:UpdateAssumeRolePolicy", "iam:PutRolePolicy", "iam:AttachRolePolicy", "iam:DetachRolePolicy", "iam:DeleteRolePolicy", "iam:DeleteRole"], Resource = "arn:aws:iam::${local.account}:role/${local.foundation_prefix}*" }
    ], each.key == "plan" ? [
    { Effect = "Allow", Action = "s3:PutObject", Resource = "${aws_s3_bucket.custody["plans"].arn}/hosted-pilot/plans/*", Condition = { Null = { "s3:if-none-match" = "false" } } }
    ] : [], each.key == "apply" ? [
    { Sid      = "WriteOriginalHumanEnrollment", Effect = "Allow", Action = "s3:PutObject",
      Resource = ["${aws_s3_bucket.custody["plans"].arn}/hosted-pilot/plans/*/review.json", "${aws_s3_bucket.custody["plans"].arn}/hosted-pilot/plans/*/human-identities-*.json"],
    Condition = { StringEquals = { "s3:if-none-match" = "*", "s3:x-amz-server-side-encryption" = "aws:kms", "s3:x-amz-server-side-encryption-aws-kms-key-id" = aws_kms_key.custody.arn } } }
  ] : []) })
}
resource "aws_iam_policy" "provision" {
  name   = "${local.foundation_prefix}-provision"
  policy = var.provision_policy_document
}
resource "aws_iam_role_policy_attachment" "provision" {
  role       = aws_iam_role.executor["apply"].name
  policy_arn = aws_iam_policy.provision.arn
}
resource "aws_iam_policy" "runtime_boundary" {
  name = "${local.foundation_prefix}-runtime-boundary"
  policy = jsonencode({ Version = "2012-10-17", Statement = concat([
    { Effect = "Deny", Action = ["iam:*", "organizations:*", "account:*", "bedrock:*"], Resource = "*" },
    { Sid = "ExcludeFoundationCustodyKey", Effect = "Deny", Action = "kms:*", Resource = aws_kms_key.custody.arn },
    { Effect = "Allow", Action = ["ecr:GetAuthorizationToken", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"], Resource = "*" },
    { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = ["arn:aws:logs:${var.region}:${local.account}:log-group:/ecs/${var.name_prefix}*:*", "arn:aws:logs:${var.region}:${local.account}:log-group:/honua/pilot/${var.name_prefix}*:*", "arn:aws:logs:${var.region}:${local.account}:log-group:/aws/ecs/${var.name_prefix}*:*"] },
    { Sid = "DecryptPilotGeneratedSecrets", Effect = "Allow", Action = "kms:Decrypt", Resource = "arn:aws:kms:${var.region}:${local.account}:key/*", Condition = {
      StringEquals = { "aws:ResourceTag/ResetBoundary" = var.name_prefix, "kms:ViaService" = "secretsmanager.${var.region}.amazonaws.com" },
      ArnLike      = { "kms:EncryptionContext:SecretARN" = "arn:aws:secretsmanager:${var.region}:${local.account}:secret:${var.name_prefix}*" }
    } },
    { Sid = "DescribePilotGeneratedKey", Effect = "Allow", Action = "kms:DescribeKey", Resource = "arn:aws:kms:${var.region}:${local.account}:key/*", Condition = { StringEquals = { "aws:ResourceTag/ResetBoundary" = var.name_prefix } } },
    { Effect = "Allow", Action = "secretsmanager:GetSecretValue", Resource = concat(["arn:aws:secretsmanager:${var.region}:${local.account}:secret:${var.name_prefix}*"], var.runtime_secret_arns) },
    { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"], Resource = ["arn:aws:s3:::${var.name_prefix}-*", "arn:aws:s3:::${var.name_prefix}-*/*"] }
    ], length(var.runtime_kms_key_arns) == 0 ? [] : [
    { Effect = "Allow", Action = ["kms:Decrypt", "kms:DescribeKey"], Resource = var.runtime_kms_key_arns }
  ]) })
}
resource "aws_s3_object" "budget" {
  bucket                 = aws_s3_bucket.custody["plans"].id
  key                    = "hosted-pilot/budget/ledger.json"
  content                = jsonencode(var.budget_ledger)
  server_side_encryption = "aws:kms"
  kms_key_id             = aws_kms_key.custody.arn
  depends_on             = [aws_s3_bucket_versioning.custody]
}

# Sensitive binary plans expire through versioned IaC; ledger reservations are
# retained to prevent replay. Workload teardown never removes custody/state.
resource "aws_s3_bucket_lifecycle_configuration" "plans" {
  bucket = aws_s3_bucket.custody["plans"].id
  rule {
    id     = "expire-sensitive-saved-plans"
    status = "Enabled"
    filter { prefix = "hosted-pilot/plans/" }
    expiration { days = 7 }
    noncurrent_version_expiration { noncurrent_days = 7 }
    abort_incomplete_multipart_upload { days_after_initiation = 1 }
  }
}
