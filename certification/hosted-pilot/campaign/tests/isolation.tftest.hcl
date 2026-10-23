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
      one([for statement in jsondecode(aws_iam_role_policy.dispatcher.policy).Statement : statement if statement.Action == "states:StartExecution"]).Resource == aws_sfn_state_machine.controller.state_machine_version_arn &&
      alltrue([for table in aws_dynamodb_table.claims : table.deletion_protection_enabled && table.point_in_time_recovery[0].enabled]) &&
      output.fixture_staging.modelReady == false &&
      output.fixture_staging.nativeAgentConfigured == false
    )
    error_message = "Dispatch must use one published bounded no-redrive task, retain immutable claims and refuse missing native readiness."
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
      itemId     = "11111111-1111-1111-1111-111111111111"
      expiresAt  = "2026-10-29T00:00:00Z"
      readGrants = []
    }
  }
  assert {
    condition = (
      tolist(jsondecode(aws_ecs_task_definition.native_bootstrap[0].container_definitions)[0].command) == tolist(["python", "tools/hosted_pilot/native_bootstrap.py"]) &&
      jsondecode(aws_ecs_task_definition.native_bootstrap[0].container_definitions)[0].readonlyRootFilesystem &&
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
      itemId     = "11111111-1111-1111-1111-111111111111"
      expiresAt  = "2026-10-29T00:00:00Z"
      readGrants = []
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
run "worker_custody_read_has_only_exact_declared_roles_and_families" {
  command = plan
  variables { worker_attempt_ids = ["p14-first", "p14-second"] }
  assert {
    condition = aws_iam_role_policy.worker_custody_observer[0].policy == jsonencode({ Version = "2012-10-17", Statement = [
      { Effect = "Allow", Action = ["iam:GetRolePolicy", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies"],
      Resource = ["arn:aws:iam::123456789012:role/honua-agent-p14-first-worker", "arn:aws:iam::123456789012:role/honua-agent-p14-second-worker"] },
      { Effect = "Allow", Action = "ecs:DescribeTaskDefinition",
      Resource = ["arn:aws:ecs:us-east-1:123456789012:task-definition/honua-agent-p14-first:*", "arn:aws:ecs:us-east-1:123456789012:task-definition/honua-agent-p14-second:*"] }
    ] })
    error_message = "Custody may read only the two declared worker policies/task families, without launch, secret, other-role or wildcard IAM authority."
  }
}
run "worker_custody_attempt_cannot_expand_to_a_role_wildcard" {
  command = plan
  variables { worker_attempt_ids = ["*"] }
  expect_failures = [var.worker_attempt_ids]
}
