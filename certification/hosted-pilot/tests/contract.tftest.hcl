# Mock plan only: no cloud identity, reads, provisioning or inference.
mock_provider "aws" {
  mock_resource "aws_acm_certificate" {
    defaults = {
      arn = "arn:aws:acm:us-west-2:123456789012:certificate/offline-test"
      domain_validation_options = [
        { domain_name = "pilot.offline.invalid", resource_record_name = "_test.pilot.offline.invalid", resource_record_type = "CNAME", resource_record_value = "_test.acm-validations.aws" },
        { domain_name = "console.pilot.offline.invalid", resource_record_name = "_test.console.pilot.offline.invalid", resource_record_type = "CNAME", resource_record_value = "_test.acm-validations.aws" }
      ]
    }
  }
  mock_resource "aws_lb_target_group" {
    defaults = { arn = "arn:aws:elasticloadbalancing:us-west-2:123456789012:targetgroup/offline/1234567890123456" }
  }
  mock_data "aws_availability_zones" {
    defaults = { names = ["us-west-2a", "us-west-2b"] }
  }
  mock_data "aws_lb_listener" {
    defaults = { default_action = [{ target_group_arn = "arn:aws:elasticloadbalancing:us-west-2:123456789012:targetgroup/offline/1234567890123456", type = "forward" }] }
  }
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
    vpc_id                = "vpc-12345678"
    private_subnet_ids    = ["subnet-12345678"]
    ecs_cluster_name      = "offline-test"
    alb_dns_name          = "offline.invalid"
    alb_security_group_id = "sg-12345678"
    https_listener_arn    = "arn:aws:elasticloadbalancing:us-west-2:123456789012:listener/app/offline/1234567890123456/1234567890123456"
    ecs_service_name      = "offline-server"
  }
}

override_module {
  target = module.network
  outputs = {
    vpc_id          = "vpc-12345678"
    vpc_cidr_block  = "10.0.0.0/16"
    private_subnets = ["subnet-12345678"]
    public_subnets  = ["subnet-87654321"]
    nat_public_ips  = ["192.0.2.20"]
  }
}

variables {
  name_prefix              = "honua-pilot-offline"
  owner                    = "offline-test-fixture"
  permissions_boundary_arn = "arn:aws:iam::123456789012:policy/honua-release-pilot-offline-runtime-boundary"
  expires_at               = "2026-10-30T00:00:00Z"
  candidate = {
    manifest_sha256  = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    server_image     = "registry.invalid/server@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    console_image    = "registry.invalid/console@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    server_revision  = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    console_revision = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    studio_revision  = "cccccccccccccccccccccccccccccccccccccccc"
    sdk_revision     = "dddddddddddddddddddddddddddddddddddddddd"
  }
  edge_proxy_image                 = "quay.io/oauth2-proxy/oauth2-proxy@sha256:8e4bec4f9fe4de10de63070209204075e4407503170aa19893d6e8d99f083d1b"
  route53_zone_id                  = "ZOFFLINE123"
  server_domain                    = "pilot.offline.invalid"
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

run "authenticated_edge_configuration" {
  command = apply
  assert {
    condition     = aws_ecs_service.console.desired_count == 1 && !aws_ecs_service.console.network_configuration[0].assign_public_ip
    error_message = "Console must remain one private task."
  }
  assert {
    condition     = length(aws_security_group.console.ingress) == 1 && one(aws_security_group.console.ingress).from_port == 8080 && one(aws_security_group.console.ingress).security_groups == toset(["sg-12345678"])
    error_message = "Only the ALB may reach the authenticated proxy."
  }
  assert {
    condition     = output.deployment.public_console_origin == var.identity_redirects.console_origin && output.deployment.public_audience_origin == null
    error_message = "Console origin must be explicit; no audience site is fabricated."
  }
  assert {
    condition = alltrue([for setting in jsondecode(aws_ecs_task_definition.console.container_definitions)[0].environment :
      setting.name != "HONUA_NATIVE_MODEL_ENABLED" || setting.value == "false"
    ]) && jsondecode(aws_ecs_task_definition.console.container_definitions)[0].secrets[0] == { name = "HONUA_AGENT_API_KEY", valueFrom = var.console_agent_secret_arn }
    error_message = "Native inference must remain disabled and only the dedicated agent credential used for agent transport."
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

# Apply only to mocked resources so generated references can be compared. The
# Server module remains overridden; its own suite verifies ECS/IAM injection.
run "server_auth_bindings" {
  command = apply
  assert {
    condition = (
      local.server_generic_oidc.admin_auth_callback_uri == "${var.identity_redirects.console_origin}/admin/auth/callback" &&
      local.server_origin == "https://${var.server_domain}" &&
      local.server_origin != var.identity_redirects.console_origin &&
      local.server_generic_oidc.scopes == ["openid", "profile", "email"] &&
      local.server_operator_bearer.max_lifetime_minutes == 15
    )
    error_message = "BFF login must return to Console while API origin, minimal scopes and short operator lifetime remain explicit."
  }
  assert {
    condition = (
      local.server_generic_oidc.authority == output.identity_preparation.issuer &&
      local.server_generic_oidc.client_id == output.identity_preparation.clients.server_bff.client_id &&
      local.server_generic_oidc.client_secret_arn == output.identity_preparation.clients.server_bff.client_secret_arn &&
      local.server_operator_bearer.signing_key_secret_arn == output.identity_preparation.operator_bearer_signing_secret_arn &&
      local.server_generic_oidc.client_secret_arn != output.identity_preparation.clients.console_edge.client_secret_arn &&
      local.server_generic_oidc.client_secret_arn != local.server_operator_bearer.signing_key_secret_arn
    )
    error_message = "Server must use its own Cognito client and independent signing secret, never the edge client secret."
  }
  assert {
    condition = (
      local.server_identity_environment.Oidc__ClaimsMapping__AdditionalRoleClaimTypes__0 == "cognito:groups" &&
      local.server_identity_environment.Oidc__AdminRoles__0 == local.identity_groups.approvers &&
      local.server_identity_environment.Oidc__AdminRoles__1 == local.identity_groups.approvers &&
      local.identity_groups.approvers != local.identity_groups.authors &&
      !contains(values(local.server_identity_environment), "admin") &&
      !contains(values(local.server_identity_environment), "administrator")
    )
    error_message = "Configured role names must use the distinct approver group; actual Server binding and entitled mapping require separate verification."
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

run "edge_header_and_network_refusals" {
  command = apply
  assert {
    condition = (
      jsondecode(local.edge_config).server.bindAddress == "0.0.0.0:8080" &&
      jsondecode(local.edge_config).upstreamConfig.upstreams[0].uri == "http://127.0.0.1:8081" &&
      alltrue([for header in jsondecode(local.edge_config).injectRequestHeaders : !header.preserveRequestValue]) &&
      alltrue([for header in jsondecode(local.edge_config).injectRequestHeaders : length(header.values) == 0 if contains(["Authorization", "X-API-Key", "X-Forwarded-Access-Token"], header.name)]) &&
      { for setting in jsondecode(aws_ecs_task_definition.console.container_definitions)[0].environment : setting.name => setting.value }.Honua__Console__Auth__EdgeForwarded__ServerSessionIssuer == local.server_generic_oidc.authority &&
      { for setting in jsondecode(aws_ecs_task_definition.console.container_definitions)[0].environment : setting.name => setting.value }.ASPNETCORE_URLS == "http://127.0.0.1:8081" &&
      jsondecode(local.edge_config).providers[0].oidcConfig.issuerURL == local.server_generic_oidc.authority &&
      jsondecode(local.edge_config).providers[0].oidcConfig.userIDClaim == "sub" &&
      jsondecode(local.edge_config).providers[0].oidcConfig.groupsClaim == "cognito:groups" &&
      !jsondecode(local.edge_config).providers[0].oidcConfig.insecureSkipNonce &&
      jsondecode(local.edge_config).providers[0].code_challenge_method == "S256" &&
      toset(jsondecode(local.edge_config).providers[0].allowedGroups) == toset(values(local.identity_groups))
    )
    error_message = "Edge must authenticate stable subjects with nonce/PKCE/groups and replace identity headers while stripping inbound bearer/API-key credentials."
  }
  assert {
    condition = (
      one(aws_ecs_service.console.load_balancer).container_name == "edge" && one(aws_ecs_service.console.load_balancer).container_port == 8080 && one(aws_ecs_service.console.load_balancer).target_group_arn == aws_lb_target_group.console.arn &&
      jsondecode(aws_ecs_task_definition.console.container_definitions)[0].portMappings == [] &&
      one(aws_lb_listener_rule.console.condition).host_header[0].values == toset(["console.pilot.offline.invalid"]) &&
      aws_route53_record.console.records == toset(["offline.invalid"]) &&
      aws_route53_record.server.records == toset(["offline.invalid"])
    )
    error_message = "Only the authenticated sidecar can be targeted; Console loopback, exact host routing and DNS remain bound."
  }
}

run "refuse_mutable_edge_image" {
  command = plan
  variables { edge_proxy_image = "registry.invalid/oauth2-proxy:latest" }
  expect_failures = [var.edge_proxy_image]
}

run "authentication_staging_without_agent_credential" {
  command = plan
  variables { console_agent_secret_arn = null }
  assert {
    condition = (
      alltrue([for secret in jsondecode(aws_ecs_task_definition.console.container_definitions)[0].secrets : secret.name != "HONUA_AGENT_API_KEY"]) &&
      alltrue([for setting in jsondecode(aws_ecs_task_definition.console.container_definitions)[0].environment : setting.name != "HONUA_NATIVE_MODEL_ENABLED" || setting.value == "false"])
    )
    error_message = "Authentication-only staging must inject no placeholder agent key or agent-secret grant and keep native inference disabled."
  }
}
