# Reuse the Server module's pinned network recipe at this root so its NAT source
# can be bound to HTTPS ingress without a module dependency cycle.
data "aws_availability_zones" "pilot" {
  count = var.campaign_context == null ? 1 : 0
  state = "available"
}

module "network" {
  count   = var.campaign_context == null ? 1 : 0
  source  = "terraform-aws-modules/vpc/aws"
  version = "5.21.0"

  name                           = "${var.name_prefix}-network"
  cidr                           = "10.0.0.0/16"
  azs                            = slice(data.aws_availability_zones.pilot[0].names, 0, 2)
  public_subnets                 = ["10.0.101.0/24", "10.0.102.0/24"]
  private_subnets                = ["10.0.1.0/24", "10.0.2.0/24"]
  enable_nat_gateway             = true
  single_nat_gateway             = true
  enable_dns_support             = true
  enable_dns_hostnames           = true
  manage_default_security_group  = true
  default_security_group_ingress = []
  default_security_group_egress  = []
}

resource "aws_route53_record" "server" {
  zone_id = var.route53_zone_id
  name    = var.server_domain
  type    = "CNAME"
  ttl     = 60
  records = [module.server.alb_dns_name]
}

resource "aws_route53_record" "console" {
  zone_id = var.route53_zone_id
  name    = trimprefix(var.identity_redirects.console_origin, "https://")
  type    = "CNAME"
  ttl     = 60
  records = [module.server.alb_dns_name]
}

# Both tasks stay private. Console reaches Server over certificate-validated
# HTTPS through the one dedicated NAT; only that /32 joins operator ingress.
# No unbounded CIDR or bypass of Server's bearer/RBAC boundary is installed.
