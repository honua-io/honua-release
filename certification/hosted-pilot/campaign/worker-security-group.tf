# Kept in its own file so the Trivy AWS-0104 exception for this group's HTTPS
# egress (.trivyignore.yaml) covers exactly this one rule.
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
