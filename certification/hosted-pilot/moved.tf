# Preserve existing standalone application ownership through the shared module.
moved {
  from = module.server
  to   = module.application.module.server
}
moved {
  from = module.network
  to   = module.application.module.network[0]
}
moved {
  from = aws_acm_certificate.pilot
  to   = module.application.aws_acm_certificate.pilot
}
moved {
  from = aws_route53_record.certificate
  to   = module.application.aws_route53_record.certificate
}
moved {
  from = aws_acm_certificate_validation.pilot
  to   = module.application.aws_acm_certificate_validation.pilot
}
moved {
  from = terraform_data.human_enrollment
  to   = module.application.terraform_data.human_enrollment
}
moved {
  from = aws_cognito_user.humans
  to   = module.application.aws_cognito_user.humans
}
moved {
  from = aws_cognito_user_in_group.humans
  to   = module.application.aws_cognito_user_in_group.humans
}
moved {
  from = aws_security_group.console
  to   = module.application.aws_security_group.console
}
moved {
  from = aws_cloudwatch_log_group.console
  to   = module.application.aws_cloudwatch_log_group.console
}
moved {
  from = aws_iam_role.console_execution
  to   = module.application.aws_iam_role.console_execution
}
moved {
  from = aws_iam_role.console_task
  to   = module.application.aws_iam_role.console_task
}
moved {
  from = aws_iam_role_policy_attachment.console_execution
  to   = module.application.aws_iam_role_policy_attachment.console_execution
}
moved {
  from = aws_iam_role_policy.console_secret
  to   = module.application.aws_iam_role_policy.console_secret
}
moved {
  from = aws_ecs_task_definition.console
  to   = module.application.aws_ecs_task_definition.console
}
moved {
  from = aws_ecs_service.console
  to   = module.application.aws_ecs_service.console
}
moved {
  from = aws_cognito_user_pool.pilot
  to   = module.application.aws_cognito_user_pool.pilot
}
moved {
  from = aws_cognito_user_pool_domain.pilot
  to   = module.application.aws_cognito_user_pool_domain.pilot
}
moved {
  from = aws_cognito_user_group.operators
  to   = module.application.aws_cognito_user_group.operators
}
moved {
  from = aws_cognito_user_pool_client.operators
  to   = module.application.aws_cognito_user_pool_client.operators
}
moved {
  from = aws_secretsmanager_secret.oidc_client
  to   = module.application.aws_secretsmanager_secret.oidc_client
}
moved {
  from = aws_secretsmanager_secret_version.oidc_client
  to   = module.application.aws_secretsmanager_secret_version.oidc_client
}
moved {
  from = random_password.operator_bearer
  to   = module.application.random_password.operator_bearer
}
moved {
  from = aws_secretsmanager_secret.operator_bearer
  to   = module.application.aws_secretsmanager_secret.operator_bearer
}
moved {
  from = aws_secretsmanager_secret_version.operator_bearer
  to   = module.application.aws_secretsmanager_secret_version.operator_bearer
}
moved {
  from = random_password.edge_shared
  to   = module.application.random_password.edge_shared
}
moved {
  from = random_password.edge_cookie
  to   = module.application.random_password.edge_cookie
}
moved {
  from = aws_secretsmanager_secret.edge
  to   = module.application.aws_secretsmanager_secret.edge
}
moved {
  from = aws_secretsmanager_secret_version.edge
  to   = module.application.aws_secretsmanager_secret_version.edge
}
moved {
  from = aws_lb_target_group.console
  to   = module.application.aws_lb_target_group.console
}
moved {
  from = aws_lb_listener_rule.console
  to   = module.application.aws_lb_listener_rule.console
}
moved {
  from = aws_lb_listener_rule.refuse_unknown_host
  to   = module.application.aws_lb_listener_rule.refuse_unknown_host
}
moved {
  from = aws_lb_listener_rule.server
  to   = module.application.aws_lb_listener_rule.server
}
moved {
  from = aws_route53_record.server
  to   = module.application.aws_route53_record.server
}
moved {
  from = aws_route53_record.console
  to   = module.application.aws_route53_record.console
}
moved {
  from = terraform_data.campaign_network
  to   = module.application.terraform_data.campaign_network
}
moved {
  from = random_password.bootstrap_admin
  to   = module.application.random_password.bootstrap_admin
}
moved {
  from = aws_s3_bucket.artifacts
  to   = module.application.aws_s3_bucket.artifacts
}
moved {
  from = aws_s3_bucket_public_access_block.artifacts
  to   = module.application.aws_s3_bucket_public_access_block.artifacts
}
moved {
  from = aws_s3_bucket_server_side_encryption_configuration.artifacts
  to   = module.application.aws_s3_bucket_server_side_encryption_configuration.artifacts
}
moved {
  from = aws_s3_bucket_versioning.artifacts
  to   = module.application.aws_s3_bucket_versioning.artifacts
}
moved {
  from = aws_s3_bucket_policy.artifact_transport
  to   = module.application.aws_s3_bucket_policy.artifact_transport
}
