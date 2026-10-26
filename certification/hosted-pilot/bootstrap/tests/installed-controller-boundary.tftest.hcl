mock_provider "aws" {
  override_during = plan
  mock_resource "aws_s3_bucket" {
    defaults = { arn = "arn:aws:s3:::offline-custody" }
  }
  mock_data "aws_caller_identity" { defaults = { account_id = "123456789012" } }
  mock_resource "aws_kms_key" {
    defaults = { arn = "arn:aws:kms:us-west-2:123456789012:key/12345678-1234-1234-1234-123456789012" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/offline" }
  }
  mock_resource "aws_iam_policy" {
    defaults = { arn = "arn:aws:iam::123456789012:policy/offline-boundary" }
  }
  mock_resource "aws_ecr_repository" {
    defaults = {
      arn            = "arn:aws:ecr:us-west-2:123456789012:repository/honua-pilot-offline-trusted-controller"
      repository_url = "123456789012.dkr.ecr.us-west-2.amazonaws.com/honua-pilot-offline-trusted-controller"
    }
  }
}
variables {
  account_id                = "123456789012"
  region                    = "us-west-2"
  owner                     = "offline-test"
  name_prefix               = "honua-pilot-offline"
  expires_at                = "2026-10-30T00:00:00Z"
  oidc_provider_arn         = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com"
  runtime_secret_arns       = ["arn:aws:secretsmanager:us-west-2:123456789012:secret:independent-example-123456"]
  provision_policy_document = "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":[\"ec2:CreateVpc\"],\"Resource\":\"*\"}]}"
  budget_ledger = {
    schema                     = "honua.hosted-pilot-budget/v1"
    expires_at                 = "2026-10-30T00:00:00Z"
    source_sha                 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    config_sha256              = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    total_budget_usd           = 2000
    cleanup_reserve_usd        = 200
    spent_usd                  = 0
    reserved_usd               = 0
    deployment_reservation_usd = 100
    active_paid_attempts       = 0
  }
  enable_agent_controller        = true
  controller_model_resource_arns = ["arn:aws:bedrock:us-west-2::foundation-model/anthropic.offline-test"]
  installed_controller_boundary = {
    "candidate_key" : "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "runtime_id" : "demo",
    "funding_expires_epoch" : 1793318400,
    "cluster_arn" : "arn:aws:ecs:us-west-2:123456789012:cluster/honua-pilot-offline-cluster",
    "control" : {
      "secret_arn" : "arn:aws:secretsmanager:us-west-2:123456789012:secret:honua-pilot-offline-agent/native-agent/control-AbCdEf",
      "version_id" : "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
    },
    "workers" : [
      {
        "slot_id" : "fixed-slot",
        "state_machine_arn" : "arn:aws:states:us-west-2:123456789012:stateMachine:honua-agent-fixed-slot",
        "task_role_arn" : "arn:aws:iam::123456789012:role/honua-agent-fixed-slot-worker",
        "command_queue_arn" : "arn:aws:sqs:us-west-2:123456789012:honua-agent-fixed-slot-commands.fifo",
        "reply_queue_arn" : "arn:aws:sqs:us-west-2:123456789012:honua-agent-fixed-slot-replies.fifo"
      }
    ]
  }
}

run "default_installs_no_new_cap" {
  command = plan
  variables { installed_controller_boundary = null }
  assert {
    condition     = length(aws_iam_policy.installed_controller_boundary) == 0 && length(aws_iam_role_policy.installed_controller_identity_output) == 0 && length(local.workload_boundary_arns) == 3
    error_message = "Default controller/worker caps must be unchanged."
  }
}
run "exact_installed_cap_keeps_authority_and_retirement_scopes" {
  command = plan
  override_resource {
    target          = aws_iam_policy.installed_controller_boundary[0]
    override_during = plan
    values          = { arn = "arn:aws:iam::123456789012:policy/honua-release-pilot-offline-foundation-installed-controller-boundary" }
  }
  assert {
    condition = (
      length(base64encode(aws_iam_policy.installed_controller_boundary[0].policy)) <= 8192 &&
      length(aws_iam_role_policy.installed_controller_identity_output) == 1 &&
      toset(one(jsondecode(aws_iam_role_policy.installed_controller_identity_output[0].policy).Statement).Resource) == toset([for name in ["review", "identity"] : "arn:aws:s3:::honua-agent-aaaaaaaaaaaaaaaaaaaaaaaa-123456789012/agent-e2e/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/installed-controllers/demo/identity/*/*/${name}.json"]) &&
      one(jsondecode(aws_iam_role_policy.installed_controller_identity_output[0].policy).Statement).Action == "s3:PutObject" &&
      one(jsondecode(aws_iam_role_policy.installed_controller_identity_output[0].policy).Statement).Condition.StringEquals["s3:if-none-match"] == "*" &&
      one(jsondecode(aws_iam_role_policy.installed_controller_identity_output[0].policy).Statement).Condition.StringEquals["s3:x-amz-server-side-encryption"] == "AES256" &&
      contains(local.installed_cap_roles, "arn:aws:iam::123456789012:role/honua-pilot-offline-agent-installed-controller") &&
      contains(local.installed_cap_roles, "arn:aws:iam::123456789012:role/honua-pilot-offline-agent-installed-controller-execution") &&
      local.installed_cap_supervisor == "honua-pilot-offline-agent-installed-controller" &&
      contains(local.installed_cap_observation_resources, "arn:aws:logs:us-west-2:123456789012:log-group:/ecs/honua-pilot-offline-agent-installed-controller:*") &&
      contains(local.workload_boundary_arns, aws_iam_policy.installed_controller_boundary[0].arn) &&
      alltrue([for row in jsondecode(local.installed_cap_policy).Statement : !contains(flatten([row.Action]), "iam:PassRole") && !contains(flatten([row.Action]), "ecs:RunTask") && !contains(flatten([row.Action]), "ecs:StopTask") && !contains(flatten([row.Action]), "dynamodb:DeleteItem") if row.Effect == "Allow"]) &&
      toset([for resource in one([for row in jsondecode(local.installed_cap_policy).Statement : row.Resource if row.Effect == "Allow" && contains(flatten([row.Action]), "sqs:SendMessage")]) : resource if startswith(resource, "arn:aws:sqs:")]) == toset([var.installed_controller_boundary.workers[0].command_queue_arn]) &&
      one([for row in jsondecode(local.installed_cap_policy).Statement : row.Resource if row.Effect == "Allow" && contains(flatten([row.Action]), "sqs:ReceiveMessage")]) == [var.installed_controller_boundary.workers[0].reply_queue_arn] &&
      toset([for resource in one([for row in jsondecode(local.installed_cap_policy).Statement : row.Resource if row.Effect == "Allow" && contains(flatten([row.Action]), "states:StartExecution")]) : resource if startswith(resource, "arn:aws:states:")]) == toset([var.installed_controller_boundary.workers[0].state_machine_arn]) &&
      toset([for resource in one([for row in jsondecode(local.installed_cap_policy).Statement : row.Resource if row.Effect == "Allow" && contains(flatten([row.Action]), "iam:GetRole")]) : resource if startswith(resource, "arn:aws:iam:")]) == toset(local.installed_cap_roles) &&
      toset([for resource in one([for row in jsondecode(local.installed_cap_policy).Statement : row.Resource if row.Effect == "Allow" && contains(flatten([row.Action]), "bedrock:InvokeModel")]) : resource if startswith(resource, "arn:aws:bedrock:")]) == toset(var.controller_model_resource_arns) &&
      one([for row in jsondecode(local.installed_cap_policy).Statement : row.Condition.StringEquals["dynamodb:EnclosingOperation"] if row.Effect == "Allow" && contains(flatten([row.Action]), "dynamodb:PutItem")]) == "TransactWriteItems" &&
      one([for row in jsondecode(local.installed_cap_policy).Statement : row.Resource if row.Effect == "Deny" && contains(flatten([row.Action]), "kms:*")]) == aws_kms_key.custody.arn &&
      one([for row in jsondecode(local.installed_cap_policy).Statement : row.Condition.StringEquals["secretsmanager:VersionId"] if row.Effect == "Allow" && contains(flatten([row.Action]), "secretsmanager:GetSecretValue")]) == var.installed_controller_boundary.control.version_id
    )
    error_message = "Installed cap must remain exact, original-expiry bound and mutation-free except fenced state/retirement."
  }
}

run "foreign_worker_refused" {
  command = plan
  variables { installed_controller_boundary = {
    "candidate_key" : "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "runtime_id" : "demo",
    "funding_expires_epoch" : 1793318400,
    "cluster_arn" : "arn:aws:ecs:us-west-2:123456789012:cluster/honua-pilot-offline-cluster",
    "control" : {
      "secret_arn" : "arn:aws:secretsmanager:us-west-2:123456789012:secret:honua-pilot-offline-agent/native-agent/control-AbCdEf",
      "version_id" : "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
    },
    "workers" : [
      {
        "slot_id" : "fixed-slot",
        "state_machine_arn" : "arn:aws:states:us-west-2:123456789012:stateMachine:honua-agent-fixed-slot",
        "task_role_arn" : "arn:aws:iam::999999999999:role/honua-agent-fixed-slot-worker",
        "command_queue_arn" : "arn:aws:sqs:us-west-2:123456789012:honua-agent-fixed-slot-commands.fifo",
        "reply_queue_arn" : "arn:aws:sqs:us-west-2:123456789012:honua-agent-fixed-slot-replies.fifo"
      }
    ]
  } }
  expect_failures = [var.installed_controller_boundary]
}

run "original_expiry_cannot_extend" {
  command = plan
  variables { installed_controller_boundary = {
    "candidate_key" : "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "runtime_id" : "demo",
    "funding_expires_epoch" : 1793318401,
    "cluster_arn" : "arn:aws:ecs:us-west-2:123456789012:cluster/honua-pilot-offline-cluster",
    "control" : {
      "secret_arn" : "arn:aws:secretsmanager:us-west-2:123456789012:secret:honua-pilot-offline-agent/native-agent/control-AbCdEf",
      "version_id" : "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
    },
    "workers" : [
      {
        "slot_id" : "fixed-slot",
        "state_machine_arn" : "arn:aws:states:us-west-2:123456789012:stateMachine:honua-agent-fixed-slot",
        "task_role_arn" : "arn:aws:iam::123456789012:role/honua-agent-fixed-slot-worker",
        "command_queue_arn" : "arn:aws:sqs:us-west-2:123456789012:honua-agent-fixed-slot-commands.fifo",
        "reply_queue_arn" : "arn:aws:sqs:us-west-2:123456789012:honua-agent-fixed-slot-replies.fifo"
      }
    ]
  } }
  expect_failures = [aws_iam_policy.installed_controller_boundary[0]]
}

run "oversized_cap_refuses_instead_of_broadening" {
  command = plan
  variables { controller_model_resource_arns = ["arn:aws:bedrock:us-west-2::foundation-model/anthropic.0.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "arn:aws:bedrock:us-west-2::foundation-model/anthropic.1.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "arn:aws:bedrock:us-west-2::foundation-model/anthropic.2.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "arn:aws:bedrock:us-west-2::foundation-model/anthropic.3.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "arn:aws:bedrock:us-west-2::foundation-model/anthropic.4.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "arn:aws:bedrock:us-west-2::foundation-model/anthropic.5.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "arn:aws:bedrock:us-west-2::foundation-model/anthropic.6.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "arn:aws:bedrock:us-west-2::foundation-model/anthropic.7.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "arn:aws:bedrock:us-west-2::foundation-model/anthropic.8.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "arn:aws:bedrock:us-west-2::foundation-model/anthropic.9.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "arn:aws:bedrock:us-west-2::foundation-model/anthropic.10.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "arn:aws:bedrock:us-west-2::foundation-model/anthropic.11.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "arn:aws:bedrock:us-west-2::foundation-model/anthropic.12.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "arn:aws:bedrock:us-west-2::foundation-model/anthropic.13.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "arn:aws:bedrock:us-west-2::foundation-model/anthropic.14.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "arn:aws:bedrock:us-west-2::foundation-model/anthropic.15.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "arn:aws:bedrock:us-west-2::foundation-model/anthropic.16.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "arn:aws:bedrock:us-west-2::foundation-model/anthropic.17.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "arn:aws:bedrock:us-west-2::foundation-model/anthropic.18.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "arn:aws:bedrock:us-west-2::foundation-model/anthropic.19.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "arn:aws:bedrock:us-west-2::foundation-model/anthropic.20.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "arn:aws:bedrock:us-west-2::foundation-model/anthropic.21.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "arn:aws:bedrock:us-west-2::foundation-model/anthropic.22.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "arn:aws:bedrock:us-west-2::foundation-model/anthropic.23.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"] }
  expect_failures = [aws_iam_policy.installed_controller_boundary[0]]
}

# Structural fixture for one exact cross-region profile and its three members;
# no model is selected, invoked or claimed available by this offline mock.
run "one_worker_exact_profile_members_fit_IAM_cap" {
  command = plan
  variables { controller_model_resource_arns = ["arn:aws:bedrock:us-west-2:123456789012:inference-profile/us.anthropic.claude-sonnet-4-5-20250929-v1:0", "arn:aws:bedrock:us-west-2::foundation-model/anthropic.claude-sonnet-4-5-20250929-v1:0", "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-sonnet-4-5-20250929-v1:0", "arn:aws:bedrock:us-east-2::foundation-model/anthropic.claude-sonnet-4-5-20250929-v1:0"] }
  assert {
    condition     = length(base64encode(aws_iam_policy.installed_controller_boundary[0].policy)) <= 8192
    error_message = "A normal exact profile/member allocation must fit without wildcards."
  }
}

run "bootstrap_prefix_cannot_substitute_campaign_control_secret" {
  command = plan
  variables { installed_controller_boundary = {
    "candidate_key" : "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "runtime_id" : "demo",
    "funding_expires_epoch" : 1793318400,
    "cluster_arn" : "arn:aws:ecs:us-west-2:123456789012:cluster/honua-pilot-offline-cluster",
    "control" : {
      "secret_arn" : "arn:aws:secretsmanager:us-west-2:123456789012:secret:honua-pilot-offline/native-agent/control-AbCdEf",
      "version_id" : "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
    },
    "workers" : [
      {
        "slot_id" : "fixed-slot",
        "state_machine_arn" : "arn:aws:states:us-west-2:123456789012:stateMachine:honua-agent-fixed-slot",
        "task_role_arn" : "arn:aws:iam::123456789012:role/honua-agent-fixed-slot-worker",
        "command_queue_arn" : "arn:aws:sqs:us-west-2:123456789012:honua-agent-fixed-slot-commands.fifo",
        "reply_queue_arn" : "arn:aws:sqs:us-west-2:123456789012:honua-agent-fixed-slot-replies.fifo"
      }
    ]
  } }
  expect_failures = [var.installed_controller_boundary]
}
