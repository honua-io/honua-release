locals {
  deployment_variables = {
    HOSTED_PILOT_REGION           = var.region
    HOSTED_PILOT_ACCOUNT_ID       = local.account
    HOSTED_PILOT_STATE_BUCKET     = aws_s3_bucket.custody["state"].id
    HOSTED_PILOT_STATE_KMS_ARN    = aws_kms_key.custody.arn
    HOSTED_PILOT_STATE_KEY        = local.state_key
    HOSTED_PILOT_BACKEND_ROLE_ARN = aws_iam_role.backend.arn
    HOSTED_PILOT_PLAN_BUCKET      = aws_s3_bucket.custody["plans"].id
    HOSTED_PILOT_PLAN_KMS_ARN     = aws_kms_key.custody.arn
    HOSTED_PILOT_PLAN_ROLE_ARN    = aws_iam_role.executor["plan"].arn
    HOSTED_PILOT_APPLY_ROLE_ARN   = aws_iam_role.executor["apply"].arn
  }
}
output "deployment_bindings" {
  description = "Nonsecret workflow bindings, consumed by the checked-in release GitHub binding workflow."
  value       = local.deployment_variables
}
output "permissions_boundary_arn" {
  value = aws_iam_policy.runtime_boundary.arn
}
output "budget_binding" {
  value = { version_id = aws_s3_object.budget.version_id, sha256 = sha256(jsonencode(var.budget_ledger)) }
}

output "bootstrap_backend" {
  value = {
    bucket     = aws_s3_bucket.custody["state"].id
    region     = var.region
    key        = "honua/hosted-pilot/${var.name_prefix}/foundation.tfstate"
    kms_key_id = aws_kms_key.custody.arn
  }
}
