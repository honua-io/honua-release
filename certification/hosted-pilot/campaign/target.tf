data "aws_availability_zones" "available" { state = "available" }
data "aws_caller_identity" "current" {}
locals {
  candidate_hex       = trimprefix(var.candidate_digest, "sha256:")
  database            = "honua_agent_${substr(local.candidate_hex, 0, 24)}"
  app_role            = "honua_agent_app"
  fixture_roles       = { cleanup = "honua_agent_cleanup", observer = "honua_agent_observer" }
  origin              = "https://${var.target_domain}"
  native_agent_key_id = var.issued_native_keys == null ? var.issued_agent_key_id : var.issued_native_keys.agent.apiKeyId
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
