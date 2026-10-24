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
run "private_sql_and_credential_custody" {
  command = plan
  assert {
    condition = (
      length(aws_security_group.controller.ingress) == 0 &&
      length(aws_security_group.worker.ingress) == 0 &&
      alltrue([for rule in aws_security_group.worker.egress : rule.to_port != 5432]) &&
      contains([for rule in aws_security_group.controller.egress : rule.to_port], 5432) &&
      jsondecode(aws_ecs_task_definition.controller.container_definitions)[0].user == "10001:10001" &&
      jsondecode(aws_ecs_task_definition.controller.container_definitions)[0].readonlyRootFilesystem &&
      !contains([for secret in jsondecode(aws_ecs_task_definition.controller.container_definitions)[0].secrets : secret.name], "PILOT_FIXTURE_MASTER_CONNECTION") &&
      alltrue([for secret in jsondecode(aws_ecs_task_definition.controller.container_definitions)[0].secrets : can(regex(":::[^:]+$", secret.valueFrom)) && !strcontains(secret.valueFrom, "::::")]) &&
      aws_secretsmanager_secret.worker_mcp.name == "honua-agent-${substr(local.candidate_hex, 0, 24)}-mcp" &&
      toset(jsondecode(aws_iam_role_policy.controller_mcp_configuration.policy).Statement[0].Action) == toset(["secretsmanager:PutSecretValue", "secretsmanager:GetSecretValue"]) &&
      jsondecode(aws_iam_role_policy.controller_mcp_configuration.policy).Statement[0].Resource == aws_secretsmanager_secret.worker_mcp.arn &&
      !strcontains(aws_iam_role_policy.dispatcher.policy, "secretsmanager:") &&
      !strcontains(aws_iam_role_policy.controller.policy, "bedrock:InvokeModel")
    )
    error_message = "Only private trusted tasks may reach SQL; dispatchers/workers must never receive SQL/master credentials."
  }
  assert {
    condition = (
      aws_sfn_state_machine.controller.publish &&
      local.controller_definition.TimeoutSeconds == 7200 &&
      local.controller_definition.States.RunController.Parameters.NetworkConfiguration.AwsvpcConfiguration.AssignPublicIp == "DISABLED" &&
      !can(local.controller_definition.States.RunController.Retry) &&
      one([for statement in jsondecode(aws_iam_role_policy.dispatcher.policy).Statement : statement if statement.Action == "states:StartExecution"]).Resource == aws_sfn_state_machine.controller.arn &&
      one([for statement in jsondecode(aws_iam_role_policy.dispatcher.policy).Statement : statement if statement.Action == "states:StartExecution"]).Condition["ForAnyValue:StringEquals"]["states:StateMachineQualifier"] == element(reverse(split(":", aws_sfn_state_machine.controller.state_machine_version_arn)), 0) &&
      alltrue([for table in aws_dynamodb_table.claims : table.deletion_protection_enabled && table.point_in_time_recovery[0].enabled]) &&
      output.fixture_staging.modelReady == false &&
      output.fixture_staging.nativeAgentConfigured == false
    )
    error_message = "Dispatch must use one published bounded no-redrive task, retain immutable claims and refuse missing native readiness."
  }
}

run "task_definition_reads_use_only_region_scoped_wildcards" {
  command = plan
  assert {
    condition = alltrue([for policy in [aws_iam_role_policy.dispatcher.policy, aws_iam_role_policy.controller.policy] :
      one([for statement in jsondecode(policy).Statement : statement if statement.Action == "ecs:DescribeTaskDefinition"]) == {
        Effect    = "Allow", Action = "ecs:DescribeTaskDefinition", Resource = "*",
        Condition = { StringEquals = { "aws:RequestedRegion" = var.region } }
      }
    ])
    error_message = "DescribeTaskDefinition must be isolated to its supported region-scoped wildcard, without adding other actions."
  }
  assert {
    condition = (
      one([for statement in jsondecode(aws_iam_role_policy.dispatcher.policy).Statement : statement if statement.Action == "ecs:DescribeTasks"]).Resource == "arn:aws:ecs:${var.region}:${var.account_id}:task/${module.application.ecs_cluster_name}/*" &&
      one([for statement in jsondecode(aws_iam_role_policy.controller.policy).Statement : statement if try(toset(statement.Action), toset([])) == toset(["ecs:DescribeTasks", "ecs:StopTask"])]).Resource == "arn:aws:ecs:${var.region}:${var.account_id}:task/${module.application.ecs_cluster_name}/*" &&
      one([for statement in jsondecode(aws_iam_role_policy.controller.policy).Statement : statement if statement.Action == "ecs:RunTask"]).Resource == aws_ecs_task_definition.fixture.arn &&
      one([for statement in jsondecode(aws_iam_role_policy.controller.policy).Statement : statement if statement.Action == "iam:PassRole"]).Resource == [aws_iam_role.fixture.arn, aws_iam_role.execution["fixture"].arn]
    )
    error_message = "Task reads, stopping, launching and role passing must retain their original exact resources."
  }
}

run "installed_native_keys_use_actual_immutable_version_ids" {
  command = plan
  variables {
    issued_native_keys = {
      setup    = { apiKeyId = "11111111-1111-1111-1111-111111111111", versionId = "1111111111111111111111111111111111111111111111111111111111111111" }
      observer = { apiKeyId = "22222222-2222-2222-2222-222222222222", versionId = "2222222222222222222222222222222222222222222222222222222222222222" }
      agent    = { apiKeyId = "33333333-3333-3333-3333-333333333333", versionId = "3333333333333333333333333333333333333333333333333333333333333333" }
    }
  }
  assert {
    condition = (
      local.native_agent_key_id == var.issued_native_keys.agent.apiKeyId &&
      length(jsondecode(aws_ecs_task_definition.controller.container_definitions)[0].secrets) == 5 &&
      alltrue([for secret in jsondecode(aws_ecs_task_definition.controller.container_definitions)[0].secrets : can(regex(":::[^:]+$", secret.valueFrom)) && !strcontains(secret.valueFrom, "::::")]) &&
      endswith(local.controller_secrets.HONUA_AGENT_REQUESTER_KEY, ":::${var.issued_native_keys.agent.versionId}")
    )
    error_message = "Reviewed native keys must install the actual agent principal and inject immutable version IDs, never version stages or latest values."
  }
}

run "reviewed_model_resources_only" {
  command = plan
  variables {
    controller_model_resource_arns = [
      "arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.anthropic.claude-sonnet-5-5",
      "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-sonnet-5-5"
    ]
  }
  assert {
    condition     = toset(one([for statement in jsondecode(aws_iam_role_policy.controller.policy).Statement : statement if can(index(statement.Action, "bedrock:InvokeModel"))]).Resource) == toset(var.controller_model_resource_arns)
    error_message = "The controller may invoke only exact reviewed profile/member resources; the independently managed boundary further restricts access."
  }
}

run "reject_wildcard_model_authority" {
  command = plan
  variables {
    controller_model_resource_arns = ["arn:aws:bedrock:us-east-1::foundation-model/*"]
  }
  expect_failures = [var.controller_model_resource_arns]
}

run "native_bootstrap_is_private_and_separates_sql_root_from_controller" {
  command = plan
  variables {
    native_bootstrap = {
      bootstrapArtifactDigest = "sha256:4444444444444444444444444444444444444444444444444444444444444444"
      operation = {
        operationId    = "original-native-bootstrap", liabilityBasisDigest = "sha256:5555555555555555555555555555555555555555555555555555555555555555"
        reservationIds = { infrastructure = "retained-native-infra", cleanup = "retained-native-cleanup" }
      }
      itemId               = "11111111-1111-1111-1111-111111111111"
      expiresAt            = "2026-10-29T00:00:00Z"
      targetId             = "native-target"
      readGrants           = []
      datasetImportScopes  = ["native-target/honua_data/pilot_points"]
      managedProcessIds    = ["analytics.buffer-aggregate-managed"]
      studioRestoreItemIds = ["11111111-1111-1111-1111-111111111111"]
    }
  }
  assert {
    condition = (
      tolist(jsondecode(aws_ecs_task_definition.native_bootstrap[0].container_definitions)[0].command) == tolist(["python", "tools/hosted_pilot/native_bootstrap.py"]) &&
      jsondecode(aws_ecs_task_definition.native_bootstrap[0].container_definitions)[0].readonlyRootFilesystem &&
      jsondecode(jsondecode(aws_ecs_task_definition.native_bootstrap[0].container_definitions)[0].environment[0].value).schemaVersion == 2 &&
      jsondecode(jsondecode(aws_ecs_task_definition.native_bootstrap[0].container_definitions)[0].environment[0].value).targetId == "native-target" &&
      toset(jsondecode(jsondecode(aws_ecs_task_definition.native_bootstrap[0].container_definitions)[0].environment[0].value).datasetImportScopes) == toset(["native-target/honua_data/pilot_points"]) &&
      toset(jsondecode(jsondecode(aws_ecs_task_definition.native_bootstrap[0].container_definitions)[0].environment[0].value).studioRestoreItemIds) == toset(["11111111-1111-1111-1111-111111111111"]) &&
      aws_iam_role.native_bootstrap[0].name != aws_iam_role.controller.name &&
      length(aws_iam_role_policy.dispatch_native_bootstrap) == 1 &&
      strcontains(aws_iam_role_policy.dispatch_native_bootstrap[0].policy, "ecs:RunTask") &&
      strcontains(aws_iam_role_policy.dispatch_native_bootstrap[0].policy, "iam:PassRole") &&
      strcontains(aws_iam_role_policy.controller.policy, "ecs:RunTask") &&
      strcontains(aws_iam_role_policy.lifecycle.policy, "ecs:RunTask") &&
      !strcontains(aws_iam_role_policy.native_bootstrap[0].policy, "secretsmanager:GetSecretValue") &&
      !strcontains(aws_iam_role_policy.controller.policy, "secretsmanager:PutSecretValue") &&
      !strcontains(aws_iam_role_policy.dispatcher.policy, "secretsmanager:GetSecretValue") &&
      contains([for secret in jsondecode(aws_ecs_task_definition.native_bootstrap[0].container_definitions)[0].secrets : secret.name], "PILOT_FIXTURE_MASTER_CONNECTION") &&
      !contains([for secret in jsondecode(aws_ecs_task_definition.controller.container_definitions)[0].secrets : secret.name], "PILOT_FIXTURE_MASTER_CONNECTION")
    )
    error_message = "Native provisioning must stay in its separate private task; SQL/root credentials cannot reach the controller, dispatcher or model."
  }
}

run "accounting_can_stage_without_inventing_a_generation" {
  command = plan
  variables { dispatch_budget = null }
  assert {
    condition = (
      output.controller_handoff == null && output.fixture_handoff == null &&
      output.native_bootstrap_handoff == null &&
      output.accounting_staging.dispatchEnabled == false &&
      output.accounting_staging.state == module.accounting.state_config &&
      output.initialization_readiness.state == module.accounting.state_config &&
      output.initialization_readiness.controller.taskDefinitionArn == aws_ecs_task_definition.controller.arn &&
      output.initialization_readiness.fixture.taskDefinitionArn == aws_ecs_task_definition.fixture.arn &&
      output.initialization_readiness.dispatcherRoleArn == aws_iam_role.dispatcher.arn &&
      output.initialization_readiness.dispatcherPolicyDigests[aws_iam_role_policy.dispatcher.name] == "sha256:${sha256(aws_iam_role_policy.dispatcher.policy)}" &&
      !contains(keys(output.initialization_readiness.controller), "dispatchBudget") &&
      length(aws_iam_role_policy.initializer) == 1 &&
      jsondecode(aws_iam_role.initializer.assume_role_policy) == {
        Version = "2012-10-17", Statement = [{ Effect = "Allow", Action = "sts:AssumeRoleWithWebIdentity",
          Principal = { Federated = var.oidc_provider_arn }, Condition = { StringEquals = {
            "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com",
            "token.actions.githubusercontent.com:sub" = "repo:honua-io/honua-release:environment:hosted-pilot-accounting-initialize"
        } } }]
      } &&
      !strcontains(aws_iam_role_policy.dispatcher.policy, "states:StartExecution")
    )
    error_message = "Accounting staging must not fabricate a generation or enable dispatch before authenticated create-only initialization."
  }
}

run "retained_generation_removes_initializer_mutation_authority" {
  command = plan
  assert {
    condition = (length(aws_iam_role_policy.initializer) == 0 && !output.accounting_initializer.initializationEnabled &&
    output.accounting_initializer.protectedEnvironment == "hosted-pilot-accounting-initialize")
    error_message = "After retained accounting is installed, the initializer must have no standing S3/DynamoDB mutation policy."
  }
  assert {
    condition = jsondecode(aws_iam_role_policy.initializer_observer.policy) == {
      Version = "2012-10-17", Statement = [
        { Effect = "Allow", Action = "s3:GetObjectVersion", Resource = "${local.state_bucket_arn}/agent-e2e/${local.candidate_hex}/*" },
        { Effect    = "Allow", Action = "dynamodb:GetItem", Resource = local.state_table_arn,
          Condition = { "ForAllValues:StringEquals" = { "dynamodb:LeadingKeys" = [var.candidate_digest] } }
        }
      ]
    }
    error_message = "Receipt recovery must retain only versioned object GET and consistent candidate-head GET permissions, without any lease, write or list authority."
  }
}

run "actual_human_session_is_private_and_immutable" {
  command = plan
  variables {
    human_operator_credential = {
      secretArn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-pilot-offline-agent/human-session/operator-ABC123"
      versionId = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
    }
  }
  assert {
    condition = (
      one([for secret in jsondecode(aws_ecs_task_definition.controller.container_definitions)[0].secrets : secret.valueFrom if secret.name == "HONUA_SOURCE_CONTRIBUTOR_OPERATOR_TOKEN"]) == "${var.human_operator_credential.secretArn}:::${var.human_operator_credential.versionId}" &&
      !strcontains(aws_ecs_task_definition.fixture.container_definitions, "HONUA_SOURCE_CONTRIBUTOR_OPERATOR_TOKEN") &&
      !strcontains(aws_iam_role_policy.execution["fixture"].policy, var.human_operator_credential.secretArn) &&
      strcontains(aws_iam_role_policy.execution["controller"].policy, var.human_operator_credential.secretArn)
    )
    error_message = "Only the trusted controller may receive the actual immutable human session credential."
  }
}

run "configured_native_bootstrap_without_budget_has_no_paid_launch_authority" {
  command = plan
  variables {
    dispatch_budget = null
    native_bootstrap = {
      bootstrapArtifactDigest = "sha256:4444444444444444444444444444444444444444444444444444444444444444"
      operation = {
        operationId    = "original-native-bootstrap", liabilityBasisDigest = "sha256:5555555555555555555555555555555555555555555555555555555555555555"
        reservationIds = { infrastructure = "retained-native-infra", cleanup = "retained-native-cleanup" }
      }
      itemId              = "11111111-1111-1111-1111-111111111111"
      expiresAt           = "2026-10-29T00:00:00Z"
      targetId            = "native-target"
      readGrants          = []
      datasetImportScopes = ["native-target/honua_data/pilot_points"]
      managedProcessIds   = ["analytics.buffer-aggregate-managed"]
    }
    controller_model_resource_arns = ["arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-sonnet-4-5-20250929-v1:0"]
  }
  assert {
    condition = (
      length(aws_ecs_task_definition.native_bootstrap) == 1 &&
      length(aws_iam_role_policy.dispatch_native_bootstrap) == 0 &&
      output.controller_handoff == null && output.fixture_handoff == null && output.native_bootstrap_handoff == null &&
      !strcontains(aws_iam_role_policy.dispatcher.policy, "states:StartExecution") &&
      !strcontains(aws_iam_role_policy.controller.policy, "ecs:RunTask") &&
      !strcontains(aws_iam_role_policy.controller.policy, "iam:PassRole") &&
      !strcontains(aws_iam_role_policy.controller.policy, "bedrock:InvokeModel") &&
      !strcontains(aws_iam_role_policy.lifecycle.policy, "ecs:RunTask") &&
      !strcontains(aws_iam_role_policy.lifecycle.policy, "iam:PassRole") &&
      !strcontains(aws_iam_role_policy.observe_native_bootstrap[0].policy, "ecs:RunTask") &&
      !strcontains(aws_iam_role_policy.observe_native_bootstrap[0].policy, "iam:PassRole") &&
      strcontains(aws_iam_role_policy.observe_native_bootstrap[0].policy, "ecs:StopTask") &&
      strcontains(aws_iam_role_policy.controller.policy, "ecs:StopTask") &&
      strcontains(aws_iam_role_policy.lifecycle.policy, "ecs:StopTask")
    )
    error_message = "Staging a native definition without retained accounting must grant no launch, pass-role or model authority; observation and original task cleanup must remain available."
  }
}

run "setup_retirement_has_separate_original_credentials_and_no_sql_or_model" {
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
      mode               = "retire", originalClaim = null
    }
  }
  assert {
    condition = (
      length(aws_iam_role_policy.dispatch_setup_retirement) == 1 &&
      length(jsondecode(aws_ecs_task_definition.setup_retirement[0].container_definitions)[0].secrets) == 2 &&
      toset([for secret in jsondecode(aws_ecs_task_definition.setup_retirement[0].container_definitions)[0].secrets : secret.name]) == toset(["PILOT_SETUP_RETIRE_ROOT_KEY", "PILOT_SETUP_RETIRE_KEY"]) &&
      endswith(one([for secret in jsondecode(aws_ecs_task_definition.setup_retirement[0].container_definitions)[0].secrets : secret.valueFrom if secret.name == "PILOT_SETUP_RETIRE_KEY"]), ":::${var.issued_native_keys.setup.versionId}") &&
      local.retirement_config.mode == "retire" && local.retirement_config.originalClaim == null &&
      local.retirement_config.setupKey.apiKeyId == var.issued_native_keys.setup.apiKeyId &&
      endswith(local.retirement_config.receiptKey, "/retire.json") &&
      !strcontains(aws_iam_role_policy.setup_retirement[0].policy, "secretsmanager:") &&
      !strcontains(aws_iam_role_policy.setup_retirement[0].policy, "ecs:RunTask") &&
      !strcontains(aws_iam_role_policy.setup_retirement[0].policy, "bedrock:") &&
      one([for statement in jsondecode(aws_iam_role_policy.setup_retirement[0].policy).Statement : statement if statement.Action == "dynamodb:GetItem"]).Resource == aws_dynamodb_table.claims["dispatch"].arn &&
      jsondecode(aws_ecs_task_definition.setup_retirement[0].container_definitions)[0].readonlyRootFilesystem &&
      alltrue([for policy in [aws_iam_role_policy.setup_retirement[0].policy, aws_iam_role_policy.observe_setup_retirement[0].policy] :
        one([for statement in jsondecode(policy).Statement : statement if statement.Action == "ecs:DescribeTaskDefinition"]) == {
          Effect    = "Allow", Action = "ecs:DescribeTaskDefinition", Resource = "*",
          Condition = { StringEquals = { "aws:RequestedRegion" = var.region } }
        }
      ]) &&
      one([for statement in jsondecode(aws_iam_role_policy.observe_setup_retirement[0].policy).Statement : statement if statement.Action == "ecs:StopTask"]).Resource == "arn:aws:ecs:${var.region}:${var.account_id}:task/${module.application.ecs_cluster_name}/*" &&
      one([for statement in jsondecode(aws_iam_role_policy.dispatch_setup_retirement[0].policy).Statement : statement if statement.Action == "ecs:RunTask"]).Resource == aws_ecs_task_definition.setup_retirement[0].arn &&
      output.setup_retirement_handoff.configuration == local.retirement_config
    )
    error_message = "Retirement must receive only its original setup key and independent administrator; no model, SQL, minting, or task-launch privilege may enter that task."
  }
}
run "configured_retirement_without_budget_has_no_launch_or_pass_role" {
  command = plan
  variables {
    dispatch_budget = null
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
      mode               = "retire", originalClaim = null
    }
  }
  assert {
    condition = (
      length(aws_ecs_task_definition.setup_retirement) == 1 && length(aws_iam_role_policy.dispatch_setup_retirement) == 0 &&
      output.setup_retirement_handoff == null &&
      !strcontains(aws_iam_role_policy.observe_setup_retirement[0].policy, "ecs:RunTask") &&
      !strcontains(aws_iam_role_policy.observe_setup_retirement[0].policy, "iam:PassRole") &&
      strcontains(aws_iam_role_policy.observe_setup_retirement[0].policy, "ecs:StopTask")
    )
    error_message = "Retirement staging cannot bypass original paid launch accounting; observation and cleanup must remain possible."
  }
}
run "retirement_cannot_invent_unissued_keys" {
  command = plan
  variables {
    setup_key_retirement = {
      bootstrapArtifactDigest    = "sha256:4444444444444444444444444444444444444444444444444444444444444444"
      nativeBootstrapOperationId = "original-native-bootstrap"
      operation = {
        operationId    = "original-native-bootstrap", liabilityBasisDigest = "sha256:5555555555555555555555555555555555555555555555555555555555555555"
        reservationIds = { infrastructure = "retained-native-infra", cleanup = "retained-native-cleanup" }
      }
      fixtureReference   = { key = "sha256:6666666666666666666666666666666666666666666666666666666666666666", sha256 = "sha256:7777777777777777777777777777777777777777777777777777777777777777" }
      preparationOutcome = { bucket = "original-controller-bucket", key = "original-outcome-key", versionId = "original-version", sha256 = "sha256:8888888888888888888888888888888888888888888888888888888888888888" }
      mode               = "retire", originalClaim = null
    }
  }
  expect_failures = [output.setup_retirement_handoff]
}
run "retirement_recovery_retains_original_claim_and_separate_receipt" {
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
      mode               = "observe"
      originalClaim      = { table = "original-claims", key = "dispatch:9999999999999999999999999999999999999999999999999999999999999999", bindingDigest = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", taskArn = "original-task", taskDefinitionArn = "original-definition:1" }
    }
  }
  assert {
    condition = (
      local.retirement_config.mode == "observe" &&
      local.retirement_config.originalClaim == var.setup_key_retirement.originalClaim &&
      endswith(local.retirement_config.receiptKey, "/observe-9999999999999999999999999999999999999999999999999999999999999999.json")
    )
    error_message = "Read-only recovery must retain the original permanent claim and publish separately without overwriting original revocation custody."
  }
}
run "worker_custody_read_is_absent_without_declared_attempts" {
  command = plan
  variables { worker_attempt_ids = [] }
  assert {
    condition     = length(aws_iam_role_policy.worker_custody_observer) == 0
    error_message = "Unconfigured custody observation cannot grant IAM or task-definition reads."
  }
}
run "worker_custody_read_has_exact_roles_and_region_scoped_definition_read" {
  command = plan
  variables { worker_attempt_ids = ["p14-first", "p14-second"] }
  assert {
    condition = aws_iam_role_policy.worker_custody_observer[0].policy == jsonencode({ Version = "2012-10-17", Statement = [
      { Effect = "Allow", Action = ["iam:GetRolePolicy", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies"],
      Resource = ["arn:aws:iam::123456789012:role/honua-agent-p14-first-worker", "arn:aws:iam::123456789012:role/honua-agent-p14-second-worker"] },
      { Effect = "Allow", Action = "ecs:DescribeTaskDefinition",
      Resource = "*", Condition = { StringEquals = { "aws:RequestedRegion" = var.region } } }
    ] })
    error_message = "Custody must retain exact worker role reads and isolate the region-scoped task-definition wildcard without launch, secrets or wildcard IAM authority."
  }
}
run "worker_custody_attempt_cannot_expand_to_a_role_wildcard" {
  command = plan
  variables { worker_attempt_ids = ["*"] }
  expect_failures = [var.worker_attempt_ids]
}

run "reject_native_import_foreign_target" {
  command = plan
  variables {
    native_bootstrap = {
      bootstrapArtifactDigest = "sha256:4444444444444444444444444444444444444444444444444444444444444444"
      operation = {
        operationId    = "original-native-bootstrap", liabilityBasisDigest = "sha256:5555555555555555555555555555555555555555555555555555555555555555"
        reservationIds = { infrastructure = "retained-native-infra", cleanup = "retained-native-cleanup" }
      }
      itemId              = "11111111-1111-1111-1111-111111111111"
      expiresAt           = "2026-10-29T00:00:00Z"
      targetId            = "native-target"
      readGrants          = []
      datasetImportScopes = ["foreign-target/honua_data/pilot_points"]
      managedProcessIds   = ["analytics.buffer-aggregate-managed"]
    }
  }
  expect_failures = [var.native_bootstrap]
}

run "reject_native_managed_process_wildcard" {
  command = plan
  variables {
    native_bootstrap = {
      bootstrapArtifactDigest = "sha256:4444444444444444444444444444444444444444444444444444444444444444"
      operation = {
        operationId    = "original-native-bootstrap", liabilityBasisDigest = "sha256:5555555555555555555555555555555555555555555555555555555555555555"
        reservationIds = { infrastructure = "retained-native-infra", cleanup = "retained-native-cleanup" }
      }
      itemId              = "11111111-1111-1111-1111-111111111111"
      expiresAt           = "2026-10-29T00:00:00Z"
      targetId            = "native-target"
      readGrants          = []
      datasetImportScopes = ["native-target/honua_data/pilot_points"]
      managedProcessIds   = ["*"]
    }
  }
  expect_failures = [var.native_bootstrap]
}

run "reject_native_mutation_in_read_grants" {
  command = plan
  variables {
    native_bootstrap = {
      bootstrapArtifactDigest = "sha256:4444444444444444444444444444444444444444444444444444444444444444"
      operation = {
        operationId    = "original-native-bootstrap", liabilityBasisDigest = "sha256:5555555555555555555555555555555555555555555555555555555555555555"
        reservationIds = { infrastructure = "retained-native-infra", cleanup = "retained-native-cleanup" }
      }
      itemId              = "11111111-1111-1111-1111-111111111111"
      expiresAt           = "2026-10-29T00:00:00Z"
      targetId            = "native-target"
      readGrants          = ["agent:dataset-import:native-target/honua_data/pilot_points"]
      datasetImportScopes = ["native-target/honua_data/pilot_points"]
      managedProcessIds   = ["analytics.buffer-aggregate-managed"]
    }
  }
  expect_failures = [var.native_bootstrap]
}

run "reject_native_studio_restore_wildcard" {
  command = plan
  variables {
    native_bootstrap = {
      bootstrapArtifactDigest = "sha256:4444444444444444444444444444444444444444444444444444444444444444"
      operation = {
        operationId    = "original-native-bootstrap", liabilityBasisDigest = "sha256:5555555555555555555555555555555555555555555555555555555555555555"
        reservationIds = { infrastructure = "retained-native-infra", cleanup = "retained-native-cleanup" }
      }
      itemId               = "11111111-1111-1111-1111-111111111111"
      expiresAt            = "2026-10-29T00:00:00Z"
      targetId             = "native-target"
      readGrants           = []
      datasetImportScopes  = ["native-target/honua_data/pilot_points"]
      managedProcessIds    = ["analytics.buffer-aggregate-managed"]
      studioRestoreItemIds = ["*"]
    }
  }
  expect_failures = [var.native_bootstrap]
}


run "reject_native_studio_restore_duplicate" {
  command = plan
  variables {
    native_bootstrap = {
      bootstrapArtifactDigest = "sha256:4444444444444444444444444444444444444444444444444444444444444444"
      operation = {
        operationId    = "original-native-bootstrap", liabilityBasisDigest = "sha256:5555555555555555555555555555555555555555555555555555555555555555"
        reservationIds = { infrastructure = "retained-native-infra", cleanup = "retained-native-cleanup" }
      }
      itemId               = "11111111-1111-1111-1111-111111111111"
      expiresAt            = "2026-10-29T00:00:00Z"
      targetId             = "native-target"
      readGrants           = []
      datasetImportScopes  = ["native-target/honua_data/pilot_points"]
      managedProcessIds    = ["analytics.buffer-aggregate-managed"]
      studioRestoreItemIds = ["11111111-1111-1111-1111-111111111111", "11111111-1111-1111-1111-111111111111"]
    }
  }
  expect_failures = [var.native_bootstrap]
}


run "reject_native_studio_restore_empty_guid" {
  command = plan
  variables {
    native_bootstrap = {
      bootstrapArtifactDigest = "sha256:4444444444444444444444444444444444444444444444444444444444444444"
      operation = {
        operationId    = "original-native-bootstrap", liabilityBasisDigest = "sha256:5555555555555555555555555555555555555555555555555555555555555555"
        reservationIds = { infrastructure = "retained-native-infra", cleanup = "retained-native-cleanup" }
      }
      itemId               = "11111111-1111-1111-1111-111111111111"
      expiresAt            = "2026-10-29T00:00:00Z"
      targetId             = "native-target"
      readGrants           = []
      datasetImportScopes  = ["native-target/honua_data/pilot_points"]
      managedProcessIds    = ["analytics.buffer-aggregate-managed"]
      studioRestoreItemIds = ["00000000-0000-0000-0000-000000000000"]
    }
  }
  expect_failures = [var.native_bootstrap]
}

run "controller_model_runtime_and_counting_member_are_pinned" {
  command = plan
  variables {
    controller_model_resource_arns = [
      "arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.anthropic.offline-v1:0",
      "arn:aws:bedrock:us-east-1::foundation-model/anthropic.offline-v1:0",
      "arn:aws:bedrock:us-west-2::foundation-model/anthropic.offline-v1:0"
    ]
    controller_model = {
      modelId               = "us.anthropic.offline-v1:0", returnedModelId = "offline-v1",
      profileArn            = "arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.anthropic.offline-v1:0",
      tokenCountingModelArn = "arn:aws:bedrock:us-east-1::foundation-model/anthropic.offline-v1:0",
      profileSha256         = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    }
  }
  assert {
    condition = (
      one([for statement in jsondecode(aws_iam_role_policy.controller.policy).Statement : statement if statement.Action == "bedrock:CountTokens"]) == {
        Effect = "Allow", Action = "bedrock:CountTokens", Resource = [var.controller_model.tokenCountingModelArn]
      } &&
      local.controller_observation.model == var.controller_model &&
      one([for row in jsondecode(aws_ecs_task_definition.controller.container_definitions)[0].environment : row.value if row.name == "TERMINAL_MODEL_NAME"]) == var.controller_model.modelId &&
      jsondecode(one([for row in jsondecode(aws_ecs_task_definition.controller.container_definitions)[0].environment : row.value if row.name == "HONUA_CONTROLLER_MODEL_RUNTIME"])) == var.controller_model &&
      !strcontains(aws_ecs_task_definition.fixture.container_definitions, "HONUA_CONTROLLER_MODEL_RUNTIME")
    )
    error_message = "Only the approved private controller may receive the exact profile/counting identity; CountTokens cannot cover profiles, other members or arbitrary models."
  }
}

run "missing_model_runtime_grants_no_counting_or_model_environment" {
  command = plan
  assert {
    condition = (
      !strcontains(aws_iam_role_policy.controller.policy, "bedrock:CountTokens") &&
      !strcontains(aws_ecs_task_definition.controller.container_definitions, "HONUA_CONTROLLER_MODEL_RUNTIME") &&
      !can(local.controller_observation.model)
    )
    error_message = "An unconfigured model runtime cannot receive token-counting permission or an inferred model identity."
  }
}

run "counting_member_cannot_be_an_unreviewed_model" {
  command = plan
  variables {
    controller_model_resource_arns = ["arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.anthropic.offline-v1:0"]
    controller_model = {
      modelId               = "us.anthropic.offline-v1:0", returnedModelId = "offline-v1",
      profileArn            = "arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.anthropic.offline-v1:0",
      tokenCountingModelArn = "arn:aws:bedrock:us-east-1::foundation-model/anthropic.unrelated-v1:0",
      profileSha256         = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    }
  }
  expect_failures = [var.controller_model]
}

run "legacy_native_bootstrap_omits_installed_fields_and_control_custody" {
  command = plan
  variables {
    native_bootstrap = {
      "bootstrapArtifactDigest" : "sha256:4444444444444444444444444444444444444444444444444444444444444444",
      "operation" : {
        "operationId" : "original-native-bootstrap",
        "liabilityBasisDigest" : "sha256:5555555555555555555555555555555555555555555555555555555555555555",
        "reservationIds" : {
          "infrastructure" : "retained-native-infra",
          "cleanup" : "retained-native-cleanup"
        }
      },
      "itemId" : "11111111-1111-1111-1111-111111111111",
      "expiresAt" : "2026-10-29T00:00:00Z",
      "targetId" : "native-target",
      "readGrants" : [],
      "datasetImportScopes" : [
        "native-target/honua_data/pilot_points"
      ],
      "managedProcessIds" : [
        "analytics.buffer-aggregate-managed"
      ]
    }
  }
  assert {
    condition     = (length(aws_secretsmanager_secret.runtime_control) == 0 && !contains(keys(local.native_bootstrap_config), "installedRuntime") && output.installed_runtime_credential_preparation == null && !contains(keys(local.controller_secrets), "HONUA_EXTERNAL_EXECUTION_CONTROL_KEY"))
    error_message = "Installed runtime credential custody must preserve exact original identities, versions and isolation."
  }
}

run "pending_runtime_creates_control_custody_without_injecting_or_activating_it" {
  command = plan
  override_resource {
    override_during = plan
    target          = aws_secretsmanager_secret.runtime_control[0]
    values = {
      arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-pilot-offline-agent/native-agent/control-aBcDeF"
      id  = "arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-pilot-offline-agent/native-agent/control-aBcDeF"
    }
  }
  variables {
    native_bootstrap = {
      "bootstrapArtifactDigest" : "sha256:4444444444444444444444444444444444444444444444444444444444444444",
      "operation" : {
        "operationId" : "original-native-bootstrap",
        "liabilityBasisDigest" : "sha256:5555555555555555555555555555555555555555555555555555555555555555",
        "reservationIds" : {
          "infrastructure" : "retained-native-infra",
          "cleanup" : "retained-native-cleanup"
        }
      },
      "itemId" : "11111111-1111-1111-1111-111111111111",
      "expiresAt" : "2026-10-29T00:00:00Z",
      "targetId" : "native-target",
      "readGrants" : [],
      "datasetImportScopes" : [
        "native-target/honua_data/pilot_points"
      ],
      "managedProcessIds" : [
        "analytics.buffer-aggregate-managed"
      ],
      "installedRuntime" : {
        "runtimeId" : "installed_runtime",
        "mutationCeilings" : {
          "createMap" : false,
          "createDashboard" : false,
          "compose" : false,
          "proposePublication" : false,
          "proposeRestore" : false,
          "datasetImportScopes" : [
            "native-target/honua_data/pilot_points"
          ],
          "managedProcessIds" : [
            "analytics.buffer-aggregate-managed"
          ]
        }
      }
    }
  }
  assert {
    condition     = (length(aws_secretsmanager_secret.runtime_control) == 1 && length(aws_iam_role_policy.issue_runtime_control) == 1 && length(aws_iam_role_policy.inject_runtime_control) == 0 && !contains(keys(local.controller_secrets), "HONUA_EXTERNAL_EXECUTION_CONTROL_KEY") && !output.installed_runtime_credential_preparation.activation && !output.installed_runtime_credential_preparation.qualification && local.native_bootstrap_config.installedRuntime == var.native_bootstrap.installedRuntime && endswith(local.native_bootstrap_secrets.PILOT_FIXTURE_ADMIN_PASSWORD, ":::aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"))
    error_message = "Installed runtime credential custody must preserve exact original identities, versions and isolation."
  }
}

run "actual_control_version_is_controller_only_and_execution_grants_are_exact" {
  command = plan
  override_resource {
    override_during = plan
    target          = aws_secretsmanager_secret.runtime_control[0]
    values = {
      arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-pilot-offline-agent/native-agent/control-aBcDeF"
      id  = "arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-pilot-offline-agent/native-agent/control-aBcDeF"
    }
  }
  variables {
    native_bootstrap = {
      "bootstrapArtifactDigest" : "sha256:4444444444444444444444444444444444444444444444444444444444444444",
      "operation" : {
        "operationId" : "original-native-bootstrap",
        "liabilityBasisDigest" : "sha256:5555555555555555555555555555555555555555555555555555555555555555",
        "reservationIds" : {
          "infrastructure" : "retained-native-infra",
          "cleanup" : "retained-native-cleanup"
        }
      },
      "itemId" : "11111111-1111-1111-1111-111111111111",
      "expiresAt" : "2026-10-29T00:00:00Z",
      "targetId" : "native-target",
      "readGrants" : [],
      "datasetImportScopes" : [
        "native-target/honua_data/pilot_points"
      ],
      "managedProcessIds" : [
        "analytics.buffer-aggregate-managed"
      ],
      "installedRuntime" : {
        "runtimeId" : "installed_runtime",
        "mutationCeilings" : {
          "createMap" : false,
          "createDashboard" : false,
          "compose" : false,
          "proposePublication" : false,
          "proposeRestore" : false,
          "datasetImportScopes" : [
            "native-target/honua_data/pilot_points"
          ],
          "managedProcessIds" : [
            "analytics.buffer-aggregate-managed"
          ]
        }
      }
    }
    issued_native_keys = {
      "setup" : {
        "apiKeyId" : "11111111-1111-1111-1111-111111111111",
        "versionId" : "1111111111111111111111111111111111111111111111111111111111111111"
      },
      "observer" : {
        "apiKeyId" : "22222222-2222-2222-2222-222222222222",
        "versionId" : "2222222222222222222222222222222222222222222222222222222222222222"
      },
      "agent" : {
        "apiKeyId" : "33333333-3333-3333-3333-333333333333",
        "versionId" : "3333333333333333333333333333333333333333333333333333333333333333"
      }
    }
    issued_runtime_control_key = {
      "apiKeyId" : "44444444-4444-4444-4444-444444444444",
      "versionId" : "4444444444444444444444444444444444444444444444444444444444444444"
    }
  }
  assert {
    condition     = (local.controller_secrets.HONUA_EXTERNAL_EXECUTION_CONTROL_KEY == "${aws_secretsmanager_secret.runtime_control[0].arn}:::${var.issued_runtime_control_key.versionId}" && length(jsondecode(aws_iam_role_policy.inject_runtime_control[0].policy).Statement) == 2 && alltrue([for row in jsondecode(aws_iam_role_policy.inject_runtime_control[0].policy).Statement : row.Resource == aws_secretsmanager_secret.runtime_control[0].arn && row.Action == "secretsmanager:GetSecretValue"]) && jsondecode(aws_iam_role_policy.inject_runtime_control[0].policy).Statement[0].Condition.StringEquals["secretsmanager:VersionId"] == var.issued_runtime_control_key.versionId && jsondecode(aws_iam_role_policy.inject_runtime_control[0].policy).Statement[1].Condition.StringNotEquals["secretsmanager:VersionId"] == var.issued_runtime_control_key.versionId && !strcontains(aws_iam_role_policy.execution["controller"].policy, aws_secretsmanager_secret.runtime_control[0].arn) && !contains(keys(local.fixture_secrets), "HONUA_EXTERNAL_EXECUTION_CONTROL_KEY") && !contains(keys(local.controller_secrets), "PILOT_FIXTURE_ADMIN_PASSWORD") && !contains(keys(local.controller_secrets), "PILOT_FIXTURE_MASTER_CONNECTION"))
    error_message = "Installed runtime credential custody must preserve exact original identities, versions and isolation."
  }
}

run "control_key_without_original_runtime_cannot_be_ignored" {
  command = plan
  variables {
    issued_runtime_control_key = {
      "apiKeyId" : "44444444-4444-4444-4444-444444444444",
      "versionId" : "4444444444444444444444444444444444444444444444444444444444444444"
    }
  }
  expect_failures = [output.installed_runtime_credential_preparation]
}

run "task_key_cannot_be_reused_as_control_key" {
  command = plan
  override_resource {
    override_during = plan
    target          = aws_secretsmanager_secret.runtime_control[0]
    values = {
      arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-pilot-offline-agent/native-agent/control-aBcDeF"
      id  = "arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-pilot-offline-agent/native-agent/control-aBcDeF"
    }
  }
  variables {
    native_bootstrap = {
      "bootstrapArtifactDigest" : "sha256:4444444444444444444444444444444444444444444444444444444444444444",
      "operation" : {
        "operationId" : "original-native-bootstrap",
        "liabilityBasisDigest" : "sha256:5555555555555555555555555555555555555555555555555555555555555555",
        "reservationIds" : {
          "infrastructure" : "retained-native-infra",
          "cleanup" : "retained-native-cleanup"
        }
      },
      "itemId" : "11111111-1111-1111-1111-111111111111",
      "expiresAt" : "2026-10-29T00:00:00Z",
      "targetId" : "native-target",
      "readGrants" : [],
      "datasetImportScopes" : [
        "native-target/honua_data/pilot_points"
      ],
      "managedProcessIds" : [
        "analytics.buffer-aggregate-managed"
      ],
      "installedRuntime" : {
        "runtimeId" : "installed_runtime",
        "mutationCeilings" : {
          "createMap" : false,
          "createDashboard" : false,
          "compose" : false,
          "proposePublication" : false,
          "proposeRestore" : false,
          "datasetImportScopes" : [
            "native-target/honua_data/pilot_points"
          ],
          "managedProcessIds" : [
            "analytics.buffer-aggregate-managed"
          ]
        }
      }
    }
    issued_native_keys = {
      "setup" : {
        "apiKeyId" : "11111111-1111-1111-1111-111111111111",
        "versionId" : "1111111111111111111111111111111111111111111111111111111111111111"
      },
      "observer" : {
        "apiKeyId" : "22222222-2222-2222-2222-222222222222",
        "versionId" : "2222222222222222222222222222222222222222222222222222222222222222"
      },
      "agent" : {
        "apiKeyId" : "33333333-3333-3333-3333-333333333333",
        "versionId" : "3333333333333333333333333333333333333333333333333333333333333333"
      }
    }
    issued_runtime_control_key = {
      "apiKeyId" : "33333333-3333-3333-3333-333333333333",
      "versionId" : "3333333333333333333333333333333333333333333333333333333333333333"
    }
  }
  expect_failures = [aws_secretsmanager_secret.runtime_control[0]]
}

run "installed_import_ceiling_must_preserve_original_exact_destinations" {
  command = plan
  variables {
    native_bootstrap = {
      "bootstrapArtifactDigest" : "sha256:4444444444444444444444444444444444444444444444444444444444444444",
      "operation" : {
        "operationId" : "original-native-bootstrap",
        "liabilityBasisDigest" : "sha256:5555555555555555555555555555555555555555555555555555555555555555",
        "reservationIds" : {
          "infrastructure" : "retained-native-infra",
          "cleanup" : "retained-native-cleanup"
        }
      },
      "itemId" : "11111111-1111-1111-1111-111111111111",
      "expiresAt" : "2026-10-29T00:00:00Z",
      "targetId" : "native-target",
      "readGrants" : [],
      "datasetImportScopes" : [
        "native-target/honua_data/pilot_points"
      ],
      "managedProcessIds" : [
        "analytics.buffer-aggregate-managed"
      ],
      "installedRuntime" : {
        "runtimeId" : "installed_runtime",
        "mutationCeilings" : {
          "createMap" : false,
          "createDashboard" : false,
          "compose" : false,
          "proposePublication" : false,
          "proposeRestore" : false,
          "datasetImportScopes" : [],
          "managedProcessIds" : [
            "analytics.buffer-aggregate-managed"
          ]
        }
      }
    }
  }
  expect_failures = [var.native_bootstrap]
}

run "no_registrar_without_explicit_original_inputs" {
  command = plan
  assert {
    condition     = (length(aws_ecs_task_definition.runtime_registration) == 0 && length(aws_sfn_state_machine.runtime_registration) == 0 && length(aws_scheduler_schedule.registration_reaper) == 0 && length(aws_iam_role_policy.dispatch_runtime_registration) == 0 && output.native_runtime_registration_handoff == null)
    error_message = "Default staging must create no privileged registrar or paid launch authority."
  }
}
run "registrar_keeps_original_versions_deadline_and_separate_reaper" {
  command = plan
  override_resource {
    override_during = plan
    target          = aws_scheduler_schedule.registration_reaper[0]
    values          = { arn = "arn:aws:scheduler:us-east-1:123456789012:schedule/honua-pilot-offline-agent-registration/honua-pilot-offline-agent-registration-reaper" }
  }
  override_resource {
    override_during = plan
    target          = aws_sfn_state_machine.runtime_registration[0]
    values          = { arn = "arn:aws:states:us-east-1:123456789012:stateMachine:honua-pilot-offline-agent-runtime-registration", state_machine_version_arn = "arn:aws:states:us-east-1:123456789012:stateMachine:honua-pilot-offline-agent-runtime-registration:1" }
  }
  override_resource {
    override_during = plan
    target          = aws_sfn_state_machine.registration_reaper[0]
    values          = { arn = "arn:aws:states:us-east-1:123456789012:stateMachine:honua-pilot-offline-agent-runtime-registration-reaper", state_machine_version_arn = "arn:aws:states:us-east-1:123456789012:stateMachine:honua-pilot-offline-agent-runtime-registration-reaper:1" }
  }
  override_resource {
    override_during = plan
    target          = aws_ecs_task_definition.runtime_registration[0]
    values          = { arn = "arn:aws:ecs:us-east-1:123456789012:task-definition/honua-pilot-offline-agent-runtime-registration:1" }
  }
  override_resource {
    override_during = plan
    target          = aws_secretsmanager_secret.runtime_control[0]
    values = {
      arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-pilot-offline-agent/native-agent/control-aBcDeF"
      id  = "arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-pilot-offline-agent/native-agent/control-aBcDeF"
    }
  }
  variables {
    native_bootstrap = {
      "bootstrapArtifactDigest" : "sha256:4444444444444444444444444444444444444444444444444444444444444444",
      "operation" : {
        "operationId" : "original-native-bootstrap",
        "liabilityBasisDigest" : "sha256:5555555555555555555555555555555555555555555555555555555555555555",
        "reservationIds" : {
          "infrastructure" : "retained-native-infra",
          "cleanup" : "retained-native-cleanup"
        }
      },
      "itemId" : "11111111-1111-1111-1111-111111111111",
      "expiresAt" : "2026-10-29T00:00:00Z",
      "targetId" : "native-target",
      "readGrants" : [],
      "datasetImportScopes" : [
        "native-target/honua_data/pilot_points"
      ],
      "managedProcessIds" : [
        "analytics.buffer-aggregate-managed"
      ],
      "installedRuntime" : {
        "runtimeId" : "installed_runtime",
        "mutationCeilings" : {
          "createMap" : false,
          "createDashboard" : false,
          "compose" : false,
          "proposePublication" : false,
          "proposeRestore" : false,
          "datasetImportScopes" : [
            "native-target/honua_data/pilot_points"
          ],
          "managedProcessIds" : [
            "analytics.buffer-aggregate-managed"
          ]
        }
      }
    }
    issued_native_keys = {
      "setup" : {
        "apiKeyId" : "11111111-1111-1111-1111-111111111111",
        "versionId" : "1111111111111111111111111111111111111111111111111111111111111111"
      },
      "observer" : {
        "apiKeyId" : "22222222-2222-2222-2222-222222222222",
        "versionId" : "2222222222222222222222222222222222222222222222222222222222222222"
      },
      "agent" : {
        "apiKeyId" : "33333333-3333-3333-3333-333333333333",
        "versionId" : "3333333333333333333333333333333333333333333333333333333333333333"
      }
    }
    issued_runtime_control_key = {
      "apiKeyId" : "44444444-4444-4444-4444-444444444444",
      "versionId" : "4444444444444444444444444444444444444444444444444444444444444444"
    }
    worker_attempt_ids = [
      "fixed-slot"
    ]
    native_runtime_registration = {
      "originalFundingExpiresEpoch" : 1793232000,
      "bootstrapArtifactDigest" : "sha256:4444444444444444444444444444444444444444444444444444444444444444",
      "operation" : {
        "operationId" : "runtime-registration-installed_runtime",
        "liabilityBasisDigest" : "sha256:13a08d89c403e92bce085ae728f82c0fba5a9ec347ad3361b3f0db416780daa9",
        "reservationIds" : {
          "infrastructure" : "registrar-original-infra",
          "cleanup" : "registrar-original-cleanup"
        }
      },
      "declaration" : {
        "path" : "certification/agent-e2e/installed-runtimes/installed_runtime.json",
        "sha256" : "sha256:6666666666666666666666666666666666666666666666666666666666666666"
      },
      "maximumUsd" : {
        "infrastructure" : "0.30",
        "cleanup" : "0.20"
      },
      "maxExecutionSeconds" : 600,
      "liabilityQuote" : {
        "path" : "certification/hosted-pilot/quotes/registrar-mock.json",
        "sha256" : "sha256:7777777777777777777777777777777777777777777777777777777777777777"
      }
    }
  }
  assert {
    condition = (
      aws_ecs_task_definition.runtime_registration[0].cpu == "256" && aws_ecs_task_definition.runtime_registration[0].memory == "1024" &&
      jsondecode(aws_ecs_task_definition.runtime_registration[0].container_definitions)[0].command == ["python", "tools/hosted_pilot/native_runtime_registration.py"] &&
      length(jsondecode(aws_ecs_task_definition.runtime_registration[0].container_definitions)[0].secrets) == 3 &&
      alltrue([for ref in jsondecode(aws_ecs_task_definition.runtime_registration[0].container_definitions)[0].secrets : can(regex(":::[a-z0-9]{32,64}$", ref.valueFrom))]) &&
      !contains(keys(local.controller_secrets), "PILOT_FIXTURE_ADMIN_PASSWORD") &&
      jsondecode(aws_sfn_state_machine.runtime_registration[0].definition).States.RunRegistrar.Arguments.ClientToken == "{% $substring($states.input.installation.configurationDigest,7) %}" &&
      !contains(keys(jsondecode(aws_sfn_state_machine.runtime_registration[0].definition).States.RunRegistrar), "Retry") &&
      jsondecode(aws_sfn_state_machine.runtime_registration[0].definition).States.Installation.Output.installation.originalFundingExpiresEpoch == var.native_runtime_registration.originalFundingExpiresEpoch &&
      jsondecode(aws_sfn_state_machine.registration_reaper[0].definition).States.Installation.Output.installation == local.registration_installation &&
      aws_scheduler_schedule.registration_reaper[0].end_date == var.expires_at && aws_scheduler_schedule.registration_reaper[0].flexible_time_window[0].mode == "OFF" &&
      aws_scheduler_schedule.registration_reaper[0].target[0].retry_policy[0].maximum_retry_attempts == 0 &&
      !strcontains(aws_iam_role_policy.registration_reaper[0].policy, "dynamodb:UpdateItem") &&
      !strcontains(aws_iam_role_policy.registration_reaper[0].policy, "secretsmanager:") &&
      jsondecode(aws_iam_role_policy.dispatch_runtime_registration[0].policy).Statement[0].Condition.DateLessThan["aws:CurrentTime"] == var.native_bootstrap.expiresAt &&
      jsondecode(aws_iam_role_policy.dispatch_runtime_registration[0].policy).Statement[0].Resource == aws_sfn_state_machine.runtime_registration[0].arn &&
      jsondecode(aws_iam_role_policy.dispatch_runtime_registration[0].policy).Statement[0].Condition["ForAnyValue:StringEquals"]["states:StateMachineQualifier"] == element(reverse(split(":", aws_sfn_state_machine.runtime_registration[0].state_machine_version_arn)), 0) &&
      jsondecode(aws_iam_role_policy.registration_schedule[0].policy).Statement[0].Resource == aws_sfn_state_machine.registration_reaper[0].arn &&
      jsondecode(aws_iam_role_policy.registration_schedule[0].policy).Statement[0].Condition["ForAnyValue:StringEquals"]["states:StateMachineQualifier"] == element(reverse(split(":", aws_sfn_state_machine.registration_reaper[0].state_machine_version_arn)), 0)
    )
    error_message = "Registrar must keep immutable credential versions, original admission expiry and a separate stop-only reaper."
  }
}

run "registrar_forged_basis_refused" {
  command = plan
  override_resource {
    override_during = plan
    target          = aws_secretsmanager_secret.runtime_control[0]
    values = {
      arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-pilot-offline-agent/native-agent/control-aBcDeF"
      id  = "arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-pilot-offline-agent/native-agent/control-aBcDeF"
    }
  }
  variables {
    native_bootstrap = {
      "bootstrapArtifactDigest" : "sha256:4444444444444444444444444444444444444444444444444444444444444444",
      "operation" : {
        "operationId" : "original-native-bootstrap",
        "liabilityBasisDigest" : "sha256:5555555555555555555555555555555555555555555555555555555555555555",
        "reservationIds" : {
          "infrastructure" : "retained-native-infra",
          "cleanup" : "retained-native-cleanup"
        }
      },
      "itemId" : "11111111-1111-1111-1111-111111111111",
      "expiresAt" : "2026-10-29T00:00:00Z",
      "targetId" : "native-target",
      "readGrants" : [],
      "datasetImportScopes" : [
        "native-target/honua_data/pilot_points"
      ],
      "managedProcessIds" : [
        "analytics.buffer-aggregate-managed"
      ],
      "installedRuntime" : {
        "runtimeId" : "installed_runtime",
        "mutationCeilings" : {
          "createMap" : false,
          "createDashboard" : false,
          "compose" : false,
          "proposePublication" : false,
          "proposeRestore" : false,
          "datasetImportScopes" : [
            "native-target/honua_data/pilot_points"
          ],
          "managedProcessIds" : [
            "analytics.buffer-aggregate-managed"
          ]
        }
      }
    }
    issued_native_keys = {
      "setup" : {
        "apiKeyId" : "11111111-1111-1111-1111-111111111111",
        "versionId" : "1111111111111111111111111111111111111111111111111111111111111111"
      },
      "observer" : {
        "apiKeyId" : "22222222-2222-2222-2222-222222222222",
        "versionId" : "2222222222222222222222222222222222222222222222222222222222222222"
      },
      "agent" : {
        "apiKeyId" : "33333333-3333-3333-3333-333333333333",
        "versionId" : "3333333333333333333333333333333333333333333333333333333333333333"
      }
    }
    issued_runtime_control_key = {
      "apiKeyId" : "44444444-4444-4444-4444-444444444444",
      "versionId" : "4444444444444444444444444444444444444444444444444444444444444444"
    }
    worker_attempt_ids = [
      "fixed-slot"
    ]
    native_runtime_registration = {
      "originalFundingExpiresEpoch" : 1793232000,
      "bootstrapArtifactDigest" : "sha256:4444444444444444444444444444444444444444444444444444444444444444",
      "operation" : {
        "operationId" : "runtime-registration-installed_runtime",
        "liabilityBasisDigest" : "sha256:0000000000000000000000000000000000000000000000000000000000000000",
        "reservationIds" : {
          "infrastructure" : "registrar-original-infra",
          "cleanup" : "registrar-original-cleanup"
        }
      },
      "declaration" : {
        "path" : "certification/agent-e2e/installed-runtimes/installed_runtime.json",
        "sha256" : "sha256:6666666666666666666666666666666666666666666666666666666666666666"
      },
      "maximumUsd" : {
        "infrastructure" : "0.30",
        "cleanup" : "0.20"
      },
      "maxExecutionSeconds" : 600,
      "liabilityQuote" : {
        "path" : "certification/hosted-pilot/quotes/registrar-mock.json",
        "sha256" : "sha256:7777777777777777777777777777777777777777777777777777777777777777"
      }
    }
  }
  expect_failures = [aws_ecs_task_definition.runtime_registration[0]]
}

run "registrar_cannot_renew_original_funding" {
  command = plan
  override_resource {
    override_during = plan
    target          = aws_secretsmanager_secret.runtime_control[0]
    values = {
      arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-pilot-offline-agent/native-agent/control-aBcDeF"
      id  = "arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-pilot-offline-agent/native-agent/control-aBcDeF"
    }
  }
  variables {
    native_bootstrap = {
      "bootstrapArtifactDigest" : "sha256:4444444444444444444444444444444444444444444444444444444444444444",
      "operation" : {
        "operationId" : "original-native-bootstrap",
        "liabilityBasisDigest" : "sha256:5555555555555555555555555555555555555555555555555555555555555555",
        "reservationIds" : {
          "infrastructure" : "retained-native-infra",
          "cleanup" : "retained-native-cleanup"
        }
      },
      "itemId" : "11111111-1111-1111-1111-111111111111",
      "expiresAt" : "2026-10-29T00:00:00Z",
      "targetId" : "native-target",
      "readGrants" : [],
      "datasetImportScopes" : [
        "native-target/honua_data/pilot_points"
      ],
      "managedProcessIds" : [
        "analytics.buffer-aggregate-managed"
      ],
      "installedRuntime" : {
        "runtimeId" : "installed_runtime",
        "mutationCeilings" : {
          "createMap" : false,
          "createDashboard" : false,
          "compose" : false,
          "proposePublication" : false,
          "proposeRestore" : false,
          "datasetImportScopes" : [
            "native-target/honua_data/pilot_points"
          ],
          "managedProcessIds" : [
            "analytics.buffer-aggregate-managed"
          ]
        }
      }
    }
    issued_native_keys = {
      "setup" : {
        "apiKeyId" : "11111111-1111-1111-1111-111111111111",
        "versionId" : "1111111111111111111111111111111111111111111111111111111111111111"
      },
      "observer" : {
        "apiKeyId" : "22222222-2222-2222-2222-222222222222",
        "versionId" : "2222222222222222222222222222222222222222222222222222222222222222"
      },
      "agent" : {
        "apiKeyId" : "33333333-3333-3333-3333-333333333333",
        "versionId" : "3333333333333333333333333333333333333333333333333333333333333333"
      }
    }
    issued_runtime_control_key = {
      "apiKeyId" : "44444444-4444-4444-4444-444444444444",
      "versionId" : "4444444444444444444444444444444444444444444444444444444444444444"
    }
    worker_attempt_ids = [
      "fixed-slot"
    ]
    native_runtime_registration = {
      "originalFundingExpiresEpoch" : 1793232060,
      "bootstrapArtifactDigest" : "sha256:4444444444444444444444444444444444444444444444444444444444444444",
      "operation" : {
        "operationId" : "runtime-registration-installed_runtime",
        "liabilityBasisDigest" : "sha256:13a08d89c403e92bce085ae728f82c0fba5a9ec347ad3361b3f0db416780daa9",
        "reservationIds" : {
          "infrastructure" : "registrar-original-infra",
          "cleanup" : "registrar-original-cleanup"
        }
      },
      "declaration" : {
        "path" : "certification/agent-e2e/installed-runtimes/installed_runtime.json",
        "sha256" : "sha256:6666666666666666666666666666666666666666666666666666666666666666"
      },
      "maximumUsd" : {
        "infrastructure" : "0.30",
        "cleanup" : "0.20"
      },
      "maxExecutionSeconds" : 600,
      "liabilityQuote" : {
        "path" : "certification/hosted-pilot/quotes/registrar-mock.json",
        "sha256" : "sha256:7777777777777777777777777777777777777777777777777777777777777777"
      }
    }
  }
  expect_failures = [aws_ecs_task_definition.runtime_registration[0]]
}
