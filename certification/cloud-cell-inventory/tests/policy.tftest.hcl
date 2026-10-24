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
    ) : jsonencode(statement) == jsonencode(local.desired.Statement[index])])
    error_message = "Preserve every restriction except the single version-inventory read."
  }
}
