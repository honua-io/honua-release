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

# No real person, key or credential is used by these mocked plans.
override_data {
  target = module.application.data.aws_kms_secrets.humans["human_author"]
  values = { plaintext = { identity = "{\"schema\":\"honua.pilot-human-enrollment/v1\",\"email\":\"author@example.invalid\",\"temporaryPassword\":\"OfflineFixture0000!\",\"emailOwnershipConfirmed\":true}" } }
}
override_data {
  target = module.application.data.aws_kms_secrets.humans["human_approver"]
  values = { plaintext = { identity = "{\"schema\":\"honua.pilot-human-enrollment/v1\",\"email\":\"approver@example.invalid\",\"temporaryPassword\":\"OfflineFixture1111!\",\"emailOwnershipConfirmed\":true}" } }
}

override_module {
  target = module.application.module.server
  outputs = {
    vpc_id                           = "vpc-12345678"
    private_subnet_ids               = ["subnet-12345678"]
    ecs_cluster_name                 = "offline-test"
    alb_dns_name                     = "offline.invalid"
    alb_security_group_id            = "sg-12345678"
    https_listener_arn               = "arn:aws:elasticloadbalancing:us-west-2:123456789012:listener/app/offline/1234567890123456/1234567890123456"
    ecs_service_name                 = "offline-server"
    db_endpoint                      = "offline.rds.invalid"
    db_connection_secret_arn         = "arn:aws:secretsmanager:us-west-2:123456789012:secret:honua-pilot-offline-db"
    admin_password_secret_arn        = "arn:aws:secretsmanager:us-west-2:123456789012:secret:honua-pilot-offline-admin"
    admin_password_secret_version_id = "dddddddddddddddddddddddddddddddd"
    ecs_task_definition_arn          = "arn:aws:ecs:us-west-2:123456789012:task-definition/honua-pilot-offline-agent-server:7"
  }
}

override_module {
  target = module.application.module.network[0]
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
  console_agent_secret_arn         = "arn:aws:secretsmanager:us-west-2:123456789012:secret:honua-pilot-offline-agent/native-agent/agent-ABC123"
  console_agent_key_id             = "11111111-1111-1111-1111-111111111111"
  console_agent_secret_version_id  = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  identity_redirects = {
    console_origin          = "https://console.pilot.offline.invalid"
    edge_callback_url       = "https://console.pilot.offline.invalid/oauth2/callback"
    edge_logout_url         = "https://console.pilot.offline.invalid/auth/login"
    server_bff_callback_url = "https://console.pilot.offline.invalid/admin/auth/callback"
    server_bff_logout_url   = "https://console.pilot.offline.invalid/admin"
  }
}

run "standalone_forwards_the_reviewed_application_contract" {
  command = apply
  assert {
    condition = (
      output.deployment.server_origin == "https://${var.server_domain}" &&
      output.deployment.public_console_origin == var.identity_redirects.console_origin &&
      output.deployment.claimed_candidate == var.candidate &&
      output.runtime_bindings.agent_actor_id == "apikey:api-key:${var.console_agent_key_id}" &&
      output.runtime_bindings.agent_secret_version_id == var.console_agent_secret_version_id &&
      output.identity_preparation.issuer == output.runtime_bindings.issuer &&
      output.runtime_bindings.server_prefix == var.name_prefix &&
      output.runtime_bindings.database == "honua" && output.runtime_bindings.app_role == "honua"
    )
    error_message = "The standalone entry point must preserve candidate, identity, database and exact agent inputs through the shared application."
  }
}
