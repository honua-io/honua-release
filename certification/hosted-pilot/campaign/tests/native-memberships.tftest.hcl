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
  mock_resource "aws_sfn_state_machine" { defaults = { state_machine_version_arn = "arn:aws:states:us-east-1:123456789012:stateMachine:honua-pilot-offline-agent-campaign-controller:1" } }
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
  target = module.application
  outputs = {
    ecs_cluster_name          = "honua-pilot-offline-agent"
    ecs_service_name          = "honua-pilot-offline-agent"
    db_endpoint               = "isolated.example.us-east-1.rds.amazonaws.com"
    db_connection_secret_arn  = "arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-pilot-offline-agent-master-db-123456"
    admin_password_secret_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-pilot-offline-agent-admin-password-123456"
    native_membership_credentials = {
      scim = { secretArn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-pilot-offline-agent/identity/native-scim-ABC123", versionId = "cccccccccccccccccccccccccccccccc" }
      root = { secretArn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-pilot-offline-agent-admin-password-123456", versionId = "dddddddddddddddddddddddddddddddd" }
    }
    deployment = { server_task_definition = "arn:aws:ecs:us-east-1:123456789012:task-definition/honua-pilot-offline-agent-server:7" }

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
run "membership_handoff" {
  command = plan
  variables {
    native_bootstrap = {
      bootstrapArtifactDigest = "sha256:4444444444444444444444444444444444444444444444444444444444444444"
      operation = {
        operationId    = "original-native-bootstrap", liabilityBasisDigest = "sha256:5555555555555555555555555555555555555555555555555555555555555555"
        reservationIds = { infrastructure = "retained-native-infra", cleanup = "retained-native-cleanup" }
      }
      itemId     = "11111111-1111-1111-1111-111111111111", expiresAt = "2026-10-29T00:00:00Z", targetId = "native-target"
      readGrants = [], datasetImportScopes = ["native-target/honua_data/pilot_points"], managedProcessIds = ["analytics.buffer-aggregate-managed"]
      humanMemberships = {
        schemaVersion = 1
        enrollment = {
          bucket = "honua-release-pilot-offline-foundation-plans-123456789012"
          key    = "hosted-pilot/plans/123/1/human-identities-2.json", versionId = "original-version"
          sha256 = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
          kmsArn = "arn:aws:kms:us-east-1:123456789012:key/12345678-1234-1234-1234-123456789012"
        }
        credentials = {
          scim = { secretArn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-pilot-offline-agent/identity/native-scim-ABC123", versionId = "cccccccccccccccccccccccccccccccc" }
          root = { secretArn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-pilot-offline-agent-admin-password-123456", versionId = "dddddddddddddddddddddddddddddddd" }
        }
      }
    }
  }
  assert {
    condition = (
      aws_iam_role.native_bootstrap[0].permissions_boundary == "arn:aws:iam::123456789012:policy/honua-release-pilot-offline-foundation-native-bootstrap-boundary" &&
      aws_iam_role.native_bootstrap_execution[0].permissions_boundary == var.permissions_boundary_arn &&
      aws_ecs_task_definition.native_bootstrap[0].execution_role_arn == aws_iam_role.native_bootstrap_execution[0].arn &&
      { for secret in jsondecode(aws_ecs_task_definition.native_bootstrap[0].container_definitions)[0].secrets : secret.name => secret.valueFrom }.HONUA_NATIVE_SCIM_TOKEN == "arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-pilot-offline-agent/identity/native-scim-ABC123:::cccccccccccccccccccccccccccccccc" &&
      { for secret in jsondecode(aws_ecs_task_definition.native_bootstrap[0].container_definitions)[0].secrets : secret.name => secret.valueFrom }.PILOT_FIXTURE_ADMIN_PASSWORD == "arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-pilot-offline-agent-admin-password-123456:::dddddddddddddddddddddddddddddddd" &&
      length([for statement in jsondecode(aws_iam_role_policy.native_bootstrap_execution[0].policy).Statement : statement if statement.Effect == "Deny" && statement.Action == "secretsmanager:GetSecretValue" && can(statement.Condition.StringNotEquals["secretsmanager:VersionId"])]) == 2 &&
      !strcontains(aws_iam_role_policy.native_bootstrap[0].policy, "secretsmanager:GetSecretValue") &&
      !strcontains(aws_ecs_task_definition.controller.container_definitions, "HONUA_NATIVE_SCIM_TOKEN") &&
      !strcontains(aws_iam_role_policy.native_bootstrap_execution[0].policy, "s3:GetObject") &&
      one([for row in jsondecode(aws_iam_role_policy.native_bootstrap[0].policy).Statement : row if try(row.Action == "ecs:DescribeTaskDefinition", false)]) == {
        Effect = "Allow", Action = "ecs:DescribeTaskDefinition", Resource = "*", Condition = { StringEquals = { "aws:RequestedRegion" = "us-east-1" } }
      }
    )
    error_message = "Only the distinct native bootstrap task may receive immutable SCIM/root credentials; controller/worker and task IAM never read those secrets."
  }
  assert {
    condition = (
      length(local.membership_object_arns) == 2 &&
      local.membership_object_arns[1] == "arn:aws:s3:::honua-release-pilot-offline-foundation-plans-123456789012/hosted-pilot/plans/123/1/review.json" &&
      one([for statement in jsondecode(aws_iam_role_policy.native_bootstrap[0].policy).Statement : statement if try(statement.Resource == local.membership_object_arns[0], false)]).Condition.StringEquals["s3:VersionId"] == "original-version" &&
      toset(one([for statement in jsondecode(aws_iam_role_policy.native_bootstrap[0].policy).Statement : statement if try(statement.Action == "kms:Decrypt", false)]).Condition.StringEquals["kms:EncryptionContext:aws:s3:arn"]) == toset(local.membership_object_arns) &&
      output.native_bootstrap_handoff.configuration.humanMemberships == var.native_bootstrap.humanMemberships
    )
    error_message = "Membership proof must use only the original object version and same-plan review, with S3 object-level KMS context."
  }
}

run "refuse_foreign_membership_secret_version" {
  command = plan
  variables {
    native_bootstrap = merge(run.membership_handoff.native_bootstrap_handoff.configuration, {
      humanMemberships = merge(run.membership_handoff.native_bootstrap_handoff.configuration.humanMemberships, {
        credentials = merge(run.membership_handoff.native_bootstrap_handoff.configuration.humanMemberships.credentials, {
          root = { secretArn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-pilot-offline-agent-admin-password-123456", versionId = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee" }
        })
      })
    })
  }
  expect_failures = [aws_ecs_task_definition.native_bootstrap]
}

run "refuse_foreign_enrollment_bucket" {
  command = plan
  variables {
    native_bootstrap = merge(run.membership_handoff.native_bootstrap_handoff.configuration, {
      humanMemberships = merge(run.membership_handoff.native_bootstrap_handoff.configuration.humanMemberships, {
        enrollment = merge(run.membership_handoff.native_bootstrap_handoff.configuration.humanMemberships.enrollment, { bucket = "foreign-private-bucket" })
      })
    })
  }
  expect_failures = [aws_ecs_task_definition.native_bootstrap]
}

run "refuse_saved_binary_as_human_enrollment" {
  command = plan
  variables {
    native_bootstrap = merge(run.membership_handoff.native_bootstrap_handoff.configuration, {
      humanMemberships = merge(run.membership_handoff.native_bootstrap_handoff.configuration.humanMemberships, {
        enrollment = merge(run.membership_handoff.native_bootstrap_handoff.configuration.humanMemberships.enrollment, { key = "hosted-pilot/plans/123/1/plan.bin" })
      })
    })
  }
  expect_failures = [aws_ecs_task_definition.native_bootstrap]
}
