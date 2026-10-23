variable "application" {
  description = "Reviewed Console/Server candidate and real human ingress for this campaign's single authenticated application."
  type = object({
    candidate = object({
      manifest_sha256 = string, server_image = string, console_image = string,
      server_revision = string, console_revision = string, studio_revision = string, sdk_revision = string
    })
    permissions_boundary_arn = string
    allowed_operator_cidrs   = list(string)
    identity_redirects = object({
      console_origin          = string, edge_callback_url = string, edge_logout_url = string,
      server_bff_callback_url = string, server_bff_logout_url = string
    })
    identity_secrets_kms_key_arn = optional(string, "")
    secret_kms_key_arns          = optional(list(string), [])
    human_enrollment = optional(object({
      pool_id = string, key_arn = string, users = map(object({ role = string, payload = string }))
    }))
  })
  validation {
    condition = (
      var.application.candidate.server_image == var.server_image &&
      "sha256:${var.application.candidate.manifest_sha256}" == var.candidate_digest &&
      split(":", var.application.permissions_boundary_arn)[4] == var.account_id &&
      var.application.permissions_boundary_arn == "${trimsuffix(var.permissions_boundary_arn, "-agent-boundary")}-runtime-boundary" &&
      var.application.permissions_boundary_arn != var.permissions_boundary_arn
    )
    error_message = "Console and campaign must share the exact candidate/image and the same foundation's separate runtime boundary; the application cannot borrow controller/model authority."
  }
}

module "application" {
  source                   = "../application"
  providers                = { aws = aws.application }
  region                   = var.region
  name_prefix              = trimsuffix(var.name_prefix, "-agent")
  owner                    = var.owner
  expires_at               = var.expires_at
  candidate                = var.application.candidate
  permissions_boundary_arn = var.application.permissions_boundary_arn
  server_domain            = var.target_domain
  route53_zone_id          = var.route53_zone_id
  allowed_operator_cidrs   = var.application.allowed_operator_cidrs
  identity_redirects       = var.application.identity_redirects
  campaign_context = {
    network = {
      vpc_id            = module.network.vpc_id, vpc_cidr = var.network_cidr,
      public_subnet_ids = module.network.public_subnets, private_subnet_ids = module.network.private_subnets,
      nat_public_ips    = module.network.nat_public_ips
    }
    controller_security_group_id = aws_security_group.controller.id
    artifact_bucket_name         = "${var.name_prefix}-target-files-${var.account_id}"
  }
  operation_certificate_secret_arn  = var.operation_certificate_secret_arn
  operation_certificate_kms_key_arn = var.operation_certificate_kms_key_arn == null ? "" : var.operation_certificate_kms_key_arn
  audit_chain_secret_arn            = var.audit_chain_secret_arn
  audit_chain_kms_key_arn           = var.audit_chain_kms_key_arn == null ? "" : var.audit_chain_kms_key_arn
  identity_secrets_kms_key_arn      = var.application.identity_secrets_kms_key_arn
  secret_kms_key_arns               = var.application.secret_kms_key_arns
  human_enrollment                  = var.application.human_enrollment
  deletion_protection               = var.deletion_protection
  console_agent_secret_arn          = var.issued_native_keys == null ? null : aws_secretsmanager_secret.api["agent"].arn
  console_agent_key_id              = var.issued_native_keys == null ? null : var.issued_native_keys.agent.apiKeyId
  console_agent_secret_version_id   = var.issued_native_keys == null ? null : var.issued_native_keys.agent.versionId
}

output "application_deployment" {
  value = {
    deployment           = module.application.deployment
    identity_preparation = module.application.identity_preparation
    runtime_bindings     = module.application.runtime_bindings
    human_enrollment     = module.application.human_enrollment
  }
  precondition {
    condition = var.human_browser == null ? true : (
      var.human_browser.consoleDeployment.region == var.region &&
      var.human_browser.consoleDeployment.clusterArn == local.target_cluster_arn &&
      var.human_browser.consoleDeployment.serviceArn == "arn:aws:ecs:${var.region}:${var.account_id}:service/${module.application.ecs_cluster_name}/${module.application.deployment.console_service}" &&
      var.human_browser.consoleDeployment.taskDefinitionArn == module.application.deployment.console_task_definition &&
      var.human_browser.consoleDeployment.imageUri == var.application.candidate.console_image &&
      var.human_browser.consoleDeployment.edgeImageUri == module.application.deployment.edge_proxy_image &&
      var.human_browser.consoleDeployment.issuer == module.application.identity_preparation.issuer
    )
    error_message = "Human browser custody must bind this actual Console service/task/image/issuer and the same native campaign target."
  }
}

# Retain the existing campaign Server, password and file store. Any actual saved
# plan must still be reviewed for provider-level replacements (including the
# certificate expansion and the DNS transition to the shared authenticated edge).
moved {
  from = module.target
  to   = module.application.module.server
}
moved {
  from = random_password.admin
  to   = module.application.random_password.bootstrap_admin
}
moved {
  from = aws_s3_bucket.target_files
  to   = module.application.aws_s3_bucket.artifacts
}
moved {
  from = aws_s3_bucket_public_access_block.target_files
  to   = module.application.aws_s3_bucket_public_access_block.artifacts
}
moved {
  from = aws_s3_bucket_versioning.target_files
  to   = module.application.aws_s3_bucket_versioning.artifacts
}
moved {
  from = aws_s3_bucket_server_side_encryption_configuration.target_files
  to   = module.application.aws_s3_bucket_server_side_encryption_configuration.artifacts
}
moved {
  from = aws_s3_bucket_policy.target_files
  to   = module.application.aws_s3_bucket_policy.artifact_transport
}
moved {
  from = aws_acm_certificate.target
  to   = module.application.aws_acm_certificate.pilot
}
moved {
  from = aws_route53_record.validation
  to   = module.application.aws_route53_record.certificate
}
moved {
  from = aws_acm_certificate_validation.target
  to   = module.application.aws_acm_certificate_validation.pilot
}
moved {
  from = module.application.module.server.aws_route53_record.service_alias[0]
  to   = module.application.aws_route53_record.server
}
