variable "enable_human_enrollment" {
  description = "Explicit reviewed opt-in for Terraform-managed humans in this pilot's tagged pool. Does not enroll users or send invitations."
  type        = bool
  default     = false
}

resource "aws_iam_role_policy" "human_enrollment_read" {
  for_each = var.enable_human_enrollment ? aws_iam_role.executor : {}
  role     = each.value.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect   = "Allow"
    Action   = ["cognito-idp:AdminGetUser", "cognito-idp:AdminListGroupsForUser"]
    Resource = "arn:aws:cognito-idp:${var.region}:${local.account}:userpool/*"
    Condition = { StringEquals = {
      "aws:ResourceTag/ResetBoundary" = var.name_prefix
      "aws:ResourceTag/ManagedBy"     = "honua-release-hosted-pilot"
    } }
  }] })
}

resource "aws_iam_role_policy" "human_enrollment_apply" {
  count = var.enable_human_enrollment ? 1 : 0
  role  = aws_iam_role.executor["apply"].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow"
    Action = [
      "cognito-idp:AdminCreateUser", "cognito-idp:AdminUpdateUserAttributes",
      "cognito-idp:AdminDeleteUser", "cognito-idp:AdminDisableUser", "cognito-idp:AdminEnableUser",
      "cognito-idp:AdminSetUserPassword", "cognito-idp:AdminAddUserToGroup", "cognito-idp:AdminRemoveUserFromGroup"
    ]
    Resource = "arn:aws:cognito-idp:${var.region}:${local.account}:userpool/*"
    Condition = { StringEquals = {
      "aws:ResourceTag/ResetBoundary" = var.name_prefix
      "aws:ResourceTag/ManagedBy"     = "honua-release-hosted-pilot"
    } }
  }] })
}
