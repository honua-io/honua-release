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
}
run "custody_and_approval_boundaries" {
  command = plan
  assert {
    condition = (
      aws_ecr_repository.controller.image_tag_mutability == "IMMUTABLE" &&
      aws_ecr_repository.controller.image_scanning_configuration[0].scan_on_push &&
      aws_iam_role_policy.controller_image.role == aws_iam_role.executor["apply"].id &&
      toset(jsondecode(aws_iam_role_policy.controller_image.policy).Statement[1].Resource) == toset([aws_ecr_repository.controller.arn, aws_ecr_repository.worker.arn]) &&
      contains(jsondecode(aws_iam_role_policy.controller_image.policy).Statement[1].Action, "ecr:PutImage") &&
      output.deployment_bindings.HOSTED_PILOT_CONTROLLER_ECR_REPOSITORY == aws_ecr_repository.controller.repository_url
    )
    error_message = "The trusted controller registry must be immutable, scanned and writable only through its exact apply-lane repository grant."
  }
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
      one([for statement in jsondecode(aws_s3_bucket_policy.custody["state"].policy).Statement : statement if statement.Sid == "RetainBootstrapRecovery"]).Effect == "Deny" &&
      contains(one([for statement in jsondecode(aws_s3_bucket_policy.custody["state"].policy).Statement : statement if statement.Sid == "RetainBootstrapRecovery"]).Action, "s3:DeleteObjectVersion") &&
      one([for statement in jsondecode(aws_s3_bucket_policy.custody["state"].policy).Statement : statement if statement.Sid == "ConditionalBootstrapRecovery"]).Condition.Null["s3:if-none-match"] == "true"
    )
    error_message = "Bootstrap recovery custody must survive native reruns through immutable conditional writes and retained object versions."
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

run "agent_orchestration_is_opt_in_and_bounded" {
  command = plan
  variables { enable_agent_controller = true }
  assert {
    condition = (
      length(aws_iam_policy.agent_boundary[0].policy) <= 6144 &&
      length(aws_iam_policy.agent_read[0].policy) <= 6144 &&
      one([for statement in jsondecode(aws_iam_policy.agent_boundary[0].policy).Statement : statement if try(statement.Sid, "") == "NoUnreviewedModel"]).Effect == "Deny" &&
      one([for statement in jsondecode(aws_iam_policy.agent_boundary[0].policy).Statement : statement if try(statement.Sid, "") == "ExcludeFoundationCustodyKey"]).Resource == aws_kms_key.custody.arn &&
      length(local.workload_boundary_arns) == 3 &&
      contains(local.workload_role_patterns, "arn:aws:iam::123456789012:role/honua-agent-*") &&
      contains(local.deployment_state_keys, "honua/hosted-pilot/${var.name_prefix}/workers/*")
    )
    error_message = "Agent orchestration must fit IAM policy limits, preserve custody/model refusals and use isolated role/state namespaces."
  }
}
run "worker_custody_boundary_ceiling_is_read_only" {
  command = plan
  variables { enable_agent_controller = true }
  assert {
    condition = (
      one([for statement in jsondecode(aws_iam_policy.agent_boundary[0].policy).Statement : statement if try(statement.Sid, "") == "ReadDeclaredWorkerCustody"]) == {
        Sid      = "ReadDeclaredWorkerCustody", Effect = "Allow",
        Action   = ["iam:GetRolePolicy", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies"],
        Resource = "arn:aws:iam::123456789012:role/honua-agent-*"
      } &&
      contains(one([for statement in jsondecode(aws_iam_policy.agent_boundary[0].policy).Statement : statement if try(statement.Sid, "") == "NoIdentityAdministration"]).Action, "iam:Put*") &&
      one([for statement in jsondecode(aws_iam_policy.agent_boundary[0].policy).Statement : statement if try(statement.Sid, "") == "NoIdentityAdministration"]).Effect == "Deny"
    )
    error_message = "The boundary must cap three custody reads to agent roles while retaining identity-administration denial; identity policies must narrow the actual attempts."
  }
}

run "native_membership_boundary_preserves_controller_and_foundation_isolation" {
  command = plan
  variables { enable_agent_controller = true }
  assert {
    condition = (
      length(aws_iam_policy.native_bootstrap_boundary[0].policy) <= 6144 &&
      contains(local.workload_boundary_arns, aws_iam_policy.native_bootstrap_boundary[0].arn) &&
      jsondecode(aws_iam_policy.native_bootstrap_boundary[0].policy).Statement[0].Effect == "Deny" &&
      contains(jsondecode(aws_iam_policy.native_bootstrap_boundary[0].policy).Statement[0].Action, "secretsmanager:GetSecretValue") &&
      contains(jsondecode(aws_iam_policy.native_bootstrap_boundary[0].policy).Statement[0].Action, "bedrock:*") &&
      one([for row in jsondecode(aws_iam_policy.native_bootstrap_boundary[0].policy).Statement : row if try(row.Action == "ecs:DescribeTaskDefinition", false)]) == {
        Effect = "Allow", Action = "ecs:DescribeTaskDefinition", Resource = "*", Condition = { StringEquals = { "aws:RequestedRegion" = "us-west-2" } }
      } &&
      one([for row in jsondecode(aws_iam_policy.agent_boundary[0].policy).Statement : row if try(row.Sid, "") == "ExcludeFoundationCustodyKey"]) == {
        Sid = "ExcludeFoundationCustodyKey", Effect = "Deny", Action = "kms:*", Resource = aws_kms_key.custody.arn
      } &&
      one([for row in jsondecode(aws_iam_policy.native_bootstrap_boundary[0].policy).Statement : row if try(row.Sid, "") == "DecryptOriginalMembershipObjects"]).Condition.ArnEquals["aws:PrincipalArn"] == "arn:aws:iam::123456789012:role/honua-pilot-offline-agent-native-bootstrap" &&
      one([for row in jsondecode(aws_iam_policy.native_bootstrap_boundary[0].policy).Statement : row if try(row.Sid, "") == "DecryptOriginalMembershipObjects"]).Condition.StringEquals["kms:ViaService"] == "s3.us-west-2.amazonaws.com" &&
      toset(one([for row in jsondecode(aws_iam_policy.native_bootstrap_boundary[0].policy).Statement : row if try(row.Sid, "") == "DecryptOriginalMembershipObjects"]).Condition.ArnLike["kms:EncryptionContext:aws:s3:arn"]) == toset([
        "${aws_s3_bucket.custody["plans"].arn}/hosted-pilot/plans/*/review.json",
        "${aws_s3_bucket.custody["plans"].arn}/hosted-pilot/plans/*/human-identities-*.json"
      ]) &&
      alltrue([for configuration in aws_s3_bucket_server_side_encryption_configuration.custody : !one(configuration.rule).bucket_key_enabled])
    )
    error_message = "Only native bootstrap may decrypt enrollment JSON through object-scoped S3 KMS context; workers/controllers retain blanket custody denial and bucket keys cannot broaden context."
  }
  assert {
    condition = (
      one([for row in jsondecode(aws_iam_role_policy.custody["apply"].policy).Statement : row if try(row.Sid, "") == "WriteOriginalHumanEnrollment"]).Condition.StringEquals["s3:if-none-match"] == "*" &&
      toset(one([for row in jsondecode(aws_iam_role_policy.custody["apply"].policy).Statement : row if try(row.Sid, "") == "WriteOriginalHumanEnrollment"]).Resource) == toset(local.native_membership_objects) &&
      one([for row in jsondecode(aws_iam_role_policy.custody["apply"].policy).Statement : row if try(row.Sid, "") == "WriteOriginalHumanEnrollment"]).Condition.StringEquals["s3:x-amz-server-side-encryption-aws-kms-key-id"] == aws_kms_key.custody.arn
    )
    error_message = "Apply may only conditionally retain the original review/enrollment JSON with the original KMS key; it cannot overwrite binary plans."
  }
}
run "refuse_global_action_grant" {
  command = plan
  variables {
    provision_policy_document = "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"*\",\"Resource\":\"*\"}]}"
  }
  expect_failures = [var.provision_policy_document]
}
run "immutable_worker_publications_have_finite_service_managed_expiry" {
  command = plan
  assert {
    condition = (
      aws_ecr_lifecycle_policy.worker.repository == aws_ecr_repository.worker.name &&
      jsondecode(aws_ecr_lifecycle_policy.worker.policy).rules[0].selection.tagStatus == "untagged" &&
      jsondecode(aws_ecr_lifecycle_policy.worker.policy).rules[0].selection.countNumber == 1 &&
      jsondecode(aws_ecr_lifecycle_policy.worker.policy).rules[1].selection.tagStatus == "any" &&
      jsondecode(aws_ecr_lifecycle_policy.worker.policy).rules[1].selection.countType == "sinceImagePushed" &&
      jsondecode(aws_ecr_lifecycle_policy.worker.policy).rules[1].selection.countNumber == 14 &&
      jsondecode(aws_ecr_lifecycle_policy.worker.policy).rules[1].action.type == "expire" &&
      contains(jsondecode(aws_iam_role_policy.controller_image.policy).Statement[1].Action, "ecr:GetLifecyclePolicy") &&
      !strcontains(aws_iam_role_policy.controller_image.policy, "ecr:BatchDeleteImage")
    )
    error_message = "Every immutable image, including an acknowledged-lost push, must expire without granting arbitrary publisher deletion."
  }
}
run "immutable_controller_publications_have_finite_service_managed_expiry" {
  command = plan
  assert {
    condition = (
      aws_ecr_lifecycle_policy.controller.repository == aws_ecr_repository.controller.name &&
      jsondecode(aws_ecr_lifecycle_policy.controller.policy).rules[0].selection.tagStatus == "untagged" &&
      jsondecode(aws_ecr_lifecycle_policy.controller.policy).rules[0].selection.countNumber == 1 &&
      jsondecode(aws_ecr_lifecycle_policy.controller.policy).rules[1].selection.tagStatus == "any" &&
      jsondecode(aws_ecr_lifecycle_policy.controller.policy).rules[1].selection.countType == "sinceImagePushed" &&
      jsondecode(aws_ecr_lifecycle_policy.controller.policy).rules[1].selection.countNumber == 14 &&
      jsondecode(aws_ecr_lifecycle_policy.controller.policy).rules[1].action.type == "expire" &&
      !strcontains(aws_iam_role_policy.controller_image.policy, "ecr:BatchDeleteImage")
    )
    error_message = "Controller archive/browser images must expire, including abandoned pushes, without granting publisher deletion."
  }
}
run "uncompiled_image_funding_cannot_create_an_unbound_owner_ledger" {
  command = plan
  variables {
    budget_ledger = {
      schema                     = "honua.hosted-pilot-budget/v1", expires_at = "2026-10-30T00:00:00Z",
      source_sha                 = null, config_sha256 = null, reservation_id = "original-image-preparation-001",
      total_budget_usd           = 2000, cleanup_reserve_usd = 200, spent_usd = 0, reserved_usd = 294,
      deployment_reservation_usd = 5, active_paid_attempts = 0
    }
  }
  expect_failures = [var.budget_ledger]
}

run "human_enrollment_has_no_implicit_identity_grants" {
  command = plan
  assert {
    condition     = length(aws_iam_role_policy.human_enrollment_read) == 0 && length(aws_iam_role_policy.human_enrollment_apply) == 0
    error_message = "Image preparation must not silently enable human account administration."
  }
}

run "human_enrollment_separates_observation_from_provisioning" {
  command = plan
  variables { enable_human_enrollment = true }
  assert {
    condition = (
      toset(keys(aws_iam_role_policy.human_enrollment_read)) == toset(["plan", "apply"]) &&
      alltrue([for lane, policy in aws_iam_role_policy.human_enrollment_read :
        policy.role == aws_iam_role.executor[lane].id &&
        toset(jsondecode(policy.policy).Statement[0].Action) == toset(["cognito-idp:AdminGetUser", "cognito-idp:AdminListGroupsForUser"]) &&
        jsondecode(policy.policy).Statement[0].Resource == "arn:aws:cognito-idp:us-west-2:123456789012:userpool/*" &&
        jsondecode(policy.policy).Statement[0].Condition.StringEquals == {
          "aws:ResourceTag/ResetBoundary" = var.name_prefix,
          "aws:ResourceTag/ManagedBy"     = "honua-release-hosted-pilot"
        }
      ]) &&
      length(aws_iam_role_policy.human_enrollment_apply) == 1 &&
      aws_iam_role_policy.human_enrollment_apply[0].role == aws_iam_role.executor["apply"].id &&
      toset(jsondecode(aws_iam_role_policy.human_enrollment_apply[0].policy).Statement[0].Action) == toset([
        "cognito-idp:AdminCreateUser", "cognito-idp:AdminUpdateUserAttributes",
        "cognito-idp:AdminDeleteUser", "cognito-idp:AdminDisableUser", "cognito-idp:AdminEnableUser",
        "cognito-idp:AdminSetUserPassword", "cognito-idp:AdminAddUserToGroup", "cognito-idp:AdminRemoveUserFromGroup"
      ]) &&
      jsondecode(aws_iam_role_policy.human_enrollment_apply[0].policy).Statement[0].Resource == "arn:aws:cognito-idp:us-west-2:123456789012:userpool/*" &&
      jsondecode(aws_iam_role_policy.human_enrollment_apply[0].policy).Statement[0].Condition.StringEquals == {
        "aws:ResourceTag/ResetBoundary" = var.name_prefix,
        "aws:ResourceTag/ManagedBy"     = "honua-release-hosted-pilot"
      }
    )
    error_message = "Only the apply executor may provision users, and both readers and writers must be confined to this tagged pilot pool."
  }
}

run "token_counting_boundary_covers_only_reviewed_regional_foundation_models" {
  command = plan
  variables {
    enable_agent_controller = true
    controller_model_resource_arns = [
      "arn:aws:bedrock:us-west-2:123456789012:inference-profile/us.anthropic.offline-v1:0",
      "arn:aws:bedrock:us-west-2::foundation-model/anthropic.offline-v1:0",
      "arn:aws:bedrock:us-east-1::foundation-model/anthropic.offline-v1:0"
    ]
  }
  assert {
    condition = (
      one([for statement in jsondecode(aws_iam_policy.agent_boundary[0].policy).Statement : statement if try(statement.Sid, "") == "ReviewedTokenCountingOnly"]) == {
        Sid      = "ReviewedTokenCountingOnly", Effect = "Allow", Action = "bedrock:CountTokens",
        Resource = ["arn:aws:bedrock:us-west-2::foundation-model/anthropic.offline-v1:0"]
      } && length(aws_iam_policy.agent_boundary[0].policy) <= 6144
    )
    error_message = "Token counting must retain exact reviewed regional foundation models and the AWS policy size bound."
  }
}
run "original_custody_tags_are_separate_from_current_phase_identity" {
  command = plan
  variables {
    bootstrap_execution_id = "200"
    bootstrap_creation_identity = {
      foundation_source_sha = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
      release_root_sha      = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
      config_path           = "certification/hosted-pilot/bootstrap/deployments/test.tfvars.json"
      config_sha256         = "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
      provider_lock_sha256  = "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
    }
    bootstrap_custody_creation_tags = {
      ManagedBy              = "honua-release-hosted-pilot-bootstrap"
      BootstrapExecution     = "199"
      BootstrapFoundationSha = "1111111111111111111111111111111111111111"
      BootstrapReleaseSha    = "2222222222222222222222222222222222222222"
      BootstrapConfigPath    = "certification/hosted-pilot/bootstrap/deployments/test.tfvars.json"
      BootstrapConfigSha256  = "3333333333333333333333333333333333333333333333333333333333333333"
      BootstrapLockSha256    = "4444444444444444444444444444444444444444444444444444444444444444"
    }
  }
  assert {
    condition     = aws_kms_key.custody.tags == var.bootstrap_custody_creation_tags && alltrue([for bucket in aws_s3_bucket.custody : bucket.tags == var.bootstrap_custody_creation_tags])
    error_message = "The actual retained key and both buckets must preserve the original custody creator, not this later phase's source/run."
  }
}
run "incomplete_original_custody_identity_refused" {
  command = plan
  variables { bootstrap_custody_creation_tags = { ManagedBy = "honua-release-hosted-pilot-bootstrap" } }
  expect_failures = [var.bootstrap_custody_creation_tags]
}

run "default_has_no_installed_identity_writer" {
  command = plan
  assert {
    condition     = length(aws_iam_role_policy.installed_worker_identity_output) == 0
    error_message = "Image preparation must not gain accounting-bucket writes."
  }
}
run "only_apply_can_create_exact_identity_objects" {
  command = plan
  variables {
    enable_agent_controller          = true
    installed_worker_identity_scopes = [{ candidate_digest = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", runtime_id = "reviewed-runtime", slot_id = "fixed-slot" }]
  }
  assert {
    condition = (
      aws_iam_role_policy.installed_worker_identity_output[0].role == aws_iam_role.executor["apply"].id &&
      length(jsondecode(aws_iam_role_policy.installed_worker_identity_output[0].policy).Statement) == 1 &&
      jsondecode(aws_iam_role_policy.installed_worker_identity_output[0].policy).Statement[0].Action == "s3:PutObject" &&
      toset(jsondecode(aws_iam_role_policy.installed_worker_identity_output[0].policy).Statement[0].Resource) == toset([
        "arn:aws:s3:::honua-agent-aaaaaaaaaaaaaaaaaaaaaaaa-123456789012/agent-e2e/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/installed-workers/reviewed-runtime/fixed-slot/*/*/installation.json",
        "arn:aws:s3:::honua-agent-aaaaaaaaaaaaaaaaaaaaaaaa-123456789012/agent-e2e/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/installed-workers/reviewed-runtime/fixed-slot/*/*/review.json",
        "arn:aws:s3:::honua-agent-aaaaaaaaaaaaaaaaaaaaaaaa-123456789012/agent-e2e/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/installed-workers/reviewed-runtime/fixed-slot/*/*/readiness.json"
      ]) &&
      jsondecode(aws_iam_role_policy.installed_worker_identity_output[0].policy).Statement[0].Condition.StringEquals["s3:if-none-match"] == "*" &&
      jsondecode(aws_iam_role_policy.installed_worker_identity_output[0].policy).Statement[0].Condition.StringEquals["s3:x-amz-server-side-encryption"] == "AES256"
    )
    error_message = "Identity retention must be exact create-only objects, never ledger, initialization or head writes."
  }
}
run "identity_writer_requires_accounting_foundation" {
  command = plan
  variables {
    enable_agent_controller          = false
    installed_worker_identity_scopes = [{ candidate_digest = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", runtime_id = "reviewed-runtime", slot_id = "fixed-slot" }]
  }
  expect_failures = [aws_iam_role_policy.installed_worker_identity_output[0]]
}
run "identity_writer_rejects_namespace_escape" {
  command = plan
  variables {
    enable_agent_controller          = true
    installed_worker_identity_scopes = [{ candidate_digest = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", runtime_id = "../other", slot_id = "fixed-slot" }]
  }
  expect_failures = [var.installed_worker_identity_scopes]
}
run "identity_writer_rejects_candidate_wildcard" {
  command = plan
  variables {
    enable_agent_controller          = true
    installed_worker_identity_scopes = [{ candidate_digest = "sha256:*", runtime_id = "reviewed-runtime", slot_id = "fixed-slot" }]
  }
  expect_failures = [var.installed_worker_identity_scopes]
}
