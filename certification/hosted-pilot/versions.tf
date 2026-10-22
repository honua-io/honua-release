terraform {
  required_version = ">= 1.10, < 2.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0, < 7.0"
    }
    random = {
      source  = "hashicorp/random"
      version = ">= 3.5, < 4.0"
    }
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      ManagedBy     = "honua-release-hosted-pilot"
      Candidate     = var.candidate.manifest_sha256
      Owner         = var.owner
      ExpiresAt     = var.expires_at
      ResetBoundary = var.name_prefix
    }
  }
}
