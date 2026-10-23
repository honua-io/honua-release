mock_provider "aws" {
  override_during = plan
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
    defaults = { domain_validation_options = [{ domain_name = "agent.pilot.demo.honua.io", resource_record_name = "_validation.agent.pilot.demo.honua.io", resource_record_type = "CNAME", resource_record_value = "_validation.acm-validations.aws" }] }
  }
}
mock_provider "random" {}
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
  target = module.target
  outputs = {
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
run "retirement_resume_keeps_original_expiry_and_claim" {
  command = plan
  variables {
    issued_native_keys = {
      setup    = { apiKeyId = "11111111-1111-1111-1111-111111111111", versionId = "1111111111111111111111111111111111111111111111111111111111111111" }
      observer = { apiKeyId = "22222222-2222-2222-2222-222222222222", versionId = "2222222222222222222222222222222222222222222222222222222222222222" }
      agent    = { apiKeyId = "33333333-3333-3333-3333-333333333333", versionId = "3333333333333333333333333333333333333333333333333333333333333333" }
    }
    setup_key_retirement = {
      bootstrapArtifactDigest    = "sha256:4444444444444444444444444444444444444444444444444444444444444444"
      nativeBootstrapOperationId = "original-native-bootstrap"
      operation = {
        operationId    = "original-native-bootstrap", liabilityBasisDigest = "sha256:5555555555555555555555555555555555555555555555555555555555555555"
        reservationIds = { infrastructure = "retained-native-infra", cleanup = "retained-native-cleanup" }
      }
      fixtureReference   = { key = "sha256:6666666666666666666666666666666666666666666666666666666666666666", sha256 = "sha256:7777777777777777777777777777777777777777777777777777777777777777" }
      preparationOutcome = { bucket = "original-controller-bucket", key = "original-outcome-key", versionId = "original-version", sha256 = "sha256:8888888888888888888888888888888888888888888888888888888888888888" }
      mode               = "resume"
      originalClaim      = { table = "original-claims", key = "dispatch:9999999999999999999999999999999999999999999999999999999999999999", bindingDigest = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", taskArn = "original-task", taskDefinitionArn = "original-definition:1" }
    }
  }
  assert {
    condition = (
      local.retirement_config.mode == "resume" &&
      local.retirement_config.originalClaim == var.setup_key_retirement.originalClaim &&
      local.retirement_config.expiresAt == var.expires_at && local.retirement_config.retirementProtocol == 2 &&
      endswith(local.retirement_config.receiptKey, "/resume-9999999999999999999999999999999999999999999999999999999999999999.json")
    )
    error_message = "Read-only recovery must retain the original permanent claim and publish separately without overwriting original revocation custody."
  }
}
