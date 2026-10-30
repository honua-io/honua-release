mock_provider "aws" {}

override_resource {
  target = aws_iam_role_policy.inventory
  values = { id = "honua-release-cicd:release-cicd-guardrails" }
}

run "single_read_exception" {
  command = plan

  assert {
    condition     = aws_iam_role_policy.inventory.role == "honua-release-cicd" && aws_iam_role_policy.inventory.name == "release-cicd-guardrails"
    error_message = "Only the fixed standing inline policy may be adopted."
  }

  assert {
    condition = alltrue([for index, statement in local.original.Statement : statement.Sid == "DenyStandingTaggedResources" ? (
      jsonencode(local.desired.Statement[index]) == jsonencode({
        Sid       = "DenyStandingTaggedResources"
        Effect    = "Deny"
        NotAction = ["lambda:ListVersionsByFunction"]
        Resource  = "*"
        Condition = { StringEquals = { "aws:ResourceTag/Environment" = ["cert", "demo"] } }
      })
      ) : statement.Sid == "BedrockInvokeOnlyCanaryModel" ? (
      jsonencode(local.desired.Statement[index]) == jsonencode(merge(statement, { NotResource = [
        "arn:aws:bedrock:us-east-1:585192672263:inference-profile/us.anthropic.claude-sonnet-5-5",
        "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-sonnet-5-5",
        "arn:aws:bedrock:us-east-2::foundation-model/anthropic.claude-sonnet-5-5",
        "arn:aws:bedrock:us-west-2::foundation-model/anthropic.claude-sonnet-5-5",
        "arn:aws:bedrock:us-east-1:585192672263:inference-profile/us.anthropic.claude-sonnet-4-6",
        "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-sonnet-4-6",
        "arn:aws:bedrock:us-east-2::foundation-model/anthropic.claude-sonnet-4-6",
        "arn:aws:bedrock:us-west-2::foundation-model/anthropic.claude-sonnet-4-6",
        "arn:aws:bedrock:us-east-1:585192672263:inference-profile/us.openai.gpt-6.1-terra",
        "arn:aws:bedrock:us-east-1::foundation-model/openai.gpt-6.1-terra",
        "arn:aws:bedrock:us-east-2::foundation-model/openai.gpt-6.1-terra",
        "arn:aws:bedrock:us-west-2::foundation-model/openai.gpt-6.1-terra",
        "arn:aws:bedrock:us-east-1:585192672263:inference-profile/us.anthropic.claude-opus-5-5",
        "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-opus-5-5",
        "arn:aws:bedrock:us-east-2::foundation-model/anthropic.claude-opus-5-5",
        "arn:aws:bedrock:us-west-2::foundation-model/anthropic.claude-opus-5-5",
        "arn:aws:bedrock:us-east-1:585192672263:inference-profile/us.openai.gpt-6.1-sol",
        "arn:aws:bedrock:us-east-1::foundation-model/openai.gpt-6.1-sol",
        "arn:aws:bedrock:us-east-2::foundation-model/openai.gpt-6.1-sol",
        "arn:aws:bedrock:us-west-2::foundation-model/openai.gpt-6.1-sol",
      ] }))
    ) : jsonencode(statement) == jsonencode(local.desired.Statement[index])])
    error_message = "Preserve every restriction except the version-inventory read and the canary model allowlist."
  }
}
