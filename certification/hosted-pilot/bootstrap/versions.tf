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
    tags = merge({ ManagedBy = "honua-release-hosted-pilot-bootstrap", Owner = var.owner, ResetBoundary = var.name_prefix }, { for key, value in var.bootstrap_creation_identity : { foundation_source_sha = "BootstrapFoundationSha", release_root_sha = "BootstrapReleaseSha", config_path = "BootstrapConfigPath", config_sha256 = "BootstrapConfigSha256", provider_lock_sha256 = "BootstrapLockSha256" }[key] => value }, var.bootstrap_execution_id == null ? {} : { BootstrapExecution = var.bootstrap_execution_id })
  }
}
