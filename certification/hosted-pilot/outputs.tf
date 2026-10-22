output "preparation" {
  description = "Resource references only; not installed provenance or a passing pilot receipt."
  value = {
    status                  = "private-preparation-identity-unbound"
    claimed_candidate       = var.candidate
    server_origin           = local.server_origin
    server_alb              = module.server.alb_dns_name
    console_service         = aws_ecs_service.console.name
    console_task_definition = aws_ecs_task_definition.console.arn
    console_log_group       = aws_cloudwatch_log_group.console.name
    artifacts_bucket        = aws_s3_bucket.artifacts.id
    reset_prefix            = var.name_prefix
    expires_at              = var.expires_at
    public_console_origin   = null
    public_audience_origin  = null
  }
}
