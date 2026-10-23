# Mock plan only: no cloud identity, reads, provisioning or inference.
mock_provider "aws" {
  mock_data "aws_vpc" { defaults = { cidr_block = "10.87.0.0/16" } }
  mock_data "aws_subnet" { defaults = { vpc_id = "vpc-12345678" } }
  mock_data "aws_security_group" { defaults = { vpc_id = "vpc-12345678" } }
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
  target = data.aws_kms_secrets.humans["human_author"]
  values = { plaintext = { identity = "{\"schema\":\"honua.pilot-human-enrollment/v1\",\"email\":\"author@example.invalid\",\"temporaryPassword\":\"OfflineFixture0000!\",\"emailOwnershipConfirmed\":true}" } }
}
override_data {
  target = data.aws_kms_secrets.humans["human_approver"]
  values = { plaintext = { identity = "{\"schema\":\"honua.pilot-human-enrollment/v1\",\"email\":\"approver@example.invalid\",\"temporaryPassword\":\"OfflineFixture1111!\",\"emailOwnershipConfirmed\":true}" } }
}

override_module {
  target = module.server
  outputs = {
    vpc_id                    = "vpc-12345678"
    private_subnet_ids        = ["subnet-12345678"]
    ecs_cluster_name          = "offline-test"
    alb_dns_name              = "offline.invalid"
    alb_security_group_id     = "sg-12345678"
    https_listener_arn        = "arn:aws:elasticloadbalancing:us-west-2:123456789012:listener/app/offline/1234567890123456/1234567890123456"
    ecs_service_name          = "offline-server"
    db_endpoint               = "offline.rds.invalid"
    db_connection_secret_arn  = "arn:aws:secretsmanager:us-west-2:123456789012:secret:honua-pilot-offline-db"
    admin_password_secret_arn = "arn:aws:secretsmanager:us-west-2:123456789012:secret:honua-pilot-offline-admin"
  }
}

override_module {
  target = module.network[0]
  outputs = {
    vpc_id          = "vpc-12345678"
    vpc_cidr_block  = "10.0.0.0/16"
    private_subnets = ["subnet-12345678"]
    public_subnets  = ["subnet-87654321"]
    nat_public_ips  = ["192.0.2.20"]
  }
}

variables {
  campaign_context = {
    network = {
      vpc_id             = "vpc-12345678", vpc_cidr = "10.87.0.0/16"
      public_subnet_ids  = ["subnet-11111111", "subnet-22222222"]
      private_subnet_ids = ["subnet-33333333", "subnet-44444444"]
      nat_public_ips     = ["192.0.2.20"]
    }
    controller_security_group_id = "sg-12345678"
    artifact_bucket_name         = "honua-pilot-offline-agent-target-files-123456789012"
  }
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

run "campaign_reuses_network_database_and_retained_storage" {
  command = apply
  assert {
    condition = (
      length(module.network) == 0 && length(data.aws_availability_zones.pilot) == 0 &&
      output.runtime_bindings.vpc_id == var.campaign_context.network.vpc_id &&
      output.runtime_bindings.database == "honua_agent_aaaaaaaaaaaaaaaaaaaaaaaa" &&
      output.runtime_bindings.app_role == "honua_agent_app" &&
      output.runtime_bindings.server_prefix == "honua-pilot-offline-agent" &&
      output.runtime_bindings.controller_security_group_id == "sg-12345678" &&
      aws_s3_bucket.artifacts.bucket == var.campaign_context.artifact_bucket_name &&
      !aws_s3_bucket.artifacts.force_destroy &&
      output.runtime_bindings.issuer == output.identity_preparation.issuer &&
      aws_cognito_user_pool.pilot.name == "honua-pilot-offline-operators"
    )
    error_message = "Campaign composition must use one existing network/database/file store and one foundation-scoped Cognito identity boundary."
  }
}
run "agent_runtime_binds_original_identity_and_secret_version" {
  command = apply
  assert {
    condition = (
      output.runtime_bindings.agent_actor_id == "apikey:api-key:${var.console_agent_key_id}" &&
      output.runtime_bindings.agent_secret_version_id == var.console_agent_secret_version_id &&
      one([for secret in jsondecode(aws_ecs_task_definition.console.container_definitions)[0].secrets : secret.valueFrom if secret.name == "HONUA_AGENT_API_KEY"]) == "${var.console_agent_secret_arn}:::${var.console_agent_secret_version_id}" &&
      one([for setting in jsondecode(aws_ecs_task_definition.console.container_definitions)[0].environment : setting.value if setting.name == "HONUA_AGENT_SERVER_BASE_URL"]) == output.runtime_bindings.server_origin &&
      one([for setting in jsondecode(aws_ecs_task_definition.console.container_definitions)[0].environment : setting.value if setting.name == "HONUA_NATIVE_MODEL_ENABLED"]) == "false"
    )
    error_message = "Console must use the exact installed target and issued agent identity/version; identity installation cannot silently enable unbudgeted inference."
  }
}
run "initial_application_requires_no_placeholder_agent" {
  command = apply
  variables {
    console_agent_secret_arn        = null
    console_agent_key_id            = null
    console_agent_secret_version_id = null
  }
  assert {
    condition = (output.runtime_bindings.agent_actor_id == null && output.runtime_bindings.agent_secret_version_id == null &&
    !contains([for secret in jsondecode(aws_ecs_task_definition.console.container_definitions)[0].secrets : secret.name], "HONUA_AGENT_API_KEY"))
    error_message = "First application creation must precede native key issuance without a guessed member or credential."
  }
}
run "refuse_subnet_in_another_vpc" {
  command = plan
  override_data {
    target = data.aws_subnet.campaign["2"]
    values = { vpc_id = "vpc-87654321" }
  }
  expect_failures = [terraform_data.campaign_network]
}
run "refuse_controller_in_another_vpc" {
  command = plan
  override_data {
    target = data.aws_security_group.campaign_controller[0]
    values = { vpc_id = "vpc-87654321" }
  }
  expect_failures = [terraform_data.campaign_network]
}
run "refuse_network_cidr_substitution" {
  command = plan
  override_data {
    target = data.aws_vpc.campaign[0]
    values = { cidr_block = "10.88.0.0/16" }
  }
  expect_failures = [terraform_data.campaign_network]
}
run "refuse_unversioned_agent_secret" {
  command = plan
  variables { console_agent_secret_version_id = null }
  expect_failures = [var.console_agent_secret_arn]
}
run "refuse_missing_agent_identity" {
  command = plan
  variables { console_agent_key_id = null }
  expect_failures = [var.console_agent_secret_arn]
}
run "refuse_agent_secret_from_other_account" {
  command = plan
  variables { console_agent_secret_arn = "arn:aws:secretsmanager:us-west-2:999999999999:secret:honua-pilot-offline-agent/native-agent/agent-ABC123" }
  expect_failures = [var.console_agent_secret_arn]
}
run "refuse_mutable_agent_version" {
  command = plan
  variables { console_agent_secret_version_id = "AWSCURRENT" }
  expect_failures = [var.console_agent_secret_version_id]
}
