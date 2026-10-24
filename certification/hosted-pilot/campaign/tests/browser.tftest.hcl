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
mock_provider "aws" {
  alias           = "application"
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
  target = module.application
  outputs = {
    deployment = {
      console_service         = "honua-pilot-offline-console"
      console_task_definition = "arn:aws:ecs:us-east-1:123456789012:task-definition/honua-pilot-offline-console:7"
      edge_proxy_image        = "quay.io/oauth2-proxy/oauth2-proxy@sha256:8e4bec4f9fe4de10de63070209204075e4407503170aa19893d6e8d99f083d1b"
    }
    identity_preparation      = { issuer = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_Offline" }
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
run "immutable_human_browser_storage_reaches_only_private_controller" {
  command = plan
  variables {
    human_browser = {
      storageState = { secretArn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-pilot-offline-agent/human-session/browser-ABC123", versionId = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee" }
      consoleDeployment = {
        region            = "us-east-1", clusterArn = "arn:aws:ecs:us-east-1:123456789012:cluster/honua-pilot-offline-agent",
        serviceArn        = "arn:aws:ecs:us-east-1:123456789012:service/honua-pilot-offline-agent/honua-pilot-offline-console",
        taskDefinitionArn = "arn:aws:ecs:us-east-1:123456789012:task-definition/honua-pilot-offline-console:7",
        imageUri          = "ghcr.io/honua-io/honua-console@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
        edgeImageUri      = "quay.io/oauth2-proxy/oauth2-proxy@sha256:8e4bec4f9fe4de10de63070209204075e4407503170aa19893d6e8d99f083d1b",
        issuer            = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_Offline"
      }
    }
  }
  assert {
    condition = (
      one([for secret in jsondecode(aws_ecs_task_definition.controller.container_definitions)[0].secrets : secret.valueFrom if secret.name == "HONUA_CONSOLE_BROWSER_STORAGE_STATE"]) == "${var.human_browser.storageState.secretArn}:::${var.human_browser.storageState.versionId}" &&
      !strcontains(aws_ecs_task_definition.fixture.container_definitions, "HONUA_CONSOLE_BROWSER_STORAGE_STATE") &&
      !strcontains(aws_iam_role_policy.execution["fixture"].policy, var.human_browser.storageState.secretArn) &&
      !strcontains(aws_iam_role_policy.dispatcher.policy, var.human_browser.storageState.secretArn) &&
      jsondecode(aws_iam_role_policy.browser_execution[0].policy).Statement[0].Resource == var.human_browser.storageState.secretArn &&
      jsondecode(aws_iam_role_policy.browser_execution[0].policy).Statement[0].Condition.StringEquals["secretsmanager:VersionId"] == var.human_browser.storageState.versionId &&
      toset([for statement in jsondecode(aws_iam_role_policy.browser_observation[0].policy).Statement : statement.Action]) == toset(["ecs:DescribeServices", "ecs:DescribeTaskDefinition"]) &&
      one([for statement in jsondecode(aws_iam_role_policy.browser_observation[0].policy).Statement : statement if statement.Action == "ecs:DescribeTaskDefinition"]) == {
        Effect    = "Allow", Action = "ecs:DescribeTaskDefinition", Resource = "*",
        Condition = { StringEquals = { "aws:RequestedRegion" = var.region } }
      } &&
      one([for statement in jsondecode(aws_iam_role_policy.browser_observation[0].policy).Statement : statement if statement.Action == "ecs:DescribeServices"]).Resource == var.human_browser.consoleDeployment.serviceArn &&
      !strcontains(aws_iam_role_policy.browser_observation[0].policy, "secretsmanager:")
    )
    error_message = "Only the trusted controller may receive immutable human browser custody; its separate native deployment observations must remain read-only."
  }
}

run "refuse_other_console_service" {
  command = plan
  variables {
    human_browser = {
      storageState = { secretArn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-pilot-offline-agent/human-session/browser-ABC123", versionId = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee" }
      consoleDeployment = {
        region            = "us-east-1", clusterArn = "arn:aws:ecs:us-east-1:123456789012:cluster/honua-pilot-offline-agent",
        serviceArn        = "arn:aws:ecs:us-east-1:123456789012:service/honua-pilot-offline-agent/other-console",
        taskDefinitionArn = "arn:aws:ecs:us-east-1:123456789012:task-definition/honua-pilot-offline-console:7",
        imageUri          = "ghcr.io/honua-io/honua-console@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
        edgeImageUri      = "quay.io/oauth2-proxy/oauth2-proxy@sha256:8e4bec4f9fe4de10de63070209204075e4407503170aa19893d6e8d99f083d1b",
        issuer            = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_Offline"
      }
    }
  }
  expect_failures = [output.application_deployment]
}

run "refuse_stale_console_revision" {
  command = plan
  variables {
    human_browser = {
      storageState = { secretArn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-pilot-offline-agent/human-session/browser-ABC123", versionId = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee" }
      consoleDeployment = {
        region            = "us-east-1", clusterArn = "arn:aws:ecs:us-east-1:123456789012:cluster/honua-pilot-offline-agent",
        serviceArn        = "arn:aws:ecs:us-east-1:123456789012:service/honua-pilot-offline-agent/honua-pilot-offline-console",
        taskDefinitionArn = "arn:aws:ecs:us-east-1:123456789012:task-definition/honua-pilot-offline-console:6",
        imageUri          = "ghcr.io/honua-io/honua-console@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
        edgeImageUri      = "quay.io/oauth2-proxy/oauth2-proxy@sha256:8e4bec4f9fe4de10de63070209204075e4407503170aa19893d6e8d99f083d1b",
        issuer            = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_Offline"
      }
    }
  }
  expect_failures = [output.application_deployment]
}

run "refuse_other_console_image" {
  command = plan
  variables {
    human_browser = {
      storageState = { secretArn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-pilot-offline-agent/human-session/browser-ABC123", versionId = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee" }
      consoleDeployment = {
        region            = "us-east-1", clusterArn = "arn:aws:ecs:us-east-1:123456789012:cluster/honua-pilot-offline-agent",
        serviceArn        = "arn:aws:ecs:us-east-1:123456789012:service/honua-pilot-offline-agent/honua-pilot-offline-console",
        taskDefinitionArn = "arn:aws:ecs:us-east-1:123456789012:task-definition/honua-pilot-offline-console:7",
        imageUri          = "ghcr.io/honua-io/honua-console@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        edgeImageUri      = "quay.io/oauth2-proxy/oauth2-proxy@sha256:8e4bec4f9fe4de10de63070209204075e4407503170aa19893d6e8d99f083d1b",
        issuer            = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_Offline"
      }
    }
  }
  expect_failures = [output.application_deployment]
}

run "refuse_other_console_issuer" {
  command = plan
  variables {
    human_browser = {
      storageState = { secretArn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-pilot-offline-agent/human-session/browser-ABC123", versionId = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee" }
      consoleDeployment = {
        region            = "us-east-1", clusterArn = "arn:aws:ecs:us-east-1:123456789012:cluster/honua-pilot-offline-agent",
        serviceArn        = "arn:aws:ecs:us-east-1:123456789012:service/honua-pilot-offline-agent/honua-pilot-offline-console",
        taskDefinitionArn = "arn:aws:ecs:us-east-1:123456789012:task-definition/honua-pilot-offline-console:7",
        imageUri          = "ghcr.io/honua-io/honua-console@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
        edgeImageUri      = "quay.io/oauth2-proxy/oauth2-proxy@sha256:8e4bec4f9fe4de10de63070209204075e4407503170aa19893d6e8d99f083d1b",
        issuer            = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_Foreign"
      }
    }
  }
  expect_failures = [output.application_deployment]
}
