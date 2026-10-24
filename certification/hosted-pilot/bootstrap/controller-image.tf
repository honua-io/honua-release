resource "aws_ecr_repository" "controller" {
  name                 = "${var.name_prefix}-trusted-controller"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = false
  image_scanning_configuration { scan_on_push = true }
  encryption_configuration { encryption_type = "AES256" }
  lifecycle { prevent_destroy = true }
}

resource "aws_iam_role_policy" "controller_image" {
  role = aws_iam_role.executor["apply"].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = "ecr:GetAuthorizationToken", Resource = "*" },
    { Effect = "Allow", Action = ["ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage", "ecr:BatchGetImage", "ecr:DescribeImages", "ecr:GetDownloadUrlForLayer", "ecr:GetLifecyclePolicy"], Resource = [aws_ecr_repository.controller.arn, aws_ecr_repository.worker.arn] }
  ] })
}

resource "aws_ecr_repository" "worker" {
  name                 = "${var.name_prefix}-worker"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = false
  image_scanning_configuration { scan_on_push = true }
  encryption_configuration { encryption_type = "AES256" }
  lifecycle { prevent_destroy = true }
}

resource "aws_ecr_lifecycle_policy" "worker" {
  repository = aws_ecr_repository.worker.name
  policy = jsonencode({ rules = [
    { rulePriority = 1, description = "Expire untagged worker artifacts after one day",
    selection = { tagStatus = "untagged", countType = "sinceImagePushed", countUnit = "days", countNumber = 1 }, action = { type = "expire" } },
    { rulePriority = 2, description = "Expire immutable worker artifacts after fourteen days",
    selection = { tagStatus = "any", countType = "sinceImagePushed", countUnit = "days", countNumber = 14 }, action = { type = "expire" } }
  ] })
  lifecycle { prevent_destroy = true }
}

resource "aws_ecr_lifecycle_policy" "controller" {
  repository = aws_ecr_repository.controller.name
  policy = jsonencode({ rules = [
    { rulePriority = 1, description = "Expire untagged controller artifacts after one day",
    selection = { tagStatus = "untagged", countType = "sinceImagePushed", countUnit = "days", countNumber = 1 }, action = { type = "expire" } },
    { rulePriority = 2, description = "Expire immutable controller artifacts after fourteen days",
    selection = { tagStatus = "any", countType = "sinceImagePushed", countUnit = "days", countNumber = 14 }, action = { type = "expire" } }
  ] })
  lifecycle { prevent_destroy = true }
}
