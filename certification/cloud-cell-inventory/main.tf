terraform {
  required_version = ">= 1.13.5, < 2.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "6.61.0"
    }
  }
}

provider "aws" {
  region              = "us-east-1"
  allowed_account_ids = ["585192672263"]
}

locals {
  original = jsondecode(file("${path.module}/guardrails.before.json"))
  # Genuine-model canary models. Each entry is the US cross-region inference profile
  # plus the three member-region foundation models from GetInferenceProfile (us-east-1,
  # us-east-2, us-west-2). Founder 2026-10-30: Claude Opus 5.5, Claude Sonnet 5.5,
  # GPT-6.1 Sol and GPT-6.1 Terra. Sonnet 4.6 stays because it is still the shipped
  # default. Sonnet 4.5 is not in this list; it remains only in the applied snapshot.
  canary_models = [
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
  ]
  desired = merge(local.original, {
    Statement = [for statement in local.original.Statement : merge(
      { for key, value in statement : key => value if key != "Action" || statement.Sid != "DenyStandingTaggedResources" },
      statement.Sid == "DenyStandingTaggedResources" ? { NotAction = ["lambda:ListVersionsByFunction"] } : {},
      statement.Sid == "BedrockInvokeOnlyCanaryModel" ? { NotResource = local.canary_models } : {}
    )]
  })
}

# The sole imported object has a fixed identity. A fresh runner reimports that
# identity; it must never create a second policy or own the role itself.
import {
  to = aws_iam_role_policy.inventory
  id = "honua-release-cicd:release-cicd-guardrails"
}

resource "aws_iam_role_policy" "inventory" {
  name   = "release-cicd-guardrails"
  role   = "honua-release-cicd"
  policy = jsonencode(local.desired)

  lifecycle {
    prevent_destroy = true
  }
}
