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
  desired = merge(local.original, {
    Statement = [for statement in local.original.Statement : merge(
      { for key, value in statement : key => value if key != "Action" || statement.Sid != "DenyStandingTaggedResources" },
      statement.Sid == "DenyStandingTaggedResources" ? { NotAction = ["lambda:ListVersionsByFunction"] } : {}
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
