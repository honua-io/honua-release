locals {
  server_origin = "https://${var.server_domain}"
}

# New-install bootstrap secrets are generated independently. Terraform state is
# sensitive; an encrypted isolated backend is mandatory before any cloud apply.
resource "random_password" "bootstrap_admin" {
  length  = 48
  special = false
}

module "server" {
  source = "git::https://github.com/honua-io/honua-iac.git//infrastructure/terraform/modules/aws-ecs?ref=e2ddbdf02907c3c7a2c142d92a709817975ce3f5"

  name_prefix                                       = var.name_prefix
  environment                                       = "pilot"
  image                                             = var.candidate.server_image
  admin_password                                    = random_password.bootstrap_admin.result
  connection_encryption_master_key                  = null
  desired_count                                     = 1
  max_capacity                                      = 1
  deployment_mode                                   = "SingleInstance"
  redis_enabled                                     = true
  operation_key_ring_certificate_secret_arn         = var.operation_certificate_secret_arn
  operation_key_ring_certificate_secret_kms_key_arn = var.operation_certificate_kms_key_arn
  audit_chain_key_secret_arn                        = var.audit_chain_secret_arn
  audit_chain_key_secret_kms_key_arn                = var.audit_chain_kms_key_arn

  domain_name                 = var.server_domain
  alb_certificate_arn         = var.certificate_arn
  domain_alias_record_enabled = false
  public_base_url             = local.server_origin
  allow_https_ingress_cidrs   = var.allowed_operator_cidrs
  allow_http_ingress_cidrs    = []
  alb_enable_http_redirect    = false
  assign_public_ip            = false
  enable_nat_gateway          = true
  single_nat_gateway          = true
  log_retention_days          = 14
  # No model provider, human issuer or publication grants guessed here.
  enable_bedrock_ai               = false
  studio_end_user_authorization   = true
  operations_policy_rules         = []
  file_storage_provider           = "AwsS3"
  file_storage_aws_s3_bucket_name = aws_s3_bucket.artifacts.id
  file_storage_aws_s3_region      = var.region
  file_storage_aws_s3_key_prefix  = "${var.name_prefix}/"
}

resource "aws_s3_bucket" "artifacts" {
  bucket_prefix = "${var.name_prefix}-"
  force_destroy = false
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
