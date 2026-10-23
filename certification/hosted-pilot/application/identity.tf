locals {
  identity_clients = {
    console_edge = {
      callback = var.identity_redirects.edge_callback_url
      logout   = var.identity_redirects.edge_logout_url
    }
    server_bff = {
      callback = var.identity_redirects.server_bff_callback_url
      logout   = var.identity_redirects.server_bff_logout_url
    }
  }
  identity_groups = {
    authors   = "${var.name_prefix}-authors"
    approvers = "${var.name_prefix}-approvers"
  }
}

# Human enrollment is empty by default. Explicit sealed nominations use the
# same reviewed saved-plan workflow; they never establish runtime qualification.
resource "aws_cognito_user_pool" "pilot" {
  name                = "${var.name_prefix}-operators"
  deletion_protection = var.deletion_protection ? "ACTIVE" : "INACTIVE"
  user_pool_tier      = "LITE"
  username_attributes = ["email"]
  username_configuration {
    case_sensitive = false
  }
  auto_verified_attributes = ["email"]
  admin_create_user_config {
    allow_admin_create_user_only = true
  }
  account_recovery_setting {
    recovery_mechanism {
      name     = "admin_only"
      priority = 1
    }
  }
  password_policy {
    minimum_length                   = 16
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = true
    temporary_password_validity_days = 1
  }
  mfa_configuration = "OPTIONAL"
  software_token_mfa_configuration {
    enabled = true
  }
}

resource "aws_cognito_user_pool_domain" "pilot" {
  domain                = var.name_prefix
  user_pool_id          = aws_cognito_user_pool.pilot.id
  managed_login_version = 1 # Classic hosted UI; no paid branding dependency.
}

resource "aws_cognito_user_group" "operators" {
  for_each     = local.identity_groups
  name         = each.value
  user_pool_id = aws_cognito_user_pool.pilot.id
  description  = each.key == "authors" ? "Nonadmin pilot authors; resource grants remain required" : "Separate human pilot approvers; Server admin-role binding remains required"
  # No IAM role assumption, automatic memberships or precedence-based promotion.
}

resource "aws_cognito_user_pool_client" "operators" {
  for_each     = local.identity_clients
  name         = "${var.name_prefix}-${replace(each.key, "_", "-")}-confidential"
  user_pool_id = aws_cognito_user_pool.pilot.id

  generate_secret                      = true
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "profile", "email"]
  supported_identity_providers         = ["COGNITO"]
  callback_urls                        = [each.value.callback]
  default_redirect_uri                 = each.value.callback
  logout_urls                          = [each.value.logout]
  explicit_auth_flows                  = ["ALLOW_REFRESH_TOKEN_AUTH"]
  enable_token_revocation              = true
  prevent_user_existence_errors        = "ENABLED"
  access_token_validity                = 15
  id_token_validity                    = 15
  refresh_token_validity               = 8
  auth_session_validity                = 3
  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "hours"
  }
  read_attributes  = ["email", "email_verified", "name"]
  write_attributes = ["email", "name"]
}

# Only individual secret references are exported. Terraform state necessarily
# holds generated client secrets; no existing secret is read by this root.
resource "aws_secretsmanager_secret" "oidc_client" {
  for_each                = local.identity_clients
  name                    = "${var.name_prefix}/oidc/${each.key}"
  description             = "Dedicated ${each.key} confidential client secret; bind only its execution principal"
  kms_key_id              = var.identity_secrets_kms_key_arn == "" ? null : var.identity_secrets_kms_key_arn
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "oidc_client" {
  for_each      = local.identity_clients
  secret_id     = aws_secretsmanager_secret.oidc_client[each.key].id
  secret_string = aws_cognito_user_pool_client.operators[each.key].client_secret
}

# OperatorBearerOptions resolves the signing key as UTF-8 (minimum 32 bytes).
# 64 independent alphanumeric characters provide 64 bytes without encoding or
# newline ambiguity; never reuse either Cognito client secret for this purpose.
resource "random_password" "operator_bearer" {
  length  = 64
  special = false
}

resource "aws_secretsmanager_secret" "operator_bearer" {
  name                    = "${var.name_prefix}/operator-bearer/signing-key"
  description             = "Independent Server operator-bearer signing key; Server execution principal only"
  kms_key_id              = var.identity_secrets_kms_key_arn == "" ? null : var.identity_secrets_kms_key_arn
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "operator_bearer" {
  secret_id     = aws_secretsmanager_secret.operator_bearer.id
  secret_string = random_password.operator_bearer.result
}
