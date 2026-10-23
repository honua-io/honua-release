mock_provider "aws" {
  override_during = plan
  mock_resource "aws_iam_role" { defaults = { arn = "arn:aws:iam::123456789012:role/honua-agent-offline-attempt-worker" } }
  mock_resource "aws_sqs_queue" { defaults = { arn = "arn:aws:sqs:us-east-1:123456789012:honua-agent-offline-attempt-commands.fifo", url = "https://sqs.us-east-1.amazonaws.com/123456789012/honua-agent-offline-attempt-commands.fifo" } }
  mock_resource "aws_ecs_task_definition" { defaults = { arn = "arn:aws:ecs:us-east-1:123456789012:task-definition/honua-agent-offline-attempt:1" } }
  mock_resource "aws_sfn_state_machine" { defaults = { arn = "arn:aws:states:us-east-1:123456789012:stateMachine:honua-agent-offline-attempt" } }
  mock_resource "aws_scheduler_schedule" { defaults = { arn = "arn:aws:scheduler:us-east-1:123456789012:schedule/honua-agent-offline-attempt/honua-agent-offline-attempt" } }
  mock_resource "aws_cloudwatch_metric_alarm" { defaults = { arn = "arn:aws:cloudwatch:us-east-1:123456789012:alarm:honua-agent-offline-attempt" } }
  mock_resource "aws_cloudwatch_log_group" { defaults = { arn = "arn:aws:logs:us-east-1:123456789012:log-group:/honua/agent-e2e/offline-attempt" } }
  mock_data "aws_sfn_state_machine" {
    defaults = { definition = "{\"StartAt\":\"OfflineOnly\",\"States\":{\"OfflineOnly\":{\"Type\":\"Succeed\"}}}", role_arn = "arn:aws:iam::123456789012:role/honua-agent-offline-attempt-lifecycle" }
  }
}
# The published worker module owns a legacy provider configuration. Terraform
# cannot replace that child configuration with this root mock. Exercise its real
# resource controls separately from its own root; these overrides test only this
# wrapper's identity/expiry/output boundaries and never contact AWS.
override_module {
  target = module.worker
  outputs = {
    controller_policy = "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"sqs:SendMessage\",\"Resource\":\"arn:aws:sqs:us-east-1:123456789012:honua-agent-offline-attempt-commands.fifo\"}]}"
    lifecycle = {
      state_machine_arn   = "arn:aws:states:us-east-1:123456789012:stateMachine:honua-agent-offline-attempt"
      task_definition_arn = "arn:aws:ecs:us-east-1:123456789012:task-definition/honua-agent-offline-attempt:1"
      worker_role_arn     = "arn:aws:iam::123456789012:role/honua-agent-offline-attempt-worker"
    }
    transport = {
      commandQueueUrl = "https://sqs.us-east-1.amazonaws.com/123456789012/honua-agent-offline-attempt-commands.fifo"
      replyQueueUrl   = "https://sqs.us-east-1.amazonaws.com/123456789012/honua-agent-offline-attempt-replies.fifo"
      attemptId       = "offline-attempt"
      expiresEpoch    = 2000006000
    }
    independent_cleanup = { state_machine_arn = "arn:aws:states:us-east-1:123456789012:stateMachine:honua-agent-offline-attempt-expiry" }
    mcp_configuration_binding = {
      secret_arn  = "arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-agent-offline-attempt-mcp-ABC123"
      version_id  = "dddddddddddddddddddddddddddddddd"
      kms_key_arn = null
    }
  }
}
variables {
  region               = "us-east-1"
  account_id           = "123456789012"
  name_prefix          = "honua-pilot-offline"
  owner                = "offline-contract"
  expires_at           = "2033-05-18T05:13:20Z"
  candidate_digest     = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  attempt_id           = "offline-attempt"
  issued_epoch         = 2000000000
  lifetime_seconds     = 6000
  cluster_arn          = "arn:aws:ecs:us-east-1:123456789012:cluster/honua-pilot-offline-agent"
  subnet_ids           = ["subnet-00000000000000001"]
  security_group_ids   = ["sg-00000000000000001"]
  worker_image         = "123456789012.dkr.ecr.us-east-1.amazonaws.com/honua-pilot-offline-worker@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  public_bundle_digest = "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
  controller_role_arn  = "arn:aws:iam::123456789012:role/honua-pilot-offline-agent-controller"
  claim_table_name     = "honua-agent-aaaaaaaaaaaaaaaaaaaaaaaa-claims"
  accounting_claim_roles = {
    administrators = ["arn:aws:iam::123456789012:role/honua-pilot-offline-agent-plan"]
    workers        = ["arn:aws:iam::123456789012:role/honua-agent-offline-attempt-lifecycle"]
  }
  cleanup_alarm_topic_arn  = "arn:aws:sns:us-east-1:123456789012:honua-pilot-offline-cleanup"
  permissions_boundary_arn = "arn:aws:iam::123456789012:policy/honua-pilot-offline-agent-boundary"
  mcp_configuration = {
    secret_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-agent-offline-attempt-mcp-ABC123"
    version_id = "dddddddddddddddddddddddddddddddd"
  }
  accounting = {
    state                   = { region = "us-east-1", bucket = "original-retained-state", table = "original-retained-head" }
    generationDigest        = "sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
    bootstrapArtifactDigest = "sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
    operation               = { operationId = "worker-offline-attempt", liabilityBasisDigest = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", reservationIds = { infrastructure = "original-infra", cleanup = "original-cleanup" } }
    maximumUsd              = { infrastructure = "5", cleanup = "2" }
  }
}
run "fixed_worker_keeps_original_identity_time_and_immutable_credential" {
  command = plan
  assert {
    condition = (
      output.worker_backend.attemptId == var.attempt_id &&
      output.worker_backend.publicBundleDigest == var.public_bundle_digest &&
      jsonencode(output.mcp_configuration_binding) == jsonencode(var.mcp_configuration) &&
      output.worker_backend.lifecycle.expiresEpoch == var.issued_epoch + 6000 &&
      toset(output.required_accounting_claim_writers) == toset(["arn:aws:iam::123456789012:role/honua-agent-offline-attempt-lifecycle"]) &&
      aws_iam_role_policy.controller_worker.role == "honua-pilot-offline-agent-controller" &&
      !strcontains(aws_iam_role_policy.controller_worker.policy, "secretsmanager:") &&
      !strcontains(aws_iam_role_policy.controller_worker.policy, "bedrock:")
    )
    error_message = "Worker dispatch must bind the exact image, immutable MCP version, original accounting and fixed expiry without secret/model/provisioning grants."
  }
}
run "reject_sliding_expiry" {
  command = plan
  variables { expires_at = "2033-05-18T05:13:21Z" }
  expect_failures = [output.worker_backend]
}
run "reject_cluster_outside_original_pilot" {
  command = plan
  variables { cluster_arn = "arn:aws:ecs:us-east-1:123456789012:cluster/unrelated-campaign" }
  expect_failures = [output.worker_backend]
}
run "unreviewed_or_missing_current_claim_writer_is_refused" {
  command = plan
  variables {
    accounting_claim_roles = {
      administrators = ["arn:aws:iam::123456789012:role/honua-pilot-offline-agent-plan"]
      workers        = ["arn:aws:iam::123456789012:role/honua-agent-foreign-attempt-lifecycle"]
    }
  }
  expect_failures = [var.accounting_claim_roles]
}
run "wildcard_claim_administrator_is_refused" {
  command = plan
  variables {
    accounting_claim_roles = {
      administrators = ["*"]
      workers        = ["arn:aws:iam::123456789012:role/honua-agent-offline-attempt-lifecycle"]
    }
  }
  expect_failures = [var.accounting_claim_roles]
}
