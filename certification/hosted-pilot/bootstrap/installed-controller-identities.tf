# One original identity-stage output namespace, never a runtime write grant.
# The same reviewed allocation that bounds roles also bounds its apply output.
locals {
  installed_controller_identity_objects = local.installed_cap == null ? [] : [
    for name in ["review", "identity"] :
    "${local.installed_cap_bucket}/${local.installed_cap_prefix}installed-controllers/${local.installed_cap.runtime_id}/identity/*/*/${name}.json"
  ]
}

resource "aws_iam_role_policy" "installed_controller_identity_output" {
  count = local.installed_cap == null ? 0 : 1
  role  = aws_iam_role.executor["apply"].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = "s3:PutObject", Resource = local.installed_controller_identity_objects,
      Condition = { StringEquals = {
        "s3:if-none-match" = "*", "s3:x-amz-server-side-encryption" = "AES256"
      } }
    }
  ] })
}
