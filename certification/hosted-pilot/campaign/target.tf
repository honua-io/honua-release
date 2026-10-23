data "aws_availability_zones" "available" { state = "available" }
data "aws_caller_identity" "current" {}
locals {
  candidate_hex = trimprefix(var.candidate_digest, "sha256:")
  database      = "honua_agent_${substr(local.candidate_hex, 0, 24)}"
  app_role      = "honua_agent_app"
  fixture_roles = { cleanup = "honua_agent_cleanup", observer = "honua_agent_observer" }
  origin        = "https://${var.target_domain}"
}
module "network" {
  source               = "terraform-aws-modules/vpc/aws"
  version              = "5.21.0"
  name                 = var.name_prefix
  cidr                 = var.network_cidr
  azs                  = slice(data.aws_availability_zones.available.names, 0, 2)
  private_subnets      = [cidrsubnet(var.network_cidr, 8, 1), cidrsubnet(var.network_cidr, 8, 2)]
  public_subnets       = [cidrsubnet(var.network_cidr, 8, 101), cidrsubnet(var.network_cidr, 8, 102)]
  enable_nat_gateway   = true
  single_nat_gateway   = true
  enable_dns_hostnames = true
  enable_dns_support   = true
}
resource "aws_acm_certificate" "target" {
  domain_name       = var.target_domain
  validation_method = "DNS"
  lifecycle { create_before_destroy = true }
}
resource "aws_route53_record" "validation" {
  for_each = toset([var.target_domain])
  zone_id  = var.route53_zone_id
  name     = one([for option in aws_acm_certificate.target.domain_validation_options : option.resource_record_name if option.domain_name == each.key])
  type     = one([for option in aws_acm_certificate.target.domain_validation_options : option.resource_record_type if option.domain_name == each.key])
  records  = [one([for option in aws_acm_certificate.target.domain_validation_options : option.resource_record_value if option.domain_name == each.key])]
  ttl      = 60
}
resource "aws_acm_certificate_validation" "target" {
  certificate_arn         = aws_acm_certificate.target.arn
  validation_record_fqdns = [for record in aws_route53_record.validation : record.fqdn]
}
resource "aws_security_group" "controller" {
  name        = "${var.name_prefix}-controller"
  description = "Private trusted controller/provisioner only; no inbound connections"
  vpc_id      = module.network.vpc_id
  ingress     = []
  egress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = [var.network_cidr]
  }
  egress {
    from_port   = 53
    to_port     = 53
    protocol    = "udp"
    cidr_blocks = [var.network_cidr]
  }
  egress {
    from_port   = 53
    to_port     = 53
    protocol    = "tcp"
    cidr_blocks = [var.network_cidr]
  }
}
resource "aws_security_group" "worker" {
  name        = "${var.name_prefix}-worker"
  description = "Independent worker transport; no SQL egress or ingress"
  vpc_id      = module.network.vpc_id
  ingress     = []
  egress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    from_port   = 53
    to_port     = 53
    protocol    = "udp"
    cidr_blocks = [var.network_cidr]
  }
  egress {
    from_port   = 53
    to_port     = 53
    protocol    = "tcp"
    cidr_blocks = [var.network_cidr]
  }
}
resource "random_password" "admin" {
  length  = 48
  special = false
}
module "target" {
  source                                            = "git::https://github.com/honua-io/honua-iac.git//infrastructure/terraform/modules/aws-ecs?ref=484e9d762e9b9ec0483170f548fa0bb306623bd2"
  permissions_boundary_arn                          = var.permissions_boundary_arn
  existing_vpc_id                                   = module.network.vpc_id
  existing_vpc_cidr                                 = var.network_cidr
  existing_public_subnet_ids                        = module.network.public_subnets
  existing_private_subnet_ids                       = module.network.private_subnets
  name_prefix                                       = var.name_prefix
  environment                                       = "agent-campaign"
  image                                             = var.server_image
  connection_encryption_master_key                  = null
  admin_password                                    = random_password.admin.result
  desired_count                                     = 1
  max_capacity                                      = 1
  deployment_mode                                   = "SingleInstance"
  redis_enabled                                     = true
  operation_key_ring_certificate_secret_arn         = var.operation_certificate_secret_arn
  operation_key_ring_certificate_secret_kms_key_arn = var.operation_certificate_kms_key_arn
  audit_chain_key_secret_arn                        = var.audit_chain_secret_arn
  audit_chain_key_secret_kms_key_arn                = var.audit_chain_kms_key_arn
  domain_name                                       = var.target_domain
  alb_certificate_arn                               = aws_acm_certificate_validation.target.certificate_arn
  domain_alias_record_enabled                       = true
  route53_zone_id                                   = var.route53_zone_id
  public_base_url                                   = local.origin
  allow_https_ingress_cidrs                         = [for ip in module.network.nat_public_ips : "${ip}/32"]
  allow_http_ingress_cidrs                          = []
  alb_enable_http_redirect                          = false
  assign_public_ip                                  = false
  enable_nat_gateway                                = true
  single_nat_gateway                                = true
  db_name                                           = local.database
  db_username                                       = local.app_role
  db_additional_ingress_security_group_ids          = [aws_security_group.controller.id]
  db_additional_ingress_cidrs                       = []
  alb_deletion_protection                           = var.deletion_protection
  rds_deletion_protection                           = var.deletion_protection
  file_storage_provider                             = "AwsS3"
  file_storage_aws_s3_bucket_name                   = aws_s3_bucket.target_files.id
  file_storage_aws_s3_region                        = var.region
  file_storage_aws_s3_key_prefix                    = "${var.name_prefix}/"
  enable_bedrock_ai                                 = false
  studio_end_user_authorization                     = true
  operations_policy_rules                           = []
  additional_env = merge({
    AgentWorkspace__DeploymentId = var.name_prefix
    AgentWorkspace__ReleaseId    = var.candidate_digest
  }, var.issued_agent_key_id == null ? {} : { AgentWorkspace__LocalAgentPrincipalId = "apikey:api-key:${var.issued_agent_key_id}" })
}

resource "aws_s3_bucket" "target_files" {
  bucket        = "${var.name_prefix}-target-files-${var.account_id}"
  force_destroy = false
}
resource "aws_s3_bucket_public_access_block" "target_files" {
  bucket                  = aws_s3_bucket.target_files.id
  block_public_acls       = true
  ignore_public_acls      = true
  block_public_policy     = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_versioning" "target_files" {
  bucket = aws_s3_bucket.target_files.id
  versioning_configuration { status = "Enabled" }
}
resource "aws_s3_bucket_server_side_encryption_configuration" "target_files" {
  bucket = aws_s3_bucket.target_files.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}
resource "aws_s3_bucket_policy" "target_files" {
  bucket = aws_s3_bucket.target_files.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Deny", Principal = "*", Action = "s3:*", Resource = [aws_s3_bucket.target_files.arn, "${aws_s3_bucket.target_files.arn}/*"], Condition = { Bool = { "aws:SecureTransport" = "false" } } }
  ] })
}
