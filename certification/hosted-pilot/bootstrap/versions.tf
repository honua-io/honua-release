terraform {
  required_version = ">= 1.10, < 2.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0, < 7.0"
    }

  }
}
provider "aws" {
  region              = var.region
  allowed_account_ids = [var.account_id]
  default_tags {
    tags = merge({ ManagedBy = "honua-release-hosted-pilot-bootstrap", Owner = var.owner, ResetBoundary = var.name_prefix }, var.bootstrap_execution_id == null ? {} : { BootstrapExecution = var.bootstrap_execution_id })
  }
}
