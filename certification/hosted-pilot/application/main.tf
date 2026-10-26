locals {
  server_origin = "https://${var.server_domain}"
  server_generic_oidc = {
    authority                 = "https://${aws_cognito_user_pool.pilot.endpoint}"
    client_id                 = aws_cognito_user_pool_client.operators["server_bff"].id
    client_secret_arn         = aws_secretsmanager_secret.oidc_client["server_bff"].arn
    client_secret_kms_key_arn = var.identity_secrets_kms_key_arn
    scopes                    = ["openid", "profile", "email"]
    admin_auth_callback_uri   = var.identity_redirects.server_bff_callback_url
  }
  server_operator_bearer = {
    signing_key_secret_arn  = aws_secretsmanager_secret.operator_bearer.arn
    signing_key_kms_key_arn = var.identity_secrets_kms_key_arn
    max_lifetime_minutes    = 15
  }
  server_identity_environment = {
    Identity__RequiredManagedIssuers__0              = local.server_generic_oidc.authority
    Oidc__ClaimsMapping__AdditionalRoleClaimTypes__0 = "cognito:groups"
    Oidc__AdminRoles__0                              = local.identity_groups.approvers
    Oidc__AdminRoles__1                              = local.identity_groups.approvers
  }
}

# New-install bootstrap secrets are generated independently. Terraform state is
# sensitive; an encrypted isolated backend is mandatory before any cloud apply.
resource "random_password" "bootstrap_admin" {
  length  = 48
  special = false
}

module "server" {
  source = "git::https://github.com/honua-io/honua-iac.git//infrastructure/terraform/modules/aws-ecs?ref=9b5669ef6975289dccc62a5dbb1903cd4b630c3d"

  permissions_boundary_arn    = var.permissions_boundary_arn
  existing_vpc_id             = local.application_network.vpc_id
  existing_vpc_cidr           = local.application_network.vpc_cidr
  existing_public_subnet_ids  = local.application_network.public_subnet_ids
  existing_private_subnet_ids = local.application_network.private_subnet_ids

  name_prefix                                       = local.application_prefix
  environment                                       = var.campaign_context == null ? "pilot" : "agent-campaign"
  db_name                                           = local.application_database
  db_username                                       = local.application_db_role
  db_additional_ingress_security_group_ids          = var.campaign_context == null ? [] : [var.campaign_context.controller_security_group_id]
  db_additional_ingress_cidrs                       = []
  image                                             = var.candidate.server_image
  admin_password                                    = random_password.bootstrap_admin.result
  connection_encryption_master_key                  = null
  desired_count                                     = 1
  max_capacity                                      = 1
  alb_deletion_protection                           = var.deletion_protection
  rds_deletion_protection                           = var.deletion_protection
  deployment_mode                                   = "SingleInstance"
  redis_enabled                                     = true
  operation_key_ring_certificate_secret_arn         = var.operation_certificate_secret_arn
  operation_key_ring_certificate_secret_kms_key_arn = var.operation_certificate_kms_key_arn
  audit_chain_key_secret_arn                        = var.audit_chain_secret_arn
  audit_chain_key_secret_kms_key_arn                = var.audit_chain_kms_key_arn

  domain_name                 = var.server_domain
  alb_certificate_arn         = aws_acm_certificate_validation.pilot.certificate_arn
  domain_alias_record_enabled = false
  public_base_url             = local.server_origin
  generic_oidc                = local.server_generic_oidc
  operator_bearer             = local.server_operator_bearer
  scim_provisioning = {
    oidc_issuer                     = local.server_generic_oidc.authority
    bearer_token_secret_arn         = aws_secretsmanager_secret.native_scim.arn
    bearer_token_secret_version_id  = aws_secretsmanager_secret_version.native_scim.version_id
    bearer_token_secret_kms_key_arn = var.identity_secrets_kms_key_arn == "" ? null : var.identity_secrets_kms_key_arn
  }
  additional_env            = merge(local.server_identity_environment, local.application_agent_environment)
  allow_https_ingress_cidrs = concat(var.allowed_operator_cidrs, [for ip in local.application_network.nat_public_ips : "${ip}/32"])
  allow_http_ingress_cidrs  = []
  alb_enable_http_redirect  = false
  assign_public_ip          = false
  enable_nat_gateway        = true
  single_nat_gateway        = true
  log_retention_days        = 14
  # Cognito is selected explicitly; actual identities/grants
  # require runtime qualification. Both configured AdminRoles indices replace the Server's
  # two appsettings admin role names. A compatible Server must also replace
  # constructor defaults when binding. Shared policy aliases remain; qualify
  # actual author refusal and approver acceptance before enabling the journey.
  enable_bedrock_ai               = false
  studio_end_user_authorization   = true
  operations_policy_rules         = []
  file_storage_provider           = "AwsS3"
  file_storage_aws_s3_bucket_name = aws_s3_bucket.artifacts.id
  file_storage_aws_s3_region      = var.region
  file_storage_aws_s3_key_prefix  = "${local.application_prefix}/"

  # ECS needs populated secret versions on its first task launch. Depending on
  # the ARN alone would permit tasks to start before the secret values exist.
  depends_on = [
    aws_secretsmanager_secret_version.oidc_client,
    aws_secretsmanager_secret_version.operator_bearer,
    aws_secretsmanager_secret_version.native_scim,
    terraform_data.campaign_network,
  ]
}

resource "aws_s3_bucket" "artifacts" {
  bucket        = try(var.campaign_context.artifact_bucket_name, null)
  bucket_prefix = var.campaign_context == null ? "${var.name_prefix}-" : null
  force_destroy = var.destroy_artifact_data
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  versioning_configuration {
    status = "Enabled"
  }
}

data "aws_iam_policy_document" "artifact_transport" {
  statement {
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.artifacts.arn, "${aws_s3_bucket.artifacts.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "artifact_transport" {
  bucket = aws_s3_bucket.artifacts.id
  policy = data.aws_iam_policy_document.artifact_transport.json
}
