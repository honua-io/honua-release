terraform {
  required_version = ">= 1.10, < 2.0"
  required_providers {
    aws    = { source = "hashicorp/aws", version = ">= 5.0, < 7.0" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
  }
}
provider "aws" {
  region              = var.region
  allowed_account_ids = [var.account_id]
  default_tags {
    tags = { ManagedBy = "honua-release-hosted-pilot", ResetBoundary = var.name_prefix, Purpose = "isolated-agent-campaign", Owner = var.owner, ExpiresAt = var.expires_at, "honua-agent-candidate" = var.candidate_digest }
  }
}
