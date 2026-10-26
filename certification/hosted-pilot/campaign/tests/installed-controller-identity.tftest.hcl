mock_provider "aws" {
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
  mock_resource "aws_sfn_state_machine" { defaults = { arn = "arn:aws:states:us-east-1:123456789012:stateMachine:honua-pilot-offline-agent-campaign-controller", state_machine_version_arn = "arn:aws:states:us-east-1:123456789012:stateMachine:honua-pilot-offline-agent-campaign-controller:1" } }
  mock_resource "aws_acm_certificate" {
    defaults = { domain_validation_options = [{ domain_name = "agent.pilot.demo.honua.io", resource_record_name = "_validation.agent.pilot.demo.honua.io", resource_record_type = "CNAME", resource_record_value = "_validation.acm-validations.aws" }] }
  }
}
mock_provider "aws" {
  alias           = "application"
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
  mock_resource "aws_sfn_state_machine" { defaults = { arn = "arn:aws:states:us-east-1:123456789012:stateMachine:honua-pilot-offline-agent-campaign-controller", state_machine_version_arn = "arn:aws:states:us-east-1:123456789012:stateMachine:honua-pilot-offline-agent-campaign-controller:1" } }
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
  target = module.application
  outputs = {
    ecs_cluster_name          = "honua-pilot-offline-agent"
    ecs_service_name          = "honua-pilot-offline-agent"
    db_endpoint               = "isolated.example.us-east-1.rds.amazonaws.com"
    db_connection_secret_arn  = "arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-pilot-offline-agent-master-db-123456"
    admin_password_secret_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-pilot-offline-agent-admin-password-123456"
    native_membership_credentials = {
      root = { secretArn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-pilot-offline-agent-admin-password-123456", versionId = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" }
      scim = { secretArn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-pilot-offline-agent-native-scim-123456", versionId = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" }
    }
  }
}
override_module {
  target  = module.accounting
  outputs = { state_config = { region = "us-east-1", bucket = "retained-original-accounting", table = "retained-original-head" }, claim_table_name = "retained-original-claims", installed_controller_policy_json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
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
run "default_has_no_installed_identity_roles_or_output" {
  command = plan
  assert {
    condition     = length(aws_iam_role.installed_controller) == 0 && length(aws_iam_role.installed_controller_execution) == 0 && output.installed_controller_identity == null
    error_message = "Null preserves the existing campaign and creates no installed role."
  }
}
run "exact_roles_stage_without_legacy_control_injection_or_activation" {
  command = plan
  override_resource {
    override_during = plan
    target          = aws_secretsmanager_secret.runtime_control[0]
    values          = { arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-pilot-offline-agent/native-agent/control-aBcDeF", id = "arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-pilot-offline-agent/native-agent/control-aBcDeF" }
  }
  variables {
    worker_attempt_ids             = ["fixed-slot"]
    installed_worker_slots         = [{ "runtime_id" : "runtime", "slot_id" : "fixed-slot" }]
    issued_native_keys             = { "setup" : { "apiKeyId" : "11111111-1111-1111-1111-111111111111", "versionId" : "1111111111111111111111111111111111111111111111111111111111111111" }, "observer" : { "apiKeyId" : "22222222-2222-2222-2222-222222222222", "versionId" : "2222222222222222222222222222222222222222222222222222222222222222" }, "agent" : { "apiKeyId" : "33333333-3333-3333-3333-333333333333", "versionId" : "3333333333333333333333333333333333333333333333333333333333333333" } }
    issued_runtime_control_key     = { "apiKeyId" : "44444444-4444-4444-4444-444444444444", "versionId" : "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd" }
    controller_model_resource_arns = ["arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.anthropic.offline-v1:0", "arn:aws:bedrock:us-east-1::foundation-model/anthropic.offline-v1:0"]
    controller_model               = { "modelId" : "us.anthropic.offline-v1:0", "returnedModelId" : "offline-v1", "profileArn" : "arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.anthropic.offline-v1:0", "tokenCountingModelArn" : "arn:aws:bedrock:us-east-1::foundation-model/anthropic.offline-v1:0", "profileSha256" : "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" }
    native_bootstrap               = { "bootstrapArtifactDigest" : "sha256:4444444444444444444444444444444444444444444444444444444444444444", "operation" : { "operationId" : "original-native-bootstrap", "liabilityBasisDigest" : "sha256:5555555555555555555555555555555555555555555555555555555555555555", "reservationIds" : { "infrastructure" : "retained-native-infra", "cleanup" : "retained-native-cleanup" } }, "itemId" : "11111111-1111-1111-1111-111111111111", "expiresAt" : "2026-10-29T00:00:00Z", "targetId" : "native-target", "readGrants" : [], "datasetImportScopes" : [], "managedProcessIds" : [], "installedRuntime" : { "runtimeId" : "runtime", "mutationCeilings" : { "createMap" : false, "createDashboard" : false, "compose" : false, "proposePublication" : false, "proposeRestore" : false, "datasetImportScopes" : [], "managedProcessIds" : [] } } }
    installed_controller_identity  = { "runtimeId" : "runtime", "originalFundingExpiresEpoch" : 1793232000, "permissionsBoundaryArn" : "arn:aws:iam::123456789012:policy/honua-release-pilot-offline-foundation-installed-controller-boundary", "control" : { "secretArn" : "arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-pilot-offline-agent/native-agent/control-aBcDeF", "versionId" : "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd" }, "workers" : [{ "slotId" : "fixed-slot", "stateMachineArn" : "arn:aws:states:us-east-1:123456789012:stateMachine:honua-agent-fixed-slot", "clusterArn" : "arn:aws:ecs:us-east-1:123456789012:cluster/honua-pilot-offline-agent", "taskRoleArn" : "arn:aws:iam::123456789012:role/honua-agent-fixed-slot-worker", "commandQueueArn" : "arn:aws:sqs:us-east-1:123456789012:honua-agent-fixed-slot-commands.fifo", "replyQueueArn" : "arn:aws:sqs:us-east-1:123456789012:honua-agent-fixed-slot-replies.fifo" }] }
  }
  assert {
    condition = (
      aws_iam_role.installed_controller[0].permissions_boundary == var.installed_controller_identity.permissionsBoundaryArn &&
      aws_iam_role.installed_controller_execution[0].permissions_boundary == var.installed_controller_identity.permissionsBoundaryArn &&
      !contains(keys(local.controller_secrets), "HONUA_EXTERNAL_EXECUTION_CONTROL_KEY") &&
      length(aws_iam_role_policy.inject_runtime_control) == 0 &&
      output.installed_controller_identity.activation == false && output.installed_controller_identity.qualification == false &&
      output.installed_controller_identity.permissionsBoundaryArn != var.permissions_boundary_arn &&
      one([for row in jsondecode(aws_iam_role_policy.installed_controller_execution[0].policy).Statement : row.Condition.StringEquals["secretsmanager:VersionId"] if contains(flatten([row.Action]), "secretsmanager:GetSecretValue")]) == var.issued_runtime_control_key.versionId &&
      alltrue([for row in jsondecode(aws_iam_role_policy.installed_controller[0].policy).Statement : !contains(flatten([row.Action]), "secretsmanager:GetSecretValue") && !contains(flatten([row.Action]), "ecs:RunTask") && !contains(flatten([row.Action]), "iam:PassRole")])
    )
    error_message = "Identity staging must bind the separate exact role/control version without root keys, task launch or activation."
  }
}

run "legacy_boundary_refused" {
  command = plan
  override_resource {
    override_during = plan
    target          = aws_secretsmanager_secret.runtime_control[0]
    values          = { arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-pilot-offline-agent/native-agent/control-aBcDeF", id = "arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-pilot-offline-agent/native-agent/control-aBcDeF" }
  }
  variables {
    worker_attempt_ids             = ["fixed-slot"]
    installed_worker_slots         = [{ "runtime_id" : "runtime", "slot_id" : "fixed-slot" }]
    issued_native_keys             = { "setup" : { "apiKeyId" : "11111111-1111-1111-1111-111111111111", "versionId" : "1111111111111111111111111111111111111111111111111111111111111111" }, "observer" : { "apiKeyId" : "22222222-2222-2222-2222-222222222222", "versionId" : "2222222222222222222222222222222222222222222222222222222222222222" }, "agent" : { "apiKeyId" : "33333333-3333-3333-3333-333333333333", "versionId" : "3333333333333333333333333333333333333333333333333333333333333333" } }
    issued_runtime_control_key     = { "apiKeyId" : "44444444-4444-4444-4444-444444444444", "versionId" : "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd" }
    controller_model_resource_arns = ["arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.anthropic.offline-v1:0", "arn:aws:bedrock:us-east-1::foundation-model/anthropic.offline-v1:0"]
    controller_model               = { "modelId" : "us.anthropic.offline-v1:0", "returnedModelId" : "offline-v1", "profileArn" : "arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.anthropic.offline-v1:0", "tokenCountingModelArn" : "arn:aws:bedrock:us-east-1::foundation-model/anthropic.offline-v1:0", "profileSha256" : "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" }
    native_bootstrap               = { "bootstrapArtifactDigest" : "sha256:4444444444444444444444444444444444444444444444444444444444444444", "operation" : { "operationId" : "original-native-bootstrap", "liabilityBasisDigest" : "sha256:5555555555555555555555555555555555555555555555555555555555555555", "reservationIds" : { "infrastructure" : "retained-native-infra", "cleanup" : "retained-native-cleanup" } }, "itemId" : "11111111-1111-1111-1111-111111111111", "expiresAt" : "2026-10-29T00:00:00Z", "targetId" : "native-target", "readGrants" : [], "datasetImportScopes" : [], "managedProcessIds" : [], "installedRuntime" : { "runtimeId" : "runtime", "mutationCeilings" : { "createMap" : false, "createDashboard" : false, "compose" : false, "proposePublication" : false, "proposeRestore" : false, "datasetImportScopes" : [], "managedProcessIds" : [] } } }
    installed_controller_identity  = { "runtimeId" : "runtime", "originalFundingExpiresEpoch" : 1793232000, "permissionsBoundaryArn" : "arn:aws:iam::123456789012:policy/honua-release-pilot-offline-foundation-agent-boundary", "control" : { "secretArn" : "arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-pilot-offline-agent/native-agent/control-aBcDeF", "versionId" : "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd" }, "workers" : [{ "slotId" : "fixed-slot", "stateMachineArn" : "arn:aws:states:us-east-1:123456789012:stateMachine:honua-agent-fixed-slot", "clusterArn" : "arn:aws:ecs:us-east-1:123456789012:cluster/honua-pilot-offline-agent", "taskRoleArn" : "arn:aws:iam::123456789012:role/honua-agent-fixed-slot-worker", "commandQueueArn" : "arn:aws:sqs:us-east-1:123456789012:honua-agent-fixed-slot-commands.fifo", "replyQueueArn" : "arn:aws:sqs:us-east-1:123456789012:honua-agent-fixed-slot-replies.fifo" }] }
  }
  expect_failures = [var.installed_controller_identity]
}

run "different_control_version_refused" {
  command = plan
  override_resource {
    override_during = plan
    target          = aws_secretsmanager_secret.runtime_control[0]
    values          = { arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-pilot-offline-agent/native-agent/control-aBcDeF", id = "arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-pilot-offline-agent/native-agent/control-aBcDeF" }
  }
  variables {
    worker_attempt_ids             = ["fixed-slot"]
    installed_worker_slots         = [{ "runtime_id" : "runtime", "slot_id" : "fixed-slot" }]
    issued_native_keys             = { "setup" : { "apiKeyId" : "11111111-1111-1111-1111-111111111111", "versionId" : "1111111111111111111111111111111111111111111111111111111111111111" }, "observer" : { "apiKeyId" : "22222222-2222-2222-2222-222222222222", "versionId" : "2222222222222222222222222222222222222222222222222222222222222222" }, "agent" : { "apiKeyId" : "33333333-3333-3333-3333-333333333333", "versionId" : "3333333333333333333333333333333333333333333333333333333333333333" } }
    issued_runtime_control_key     = { "apiKeyId" : "44444444-4444-4444-4444-444444444444", "versionId" : "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd" }
    controller_model_resource_arns = ["arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.anthropic.offline-v1:0", "arn:aws:bedrock:us-east-1::foundation-model/anthropic.offline-v1:0"]
    controller_model               = { "modelId" : "us.anthropic.offline-v1:0", "returnedModelId" : "offline-v1", "profileArn" : "arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.anthropic.offline-v1:0", "tokenCountingModelArn" : "arn:aws:bedrock:us-east-1::foundation-model/anthropic.offline-v1:0", "profileSha256" : "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" }
    native_bootstrap               = { "bootstrapArtifactDigest" : "sha256:4444444444444444444444444444444444444444444444444444444444444444", "operation" : { "operationId" : "original-native-bootstrap", "liabilityBasisDigest" : "sha256:5555555555555555555555555555555555555555555555555555555555555555", "reservationIds" : { "infrastructure" : "retained-native-infra", "cleanup" : "retained-native-cleanup" } }, "itemId" : "11111111-1111-1111-1111-111111111111", "expiresAt" : "2026-10-29T00:00:00Z", "targetId" : "native-target", "readGrants" : [], "datasetImportScopes" : [], "managedProcessIds" : [], "installedRuntime" : { "runtimeId" : "runtime", "mutationCeilings" : { "createMap" : false, "createDashboard" : false, "compose" : false, "proposePublication" : false, "proposeRestore" : false, "datasetImportScopes" : [], "managedProcessIds" : [] } } }
    installed_controller_identity  = { "runtimeId" : "runtime", "originalFundingExpiresEpoch" : 1793232000, "permissionsBoundaryArn" : "arn:aws:iam::123456789012:policy/honua-release-pilot-offline-foundation-installed-controller-boundary", "control" : { "secretArn" : "arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-pilot-offline-agent/native-agent/control-aBcDeF", "versionId" : "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee" }, "workers" : [{ "slotId" : "fixed-slot", "stateMachineArn" : "arn:aws:states:us-east-1:123456789012:stateMachine:honua-agent-fixed-slot", "clusterArn" : "arn:aws:ecs:us-east-1:123456789012:cluster/honua-pilot-offline-agent", "taskRoleArn" : "arn:aws:iam::123456789012:role/honua-agent-fixed-slot-worker", "commandQueueArn" : "arn:aws:sqs:us-east-1:123456789012:honua-agent-fixed-slot-commands.fifo", "replyQueueArn" : "arn:aws:sqs:us-east-1:123456789012:honua-agent-fixed-slot-replies.fifo" }] }
  }
  expect_failures = [aws_iam_role.installed_controller[0]]
}

run "different_original_runtime_refused" {
  command = plan
  override_resource {
    override_during = plan
    target          = aws_secretsmanager_secret.runtime_control[0]
    values          = { arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-pilot-offline-agent/native-agent/control-aBcDeF", id = "arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-pilot-offline-agent/native-agent/control-aBcDeF" }
  }
  variables {
    worker_attempt_ids             = ["fixed-slot"]
    installed_worker_slots         = [{ "runtime_id" : "runtime", "slot_id" : "fixed-slot" }]
    issued_native_keys             = { "setup" : { "apiKeyId" : "11111111-1111-1111-1111-111111111111", "versionId" : "1111111111111111111111111111111111111111111111111111111111111111" }, "observer" : { "apiKeyId" : "22222222-2222-2222-2222-222222222222", "versionId" : "2222222222222222222222222222222222222222222222222222222222222222" }, "agent" : { "apiKeyId" : "33333333-3333-3333-3333-333333333333", "versionId" : "3333333333333333333333333333333333333333333333333333333333333333" } }
    issued_runtime_control_key     = { "apiKeyId" : "44444444-4444-4444-4444-444444444444", "versionId" : "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd" }
    controller_model_resource_arns = ["arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.anthropic.offline-v1:0", "arn:aws:bedrock:us-east-1::foundation-model/anthropic.offline-v1:0"]
    controller_model               = { "modelId" : "us.anthropic.offline-v1:0", "returnedModelId" : "offline-v1", "profileArn" : "arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.anthropic.offline-v1:0", "tokenCountingModelArn" : "arn:aws:bedrock:us-east-1::foundation-model/anthropic.offline-v1:0", "profileSha256" : "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" }
    native_bootstrap               = { "bootstrapArtifactDigest" : "sha256:4444444444444444444444444444444444444444444444444444444444444444", "operation" : { "operationId" : "original-native-bootstrap", "liabilityBasisDigest" : "sha256:5555555555555555555555555555555555555555555555555555555555555555", "reservationIds" : { "infrastructure" : "retained-native-infra", "cleanup" : "retained-native-cleanup" } }, "itemId" : "11111111-1111-1111-1111-111111111111", "expiresAt" : "2026-10-29T00:00:00Z", "targetId" : "native-target", "readGrants" : [], "datasetImportScopes" : [], "managedProcessIds" : [], "installedRuntime" : { "runtimeId" : "other_runtime", "mutationCeilings" : { "createMap" : false, "createDashboard" : false, "compose" : false, "proposePublication" : false, "proposeRestore" : false, "datasetImportScopes" : [], "managedProcessIds" : [] } } }
    installed_controller_identity  = { "runtimeId" : "runtime", "originalFundingExpiresEpoch" : 1793232000, "permissionsBoundaryArn" : "arn:aws:iam::123456789012:policy/honua-release-pilot-offline-foundation-installed-controller-boundary", "control" : { "secretArn" : "arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-pilot-offline-agent/native-agent/control-aBcDeF", "versionId" : "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd" }, "workers" : [{ "slotId" : "fixed-slot", "stateMachineArn" : "arn:aws:states:us-east-1:123456789012:stateMachine:honua-agent-fixed-slot", "clusterArn" : "arn:aws:ecs:us-east-1:123456789012:cluster/honua-pilot-offline-agent", "taskRoleArn" : "arn:aws:iam::123456789012:role/honua-agent-fixed-slot-worker", "commandQueueArn" : "arn:aws:sqs:us-east-1:123456789012:honua-agent-fixed-slot-commands.fifo", "replyQueueArn" : "arn:aws:sqs:us-east-1:123456789012:honua-agent-fixed-slot-replies.fifo" }] }
  }
  expect_failures = [aws_iam_role.installed_controller[0]]
}

run "different_current_cluster_refused" {
  command = plan
  override_resource {
    override_during = plan
    target          = aws_secretsmanager_secret.runtime_control[0]
    values          = { arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-pilot-offline-agent/native-agent/control-aBcDeF", id = "arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-pilot-offline-agent/native-agent/control-aBcDeF" }
  }
  variables {
    worker_attempt_ids             = ["fixed-slot"]
    installed_worker_slots         = [{ "runtime_id" : "runtime", "slot_id" : "fixed-slot" }]
    issued_native_keys             = { "setup" : { "apiKeyId" : "11111111-1111-1111-1111-111111111111", "versionId" : "1111111111111111111111111111111111111111111111111111111111111111" }, "observer" : { "apiKeyId" : "22222222-2222-2222-2222-222222222222", "versionId" : "2222222222222222222222222222222222222222222222222222222222222222" }, "agent" : { "apiKeyId" : "33333333-3333-3333-3333-333333333333", "versionId" : "3333333333333333333333333333333333333333333333333333333333333333" } }
    issued_runtime_control_key     = { "apiKeyId" : "44444444-4444-4444-4444-444444444444", "versionId" : "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd" }
    controller_model_resource_arns = ["arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.anthropic.offline-v1:0", "arn:aws:bedrock:us-east-1::foundation-model/anthropic.offline-v1:0"]
    controller_model               = { "modelId" : "us.anthropic.offline-v1:0", "returnedModelId" : "offline-v1", "profileArn" : "arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.anthropic.offline-v1:0", "tokenCountingModelArn" : "arn:aws:bedrock:us-east-1::foundation-model/anthropic.offline-v1:0", "profileSha256" : "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" }
    native_bootstrap               = { "bootstrapArtifactDigest" : "sha256:4444444444444444444444444444444444444444444444444444444444444444", "operation" : { "operationId" : "original-native-bootstrap", "liabilityBasisDigest" : "sha256:5555555555555555555555555555555555555555555555555555555555555555", "reservationIds" : { "infrastructure" : "retained-native-infra", "cleanup" : "retained-native-cleanup" } }, "itemId" : "11111111-1111-1111-1111-111111111111", "expiresAt" : "2026-10-29T00:00:00Z", "targetId" : "native-target", "readGrants" : [], "datasetImportScopes" : [], "managedProcessIds" : [], "installedRuntime" : { "runtimeId" : "runtime", "mutationCeilings" : { "createMap" : false, "createDashboard" : false, "compose" : false, "proposePublication" : false, "proposeRestore" : false, "datasetImportScopes" : [], "managedProcessIds" : [] } } }
    installed_controller_identity  = { "runtimeId" : "runtime", "originalFundingExpiresEpoch" : 1793232000, "permissionsBoundaryArn" : "arn:aws:iam::123456789012:policy/honua-release-pilot-offline-foundation-installed-controller-boundary", "control" : { "secretArn" : "arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-pilot-offline-agent/native-agent/control-aBcDeF", "versionId" : "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd" }, "workers" : [{ "slotId" : "fixed-slot", "stateMachineArn" : "arn:aws:states:us-east-1:123456789012:stateMachine:honua-agent-fixed-slot", "clusterArn" : "arn:aws:ecs:us-east-1:123456789012:cluster/other", "taskRoleArn" : "arn:aws:iam::123456789012:role/honua-agent-fixed-slot-worker", "commandQueueArn" : "arn:aws:sqs:us-east-1:123456789012:honua-agent-fixed-slot-commands.fifo", "replyQueueArn" : "arn:aws:sqs:us-east-1:123456789012:honua-agent-fixed-slot-replies.fifo" }] }
  }
  expect_failures = [aws_iam_role.installed_controller[0]]
}
