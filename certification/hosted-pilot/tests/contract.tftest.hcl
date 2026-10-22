# Mock plan only: no cloud identity, reads, provisioning or inference.
mock_provider "aws" {
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/offline-test" }
  }
  mock_resource "aws_cognito_user_pool" {
    defaults = { id = "us-west-2_Offline123", endpoint = "cognito-idp.us-west-2.amazonaws.com/us-west-2_Offline123" }
  }
}
mock_provider "random" {}
mock_provider "null" {}

override_module {
  target = module.server
  outputs = {
    vpc_id             = "vpc-12345678"
    private_subnet_ids = ["subnet-12345678"]
    ecs_cluster_name   = "offline-test"
    alb_dns_name       = "offline.invalid"
  }
}

variables {
  name_prefix = "honua-pilot-offline"
  owner       = "offline-test-fixture"
  expires_at  = "2026-10-30T00:00:00Z"
  candidate = {
    manifest_sha256  = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    server_image     = "registry.invalid/server@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    console_image    = "registry.invalid/console@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    server_revision  = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    console_revision = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    studio_revision  = "cccccccccccccccccccccccccccccccccccccccc"
    sdk_revision     = "dddddddddddddddddddddddddddddddddddddddd"
  }
  server_domain                    = "pilot.offline.invalid"
  certificate_arn                  = "arn:aws:acm:us-west-2:123456789012:certificate/offline-test"
  allowed_operator_cidrs           = ["192.0.2.1/32"]
  operation_certificate_secret_arn = "arn:aws:secretsmanager:us-west-2:123456789012:secret:offline-operations"
  audit_chain_secret_arn           = "arn:aws:secretsmanager:us-west-2:123456789012:secret:offline-audit"
  console_agent_secret_arn         = "arn:aws:secretsmanager:us-west-2:123456789012:secret:offline-agent"
  identity_redirects = {
    console_origin          = "https://console.pilot.offline.invalid"
    edge_callback_url       = "https://console.pilot.offline.invalid/oauth2/callback"
    edge_logout_url         = "https://console.pilot.offline.invalid/auth/login"
    server_bff_callback_url = "https://console.pilot.offline.invalid/admin/auth/callback"
    server_bff_logout_url   = "https://console.pilot.offline.invalid/admin"
  }
}

run "private_preparation" {
  command = plan
  assert {
    condition     = aws_ecs_service.console.desired_count == 1 && !aws_ecs_service.console.network_configuration[0].assign_public_ip
    error_message = "Console must remain one private task."
  }
  assert {
    condition     = length(aws_security_group.console.ingress) == 0
    error_message = "Unbound identity must not expose Console."
  }
  assert {
    condition     = output.preparation.public_console_origin == null && output.preparation.public_audience_origin == null
    error_message = "Preparation cannot advertise public origins."
  }
  assert {
    condition = alltrue([for setting in jsondecode(aws_ecs_task_definition.console.container_definitions)[0].environment :
      setting.name != "HONUA_NATIVE_MODEL_ENABLED" || setting.value == "false"
    ]) && jsondecode(aws_ecs_task_definition.console.container_definitions)[0].secrets == [{ name = "HONUA_AGENT_API_KEY", valueFrom = var.console_agent_secret_arn }]
    error_message = "Native inference must remain disabled and only the dedicated agent secret reference injected."
  }
}

run "isolated_cognito_preparation" {
  command = plan
  assert {
    condition = (
      aws_cognito_user_pool.pilot.name == "${var.name_prefix}-operators" &&
      aws_cognito_user_pool_domain.pilot.domain == var.name_prefix &&
      aws_cognito_user_pool.pilot.admin_create_user_config[0].allow_admin_create_user_only &&
      aws_cognito_user_pool.pilot.deletion_protection == "ACTIVE" &&
      aws_cognito_user_pool.pilot.account_recovery_setting[0].recovery_mechanism == toset([{ name = "admin_only", priority = 1 }])
    )
    error_message = "Pool/domain must be isolated and user provisioning/recovery restricted to administrators."
  }
  assert {
    condition = (
      toset(keys(aws_cognito_user_pool_client.operators)) == toset(["console_edge", "server_bff"]) &&
      aws_cognito_user_pool_client.operators["console_edge"].name != aws_cognito_user_pool_client.operators["server_bff"].name &&
      alltrue([for client in aws_cognito_user_pool_client.operators :
        client.generate_secret && client.allowed_oauth_flows_user_pool_client &&
        client.allowed_oauth_flows == toset(["code"]) &&
        client.allowed_oauth_scopes == toset(["openid", "profile", "email"]) &&
        client.explicit_auth_flows == toset(["ALLOW_REFRESH_TOKEN_AUTH"]) &&
        client.enable_token_revocation && client.prevent_user_existence_errors == "ENABLED" &&
        client.access_token_validity == 15 && client.id_token_validity == 15 && client.refresh_token_validity == 8 &&
        client.token_validity_units[0].access_token == "minutes" && client.token_validity_units[0].id_token == "minutes" &&
        client.token_validity_units[0].refresh_token == "hours"
      ])
    )
    error_message = "Separate confidential clients must retain code-only scope/flow, short-token and revocation boundaries."
  }
  assert {
    condition = (
      aws_cognito_user_pool_client.operators["console_edge"].callback_urls == toset([var.identity_redirects.edge_callback_url]) &&
      aws_cognito_user_pool_client.operators["server_bff"].callback_urls == toset([var.identity_redirects.server_bff_callback_url]) &&
      aws_cognito_user_pool_client.operators["console_edge"].logout_urls == toset([var.identity_redirects.edge_logout_url]) &&
      aws_cognito_user_pool_client.operators["server_bff"].logout_urls == toset([var.identity_redirects.server_bff_logout_url]) &&
      aws_cognito_user_group.operators["authors"].name != aws_cognito_user_group.operators["approvers"].name &&
      alltrue([for secret in aws_secretsmanager_secret.oidc_client : startswith(secret.name, "${var.name_prefix}/oidc/")]) &&
      alltrue([for client in output.identity_preparation.clients : toset(keys(client)) == toset(["client_id", "client_secret_arn"])]) &&
      toset(keys(output.identity_preparation)) == toset(["status", "pool_id", "issuer", "hosted_login_url", "groups", "redirects", "clients", "operator_bearer_signing_secret_arn"]) &&
      random_password.operator_bearer.length == 64 && !random_password.operator_bearer.special &&
      aws_secretsmanager_secret.operator_bearer.name == "${var.name_prefix}/operator-bearer/signing-key"
    )
    error_message = "Redirects, distinct groups and individual secret references must remain bound; client secret values cannot be output."
  }
}

run "refuse_http_identity_origin" {
  command = plan
  variables {
    identity_redirects = {
      console_origin          = "http://console.pilot.offline.invalid"
      edge_callback_url       = "http://console.pilot.offline.invalid/oauth2/callback"
      edge_logout_url         = "http://console.pilot.offline.invalid/auth/login"
      server_bff_callback_url = "http://console.pilot.offline.invalid/admin/auth/callback"
      server_bff_logout_url   = "http://console.pilot.offline.invalid/admin"
    }
  }
  expect_failures = [var.identity_redirects]
}

run "refuse_other_origin_bff_callback" {
  command = plan
  variables {
    identity_redirects = {
      console_origin          = "https://console.pilot.offline.invalid"
      edge_callback_url       = "https://console.pilot.offline.invalid/oauth2/callback"
      edge_logout_url         = "https://console.pilot.offline.invalid/auth/login"
      server_bff_callback_url = "https://pilot.offline.invalid/admin/auth/callback"
      server_bff_logout_url   = "https://console.pilot.offline.invalid/admin"
    }
  }
  expect_failures = [var.identity_redirects]
}

run "refuse_identity_redirect_query_and_logout_fragment" {
  command = plan
  variables {
    identity_redirects = {
      console_origin          = "https://console.pilot.offline.invalid"
      edge_callback_url       = "https://console.pilot.offline.invalid/oauth2/callback?next=attacker"
      edge_logout_url         = "https://console.pilot.offline.invalid/auth/login#fragment"
      server_bff_callback_url = "https://console.pilot.offline.invalid/admin/auth/callback"
      server_bff_logout_url   = "https://console.pilot.offline.invalid/admin"
    }
  }
  expect_failures = [var.identity_redirects]
}

run "refuse_identity_ip_origin" {
  command = plan
  variables {
    identity_redirects = {
      console_origin          = "https://192.0.2.1"
      edge_callback_url       = "https://192.0.2.1/oauth2/callback"
      edge_logout_url         = "https://192.0.2.1/auth/login"
      server_bff_callback_url = "https://192.0.2.1/admin/auth/callback"
      server_bff_logout_url   = "https://192.0.2.1/admin"
    }
  }
  expect_failures = [var.identity_redirects]
}

run "refuse_external_identity_logout" {
  command = plan
  variables {
    identity_redirects = {
      console_origin          = "https://console.pilot.offline.invalid"
      edge_callback_url       = "https://console.pilot.offline.invalid/oauth2/callback"
      edge_logout_url         = "https://elsewhere.offline.invalid/auth/login"
      server_bff_callback_url = "https://console.pilot.offline.invalid/admin/auth/callback"
      server_bff_logout_url   = "https://console.pilot.offline.invalid/admin"
    }
  }
  expect_failures = [var.identity_redirects]
}

run "refuse_public_demo" {
  command = plan
  variables { server_domain = "demo.honua.io" }
  expect_failures = [var.server_domain]
}

run "refuse_mutable_console_image" {
  command = plan
  variables {
    candidate = {
      manifest_sha256  = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
      server_image     = "registry.invalid/server@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
      console_image    = "registry.invalid/console:latest"
      server_revision  = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
      console_revision = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
      studio_revision  = "cccccccccccccccccccccccccccccccccccccccc"
      sdk_revision     = "dddddddddddddddddddddddddddddddddddddddd"
    }
  }
  expect_failures = [var.candidate]
}

run "refuse_world_open_server" {
  command = plan
  variables { allowed_operator_cidrs = ["0.0.0.0/0"] }
  expect_failures = [var.allowed_operator_cidrs]
}

run "refuse_noncanonical_world_open_server" {
  command = plan
  variables { allowed_operator_cidrs = ["1.2.3.4/0"] }
  expect_failures = [var.allowed_operator_cidrs]
}

run "refuse_malformed_operator_cidr" {
  command = plan
  variables { allowed_operator_cidrs = ["not-a-cidr"] }
  expect_failures = [var.allowed_operator_cidrs]
}

run "refuse_ipv6_operator_cidr" {
  command = plan
  variables { allowed_operator_cidrs = ["2001:db8::/64"] }
  expect_failures = [var.allowed_operator_cidrs]
}

run "refuse_literal_secret" {
  command = plan
  variables { console_agent_secret_arn = "not-a-secret-reference" }
  expect_failures = [var.console_agent_secret_arn]
}

run "refuse_shared_security_secrets" {
  command = plan
  variables {
    console_agent_secret_arn = "arn:aws:secretsmanager:us-west-2:123456789012:secret:offline-audit"
  }
  expect_failures = [var.console_agent_secret_arn]
}
