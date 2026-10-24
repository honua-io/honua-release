output "deployment" {
  description = "Deployment references only; not installed provenance or a passing human qualification receipt."
  value = {
    status                  = "configured-cognito-edge-awaiting-readiness"
    claimed_candidate       = var.candidate
    server_origin           = local.server_origin
    server_alb              = module.server.alb_dns_name
    edge_proxy_image        = var.edge_proxy_image
    console_service         = aws_ecs_service.console.name
    console_task_definition = aws_ecs_task_definition.console.arn
    console_log_group       = aws_cloudwatch_log_group.console.name
    artifacts_bucket        = aws_s3_bucket.artifacts.id
    reset_prefix            = var.name_prefix
    expires_at              = var.expires_at
    public_console_origin   = var.identity_redirects.console_origin
    ecs_cluster             = module.server.ecs_cluster_name
    server_service          = module.server.ecs_service_name
    server_task_definition  = module.server.ecs_task_definition_arn
    public_audience_origin  = null
  }
}

output "identity_preparation" {
  description = "Cognito and Server binding references only; no secret values, users or live human qualification."
  value = {
    status                             = "cognito-configured-awaiting-human-qualification"
    pool_id                            = aws_cognito_user_pool.pilot.id
    issuer                             = "https://${aws_cognito_user_pool.pilot.endpoint}"
    hosted_login_url                   = "https://${aws_cognito_user_pool_domain.pilot.domain}.auth.${var.region}.amazoncognito.com"
    groups                             = local.identity_groups
    redirects                          = var.identity_redirects
    operator_bearer_signing_secret_arn = aws_secretsmanager_secret.operator_bearer.arn
    clients = { for name, client in aws_cognito_user_pool_client.operators : name => {
      client_id         = client.id
      client_secret_arn = aws_secretsmanager_secret.oidc_client[name].arn
    } }
  }
}
