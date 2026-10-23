# Native managed membership is established through the Server's SCIM and role
# APIs. This secret is independent of root, OperatorBearer, and Cognito secrets.
resource "random_password" "native_scim" {
  length  = 48
  special = false
}

resource "aws_secretsmanager_secret" "native_scim" {
  name                    = "${local.application_prefix}/identity/native-scim"
  description             = "Private one-shot native membership provisioning credential"
  kms_key_id              = var.identity_secrets_kms_key_arn == "" ? null : var.identity_secrets_kms_key_arn
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "native_scim" {
  secret_id     = aws_secretsmanager_secret.native_scim.id
  secret_string = random_password.native_scim.result
}
