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
}
run "custody_and_approval_boundaries" {
  command = plan
  assert {
    condition = alltrue([for lane, role in aws_iam_role.executor :
      jsondecode(role.assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == "repo:honua-io/honua-release:environment:hosted-pilot-${lane}" &&
      jsondecode(role.assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:aud"] == "sts.amazonaws.com"
    ])
    error_message = "Each OIDC lane needs its own exact environment subject and STS audience."
  }
  assert {
    condition = (
      alltrue([for binding in aws_s3_bucket_public_access_block.custody : binding.block_public_acls && binding.block_public_policy && binding.ignore_public_acls && binding.restrict_public_buckets]) &&
      alltrue([for bucket in aws_s3_bucket_versioning.custody : bucket.versioning_configuration[0].status == "Enabled"]) &&
      alltrue([for bucket in aws_s3_bucket_server_side_encryption_configuration.custody : one(bucket.rule).apply_server_side_encryption_by_default[0].sse_algorithm == "aws:kms"]) &&
      aws_kms_key.custody.enable_key_rotation
    )
    error_message = "Plan and state custody must be private, versioned and KMS encrypted."
  }
  assert {
    condition = (
      alltrue([for statement in jsondecode(aws_iam_role_policy.custody["apply"].policy).Statement :
        !(statement.Effect == "Allow" && try(statement.Resource == "${aws_s3_bucket.custody["plans"].arn}/hosted-pilot/plans/*", false) && try(statement.Action == "s3:PutObject", false))
      ])
    )
    error_message = "Apply cannot replace the producing saved plan."
  }
  assert {
    condition = (
      length([for statement in jsondecode(aws_s3_bucket_policy.custody["plans"].policy).Statement : statement if statement.Sid == "ConditionalArtifacts" && statement.Effect == "Deny"]) == 1 &&
      length([for statement in jsondecode(aws_s3_bucket_policy.custody["plans"].policy).Statement : statement if statement.Sid == "ImmutableArtifacts" && contains(statement.Action, "s3:DeleteObjectVersion")]) == 1 &&
      jsondecode(aws_iam_policy.runtime_boundary.policy).Statement[0].Effect == "Deny" &&
      contains(jsondecode(aws_iam_policy.runtime_boundary.policy).Statement[0].Action, "bedrock:*") &&
      one([for statement in jsondecode(aws_iam_policy.runtime_boundary.policy).Statement : statement if try(statement.Sid, "") == "ExcludeFoundationCustodyKey"]).Effect == "Deny" &&
      one([for statement in jsondecode(aws_iam_policy.runtime_boundary.policy).Statement : statement if try(statement.Sid, "") == "ExcludeFoundationCustodyKey"]).Action == "kms:*" &&
      one([for statement in jsondecode(aws_iam_policy.runtime_boundary.policy).Statement : statement if try(statement.Sid, "") == "ExcludeFoundationCustodyKey"]).Resource == aws_kms_key.custody.arn &&
      one([for statement in jsondecode(aws_iam_policy.runtime_boundary.policy).Statement : statement if try(statement.Sid, "") == "DecryptPilotGeneratedSecrets"]).Condition.StringEquals["aws:ResourceTag/ResetBoundary"] == var.name_prefix &&
      one([for statement in jsondecode(aws_iam_policy.runtime_boundary.policy).Statement : statement if try(statement.Sid, "") == "DecryptPilotGeneratedSecrets"]).Condition.StringEquals["kms:ViaService"] == "secretsmanager.${var.region}.amazonaws.com" &&
      one([for statement in jsondecode(aws_iam_policy.runtime_boundary.policy).Statement : statement if try(statement.Sid, "") == "DecryptPilotGeneratedSecrets"]).Condition.ArnLike["kms:EncryptionContext:SecretARN"] == "arn:aws:secretsmanager:${var.region}:123456789012:secret:${var.name_prefix}*"
    )
    error_message = "Saved-plan versions and single-use reservations must be immutable; runtime credentials cannot administer identities or invoke models."
  }
}
run "refuse_global_action_grant" {
  command = plan
  variables {
    provision_policy_document = "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"*\",\"Resource\":\"*\"}]}"
  }
  expect_failures = [var.provision_policy_document]
}
