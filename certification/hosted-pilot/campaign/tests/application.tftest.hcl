mock_provider "aws" {
  mock_data "aws_vpc" { defaults = { cidr_block = "10.87.0.0/16" } }
  mock_data "aws_subnet" { defaults = { vpc_id = "vpc-0123456789abcdef0" } }
  mock_data "aws_security_group" { defaults = { vpc_id = "vpc-0123456789abcdef0" } }
  mock_data "aws_iam_policy_document" { defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" } }
  mock_data "aws_lb_listener" { defaults = { default_action = [{ target_group_arn = "arn:aws:elasticloadbalancing:us-east-1:123456789012:targetgroup/offline/1234567890123456", type = "forward" }] } }
  mock_resource "aws_lb_target_group" { defaults = { arn = "arn:aws:elasticloadbalancing:us-east-1:123456789012:targetgroup/offline/1234567890123456" } }
  mock_resource "aws_cognito_user_pool" { defaults = { id = "us-east-1_Offline123", endpoint = "cognito-idp.us-east-1.amazonaws.com/us-east-1_Offline123" } }
  override_during = plan
  mock_resource "aws_iam_role_policy" { defaults = { name = "offline-reviewed-dispatch-policy" } }
  mock_data "aws_caller_identity" { defaults = { account_id = "123456789012" } }
  mock_data "aws_availability_zones" { defaults = { names = ["us-east-1a", "us-east-1b"] } }
  mock_resource "aws_s3_bucket" { defaults = { id = "honua-pilot-offline-agent-controller-123456789012", arn = "arn:aws:s3:::honua-pilot-offline-agent-controller-123456789012" } }
  mock_resource "aws_iam_role" { defaults = { arn = "arn:aws:iam::123456789012:role/honua-pilot-offline-agent" } }
  mock_resource "aws_ecs_task_definition" { defaults = { arn = "arn:aws:ecs:us-east-1:123456789012:task-definition/honua-pilot-offline-agent-campaign-controller:1" } }
  mock_resource "aws_security_group" { defaults = { id = "sg-0123456789abcdef0" } }
  mock_resource "aws_dynamodb_table" { defaults = { arn = "arn:aws:dynamodb:us-east-1:123456789012:table/honua-pilot-offline-agent-controller-claims" } }
  mock_resource "aws_secretsmanager_secret" { defaults = { arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-pilot-offline-agent-fixture-offline", id = "arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-pilot-offline-agent-fixture-offline" } }
  mock_resource "aws_secretsmanager_secret_version" { defaults = { version_id = "offline-version" } }
  mock_resource "aws_cloudwatch_log_group" { defaults = { arn = "arn:aws:logs:us-east-1:123456789012:log-group:/ecs/honua-pilot-offline-agent" } }
  mock_resource "aws_sfn_state_machine" { defaults = { state_machine_version_arn = "arn:aws:states:us-east-1:123456789012:stateMachine:honua-pilot-offline-agent-campaign-controller:1" } }
  mock_resource "aws_acm_certificate" {
    defaults = { domain_validation_options = [{ domain_name = "console.pilot.demo.honua.io", resource_record_name = "_validation.console.pilot.demo.honua.io", resource_record_type = "CNAME", resource_record_value = "_validation.acm-validations.aws" }, { domain_name = "agent.pilot.demo.honua.io", resource_record_name = "_validation.agent.pilot.demo.honua.io", resource_record_type = "CNAME", resource_record_value = "_validation.acm-validations.aws" }] }
  }
}
mock_provider "aws" {
  alias = "application"
  mock_data "aws_vpc" { defaults = { cidr_block = "10.87.0.0/16" } }
  mock_data "aws_subnet" { defaults = { vpc_id = "vpc-0123456789abcdef0" } }
  mock_data "aws_security_group" { defaults = { vpc_id = "vpc-0123456789abcdef0" } }
  mock_data "aws_iam_policy_document" { defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" } }
  mock_data "aws_lb_listener" { defaults = { default_action = [{ target_group_arn = "arn:aws:elasticloadbalancing:us-east-1:123456789012:targetgroup/offline/1234567890123456", type = "forward" }] } }
  mock_resource "aws_lb_target_group" { defaults = { arn = "arn:aws:elasticloadbalancing:us-east-1:123456789012:targetgroup/offline/1234567890123456" } }
  mock_resource "aws_cognito_user_pool" { defaults = { id = "us-east-1_Offline123", endpoint = "cognito-idp.us-east-1.amazonaws.com/us-east-1_Offline123" } }
  override_during = plan
  mock_resource "aws_iam_role_policy" { defaults = { name = "offline-reviewed-dispatch-policy" } }
  mock_data "aws_caller_identity" { defaults = { account_id = "123456789012" } }
  mock_data "aws_availability_zones" { defaults = { names = ["us-east-1a", "us-east-1b"] } }
  mock_resource "aws_s3_bucket" { defaults = { id = "honua-pilot-offline-agent-controller-123456789012", arn = "arn:aws:s3:::honua-pilot-offline-agent-controller-123456789012" } }
  mock_resource "aws_iam_role" { defaults = { arn = "arn:aws:iam::123456789012:role/honua-pilot-offline-agent" } }
  mock_resource "aws_ecs_task_definition" { defaults = { arn = "arn:aws:ecs:us-east-1:123456789012:task-definition/honua-pilot-offline-agent-campaign-controller:1" } }
  mock_resource "aws_security_group" { defaults = { id = "sg-0123456789abcdef0" } }
  mock_resource "aws_dynamodb_table" { defaults = { arn = "arn:aws:dynamodb:us-east-1:123456789012:table/honua-pilot-offline-agent-controller-claims" } }
  mock_resource "aws_secretsmanager_secret" { defaults = { arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-pilot-offline-agent-fixture-offline", id = "arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-pilot-offline-agent-fixture-offline" } }
  mock_resource "aws_secretsmanager_secret_version" { defaults = { version_id = "offline-version" } }
  mock_resource "aws_cloudwatch_log_group" { defaults = { arn = "arn:aws:logs:us-east-1:123456789012:log-group:/ecs/honua-pilot-offline-agent" } }
  mock_resource "aws_sfn_state_machine" { defaults = { state_machine_version_arn = "arn:aws:states:us-east-1:123456789012:stateMachine:honua-pilot-offline-agent-campaign-controller:1" } }
  mock_resource "aws_acm_certificate" {
    defaults = { domain_validation_options = [{ domain_name = "console.pilot.demo.honua.io", resource_record_name = "_validation.console.pilot.demo.honua.io", resource_record_type = "CNAME", resource_record_value = "_validation.acm-validations.aws" }, { domain_name = "agent.pilot.demo.honua.io", resource_record_name = "_validation.agent.pilot.demo.honua.io", resource_record_type = "CNAME", resource_record_value = "_validation.acm-validations.aws" }] }
  }
}
mock_provider "random" {}
mock_provider "null" {}
override_module {
  target = module.network
  outputs = {
    vpc_id          = "vpc-0123456789abcdef0"
    private_subnets = ["subnet-11111111111111111", "subnet-22222222222222222"]
    public_subnets  = ["subnet-33333333333333333", "subnet-44444444444444444"]
    nat_public_ips  = ["192.0.2.20"]
  }
}
override_module {
  target = module.application.module.server
  outputs = {
    vpc_id                    = "vpc-0123456789abcdef0"
    private_subnet_ids        = ["subnet-11111111111111111", "subnet-22222222222222222"]
    alb_dns_name              = "offline.elb.invalid"
    alb_security_group_id     = "sg-0123456789abcdef0"
    https_listener_arn        = "arn:aws:elasticloadbalancing:us-east-1:123456789012:listener/app/offline/1234567890123456/1234567890123456"
    ecs_cluster_name          = "honua-pilot-offline-agent"
    ecs_service_name          = "honua-pilot-offline-agent"
    db_endpoint               = "isolated.example.us-east-1.rds.amazonaws.com"
    db_connection_secret_arn  = "arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-pilot-offline-agent-master-db-123456"
    admin_password_secret_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-pilot-offline-agent-admin-password-123456"
  }
}
override_module {
  target  = module.accounting
  outputs = { state_config = { region = "us-east-1", bucket = "retained-original-accounting", table = "retained-original-head" } }
}
variables {
  application = {
    candidate = {
      manifest_sha256  = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
      server_image     = "ghcr.io/honua-io/honua-server@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
      console_image    = "ghcr.io/honua-io/honua-console@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
      server_revision  = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
      console_revision = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
      studio_revision  = "cccccccccccccccccccccccccccccccccccccccc"
      sdk_revision     = "dddddddddddddddddddddddddddddddddddddddd"
    }
    permissions_boundary_arn = "arn:aws:iam::123456789012:policy/honua-release-pilot-offline-foundation-runtime-boundary"
    allowed_operator_cidrs   = ["192.0.2.10/32"]
    identity_redirects = {
      console_origin          = "https://console.pilot.demo.honua.io"
      edge_callback_url       = "https://console.pilot.demo.honua.io/oauth2/callback"
      edge_logout_url         = "https://console.pilot.demo.honua.io/auth/login"
      server_bff_callback_url = "https://console.pilot.demo.honua.io/admin/auth/callback"
      server_bff_logout_url   = "https://console.pilot.demo.honua.io/admin"
    }
  }
  region                   = "us-east-1"
  account_id               = "123456789012"
  name_prefix              = "honua-pilot-offline-agent"
  owner                    = "offline-contract"
  expires_at               = "2026-10-30T00:00:00Z"
  candidate_digest         = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  permissions_boundary_arn = "arn:aws:iam::123456789012:policy/honua-release-pilot-offline-foundation-agent-boundary"
  oidc_provider_arn        = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com"
  state_admin_role_arns    = ["arn:aws:iam::123456789012:role/retained-plan", "arn:aws:iam::123456789012:role/retained-apply"]
  target_domain            = "agent.pilot.demo.honua.io"
  route53_zone_id          = "ZOFFLINETEST"
  server_image             = "ghcr.io/honua-io/honua-server@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  controller_image = {
    uri                   = "123456789012.dkr.ecr.us-east-1.amazonaws.com/honua-pilot-offline-trusted-controller@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
    sourceSha             = "dddddddddddddddddddddddddddddddddddddddd"
    treeId                = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
    archiveSha256         = "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
    runtimeContractSha256 = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
  }
  operation_certificate_secret_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:independent-operation-123456"
  audit_chain_secret_arn           = "arn:aws:secretsmanager:us-east-1:123456789012:secret:independent-audit-123456"
  dispatch_budget = {
    state                 = { region = "us-east-1", bucket = "retained-original-accounting", table = "retained-original-head" }
    generationDigest      = "sha256:2222222222222222222222222222222222222222222222222222222222222222"
    reservationId         = "original-controller-operation"
    operationId           = "retained-controller-shared"
    basisDigest           = "sha256:3333333333333333333333333333333333333333333333333333333333333333"
    perDispatchCeilingUsd = "2"
    maximumDispatches     = 4
  }
}
run "one_authenticated_application_is_the_native_campaign_target" {
  command = plan
  assert {
    condition = (
      module.application.runtime_bindings.server_origin == local.origin &&
      module.application.runtime_bindings.database == local.fixture_config.database &&
      module.application.runtime_bindings.app_role == local.app_role &&
      module.application.runtime_bindings.vpc_id == module.network.vpc_id &&
      module.application.runtime_bindings.controller_security_group_id == aws_security_group.controller.id &&
      module.application.runtime_bindings.server_prefix == var.name_prefix &&
      module.application.runtime_bindings.console_origin == var.application.identity_redirects.console_origin &&
      module.application.runtime_bindings.issuer == "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_Offline123" &&
      module.application.runtime_bindings.agent_actor_id == null &&
      module.application.deployment.ecs_cluster == module.application.ecs_cluster_name &&
      module.application.deployment.server_service == module.application.ecs_service_name
    )
    error_message = "Human Console, native preparation and independent SQL observations must refer to the same actual application/database/network; first creation has no guessed agent member."
  }
}
run "refuse_candidate_digest_different_from_application" {
  command = plan
  variables { candidate_digest = "sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee" }
  expect_failures = [var.application]
}
run "refuse_server_image_different_from_application" {
  command = plan
  variables { server_image = "ghcr.io/honua-io/honua-server@sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee" }
  expect_failures = [var.application]
}
run "refuse_legacy_agent_identity_without_original_issuance" {
  command = plan
  variables { issued_agent_key_id = "11111111-1111-1111-1111-111111111111" }
  expect_failures = [var.issued_agent_key_id]
}
